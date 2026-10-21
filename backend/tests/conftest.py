"""Provide isolated mock databases and authenticated HTTP clients."""

from fastapi.testclient import TestClient
from langfuse import propagate_attributes
import pytest
import hashlib
import logging

from helpdesk.telemetry import observation, tracing_client
from helpdesk.mock.init_db import initialize_database
from helpdesk.mock.api import create_app
from helpdesk.paths import DATA_DIR


TEST_REPORTS = pytest.StashKey[dict]()
logger = logging.getLogger(__name__)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Keep setup, call, and teardown outcomes for the enclosing test span."""
    report = yield
    item.stash.setdefault(TEST_REPORTS, {})[report.when] = report
    return report


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    """Group each complete pytest lifecycle under its own protocol observation."""
    client = tracing_client()
    if client is None:
        return (yield)

    # Pytest auto-generated parameter IDs can contain megabytes of fixture data.
    digest = hashlib.sha256(item.nodeid.encode()).hexdigest()
    identifier = item.nodeid if len(item.nodeid) <= 180 else (
        item.nodeid.partition("[")[0][:140] + f"[sha256:{digest[:12]}]"
    )
    with propagate_attributes(environment="protocol-tests", tags=["protocol-tests"]):
        with observation("protocol.test", metadata={"pytest_nodeid": identifier,
                                                    "pytest_nodeid_sha256": digest}) as span:
            result = yield
            reports = item.stash.get(TEST_REPORTS, {})
            failed = any(report.failed for report in reports.values())
            skipped = any(report.skipped for report in reports.values())
            span.update(output={"status": "failed" if failed else "skipped" if skipped else "passed"})
            if failed:
                span.update(level="ERROR", status_message="pytest_failed")

    # Fast protocol tests can outpace the exporter queue; drain complete trees.
    try:
        client.flush()
    except Exception:
        logger.warning("protocol_telemetry_flush_failed")
    return result


@pytest.fixture
def database(tmp_path):
    """Initialize an independent database for every test."""
    path = tmp_path / "helpdesk.sqlite3"
    initialize_database(path, DATA_DIR / "seed.json")
    return path


@pytest.fixture
def mock_config(database):
    """Return synthetic credentials and the database path for app recreation."""
    return {
        "database": database,
        "service_key": "test-service-key",
        "user_tokens": {f"test-token-{n}": f"user-{n:03}" for n in range(1, 5)},
    }


@pytest.fixture
def mock_app(mock_config):
    """Build the mock with the test's database and credentials."""
    return create_app(**mock_config)


@pytest.fixture
def client(mock_app):
    """Open a synchronous HTTP client for the isolated mock application."""
    with TestClient(mock_app) as test_client:
        yield test_client
