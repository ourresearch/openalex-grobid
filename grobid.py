import os
from io import BytesIO
import gzip
from urllib.parse import quote
import uuid

import requests
import boto3

PDF_BUCKET = os.getenv("PDF_BUCKET", "openalex-harvested-pdf")
GROBID_XML_BUCKET = os.getenv("GROBID_XML_BUCKET", "openalex-grobid-xml")
GROBID_URL = os.getenv("GROBID_URL", "http://grobid:8070")
MAX_FILE_SIZE_IN_MB = 20

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")


def check_grobid_health():
    try:
        response = requests.get(f"{GROBID_URL}/api/isalive")
        response.raise_for_status()
        return response.json()["status"] == "ok"
    except requests.RequestException:
        return False

def parse_pdf(pdf_url, pdf_uuid, native_id, native_id_namespace):
    # try to get the file from s3
    try:
        pdf_content = get_file_from_s3(pdf_uuid)
    except FileNotFoundError as e:
        return {"error": str(e)}

    # validate the file
    if is_file_too_large(pdf_content):
        return {"error": "File is too large. Max file size is 20mb."}
    elif is_pdf_empty(pdf_content):
        return {"error": "PDF is empty."}

    # call grobid api
    grobid_response = call_grobid_api(pdf_content)

    # create a new id and save the file
    xml_uuid = str(uuid.uuid4())
    xml_content = grobid_response.content.decode('utf-8')
    save_grobid_response_to_s3(xml_content, xml_uuid, pdf_url, native_id, native_id_namespace)


def get_file_from_s3(pdf_uuid):
    response = s3.get_object(Bucket=PDF_BUCKET, Key=f"{pdf_uuid}.pdf")
    # file not found
    if response["ResponseMetadata"]["HTTPStatusCode"] != 200:
        raise FileNotFoundError(f"File not found in S3: {pdf_uuid}.pdf")
    return response["Body"].read()


def is_file_too_large(pdf_content):
    # check if file size in mb is less than MAX_FILE_SIZE
    file_size_mb = len(pdf_content) / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_IN_MB:
        return True
    return False


def is_pdf_empty(file):
    return len(file) == 0


def call_grobid_api(pdf_content):
    files = {
        "input": ("file.pdf", BytesIO(pdf_content), "application/pdf")
    }

    data = {
        "segmentSentences": "1",
        "includeRawCitations": "1",
        "includeRawAffiliations": "1"
    }

    response = requests.post(
        f"{GROBID_URL}/processFulltextDocument",
        files=files,
        data=data,
        timeout=60
    )
    response.raise_for_status()
    return response.json()


def save_grobid_response_to_s3(xml_content, xml_uuid, pdf_url, native_id, native_id_namespace):
    # save the response to s3
    xml_content_compressed = gzip.compress(xml_content.encode('utf-8'))
    pdf_url_encoded = quote(pdf_url)
    native_id_encoded = quote(native_id)
    native_id_namespace_encoded = quote(native_id_namespace)

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