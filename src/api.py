"""
FastAPI REST service for the Flavor Innovation Agent.

Endpoints:
  GET  /        — serves the interactive HTML frontend
  GET  /health  — liveness + readiness probe (checks Neo4j connectivity)
  POST /query   — run a flavor question through the agent
  POST /ask     — same pipeline as /query + raw rows + LLM-as-judge scores
                  (intended for the interactive frontend)

Error → HTTP status mapping:
  AgentInputError      → 400  (caller's fault — bad or malicious input)
  AgentTimeoutError    → 504  (infra — query exceeded timeout)
  AgentQueryError      → 422  (Cypher failed after all retries)
  Neo4jConnectionError → 503  (Neo4j unreachable)
  Unhandled            → 500  (logged with request_id for tracing)

Async design:
  Both endpoints are `async def`. Blocking calls (Neo4j, OpenAI) are
  dispatched to a thread pool via `asyncio.to_thread()` so the Uvicorn
  event loop is never blocked during a 5-7s LLM round-trip. Without this,
  10 concurrent requests would serialize completely.

Singleton agent:
  FlavorInnovationAgent is created once at startup and stored on app.state.
  This means the Neo4j connection pool (10 connections) is shared across all
  concurrent requests instead of being created and destroyed per request.
  A new pool per request would work functionally but wastes ~50ms of
  connection handshake time on every call.

Run locally:
    uvicorn src.api:app --reload --port 8000
"""

import asyncio
import logging
import pathlib
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from openai import OpenAI
from pydantic import BaseModel, Field

from src.agent import (
    FlavorInnovationAgent,
    AgentInputError,
    AgentOffTopicError,
    AgentTimeoutError,
    AgentQueryError,
)
from src.database import Neo4jDatabase, Neo4jConnectionError

logger = logging.getLogger(__name__)

