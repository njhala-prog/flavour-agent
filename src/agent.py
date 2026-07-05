"""
Flavor Innovation Agent — production-grade implementation.

Production additions vs prototype:
  - Input validation: length cap + prompt-injection pattern detection
  - Tenacity retry on transient LLM errors (RateLimit, Timeout, Connection)
    with exponential back-off — retries are transparent to the caller
  - ThreadPoolExecutor timeout: hard cap at settings.query_timeout_seconds
    so a hung OpenAI call never blocks the server thread indefinitely
  - Conversation-style Cypher correction: LLM sees its own failed query and
    the Neo4j error in context, so corrections are far more targeted
  - Structured logging with request IDs: every log line is traceable
  - Error hierarchy: AgentError subclasses map cleanly to HTTP status codes
  - Code-level Cypher cleaning: strip fences, find query start, reject writes —
    this replaces fragile prompt rules like "output only raw Cypher"
  - _synthesize_empty(): frames absent data as white-space insight rather than
    returning a generic "no results" string
"""

import re
import time
import uuid
import logging
import concurrent.futures
from dataclasses import dataclass
from typing import Any

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)
from openai import OpenAI, RateLimitError, APITimeoutError, APIConnectionError
from dotenv import load_dotenv

from src.config import settings
from src.database import Neo4jDatabase, Neo4jQueryError
from src.prompts import (
    CYPHER_GENERATION_SYSTEM,
    INSIGHT_SYNTHESIS_SYSTEM,
    BROADEN_QUERY_MESSAGE,
    OFF_TOPIC_RESPONSE,
)

load_dotenv()
logger = logging.getLogger(__name__)


# ── Module-level compiled regexes (compiled once at import) ──────────────────

# Broad-question detection: signals like "most", "top", "popular" with no scope
_BROAD_SIGNALS_RE = re.compile(
    r"\b(most|top|best|popular|common|all|every|trending|biggest|largest)\b",
    re.IGNORECASE,
)

# Known scoping terms from the schema — any of these makes a question "specific"
_SCOPE_TERMS = frozenset({
    # cuisines
    "american", "chinese", "indian", "italian", "japanese", "korean",
    "mediterranean", "mexican", "thai", "coffee",
    # neighborhoods
    "astoria", "bronx", "brooklyn", "bushwick", "chelsea", "dumbo",
    "east village", "harlem", "manhattan", "midtown", "queens", "soho",
    "staten island", "tribeca", "williamsburg", "west village",
    # restaurant types
    "fast casual", "casual dining", "fine dining", "qsr", "fast food",
    # ingredient categories
    "protein", "vegetable", "dairy", "grain", "sauce", "spice",
    "fruit", "sweetener", "oil", "nut",
    # common dish types (GMI.name values)
    "ramen", "burger", "pizza", "sushi", "taco", "salad", "sandwich",
    "pasta", "steak", "curry", "dumpling", "noodle",
    # flavor profiles
    "spicy", "sweet", "savory", "umami", "sour", "smoky", "herbal",
    # price
    "budget", "premium",
})

# Broad food/restaurant vocabulary for the topic guard fast-path.
# If ANY of these words appear in a question, it is immediately treated as
# on-topic and the LLM topic-check call is skipped entirely.
_FOOD_KEYWORDS = _SCOPE_TERMS | frozenset({
    # restaurant / venue words
    "restaurant", "restaurants", "chain", "chains", "venue", "venues",
    "menu", "menus", "kitchen", "chef", "dining", "dine", "eatery",
    # food / cooking words
    "food", "foods", "ingredient", "ingredients", "dish", "dishes",
    "cuisine", "cuisines", "recipe", "recipes", "cook", "cooking",
    "flavor", "flavors", "flavour", "taste", "pairing", "pairings",
    # service / ordering words
    "serve", "serves", "serving", "order", "eat", "eating", "meal",
    # meal categories
    "breakfast", "lunch", "dinner", "brunch", "appetizer", "entree",
    "dessert", "beverage", "soup", "snack", "side",
    # product / price words
    "item", "items", "price", "prices", "cost", "expensive", "cheap",
    # trend / analysis words
    "trend", "trends", "trending", "popular", "popularity", "common",
    "combination", "combinations", "crossover", "fusion", "innovation",
})

