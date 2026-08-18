"""End-to-end tests for POST /parse, through Flask, with fake storage.

Covers the part that matters to the caller: which HTTP status a given GROBID
outcome turns into. Before the retry work, a 503 from GROBID escaped the route
as an unhandled 500 with a traceback; these pin the mapping so it stays fixed.

R2 and DynamoDB are replaced with in-memory doubles — the interesting behaviour
is the status mapping and what gets persisted, neither of which needs a network.
"""
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ReadTimeoutError,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grobid  # noqa: E402
from fake_grobid import FakeGrobid  # noqa: E402

PDF = b"%PDF-1.4\n" + b"body " * 200
TEI = b'<?xml version="1.0"?><TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader/></TEI>'

REQUEST = {
    "url": "http://arxiv.org/pdf/2502.14867",
    "native_id": "oai:arxiv.org:2502.14867",
    "native_id_namespace": "pmh",
    "pdf_uuid": "dc967f71-1a3f-4d70-a869-ec85bf34faa4",
}


class FakeBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeS3:
    """Just enough of the boto3 S3 client for these paths."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        return {"Body": FakeBody(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, Metadata=None):
        self.objects[(Bucket, Key)] = Body
        self.puts.append({"Bucket": Bucket, "Key": Key, "Metadata": Metadata})


class FakeTable:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.put_items = []

    def query(self, IndexName, KeyConditionExpression):
        return {"Items": list(self.items)}

    def put_item(self, Item):
        self.put_items.append(Item)


class FakeDynamo:
    def __init__(self, table):
        self._table = table

    def Table(self, name):
        return self._table


@pytest.fixture
def storage(monkeypatch):
    """Empty R2 with the PDF present, and an empty DynamoDB (no cache hit)."""
    s3 = FakeS3({(grobid.PDF_BUCKET, f"{REQUEST['pdf_uuid']}.pdf"): PDF})
    table = FakeTable()
    monkeypatch.setattr(grobid, "s3", s3)
    monkeypatch.setattr(grobid, "dynamodb", FakeDynamo(table))
    return s3, table


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(grobid, "GROBID_BACKOFF_BASE_SECONDS", 0.01)
    monkeypatch.setattr(grobid, "GROBID_BACKOFF_MAX_SECONDS", 0.05)
    import app as app_module
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def post_parse(client, **overrides):
    payload = dict(REQUEST, **overrides)
    return client.post("/parse", data=json.dumps(payload),
                       content_type="application/json")


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

def test_successful_parse_returns_201_and_persists(client, storage, monkeypatch):
    s3, table = storage
    with FakeGrobid([{"status": 200, "body": TEI}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        response = post_parse(client)

    assert response.status_code == 201
    body = response.get_json()
    assert body["status"] == "success"
    assert "<TEI" in body["xml_content"]

    assert len(s3.puts) == 1, "the TEI should be written to R2"
    assert s3.puts[0]["Bucket"] == grobid.GROBID_XML_BUCKET
    assert len(table.put_items) == 1, "and indexed in DynamoDB"
    assert table.put_items[0]["source_pdf_id"] == REQUEST["pdf_uuid"]


def test_parse_survives_grobid_back_pressure(client, storage, monkeypatch):
    """Two 503s then success: the caller should never see the back-pressure."""
    with FakeGrobid([{"status": 503}, {"status": 503},
                     {"status": 200, "body": TEI}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        response = post_parse(client)

    assert response.status_code == 201
    assert server.request_count == 3


# --------------------------------------------------------------------------
# The mapping that matters: retryable vs terminal
# --------------------------------------------------------------------------

def test_saturated_grobid_returns_retryable_5xx_not_a_traceback(
    client, storage, monkeypatch
):
    """Before the retry work this escaped as an unhandled 500 with a traceback,
    which reads as a server bug and permanently failed the PDF."""
    with FakeGrobid([{"status": 503}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        response = post_parse(client)

    assert response.status_code == 503
    assert "error" in response.get_json()


def test_unparseable_document_returns_terminal_4xx(client, storage, monkeypatch):
    """4xx tells the queue to stop: this PDF fails identically every time."""
    with FakeGrobid([{"status": 500, "body": b"pdfalto failed"}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        response = post_parse(client)

    assert response.status_code == 422
    assert "pdfalto failed" in response.get_json()["error"]


def test_nothing_is_persisted_when_grobid_fails(client, storage, monkeypatch):
    s3, table = storage
    with FakeGrobid([{"status": 500, "body": b"pdfalto failed"}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        post_parse(client)

    assert s3.puts == []
    assert table.put_items == []


# --------------------------------------------------------------------------
# Storage failures: the parse succeeded, so they are transient
# --------------------------------------------------------------------------

def test_r2_write_failure_is_retryable(client, storage, monkeypatch):
    s3, _ = storage

    def boom(**kwargs):
        raise ClientError(
            {"Error": {"Code": "InternalError", "Message": "R2 unavailable"}},
            "PutObject",
        )

    monkeypatch.setattr(s3, "put_object", boom)
    with FakeGrobid([{"status": 200, "body": TEI}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        response = post_parse(client)

    assert response.status_code == 503, "the PDF parsed fine; only storage failed"


def test_dynamodb_write_failure_is_retryable(client, storage, monkeypatch):
    _, table = storage

    def boom(**kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                       "Message": "throttled"}},
            "PutItem",
        )

    monkeypatch.setattr(table, "put_item", boom)
    with FakeGrobid([{"status": 200, "body": TEI}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        response = post_parse(client)

    assert response.status_code == 503


@pytest.mark.parametrize("target, op", [
    ("get_object", "reading the PDF"),
    ("put_object", "writing the TEI"),
])
def test_r2_transport_failures_are_retryable(client, storage, monkeypatch, target, op):
    """BotoCoreError is a sibling of ClientError, not a subclass, so handlers
    written against ClientError alone miss every timeout and connection failure.
    One escaped in production as `unhandled ReadTimeoutError` — recorded by the
    caller as a permanent failure, which then blocks the PDF from ever being
    retried, for what was a transport hiccup."""
    s3, _ = storage

    def boom(**kwargs):
        raise ReadTimeoutError(endpoint_url="https://r2.example")

    monkeypatch.setattr(s3, target, boom)
    with FakeGrobid([{"status": 200, "body": TEI}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        response = post_parse(client)

    assert response.status_code == 503, f"{op} timed out; nothing is wrong with the PDF"
    assert "storage error" in response.get_json()["error"]


def test_unexpected_errors_still_answer_json(client, storage, monkeypatch):
    """An unhandled exception used to render Flask's HTML error page, which
    callers stored verbatim as an opaque 500. Whatever breaks next should at
    least arrive named and parseable."""
    def boom(*args, **kwargs):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(grobid, "call_grobid_api", boom)
    response = post_parse(client)

    assert response.status_code == 500
    assert response.is_json, "must never be an HTML error page"
    assert "RuntimeError" in response.get_json()["error"]


# --------------------------------------------------------------------------
# Paths that never reach GROBID
# --------------------------------------------------------------------------

def test_missing_pdf_returns_404(client, monkeypatch):
    monkeypatch.setattr(grobid, "s3", FakeS3())
    monkeypatch.setattr(grobid, "dynamodb", FakeDynamo(FakeTable()))
    response = post_parse(client)
    assert response.status_code == 404


def test_non_pdf_bytes_are_rejected_before_grobid(client, monkeypatch):
    s3 = FakeS3({(grobid.PDF_BUCKET, f"{REQUEST['pdf_uuid']}.pdf"): b"<html>nope"})
    monkeypatch.setattr(grobid, "s3", s3)
    monkeypatch.setattr(grobid, "dynamodb", FakeDynamo(FakeTable()))

    with FakeGrobid([{"status": 200, "body": TEI}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        response = post_parse(client)
        assert server.request_count == 0, "must not spend a GROBID slot on this"

    assert response.status_code == 400


def test_missing_required_fields_returns_400(client):
    response = client.post("/parse", data=json.dumps({"url": "x"}),
                           content_type="application/json")
    assert response.status_code == 400


def test_prior_parse_does_not_short_circuit(client, storage, monkeypatch):
    """The DynamoDB cache-read was removed (oxjob #789 Stage 2): a pdf with a
    prior parse on record must be parsed FRESH and persisted under a new uuid,
    so re-queued PDFs pick up current-parser output."""
    s3, table = storage
    table.items = [{"id": "old-uuid", "source_pdf_id": REQUEST["pdf_uuid"]}]

    with FakeGrobid([{"status": 200, "body": TEI}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        response = post_parse(client)
        assert server.request_count == 1, "must actually re-parse"

    body = response.get_json()
    assert response.status_code == 201
    assert body["status"] == "success"
    assert body["id"] != "old-uuid", "fresh parse must mint a new uuid"
    assert len(s3.puts) == 1, "fresh xml must be persisted to R2"
    assert len(table.put_items) == 1, "fresh parse must be recorded in DynamoDB"


def test_bypass_cache_reparses_without_persisting(client, storage, monkeypatch):
    """The flag exists for parser-version comparison runs: it must return a
    fresh parse and leave stored state untouched."""
    s3, table = storage
    table.items = [{"id": "old-uuid", "source_pdf_id": REQUEST["pdf_uuid"]}]

    with FakeGrobid([{"status": 200, "body": TEI}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        response = post_parse(client, bypass_cache=True)
        assert server.request_count == 1, "must actually re-parse"

    assert response.status_code == 201
    assert response.get_json()["status"] == "success - not persisted"
    assert s3.puts == [], "bypass_cache must not write to R2"
    assert table.put_items == [], "bypass_cache must not write to DynamoDB"


# --------------------------------------------------------------------------
# Nothing extractable
# --------------------------------------------------------------------------

def test_204_scanned_pdf_is_terminal_not_a_server_error(client, storage, monkeypatch):
    """A scanned, image-only PDF parses fine but yields nothing.

    GROBID answers 204 and the empty body falls through to the no-content check.
    That is a terminal, expected outcome for those bytes, so it belongs in the
    permanent 4xx class — as a 500 it reads as a server bug and invites the queue
    to retry a document that will never parse.
    """
    with FakeGrobid([{"status": 204, "body": b""}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        response = post_parse(client)

    assert response.status_code == 422
    assert "no content" in response.get_json()["error"]


def test_nothing_is_persisted_when_there_is_no_content(client, storage, monkeypatch):
    s3, table = storage
    with FakeGrobid([{"status": 204, "body": b""}]) as server:
        monkeypatch.setattr(grobid, "GROBID_URL", server.url)
        post_parse(client)

    assert s3.puts == []
    assert table.put_items == []