_STATIC_DIR = pathlib.Path(__file__).parent.parent / "static"


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Create the shared agent singleton at startup; close it on shutdown.

    The agent holds the Neo4j connection pool. Creating it here means
    all requests share the same pool instead of each request opening
    and closing its own set of connections.
    """
    agent = FlavorInnovationAgent()
    try:
        await asyncio.to_thread(agent._db.verify_connectivity)
        logger.info("Startup: Neo4j connectivity verified")
    except Neo4jConnectionError as e:
        logger.warning("Startup: Neo4j unreachable — %s (service starting degraded)", e)

    app.state.agent = agent
    yield
    agent.close()
    logger.info("Shutdown: agent closed")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Flavor Innovation Agent",
    description="AI-powered NYC restaurant menu insight engine (Neo4j + GPT-4o)",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Pydantic models ───────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=3,
        max_length=500,
        examples=["What are the most common ingredients in ramen?"],
    )


class QueryResponse(BaseModel):
    request_id: str
    question: str
    insight: str
    cypher: str
    row_count: int
    latency_seconds: float
    retries: int


class JudgeScores(BaseModel):
    relevance: float
    relevance_label: str        # YES | PARTIAL | NO
    creativity: float
    creativity_label: str       # HIGH | MEDIUM | LOW
    latency_score: float
    composite: float            # (relevance + creativity + latency_score) / 3


class AskResponse(BaseModel):
    request_id: str
    question: str
    insight: str
    cypher: str
    raw_rows: list[dict[str, Any]]
    row_count: int
    latency_seconds: float
    retries: int
    scores: JudgeScores


class HealthResponse(BaseModel):
    status: str   # "ok" | "degraded"
    neo4j: str    # "connected" | "unreachable"
    version: str


# ── LLM-as-judge helpers ──────────────────────────────────────────────────────

def _llm_judge(llm: OpenAI, system_prompt: str, user_content: str) -> str:
    resp = llm.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return resp.choices[0].message.content.strip().upper()


def _score_relevance(llm: OpenAI, question: str, output: str) -> tuple[float, str]:
    verdict = _llm_judge(
        llm,
        "Score whether the answer directly addresses the question.\n"
        "Reply with exactly one word: YES, PARTIAL, or NO.\n"
        "YES = on-topic and complete. PARTIAL = related but incomplete. NO = off-topic.",
        f"Question: {question}\nAnswer: {output}",
    )
    label = "YES" if "YES" in verdict else "PARTIAL" if "PARTIAL" in verdict else "NO"
    score = 1.0 if label == "YES" else 0.5 if label == "PARTIAL" else 0.0
    return score, label


def _score_creativity(llm: OpenAI, question: str, output: str) -> tuple[float, str]:
    verdict = _llm_judge(
        llm,
        "Does the answer surface non-obvious insights or unexpected connections beyond a plain list?\n"
        "Reply with exactly one word: HIGH, MEDIUM, or LOW.\n"
        "HIGH = surprising or novel finding backed by data. "
        "MEDIUM = useful but expected. LOW = just a plain list.",
        f"Question: {question}\nAnswer: {output}",
    )
    label = "HIGH" if "HIGH" in verdict else "MEDIUM" if "MEDIUM" in verdict else "LOW"
    score = 1.0 if label == "HIGH" else 0.5 if label == "MEDIUM" else 0.0
    return score, label


def _score_latency(latency_seconds: float) -> float:
    return round(max(0.0, 1.0 - latency_seconds / 10.0), 3)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_frontend():
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health(request: Request):
    """
    Liveness + readiness probe.
    Checks Neo4j reachability via the shared agent's connection pool.
    Returns 503 when degraded — suitable for Kubernetes readinessProbe.
    """
    agent: FlavorInnovationAgent = request.app.state.agent
    try:
        await asyncio.to_thread(agent._db.verify_connectivity)
        neo4j_status = "connected"
    except Neo4jConnectionError:
        neo4j_status = "unreachable"

    if neo4j_status != "connected":
        raise HTTPException(
            status_code=503,
            detail={"status": "degraded", "neo4j": neo4j_status, "version": app.version},
        )
    return HealthResponse(status="ok", neo4j=neo4j_status, version=app.version)


@app.post("/query", response_model=QueryResponse, tags=["agent"])
async def query_endpoint(req: QueryRequest, request: Request):
    """
    Translate a natural language question into a flavor insight.

    Uses the shared agent singleton from app.state — no connection pool
    overhead per request. The blocking Neo4j + OpenAI calls run in a
    thread via asyncio.to_thread() so concurrent requests don't serialize
    on the event loop.
    """
    request_id = str(uuid.uuid4())
    logger.info("[%s] POST /query: %.80s", request_id, req.question)

    agent: FlavorInnovationAgent = request.app.state.agent

    try:
        response = await asyncio.to_thread(
            agent.query, req.question, request_id
        )

        return QueryResponse(
            request_id=request_id,
            question=response.question,
            insight=response.insight,
            cypher=response.cypher,
            row_count=len(response.raw_results),
            latency_seconds=response.latency_seconds,
            retries=response.retries,
        )

    except AgentOffTopicError as e:
        return QueryResponse(
            request_id=request_id,
            question=req.question,
            insight=str(e),
            cypher="-- Not applicable: question is not related to food or restaurant data",
            row_count=0,
            latency_seconds=0.0,
            retries=0,
        )
    except AgentInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AgentTimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except AgentQueryError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Neo4jConnectionError as e:
        logger.error("[%s] DB unavailable: %s", request_id, e)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")
    except Exception as e:
        logger.error("[%s] Unhandled error: %s", request_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/ask", response_model=AskResponse, tags=["agent"])
async def ask_endpoint(req: QueryRequest, request: Request):
    """
    Same pipeline as /query, plus raw rows and LLM-as-judge scores.

    After the agent returns, two judge calls (relevance + creativity) run
    concurrently via asyncio.gather so they don't serialize. Latency score
    is deterministic and requires no LLM call.

    Composite = (relevance + creativity + latency_score) / 3
    (Accuracy is omitted here — no ground-truth assertion for ad-hoc questions.)
    """
    request_id = str(uuid.uuid4())
    logger.info("[%s] POST /ask: %.80s", request_id, req.question)

    agent: FlavorInnovationAgent = request.app.state.agent

    try:
        response = await asyncio.to_thread(agent.query, req.question, request_id)

        # Run both judge calls concurrently — each is a blocking LLM round-trip
        (rel_score, rel_label), (cr_score, cr_label) = await asyncio.gather(
            asyncio.to_thread(_score_relevance, agent._llm, req.question, response.insight),
            asyncio.to_thread(_score_creativity, agent._llm, req.question, response.insight),
        )
        lat_score = _score_latency(response.latency_seconds)
        composite = round((rel_score + cr_score + lat_score) / 3, 3)

        return AskResponse(
            request_id=request_id,
            question=response.question,
            insight=response.insight,
            cypher=response.cypher,
            raw_rows=response.raw_results,
            row_count=len(response.raw_results),
            latency_seconds=response.latency_seconds,
            retries=response.retries,
            scores=JudgeScores(
                relevance=rel_score,
                relevance_label=rel_label,
                creativity=cr_score,
                creativity_label=cr_label,
                latency_score=lat_score,
                composite=composite,
            ),
        )

    except AgentOffTopicError as e:
        return AskResponse(
            request_id=request_id,
            question=req.question,
            insight=str(e),
            cypher="-- Not applicable: question is not related to food or restaurant data",
            raw_rows=[],
            row_count=0,
            latency_seconds=0.0,
            retries=0,
            scores=JudgeScores(
                relevance=0.0,
                relevance_label="N/A",
                creativity=0.0,
                creativity_label="N/A",
                latency_score=1.0,
                composite=0.0,
            ),
        )

    except AgentInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AgentTimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except AgentQueryError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Neo4jConnectionError as e:
        logger.error("[%s] DB unavailable: %s", request_id, e)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")
    except Exception as e:
        logger.error("[%s] Unhandled error: %s", request_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
