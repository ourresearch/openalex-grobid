"""Integration tests for call_grobid_api against a real (fake) HTTP server.

These drive the actual `requests` stack over real sockets: the multipart body is
really encoded, the read timeout really fires, the connection error really comes
from a refused TCP connect. See fake_grobid.py for why that matters.

Run:  pytest tests/ -v
"""
import re
import socket
import sys
import time
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grobid  # noqa: E402
from exceptions import PDFProcessingError  # noqa: E402
from fake_grobid import FakeGrobid  # noqa: E402

PDF = b"%PDF-1.4\n" + b"payload bytes " * 500 + b"\n%%EOF"
TEI = b'<?xml version="1.0"?><TEI xmlns="http://www.tei-c.org/ns/1.0"/>'


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """Shrink the sleeps so the suite runs in seconds.

    Only the durations are scaled; attempt counts and branching are untouched.
    The real constants' bounds are asserted separately in test_backoff_bounds.
    """
    monkeypatch.setattr(grobid, "GROBID_BACKOFF_BASE_SECONDS", 0.01)
    monkeypatch.setattr(grobid, "GROBID_BACKOFF_MAX_SECONDS", 0.05)


@pytest.fixture
def point_at(monkeypatch):
    """Aim the client at a given base URL."""
    def _point(url):
        monkeypatch.setattr(grobid, "GROBID_URL", url)
    return _point


