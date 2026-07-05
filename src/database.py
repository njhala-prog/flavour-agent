"""
Neo4j connection wrapper — production-grade.

Production additions vs prototype:
  - Read-only session enforcement (READ_ACCESS mode) — writes rejected at session level,
    not just by checking the Cypher string
  - Distinct error types: Neo4jQueryError (bad Cypher) vs Neo4jConnectionError (infra down)
    — callers can handle them differently (400 Bad Request vs 503 Service Unavailable)
  - Connection pool configuration via Settings
  - verify_connectivity() for health checks and startup probes
"""

import logging
from typing import Any

from neo4j import GraphDatabase, READ_ACCESS
from neo4j.exceptions import (
    CypherSyntaxError,
    CypherTypeError,
    ClientError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

from src.config import settings

logger = logging.getLogger(__name__)


class Neo4jQueryError(Exception):
    """Bad or unsupported Cypher — safe to surface to the caller as a 4xx."""


class Neo4jConnectionError(Exception):
    """Database is unreachable or the connection pool is exhausted — surface as 503."""


class Neo4jDatabase:
    """
    Thread-safe Neo4j driver wrapper.

    The driver manages an internal connection pool; sessions are opened and
    closed per-query. All sessions run in READ_ACCESS mode so the database
    enforces read-only constraints independently of our Cypher validation.
    """

    def __init__(self):
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_pool_size=settings.neo4j_pool_size,
            connection_timeout=settings.neo4j_conn_timeout,
            keep_alive=True,
        )

    def verify_connectivity(self) -> None:
        """
        Raises Neo4jConnectionError if the database is unreachable.
        Used by the API health endpoint and startup probe.
        """
        try:
            self._driver.verify_connectivity()
        except Exception as e:
            raise Neo4jConnectionError(f"Cannot reach Neo4j at {settings.neo4j_uri}: {e}") from e

    def run(
        self,
        cypher: str,
        params: dict | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Execute a read-only Cypher query and return results as plain dicts.

        Automatically appends LIMIT if absent to prevent unbounded result sets
        flooding the LLM context window.

        Raises:
          Neo4jQueryError     — malformed or invalid Cypher
          Neo4jConnectionError — database unreachable or transient infra failure
        """
        params = params or {}

        if "RETURN" in cypher.upper() and "LIMIT" not in cypher.upper():
            cypher = cypher.rstrip().rstrip(";") + f"\nLIMIT {limit}"

        try:
            with self._driver.session(default_access_mode=READ_ACCESS) as session:
                result = session.run(cypher, params)
                rows = [dict(record) for record in result]
                logger.debug("Query returned %d rows", len(rows))
                return rows
        except (CypherSyntaxError, CypherTypeError, ClientError) as e:
            raise Neo4jQueryError(str(e)) from e
        except (ServiceUnavailable, SessionExpired, TransientError) as e:
            raise Neo4jConnectionError(str(e)) from e

    def close(self) -> None:
        self._driver.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
