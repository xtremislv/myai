"""Secrets in query strings (Meta's verify token, OAuth codes) are masked in the access log."""
import logging

from app.observability import RedactQueryFilter, redact_query, setup_logging


def test_redact_query_masks_secrets_and_keeps_the_rest():
    path = "/v1/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=SECRET1&hub.challenge=123"
    assert redact_query(path) == "/v1/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=***&hub.challenge=123"
    assert "S2" not in redact_query("/x?hub_verify_token=S2&hub_challenge=9")
    assert redact_query("/cb?code=abc&state=xyz&scope=mail") == "/cb?code=***&state=***&scope=mail"
    assert redact_query("/v1/healthz") == "/v1/healthz"
    assert redact_query("/x?access_token=t&page=2") == "/x?access_token=***&page=2"


def test_access_log_record_is_masked():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", "/w?hub.verify_token=SECRET&hub.challenge=1", "1.1", 200), None)
    assert RedactQueryFilter().filter(record)
    line = record.getMessage()
    assert "SECRET" not in line and "hub.challenge=1" in line


def test_setup_logging_installs_the_filter_once():
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        setup_logging()
        setup_logging()
        access = logging.getLogger("uvicorn.access")
        assert sum(isinstance(f, RedactQueryFilter) for f in access.filters) == 1
    finally:
        root.handlers, _ = saved[0], root.setLevel(saved[1])
