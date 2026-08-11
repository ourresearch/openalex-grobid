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
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "8", "--timeout", "180", "app:app"]