FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# --workers must track GROBID's `concurrency` (set it to 8 on the sidecar too):
# fewer workers than engines leaves GROBID capacity unusable, more workers than
# engines just converts the surplus into 503s and backoff.
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
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "8", "--timeout", "210", "app:app"]