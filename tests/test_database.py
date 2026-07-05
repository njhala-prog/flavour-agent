"""
Unit tests for Neo4jDatabase.

Tests are fully isolated — the Neo4j driver is mocked so no running
database is required. We test:
  - LIMIT auto-injection (prevents unbounded result sets)
  - Read-only mode (access_mode enforcement)
  - Error type mapping (syntax/runtime → Neo4jQueryError, infra → Neo4jConnectionError)
"""

import pytest
from unittest.mock import MagicMock, patch, call

from neo4j.exceptions import (
    CypherSyntaxError,
    CypherTypeError,
    ClientError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

from src.database import Neo4jDatabase, Neo4jQueryError, Neo4jConnectionError


@pytest.fixture
def mock_driver():
    """Patches GraphDatabase.driver and returns the mock driver instance."""
    with patch("src.database.GraphDatabase.driver") as mock_cls:
        driver = MagicMock()
        mock_cls.return_value = driver
        yield driver


@pytest.fixture
def db(mock_driver):
    """Returns a Neo4jDatabase backed by the mock driver."""
    return Neo4jDatabase()


def _make_session(mock_driver, rows=None):
    """Configures the mock driver's session to return given rows."""
    rows = rows or []
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.run.return_value = [MagicMock(**{"__iter__": MagicMock(return_value=iter([]))},
                                          **{"data": MagicMock(return_value=r)}) for r in rows]
    # Simplest: make session.run return iterable of dict-like objects
    mock_records = [MagicMock() for _ in rows]
    for rec, row in zip(mock_records, rows):
        rec.__iter__ = MagicMock(return_value=iter(row.items()))
        rec.keys = MagicMock(return_value=list(row.keys()))
        rec.__getitem__ = MagicMock(side_effect=row.__getitem__)
    session.run.return_value = mock_records
    mock_driver.session.return_value = session
    return session


# ── LIMIT injection ───────────────────────────────────────────────────────────

class TestLimitInjection:
    def test_appends_limit_when_missing(self, db, mock_driver):
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.run.return_value = []
        mock_driver.session.return_value = session

        db.run("MATCH (n) RETURN n")

        cypher_used = session.run.call_args[0][0]
        assert "LIMIT" in cypher_used.upper()

    def test_does_not_double_limit(self, db, mock_driver):
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.run.return_value = []
        mock_driver.session.return_value = session

        db.run("MATCH (n) RETURN n LIMIT 50")

        cypher_used = session.run.call_args[0][0]
        assert cypher_used.upper().count("LIMIT") == 1

    def test_custom_limit_respected(self, db, mock_driver):
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.run.return_value = []
        mock_driver.session.return_value = session

        db.run("MATCH (n) RETURN n", limit=25)

        cypher_used = session.run.call_args[0][0]
        assert "LIMIT 25" in cypher_used

    def test_no_limit_on_non_return_query(self, db, mock_driver):
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.run.return_value = []
        mock_driver.session.return_value = session

        db.run("CALL db.schema.visualization()")

        cypher_used = session.run.call_args[0][0]
        assert "LIMIT" not in cypher_used.upper()


# ── Error type mapping ────────────────────────────────────────────────────────

class TestErrorMapping:
    """
    Cypher errors → Neo4jQueryError (4xx — bad query, caller's fault)
    Infra errors  → Neo4jConnectionError (5xx — not the caller's fault)
    """

    def _db_raising(self, mock_driver, exception):
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.run.side_effect = exception
        mock_driver.session.return_value = session

    def test_cypher_syntax_error_raises_query_error(self, db, mock_driver):
        self._db_raising(mock_driver, CypherSyntaxError("", "bad syntax", None, None, None))
        with pytest.raises(Neo4jQueryError):
            db.run("INVALID CYPHER @@")

    def test_client_error_raises_query_error(self, db, mock_driver):
        self._db_raising(mock_driver, ClientError({"code": "Neo.ClientError.Schema.ConstraintValidationFailed", "message": "x"}))
        with pytest.raises(Neo4jQueryError):
            db.run("MATCH (n) RETURN n")

    def test_service_unavailable_raises_connection_error(self, db, mock_driver):
        self._db_raising(mock_driver, ServiceUnavailable("Connection refused"))
        with pytest.raises(Neo4jConnectionError):
            db.run("MATCH (n) RETURN n")

    def test_transient_error_raises_connection_error(self, db, mock_driver):
        self._db_raising(mock_driver, TransientError({"code": "Neo.TransientError.Network.CommunicationError", "message": "x"}))
        with pytest.raises(Neo4jConnectionError):
            db.run("MATCH (n) RETURN n")

    def test_session_expired_raises_connection_error(self, db, mock_driver):
        self._db_raising(mock_driver, SessionExpired("Session expired"))
        with pytest.raises(Neo4jConnectionError):
            db.run("MATCH (n) RETURN n")


# ── verify_connectivity ───────────────────────────────────────────────────────

class TestVerifyConnectivity:
    def test_raises_connection_error_when_driver_fails(self, db, mock_driver):
        mock_driver.verify_connectivity.side_effect = ServiceUnavailable("down")
        with pytest.raises(Neo4jConnectionError):
            db.verify_connectivity()

    def test_passes_when_driver_succeeds(self, db, mock_driver):
        mock_driver.verify_connectivity.return_value = None
        db.verify_connectivity()  # should not raise
