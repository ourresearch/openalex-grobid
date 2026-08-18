import datetime
import gzip
from io import BytesIO
import random
import time
from urllib.parse import quote
import uuid

import os

import boto3
from botocore.exceptions import BotoCoreError, ClientError
import requests

from exceptions import PDFProcessingError

GROBID_URL = "http://localhost:8070"
GROBID_XML_BUCKET = "openalex-grobid-xml"
MAX_FILE_SIZE_IN_MB = 20
PDF_BUCKET = "openalex-pdfs"

# A 503 from the sidecar is back-pressure, not failure, so it is worth actually
# retrying rather than handing straight back to the caller: the queue would only
# redeliver the same request into the same saturated pool, having paid a full
# round trip for it.
GROBID_MAX_ATTEMPTS = 5
GROBID_BACKOFF_BASE_SECONDS = 2
GROBID_BACKOFF_MAX_SECONDS = 30
# Wall-clock budget for the whole call, retries and backoff included — not per
# attempt, or a run of connection failures stacks past gunicorn's --timeout and
# the worker is SIGKILLed mid-retry, so the caller sees a dropped connection
# instead of the clean 503 the retry loop exists to produce. Keep gunicorn's
# --timeout above this (currently 180s).
#
# Sized against the sidecar: stock grobid 0.9.1 gives pdfalto timeoutSec: 120 and
# runs model inference *after* that, so this must clear 120 with room to spare or
# we abandon documents grobid is still legitimately working on. 170 leaves ~50s
# of inference headroom on top of a worst-case pdfalto run.
GROBID_TOTAL_DEADLINE_SECONDS = 170
# Separate, short connect timeout: the sidecar is on localhost, so a slow connect
# means a full listen backlog, not a slow document. Failing fast here leaves the
# budget for actual processing.
GROBID_CONNECT_TIMEOUT_SECONDS = 5
# Don't start an attempt that cannot plausibly finish: it burns what is left of
# the budget to arrive at the same retryable answer, and reports it as a 504
# rather than the 503 the exhausted-budget path gives.
GROBID_MIN_ATTEMPT_SECONDS = 10
# Liveness probe, not a parse: it must fail fast. Without a timeout this call can
# hang indefinitely and pin a worker — on the endpoint whose whole job is noticing
# that grobid has stopped answering.
GROBID_HEALTH_TIMEOUT_SECONDS = 5
# The sidecar's engine pool size, which gunicorn's --workers has to match. We run
# the stock image with no mounted grobid.yaml and GROBID no longer reads env vars,
# so we cannot set this — it is a record of what the pinned image ships, and the
# Dockerfile default is tested against it. Verify against a running sidecar at
# /grobid-health (pool metrics) and re-check on any GROBID_IMAGE bump.
SIDECAR_CONCURRENCY = 10  # stock grobid 0.9.1

# Cloudflare R2, not S3 — an S3-protocol client pointed at R2_ENDPOINT. Both
# buckets above are R2. The `s3` name here, the s3_key/s3_path response fields
# and the "S3 bucket" error strings are kept because callers persist them
# (walden's grobid_processing_results columns, and backlog queries matching
# historical rows on those strings); they all describe R2 objects. See README.
s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
    region_name="auto",
)
# the one thing still on AWS: the source_pdf_id -> xml_uuid index
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")