def free_port():
    """A port with nothing listening on it, for connection-refused tests."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --------------------------------------------------------------------------
# 503: back-pressure, must be retried
# --------------------------------------------------------------------------

def test_503_then_success_returns_tei(point_at):
    with FakeGrobid([{"status": 503}, {"status": 503},
                     {"status": 200, "body": TEI}]) as server:
        point_at(server.url)
        response = grobid.call_grobid_api(PDF)

    assert response.status_code == 200
    assert response.content == TEI
    assert server.request_count == 3


def test_retry_resends_the_whole_pdf(point_at):
    """The BytesIO is consumed by the first POST; a naive retry sends 0 bytes.

    This is the failure the retry loop is most likely to have, and it is
    invisible to a mocked post() — only a server that reads the body catches it.
    """
    with FakeGrobid([{"status": 503}, {"status": 503},
                     {"status": 200, "body": TEI}]) as server:
        point_at(server.url)
        grobid.call_grobid_api(PDF)

    assert [r.file_bytes for r in server.requests] == [PDF, PDF, PDF]


def test_persistent_503_exhausts_attempts_and_stays_retryable(point_at):
    with FakeGrobid([{"status": 503}]) as server:
        point_at(server.url)
        with pytest.raises(PDFProcessingError) as excinfo:
            grobid.call_grobid_api(PDF)

    # 5xx, so the upstream queue redelivers rather than failing the PDF
    assert excinfo.value.status_code == 503
    assert server.request_count == grobid.GROBID_MAX_ATTEMPTS


# --------------------------------------------------------------------------
# 500 and 4xx: terminal, must NOT be retried
# --------------------------------------------------------------------------

def test_500_is_terminal_and_carries_grobid_body(point_at):
    reason = b"[GENERAL] An exception occurred while running Grobid: pdfalto failed"
    with FakeGrobid([{"status": 500, "body": reason}]) as server:
        point_at(server.url)
        with pytest.raises(PDFProcessingError) as excinfo:
            grobid.call_grobid_api(PDF)

    assert excinfo.value.status_code == 422
    assert "pdfalto failed" in excinfo.value.message
    assert server.request_count == 1, "a bad document must not be retried"


def test_400_is_terminal(point_at):
    with FakeGrobid([{"status": 400, "body": b"bad input"}]) as server:
        point_at(server.url)
        with pytest.raises(PDFProcessingError) as excinfo:
            grobid.call_grobid_api(PDF)

    assert excinfo.value.status_code == 422
    assert server.request_count == 1


def test_grobid_error_body_is_truncated(point_at):
    """The sidecar's body is the only diagnostic available, but a stack trace
    should not end up stored verbatim as the error message."""
    with FakeGrobid([{"status": 500, "body": b"x" * 10000}]) as server:
        point_at(server.url)
        with pytest.raises(PDFProcessingError) as excinfo:
            grobid.call_grobid_api(PDF)

    assert len(excinfo.value.message) < 400


# --------------------------------------------------------------------------
# Transport failures
# --------------------------------------------------------------------------

def test_connection_refused_is_retried_then_reported_retryable(point_at):
    """Nothing listening: the request never lands, so re-sending is safe."""
    point_at(f"http://127.0.0.1:{free_port()}")
    with pytest.raises(PDFProcessingError) as excinfo:
        grobid.call_grobid_api(PDF)

    assert excinfo.value.status_code == 503


def test_connection_recovers_when_sidecar_comes_back(point_at, monkeypatch):
    """First attempt refused, then the sidecar starts answering."""
    server = FakeGrobid([{"status": 200, "body": TEI}])
    port = free_port()
    point_at(f"http://127.0.0.1:{port}")

    real_sleep = time.sleep

    def start_server_during_backoff(seconds):
        real_sleep(seconds)
        if server._server is None:
            server._server = None
            _start_on_port(server, port)

    monkeypatch.setattr(grobid.time, "sleep", start_server_during_backoff)
    try:
        response = grobid.call_grobid_api(PDF)
        assert response.content == TEI
        assert server.request_count == 1
    finally:
        server.stop()


def _start_on_port(server, port):
    from http.server import ThreadingHTTPServer
    import threading
    from fake_grobid import _make_handler

    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(server))
    httpd.daemon_threads = True
    server._server = httpd
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


def test_slow_document_times_out_as_retryable_5xx(point_at, monkeypatch):
    """A document GROBID never finishes must surface as retryable, not terminal.

    The deadline is shortened so the socket really times out without the test
    waiting the production 170s.
    """
    monkeypatch.setattr(grobid, "GROBID_TOTAL_DEADLINE_SECONDS", 2)
    monkeypatch.setattr(grobid, "GROBID_MIN_ATTEMPT_SECONDS", 0.5)

    with FakeGrobid([{"delay": 10, "status": 200, "body": TEI}]) as server:
        point_at(server.url)
        started = time.monotonic()
        with pytest.raises(PDFProcessingError) as excinfo:
            grobid.call_grobid_api(PDF)
        elapsed = time.monotonic() - started

    # 503 and 504 are both "requeue me"; which one depends on how the jitter
    # left the budget, so assert the class, never the exact code
    assert 500 <= excinfo.value.status_code < 600
    assert excinfo.value.status_code != 422
    assert elapsed < 5, f"overran its own deadline: {elapsed:.1f}s"
    assert server.request_count == 1, "a slow document must not be re-sent"


# --------------------------------------------------------------------------
# The request GROBID actually receives
# --------------------------------------------------------------------------

def test_request_parameters_reach_grobid(point_at):
    with FakeGrobid([{"status": 200, "body": TEI}]) as server:
        point_at(server.url)
        grobid.call_grobid_api(PDF)

    sent = server.requests[0]
    assert sent.path == "/api/processFulltextDocument"
    assert sent.fields == {
        "segmentSentences": "1",
        "includeRawCitations": "1",
        "includeRawAffiliations": "1",
    }
    assert sent.file_bytes == PDF


def test_consolidation_stays_off(point_at):
    """OpenAlex resolves metadata against its own corpus; consolidation would
    make GROBID hit external services on every parse. Guard against a
    well-meaning re-enable."""
    with FakeGrobid([{"status": 200, "body": TEI}]) as server:
        point_at(server.url)
        grobid.call_grobid_api(PDF)

    fields = server.requests[0].fields
    assert "consolidateHeader" not in fields
    assert "consolidateCitations" not in fields


# --------------------------------------------------------------------------
# Timeout budget invariants
# --------------------------------------------------------------------------

def test_backoff_bounds_and_jitter():
    """Production constants: bounded, and genuinely jittered."""
    delays = [grobid.backoff_delay(n) for n in range(1, 10)]
    assert all(0 <= d <= grobid.GROBID_BACKOFF_MAX_SECONDS for d in delays)

    repeated = {round(grobid.backoff_delay(3), 6) for _ in range(50)}
    assert len(repeated) > 1, "fixed sleeps resynchronise workers into a herd"


def test_deadline_clears_the_sidecar_ceiling():
    """The client must outlive pdfalto's 120s, or we abandon documents GROBID
    is still legitimately working on. See CLAUDE.md, 'Timeout budget'."""
    PDFALTO_TIMEOUT_SEC = 120  # stock grobid 0.9.1
    assert grobid.GROBID_TOTAL_DEADLINE_SECONDS > PDFALTO_TIMEOUT_SEC
    assert grobid.GROBID_CONNECT_TIMEOUT_SECONDS < grobid.GROBID_TOTAL_DEADLINE_SECONDS


def gunicorn_command():
    """The CMD line, in either exec or shell form."""
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    return [line for line in dockerfile.splitlines()
            if line.startswith("CMD") and "gunicorn" in line][0]


def test_gunicorn_timeout_exceeds_the_client_deadline():
    """If gunicorn's --timeout drops below the budget, the worker is SIGKILLed
    mid-retry and the caller sees a dropped connection instead of a clean 503.
    The two numbers live in different files, so nothing else couples them."""
    gunicorn_timeout = int(re.search(r"--timeout\s+(\d+)", gunicorn_command()).group(1))

    assert gunicorn_timeout > grobid.GROBID_TOTAL_DEADLINE_SECONDS


def test_gunicorn_workers_match_the_sidecar_pool():
    """--workers below the sidecar's concurrency strands engines nobody can reach;
    above it, the surplus is just 503s and backoff. The Dockerfile assumed 4 while
    the stock image shipped 10, so a quarter of the pool sat idle unnoticed."""
    default_workers = int(
        re.search(r"--workers\s+\$\{GUNICORN_WORKERS:-(\d+)\}", gunicorn_command()).group(1)
    )

    assert default_workers == grobid.SIDECAR_CONCURRENCY


def test_health_check_has_a_timeout(point_at):
    """Without one this call hangs forever — on the endpoint whose job is to
    notice that GROBID has stopped answering."""
    with FakeGrobid([{"delay": 10, "status": 200}]) as server:
        point_at(server.url)
        started = time.monotonic()
        assert grobid.check_grobid_health() is False
        assert time.monotonic() - started < grobid.GROBID_HEALTH_TIMEOUT_SECONDS + 3


def test_health_check_reports_alive(point_at):
    with FakeGrobid([{"status": 200, "body": b"true"}]) as server:
        point_at(server.url)
        assert grobid.check_grobid_health() is True


# --------------------------------------------------------------------------
# Nothing extractable
# --------------------------------------------------------------------------

def test_204_passes_through_for_the_caller_to_classify(point_at):
    """204 means grobid parsed the document and found nothing extractable —
    typically a scanned, image-only PDF. It is not an error at this layer, so
    the response passes through and parse_pdf turns the empty body into a
    terminal 422 (see test_parse_route.py)."""
    with FakeGrobid([{"status": 204, "body": b""}]) as server:
        point_at(server.url)
        response = grobid.call_grobid_api(PDF)

    assert response.status_code == 204
    assert response.content == b""
