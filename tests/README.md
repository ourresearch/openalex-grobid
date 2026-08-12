# Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

No containers, no credentials, no network: R2 and DynamoDB are replaced with
in-memory doubles, and GROBID with a real local HTTP server that impersonates
it (`fake_grobid.py`). Runs in ~20s.

## What is covered

| File | Scope |
|---|---|
| `test_grobid_client.py` | `call_grobid_api()` over real sockets — retries, status mapping, timeout budget |
| `test_parse_route.py` | `POST /parse` through Flask — which GROBID outcome becomes which HTTP status, and what gets persisted |
| `test_live_grobid.py` | The same assumptions against a **real** GROBID; skipped unless one is reachable |

The fake GROBID is a real HTTP server rather than a mocked `requests.post`
because the likely bugs are in the plumbing, not the branching — a retry that
re-sends an already-consumed `BytesIO` as zero bytes looks perfectly healthy to
a mock, and `test_retry_resends_the_whole_pdf` catches it by reading the body
server-side.

The status contract they pin, since it is the part callers depend on:

| Outcome | Status | Caller should |
|---|---|---|
| sidecar saturated (503), after retries | 503 | redeliver |
| unreachable, timeout | 503, 504 | redeliver |
| grobid rejects the file (4xx/5xx) | 422 | stop — same bytes fail the same way |
| parsed, nothing extractable (204) | 422 | stop |
| R2 / DynamoDB write failed | 503 | redeliver — the parse itself succeeded |
| anything unanticipated | 500, as JSON | investigate |

503 and 504 differ only to be greppable in logs; nothing should branch on which
one a slow document lands on.

## Against a real GROBID

```bash
docker-compose up grobid                      # sidecar only, no .env needed
pytest tests/test_live_grobid.py -v -s
```

The synthetic PDF these generate is structurally valid but too thin for GROBID
to find a real header, so it may legitimately return 204. For a meaningful
parse, point at a real paper:

```bash
GROBID_TEST_PDF=~/Downloads/2502.14867.pdf pytest tests/test_live_grobid.py -v -s
```

`GROBID_TEST_URL` retargets the sidecar (default `http://localhost:8070`).

The concurrency test is opt-in because it deliberately saturates the sidecar:

```bash
GROBID_LOAD_TEST=1 GROBID_LOAD_TEST_N=16 pytest tests/test_live_grobid.py -v -s
```

It fires more requests than GROBID has engines and asserts that the surplus
comes back as retried 503s rather than terminal errors — the scenario the retry
loop exists for, and the one that validates the `--workers` / `concurrency`
pairing described in CLAUDE.md.