def check_grobid_health():
    try:
        response = requests.get(
            f"{GROBID_URL}/api/isalive",
            timeout=GROBID_HEALTH_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException:
        return False


def grobid_pool_status():
    """The sidecar's own /api/health, which carries its engine pool metrics.

    Our gunicorn worker count has to match that pool's size, and we cannot set it
    (GROBID takes configuration only from a mounted yaml), so the number is
    whatever the pinned image ships. Returned verbatim rather than parsed for a
    named field: an upstream rename then degrades to "shown but unrecognised"
    instead of silently reporting a wrong number.
    """
    try:
        response = requests.get(
            f"{GROBID_URL}/api/health",
            timeout=GROBID_HEALTH_TIMEOUT_SECONDS
        )
        return response.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


def parse_pdf(pdf_url, pdf_uuid, native_id, native_id_namespace, bypass_cache=False):
    # bypass_cache: fresh parse returned but NOT saved to S3/DynamoDB —
    # for parser-version comparison runs; leaves all stored state untouched.
    # Every persisted request parses fresh: the DynamoDB cache-read was removed
    # (oxjob #789 Stage 2) so re-queued PDFs get current-parser output instead of
    # a stale "success - cached response". Dedup lives upstream in the walden
    # driver's anti-join; DynamoDB items remain as write-only parse lineage.

    # try to get the file from s3
    pdf_content = get_pdf_file_from_s3(pdf_uuid)

    # validate the file
    if is_file_too_large(pdf_content):
        raise PDFProcessingError(
            message=f"File is too large. Max file size is {MAX_FILE_SIZE_IN_MB}mb.",
            status_code=413
        )
    elif is_pdf_empty(pdf_content):
        raise PDFProcessingError(
            message="PDF is empty.",
            status_code=400
        )
    elif not_a_pdf(pdf_content):
        raise PDFProcessingError(
            message="File does not appear to be a PDF.",
            status_code=400
        )

    # call grobid api
    grobid_response = call_grobid_api(pdf_content)

    # create a new uuid and save the file
    xml_uuid = str(uuid.uuid4())
    xml_content = grobid_response.content.decode('utf-8')

    # validate the xml content
    if not xml_content:
        raise PDFProcessingError(
            message="grobid cannot process pdf: returned no content",
            status_code=422
        )

    # save
    if bypass_cache:
        return {
            "id": xml_uuid,
            "status": "success - not persisted",
            "source_pdf_id": pdf_uuid,
            "s3_key": None,
            "s3_path": None,
            "xml_content": xml_content
        }
    save_grobid_response_to_s3(xml_content, xml_uuid, pdf_url, native_id, native_id_namespace)
    save_grobid_metadata_to_dynamodb(xml_uuid, pdf_uuid, pdf_url, native_id, native_id_namespace)
    return {
        "id": xml_uuid,
        "status": "success",
        "source_pdf_id": pdf_uuid,
        "s3_key": f"{xml_uuid}.xml.gz",
        "s3_path": f"s3://{GROBID_XML_BUCKET}/{xml_uuid}.xml.gz",
        "xml_content": xml_content
    }


def get_pdf_file_from_s3(pdf_uuid):
    try:
        response = s3.get_object(
            Bucket=PDF_BUCKET,
            Key=f"{pdf_uuid}.pdf"
        )
        return response["Body"].read()
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'NoSuchKey':
            raise PDFProcessingError(
                message=f"PDF not found in S3 bucket: {PDF_BUCKET}",
                status_code=404
            )
        elif error_code == 'NoSuchBucket':
            raise PDFProcessingError(
                message=f"S3 bucket not found: {PDF_BUCKET}",
                status_code=503
            )
        else:
            raise PDFProcessingError(
                message=f"S3 error: {str(e)}",
                status_code=503
            )
    except BotoCoreError as e:
        # timeouts, endpoint and connection failures — a transport problem, not a
        # verdict on the PDF, so it must stay retryable rather than escaping as an
        # unhandled 500 the caller records as permanent
        raise PDFProcessingError(
            message=f"storage error: s3 get_object failed: {type(e).__name__}: {e}",
            status_code=503
        )

def gunzip(content):
    """Decompress gzipped content"""
    try:
        with gzip.GzipFile(fileobj=BytesIO(content), mode='rb') as gz_file:
            decompressed_content = gz_file.read()
        return decompressed_content
    except gzip.BadGzipFile as e:
        print(f"Error decompressing content: {str(e)}")
        return content


def get_xml_file_from_s3(xml_uuid):
    try:
        response = s3.get_object(
            Bucket=GROBID_XML_BUCKET,
            Key=f"{xml_uuid}.xml.gz"
        )
        content = response["Body"].read()
        return gunzip(content)
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'NoSuchKey':
            raise PDFProcessingError(
                message=f"XML not found in S3 bucket: {GROBID_XML_BUCKET}",
                status_code=404
            )
        else:
            raise PDFProcessingError(
                message=f"S3 error: {str(e)}",
                status_code=503
            )
    except BotoCoreError as e:
        raise PDFProcessingError(
            message=f"storage error: s3 get_object failed: {type(e).__name__}: {e}",
            status_code=503
        )


def is_file_too_large(pdf_content):
    # check if file size in mb is less than MAX_FILE_SIZE
    file_size_mb = len(pdf_content) / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_IN_MB:
        return True
    return False


def is_pdf_empty(file):
    return len(file) == 0


def not_a_pdf(file):
    return file[:4] != b"%PDF"


def backoff_delay(attempt):
    """Exponential backoff with full jitter, in seconds. `attempt` is 1-based.

    Full jitter (uniform over [0, ceiling]) rather than a fixed sleep: every worker
    that hits the saturated sidecar backs off by a different amount, so they don't
    resynchronise and retry as a thundering herd.
    """
    ceiling = min(
        GROBID_BACKOFF_MAX_SECONDS,
        GROBID_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    )
    return random.uniform(0, ceiling)


def call_grobid_api(pdf_content):
    # No consolidation: OpenAlex resolves header and citation metadata itself, so we
    # only ask GROBID for what it can extract from the document.
    data = {
        "segmentSentences": "1",
        "includeRawCitations": "1",
        "includeRawAffiliations": "1"
    }

    started = time.monotonic()

    for attempt in range(1, GROBID_MAX_ATTEMPTS + 1):
        remaining = GROBID_TOTAL_DEADLINE_SECONDS - (time.monotonic() - started)
        if remaining < GROBID_MIN_ATTEMPT_SECONDS:
            retryable_reason = (
                f"exhausted the {GROBID_TOTAL_DEADLINE_SECONDS}s budget"
            )
            break

        # rebuilt each attempt: requests consumes the BytesIO on the previous
        # POST, so a retry that reuses it would silently send an empty file
        files = {
            "input": ("file.pdf", BytesIO(pdf_content), "application/pdf")
        }

        try:
            response = requests.post(
                f"{GROBID_URL}/api/processFulltextDocument",
                files=files,
                data=data,
                # read timeout is whatever is left of the budget, so no single
                # attempt can overrun it
                timeout=(GROBID_CONNECT_TIMEOUT_SECONDS, remaining)
            )
        except requests.exceptions.ReadTimeout:
            # grobid is still working on this document; retrying in-process would
            # stack a second parse on top of work already in flight. 504 rather
            # than 503 only to separate them in logs — both are transient and both
            # mean redeliver, as against the 422 paths, which mean this document is
            # done for. Which of the two a slow document lands on depends on how
            # much budget the jitter left for the final attempt, so callers must
            # treat them identically.
            raise PDFProcessingError(
                message=(
                    "grobid timeout: no response within "
                    f"{GROBID_TOTAL_DEADLINE_SECONDS}s"
                ),
                status_code=504
            )
        except requests.exceptions.ConnectionError as e:
            # the request never landed (sidecar restarting, not yet up), so
            # nothing is in flight server-side and re-sending is safe
            retryable_reason = f"{type(e).__name__}: {e}"
        except requests.exceptions.RequestException as e:
            raise PDFProcessingError(
                message=f"grobid unavailable: {type(e).__name__}: {e}",
                status_code=503
            )
        else:
            # 503 from the sidecar means its handler pool is saturated — transient,
            # worth a retry. Everything else it rejects (5xx from pdfalto, 4xx
            # malformed input) is deterministic for this file: same bytes will fail
            # the same way, so there is nothing to wait for.
            if response.status_code == 503:
                retryable_reason = f"sidecar busy: {response.text[:200]}"
            elif response.status_code >= 400:
                raise PDFProcessingError(
                    message=f"grobid cannot process pdf: {response.status_code}: {response.text[:200]}",
                    status_code=422
                )
            else:
                return response

        if attempt == GROBID_MAX_ATTEMPTS:
            break

        # full jitter: several workers hitting one saturated sidecar must not
        # resynchronise and come back as a herd
        delay = backoff_delay(attempt)
        if (time.monotonic() - started) + delay >= GROBID_TOTAL_DEADLINE_SECONDS:
            # no point sleeping into the deadline just to give up on the far side
            retryable_reason = (
                f"{retryable_reason}, and the "
                f"{GROBID_TOTAL_DEADLINE_SECONDS}s budget is spent"
            )
            break

        print(
            f"grobid unavailable ({retryable_reason}); retrying in {delay:.1f}s "
            f"(attempt {attempt}/{GROBID_MAX_ATTEMPTS})"
        )
        time.sleep(delay)

    # Out of attempts. This is our capacity problem, not a bad document, so it
    # keeps the transient code and the caller redelivers.
    raise PDFProcessingError(
        message=(
            f"grobid unavailable: {retryable_reason} "
            f"(after {GROBID_MAX_ATTEMPTS} attempts)"
        ),
        status_code=503
    )


def save_grobid_response_to_s3(xml_content, xml_uuid, pdf_url, native_id, native_id_namespace):
    xml_content_compressed = gzip.compress(xml_content.encode('utf-8'))
    pdf_url_encoded = quote(pdf_url)
    native_id_encoded = quote(native_id)
    native_id_namespace_encoded = quote(native_id_namespace)

    # the parse itself succeeded; a storage failure here is transient, so it gets
    # a retryable code rather than escaping as an untyped 500
    try:
        s3.put_object(
            Bucket=GROBID_XML_BUCKET,
            Key=f"{xml_uuid}.xml.gz",
            Body=xml_content_compressed,
            Metadata={
                "pdf_url": pdf_url_encoded,
                "native_id": native_id_encoded,
                "native_id_namespace": native_id_namespace_encoded
            }
        )
    except (ClientError, BotoCoreError) as e:
        raise PDFProcessingError(
            message=f"storage error: s3 put_object failed: {type(e).__name__}: {e}",
            status_code=503
        )


def save_grobid_metadata_to_dynamodb(xml_uuid, pdf_uuid, pdf_url, native_id, native_id_namespace):
    table = dynamodb.Table("grobid-xml")
    try:
        table.put_item(
            Item={
                "id": xml_uuid,
                "native_id": normalize_native_id(native_id),
                "native_id_namespace": native_id_namespace,
                "s3_key": f"{xml_uuid}.xml.gz",
                "source_pdf_id": pdf_uuid,
                "url": pdf_url,
                "new_format": True,
                "created_date": datetime.datetime.now().isoformat(),
                "created_timestamp": int(time.time())
            }
        )
    except (ClientError, BotoCoreError) as e:
        raise PDFProcessingError(
            message=f"storage error: dynamodb put_item failed: {type(e).__name__}: {e}",
            status_code=503
        )


def normalize_native_id(native_id):
    return native_id.lower().strip()