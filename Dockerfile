FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# --workers must track the sidecar's `concurrency`: fewer workers than engines
# leaves GROBID capacity unusable, more workers than engines just converts the
# surplus into 503s and backoff.
#
# We run the stock grobid image with no mounted grobid.yaml, so that number is
# whatever the image ships — 10 in 0.9.1, NOT the 4 this file used to assume.
# GROBID dropped environment-variable configuration, so the only way to set it
# ourselves would be mounting a config file into the sidecar; until we do, the
# image is the source of truth and this has to match it. Re-check on any
# GROBID_IMAGE bump: `concurrency` in grobid-home/config/grobid.yaml upstream,
# or the live pool metrics at the sidecar's /api/health.
#
# GUNICORN_WORKERS overrides it from the task definition, so tuning does not
# need a rebuild. `exec` keeps gunicorn as PID 1 so ECS's SIGTERM reaches it.
#
# --timeout must stay above GROBID_TOTAL_DEADLINE_SECONDS in grobid.py (170s),
# or gunicorn SIGKILLs the worker mid-retry instead of letting it return a 503.
# It also has to cover the R2 fetch before that call and the R2 + DynamoDB writes
# after it, which sit outside the deadline: at 180 a worst-case request lands
# within a few seconds of the kill line. 210 leaves room for both ends.
#
# Every layer above must be more patient than the one below, or the request is
# severed from outside while work continues in here:
#   pdfalto 120s < deadline 170s < gunicorn 210s < ALB idle 240s < client 270s
# The ALB is configured out-of-band (idle_timeout.timeout_seconds); the client is
# openalex-walden notebooks/parsing/parse_pdfs.ipynb.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:8080 --workers ${GUNICORN_WORKERS:-10} --timeout 210 app:app"]