_WRITE_OPS = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH)\b", re.IGNORECASE
)
_FENCE_RE = re.compile(r"```(?:cypher)?", re.IGNORECASE)
_CYPHER_KEYWORD = re.compile(
    r"(?:^|\n)[ \t]*(MATCH|WITH|CALL|RETURN|UNWIND|OPTIONAL\s+MATCH)",
    re.IGNORECASE,
)
_INJECTION_RE = re.compile(
    r"\b(ignore\s+(all\s+)?(previous|prior|above|instructions)|"
    r"forget\s+(all\s+)?(previous|your|the\s+above)|"
    r"you\s+are\s+now|jailbreak|"
    r"override\s+(all\s+)?(previous|instructions))\b",
    re.IGNORECASE,
)

# Module-level tenacity decorator — uses settings values, built once at import
_LLM_RETRY = retry(
    retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIConnectionError)),
    wait=wait_exponential(
        multiplier=1,
        min=settings.llm_retry_min_wait,
        max=settings.llm_retry_max_wait,
    ),
    stop=stop_after_attempt(settings.llm_retry_attempts),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


# ── Error hierarchy ───────────────────────────────────────────────────────────

class AgentError(Exception):
    """Base class — safe to expose to API callers."""

class AgentInputError(AgentError):
    """Invalid, empty, or potentially malicious input → HTTP 400."""

class AgentOffTopicError(AgentError):
    """Question is unrelated to food/restaurants → return friendly redirect, no Cypher."""

class AgentTimeoutError(AgentError):
    """Query exceeded the configured timeout → HTTP 504."""

class AgentQueryError(AgentError):
    """Cypher failed after all retries → HTTP 422."""


# ── Response dataclass ────────────────────────────────────────────────────────

@dataclass
class AgentResponse:
    question: str
    cypher: str
    raw_results: list[dict]
    insight: str
    latency_seconds: float
    retries: int
    request_id: str


# ── Agent ─────────────────────────────────────────────────────────────────────

