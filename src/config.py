"""
Centralized configuration — all tuneable values in one place.

Why a dataclass instead of scattered os.getenv() calls:
  - Single source of truth for every config key
  - frozen=True means nothing mutates settings at runtime
  - Easy to override in tests by constructing a different Settings instance
  - Explicit defaults make behaviour predictable without a .env file
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # ── Neo4j ─────────────────────────────────────────────────────────────────
    neo4j_uri:      str = field(default_factory=lambda: os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    neo4j_user:     str = field(default_factory=lambda: os.getenv("NEO4J_USER", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: os.getenv("NEO4J_PASSWORD", "password"))
    neo4j_pool_size: int = 10
    neo4j_conn_timeout: int = 10  # seconds

    # ── OpenAI ────────────────────────────────────────────────────────────────
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))

    # ── Braintrust ────────────────────────────────────────────────────────────
    braintrust_api_key: str = field(default_factory=lambda: os.getenv("BRAINTRUST_API_KEY", ""))

    # ── Agent behaviour ───────────────────────────────────────────────────────
    cypher_model:          str   = "gpt-4o"
    synthesis_model:       str   = "gpt-4o"
    cypher_temperature:    float = 0.0   # deterministic Cypher
    synthesis_temperature: float = 0.4   # slight creativity for narrative
    max_retries:           int   = 2
    query_timeout_seconds: int   = 45    # hard cap — 2 LLM calls + Neo4j needs headroom
    max_input_length:      int   = 500   # chars; rejects suspiciously long prompts

    # ── LLM retry (tenacity) ─────────────────────────────────────────────────
    llm_retry_attempts:  int   = 3
    llm_retry_min_wait:  float = 2.0    # seconds
    llm_retry_max_wait:  float = 30.0   # seconds


settings = Settings()
