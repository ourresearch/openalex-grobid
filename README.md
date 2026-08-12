## OpenAlex Grobid

REST API that parses PDFs with GROBID and stores the resulting TEI XML.

Runs as two containers in one ECS task: this Flask wrapper, and a stock
`grobid/grobid` sidecar it talks to over localhost.

### Storage: Cloudflare R2, not S3

Both buckets live in **Cloudflare R2**, reached through the S3-compatible API:

| what | where |
|---|---|
| source PDFs (read) | R2 bucket `openalex-pdfs` |
| TEI XML (written, gzipped) | R2 bucket `openalex-grobid-xml` |
| parse index (`source_pdf_id` → `xml_uuid`) | AWS DynamoDB table `grobid-xml`, `us-east-1` |

Only the index is on AWS. Objects have not been on S3 since `abc78b3`.

**The `s3` naming that remains is deliberate, and does not mean S3.** The boto3
client is an S3-protocol client pointed at R2 via `R2_ENDPOINT`; the `s3_key` and
`s3_path` response fields, the `s3://` URIs inside them, and the "S3 bucket"
wording in some error messages all describe R2 objects. They are kept because
callers persist them: `openalex.pdf.grobid_processing_results` in Walden has
`s3_key`/`s3_path` columns, and backlog queries match historical rows on error
strings like `%not found in S3 bucket%`. Renaming would split those queries
across two vocabularies for no functional gain.

### Configuration

`.env` for local runs; ECS task definition in production:

```
R2_ENDPOINT           https://<account>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
```

DynamoDB uses the task role, so it needs no explicit credentials.

### Response contract

What a caller should do with each status:

| outcome | status | caller should |
|---|---|---|
| parsed, or served from the index | 201 | store the TEI |
| sidecar saturated (503), after in-process retries | 503 | redeliver |
| sidecar unreachable, or timed out | 503, 504 | redeliver |
| R2 / DynamoDB failure, including timeouts | 503 | redeliver — the document is fine |
| GROBID rejects the file (4xx/5xx), or extracts nothing | 422 | stop — the same bytes fail the same way |
| too large, not a PDF, empty | 413, 400 | stop |
| PDF absent from R2 | 404 | re-harvest first |
| anything unanticipated | 500, as JSON | investigate |

Responses are always JSON — never an HTML error page.

The timeout chain, innermost first. Every layer must be more patient than the one
below it, or a request is severed from outside while work continues inside:

```
pdfalto 120s  <  grobid deadline 170s  <  gunicorn 210s  <  ALB idle 240s  <  client 270s
```

`GROBID_TOTAL_DEADLINE_SECONDS` and gunicorn's `--timeout` live here; the ALB idle
timeout is an out-of-band load balancer attribute, and the client is
openalex-walden `notebooks/parsing/parse_pdfs.ipynb`.

### `POST /parse`

```json
{
    "url": "http://arxiv.org/pdf/2502.14867",
    "native_id": "oai:arxiv.org:2502.14867",
    "native_id_namespace": "pmh",
    "pdf_uuid": "dc967f71-1a3f-4d70-a869-ec85bf34faa4"
}
```

`pdf_uuid` is the R2 key of the source PDF, minus the `.pdf` suffix. Add
`"bypass_cache": true` to force a fresh parse that is returned but not persisted
— for comparing parser versions without touching stored state.

### Run locally

```bash
docker-compose up --build
# then POST the body above to http://0.0.0.0:8080/parse
```

### Tests

See [tests/README.md](tests/README.md). No containers, credentials, or network
needed — R2 and DynamoDB are in-memory doubles and GROBID is a local fake.

```bash
pip install -r requirements-dev.txt
pytest tests/
```

### Deploy

Push to `main` builds the image and calls `aws ecs update-service
--force-new-deployment`. The workflow goes green when the rollout is *requested*,
not when it completes — new code is not serving until the service stabilises:

```bash
aws ecs wait services-stable --cluster grobid --services grobid-service --region us-east-1
```

The GROBID sidecar image is pinned in `.github/workflows/aws.yml` as
`GROBID_IMAGE`; reverting it is a one-line change plus a push.
