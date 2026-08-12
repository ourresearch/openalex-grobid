"""Tests against a REAL GROBID sidecar. Skipped unless one is reachable.

    docker-compose up grobid          # or the full stack
    pytest tests/test_live_grobid.py -v -s

Point elsewhere with GROBID_TEST_URL. Use a real paper for a meaningful parse:

    GROBID_TEST_PDF=/path/to/paper.pdf pytest tests/test_live_grobid.py -v -s

Everything else in tests/ runs against a fake server and needs no containers;
this file is the one that checks our assumptions about GROBID are still true.
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grobid  # noqa: E402
from exceptions import PDFProcessingError  # noqa: E402

LIVE_URL = os.getenv("GROBID_TEST_URL", "http://localhost:8070")


def grobid_is_up():
    try:
        return requests.get(f"{LIVE_URL}/api/isalive", timeout=3).status_code == 200
    except requests.exceptions.RequestException:
        return False


pytestmark = pytest.mark.skipif(
    not grobid_is_up(),
    reason=f"no GROBID at {LIVE_URL} — start one with `docker-compose up grobid`",
)


@pytest.fixture(autouse=True)
def point_at_live(monkeypatch):
    monkeypatch.setattr(grobid, "GROBID_URL", LIVE_URL)


def minimal_pdf(lines):
    """A small but structurally valid PDF, with a correct xref table.

    Enough for pdfalto to accept; not enough for GROBID to find a real header,
    which is exactly why test_real_parse prefers GROBID_TEST_PDF when given.
    """
    text = "BT /F1 12 Tf 72 720 Td 14 TL\n"
    for line in lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        text += f"({escaped}) Tj T*\n"
    text += "ET"
    stream = text.encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)


def sample_pdf():
    supplied = os.getenv("GROBID_TEST_PDF")
    if supplied:
        return Path(supplied).read_bytes()
    return minimal_pdf([
        "Deep Learning for Scholarly Document Processing",
        "Jane Doe, Massachusetts Institute of Technology",
        "Abstract",
        "We present a method for extracting structure from scholarly PDFs.",
        "1. Introduction",
        "Prior work on document understanding is extensive.",
        "References",
        "[1] Smith, J. (2020). A study of citations. Journal of Testing, 4(2).",
    ])


def test_health_check_sees_a_live_sidecar():
    assert grobid.check_grobid_health() is True


def test_report_grobid_version():
    """Not an assertion so much as the evidence for proposal #1 in CLAUDE.md:
    this version is knowable at runtime but is never stored with the parse."""
    response = requests.get(f"{LIVE_URL}/api/version", timeout=5)
    assert response.status_code == 200
    print(f"\n  GROBID version: {response.text.strip()}")


def test_real_parse_returns_tei():
    pdf = sample_pdf()
    started = time.monotonic()
    response = grobid.call_grobid_api(pdf)
    elapsed = time.monotonic() - started

    print(f"\n  parsed {len(pdf)} bytes in {elapsed:.1f}s "
          f"-> {response.status_code}, {len(response.content)} bytes TEI")

    if response.status_code == 204:
        pytest.skip(
            "GROBID returned 204 (nothing extractable) — expected for the "
            "synthetic PDF. Set GROBID_TEST_PDF to a real paper for a "
            "meaningful assertion. Note 204 currently becomes a 500 upstream; "
            "see CLAUDE.md."
        )

    assert response.status_code == 200
    assert b"<TEI" in response.content


def test_corrupt_pdf_is_terminal_not_retried():
    """A file with a PDF header and garbage inside: GROBID should reject it,
    and we should surface that as terminal rather than retrying forever."""
    junk = b"%PDF-1.4\n" + bytes(range(256)) * 40

    with pytest.raises(PDFProcessingError) as excinfo:
        grobid.call_grobid_api(junk)

    print(f"\n  corrupt PDF -> {excinfo.value.status_code}: "
          f"{excinfo.value.message[:160]}")
    assert 400 <= excinfo.value.status_code < 500, (
        "a document GROBID cannot parse must be terminal, or the queue will "
        "redeliver it forever"
    )


@pytest.mark.skipif(
    os.getenv("GROBID_LOAD_TEST") != "1",
    reason="set GROBID_LOAD_TEST=1 to run (saturates the sidecar for a while)",
)
def test_concurrent_load_is_absorbed():
    """Fire more requests than GROBID has engines and check we absorb the 503s.

    This is the scenario the retry loop exists for. With `concurrency` set on
    the sidecar, the surplus should come back as 503, be retried, and every
    caller should still get a result — no unhandled errors, no terminal 4xx.
    """
    pdf = sample_pdf()
    workers = int(os.getenv("GROBID_LOAD_TEST_N", "16"))

    def one():
        try:
            return grobid.call_grobid_api(pdf).status_code
        except PDFProcessingError as e:
            return e.status_code

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: one(), range(workers)))
    elapsed = time.monotonic() - started

    print(f"\n  {workers} concurrent -> {results} in {elapsed:.1f}s")
    assert not [r for r in results if 400 <= r < 500], (
        "back-pressure must never be reported as a terminal document error"
    )