class FlavorInnovationAgent:
    """
    Production-grade Text-to-Cypher agent for flavor trend analysis.

    Each call to query() is fully isolated via a request_id that threads
    through every log line. Transient LLM errors are retried transparently.
    The entire query is hard-capped at settings.query_timeout_seconds via a
    ThreadPoolExecutor so a hung network call never blocks the server thread.
    """

    def __init__(self):
        self._db = Neo4jDatabase()
        self._llm = OpenAI(api_key=settings.openai_api_key)

    # ── Public API ────────────────────────────────────────────────────────────

    def query(self, question: str, request_id: str | None = None) -> AgentResponse:
        """
        Translate a natural language question into a Neo4j insight.

        Thread-safe — each call gets its own request_id for log tracing.

        Raises:
          AgentInputError    — bad or malicious input
          AgentTimeoutError  — query exceeded timeout
          AgentQueryError    — Cypher failed after all retries
        """
        rid = request_id or str(uuid.uuid4())
        self._validate_input(question, rid)

        logger.info("[%s] query start: %.80s", rid, question)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._query_impl, question, rid)
            try:
                response = future.result(timeout=settings.query_timeout_seconds)
            except concurrent.futures.TimeoutError:
                logger.error(
                    "[%s] timed out after %ds", rid, settings.query_timeout_seconds
                )
                raise AgentTimeoutError(
                    f"Query exceeded {settings.query_timeout_seconds}s timeout"
                )

        logger.info(
            "[%s] query done: %.2fs | rows=%d | retries=%d",
            rid, response.latency_seconds, len(response.raw_results), response.retries,
        )
        return response

    # ── Internal orchestration ─────────────────────────────────────────────────

    def _query_impl(self, question: str, rid: str) -> AgentResponse:
        start = time.time()
        self._check_topic(question, rid)
        cypher, raw_results, retries = self._generate_and_execute(question, rid)

        if not raw_results:
            logger.info("[%s] 0 results — attempting broadened query", rid)
            cypher, raw_results = self._try_broaden(question, cypher, rid)

        insight = self._synthesize(question, raw_results, cypher)
        return AgentResponse(
            question=question,
            cypher=cypher,
            raw_results=raw_results,
            insight=insight,
            latency_seconds=round(time.time() - start, 2),
            retries=retries,
            request_id=rid,
        )

    # ── Input validation ──────────────────────────────────────────────────────

    @staticmethod
    def _is_broad_question(question: str) -> bool:
        """
        True when the question uses broad aggregation language (most, top, popular…)
        but mentions no scoping term from the schema (cuisine, neighborhood, dish, etc.).
        These need an extra hint so the LLM groups results instead of returning a flat list.
        """
        lower = question.lower()
        has_scope = any(term in lower for term in _SCOPE_TERMS)
        return not has_scope and bool(_BROAD_SIGNALS_RE.search(question))

    @staticmethod
    def _validate_input(question: str, rid: str) -> None:
        if not question.strip():
            raise AgentInputError("Question cannot be empty")
        if len(question) > settings.max_input_length:
            raise AgentInputError(
                f"Question too long ({len(question)} chars, max {settings.max_input_length})"
            )
        if _INJECTION_RE.search(question):
            logger.warning("[%s] prompt injection pattern rejected", rid)
            raise AgentInputError("Question contains disallowed patterns")

    def _check_topic(self, question: str, rid: str) -> None:
        """
        Fast topic guard — fires before any Cypher is generated.

        Fast path: if the question contains any known food/restaurant keyword
        it is immediately allowed through — no LLM call needed. This prevents
        false positives on clearly valid questions like "Which chains don't
        serve vegetable ingredients?" that contain schema vocabulary.

        Slow path: only questions with zero food vocabulary reach the LLM
        (e.g. "how are you", "what's the weather"). gpt-4o-mini decides YES/NO.

        Fails open: if the LLM call errors, the question is allowed through.
        """
        lower = question.lower()
        if any(term in lower for term in _FOOD_KEYWORDS):
            return  # clearly on-topic — skip LLM call entirely

        try:
            resp = self._llm.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0,
                max_tokens=5,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a strict topic classifier. Reply with exactly one word: YES or NO.\n"
                            "YES — the question is about food, restaurants, ingredients, menu items, "
                            "cuisines, flavors, cooking, or culinary topics.\n"
                            "NO  — the question is about anything else (greetings, weather, sports, "
                            "politics, personal questions, coding, etc.)."
                        ),
                    },
                    {"role": "user", "content": question},
                ],
            )
            verdict = (resp.choices[0].message.content or "").strip().upper()
            if "NO" in verdict:
                logger.info("[%s] off-topic question blocked: %.80s", rid, question)
                raise AgentOffTopicError(OFF_TOPIC_RESPONSE)
        except AgentOffTopicError:
            raise
        except Exception as exc:
            # Fail open — don't block valid queries if the guard LLM call fails
            logger.warning("[%s] topic check failed, allowing through: %s", rid, exc)

    # ── Cypher generation & execution ─────────────────────────────────────────

    def _try_broaden(self, question: str, cypher: str, rid: str) -> tuple[str, list[dict]]:
        """
        One automatic attempt to relax a 0-result query.

        Sends the LLM the original query + an instruction to remove the most
        restrictive filter or switch to fuzzy matching. Falls back to returning
        (original_cypher, []) silently if the broadened query also fails or errors,
        so the caller can fall through to _synthesize_empty without crashing.
        """
        messages: list[Any] = [
            {"role": "system", "content": CYPHER_GENERATION_SYSTEM},
            {"role": "user", "content": question},
            {"role": "assistant", "content": cypher},
            {"role": "user", "content": BROADEN_QUERY_MESSAGE},
        ]
        try:
            broad_cypher = self._clean_cypher(self._call_cypher_llm(messages))
            results = self._db.run(broad_cypher)
            if results:
                logger.info("[%s] broadened query returned %d rows", rid, len(results))
            else:
                logger.info("[%s] broadened query also returned 0 rows", rid)
            return broad_cypher, results
        except Exception as exc:
            logger.warning("[%s] broaden attempt failed: %s", rid, exc)
            return cypher, []

    def _generate_and_execute(
        self, question: str, rid: str
    ) -> tuple[str, list[dict], int]:
        """
        Generate Cypher, execute it, and retry with full conversation history
        on failure. The LLM sees its own failed query + the Neo4j error, so
        corrections are targeted rather than generic.
        """
        user_content = question
        if self._is_broad_question(question):
            logger.info("[%s] broad question detected — injecting scope hint", rid)
            user_content = (
                question
                + "\n\n[Scope hint: no filter was specified. Group results by a meaningful "
                "dimension (cuisine, ingredient.category, or restaurant.type) and return "
                "the top 10 by count. Add a // Interpretation comment stating which "
                "dimension you chose.]"
            )

        messages: list[Any] = [
            {"role": "system", "content": CYPHER_GENERATION_SYSTEM},
            {"role": "user", "content": user_content},
        ]

        cypher = self._clean_cypher(self._call_cypher_llm(messages))
        logger.debug("[%s] initial cypher: %.200s", rid, cypher)
        retries = 0

        for attempt in range(settings.max_retries + 1):
            try:
                results = self._db.run(cypher)
                return cypher, results, retries
            except Neo4jQueryError as e:
                if attempt == settings.max_retries:
                    logger.error(
                        "[%s] query failed after %d retries: %s",
                        rid, settings.max_retries, e,
                    )
                    raise AgentQueryError(
                        f"Cypher failed after {settings.max_retries} retries: {e}"
                    ) from e
                logger.warning("[%s] attempt %d failed, correcting: %s", rid, attempt + 1, e)
                messages += [
                    {"role": "assistant", "content": cypher},
                    {
                        "role": "user",
                        "content": (
                            f"That query failed with this Neo4j error:\n{e}\n\n"
                            "Return ONLY the corrected Cypher query, nothing else."
                        ),
                    },
                ]
                cypher = self._clean_cypher(self._call_cypher_llm(messages))
                retries += 1

        raise AgentQueryError("Exhausted retry loop without success")  # unreachable

    @_LLM_RETRY
    def _call_cypher_llm(self, messages: list[Any]) -> str:
        response = self._llm.chat.completions.create(
            model=settings.cypher_model,
            temperature=settings.cypher_temperature,
            messages=messages,
        )
        return response.choices[0].message.content.strip()

    @staticmethod
    def _clean_cypher(raw: str) -> str:
        """
        Code-level sanitisation — runs on every LLM output before the database
        sees it. Handles the most common LLM formatting mistakes:

          1. Strip markdown code fences (```cypher ... ```)
          2. Skip preamble text — find where the actual Cypher starts
          3. Drop trailing explanation after the query ends
          4. Hard-reject any write operation (second line of defence after
             READ_ACCESS session enforcement in Neo4jDatabase)
        """
        # 1. Strip fences
        cleaned = _FENCE_RE.sub("", raw).strip("`").strip()

        # 2. Find the real query start
        match = _CYPHER_KEYWORD.search(cleaned)
        if match and match.start() > 0:
            cleaned = cleaned[match.start():].strip()

        # 3. Drop trailing text (first blank line after content signals end)
        result_lines: list[str] = []
        for line in cleaned.splitlines():
            if not line.strip() and result_lines:
                break
            if line.strip():
                result_lines.append(line)
        cleaned = "\n".join(result_lines).strip()

        # 4. Reject writes
        if _WRITE_OPS.search(cleaned):
            raise AgentQueryError(
                "Write operation detected in generated Cypher — rejected for safety"
            )

        return cleaned

    # ── Insight synthesis ─────────────────────────────────────────────────────

    @_LLM_RETRY
    def _synthesize(self, question: str, results: list[dict], cypher: str) -> str:
        if not results:
            return self._synthesize_empty(question, cypher)

        row_count = len(results)
        truncation = " (showing first 50)" if row_count > 50 else ""
        results_text = "\n".join(str(row) for row in results[:50])

        response = self._llm.chat.completions.create(
            model=settings.synthesis_model,
            temperature=settings.synthesis_temperature,
            messages=[
                {"role": "system", "content": INSIGHT_SYNTHESIS_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n\n"
                        f"Query results ({row_count} rows{truncation}):\n{results_text}"
                    ),
                },
            ],
        )
        return response.choices[0].message.content.strip()

    def _synthesize_empty(self, question: str, cypher: str) -> str:
        """
        Empty results deserve more than 'no data found'. Ask the LLM to frame
        absence as a white-space insight — scarce data is itself actionable.
        Uses temperature=0.2 (lower than normal) to keep the answer grounded.
        """
        response = self._llm.chat.completions.create(
            model=settings.synthesis_model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": INSIGHT_SYNTHESIS_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n\n"
                        f"The database query returned 0 results. Query used:\n{cypher}\n\n"
                        "Explain what this absence likely means for a 50,000-item NYC restaurant dataset. "
                        "Describe the scarcity honestly — use words like 'limited', 'scarce', 'few', "
                        "or 'rare'. Frame it as a white-space opportunity for menu innovation where relevant."
                    ),
                },
            ],
        )
        return response.choices[0].message.content.strip()

    # ── Context manager ───────────────────────────────────────────────────────

    def close(self) -> None:
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
