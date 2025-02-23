import datetime
import gzip
from io import BytesIO
import time
from urllib.parse import quote
import uuid

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
import requests

from exceptions import PDFProcessingError

GROBID_URL = "http://grobid:8070"
GROBID_XML_BUCKET = "openalex-harvested-grobid-xml"
MAX_FILE_SIZE_IN_MB = 20
PDF_BUCKET = "openalex-harvested-pdfs"

s3 = boto3.client("s3", region_name="us-east-1")
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")


def check_grobid_health():
    try:
        response = requests.get(f"{GROBID_URL}/api/isalive")
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException:
        return False


def parse_pdf(pdf_url, pdf_uuid, native_id, native_id_namespace):
    # check if already parsed
    # previous_xml_uuid = previous_parse(pdf_uuid)
    # if previous_xml_uuid:
    #     raise PDFProcessingError(
    #         message=f"PDF has already been parsed with id: {previous_xml_uuid}",
    #         status_code=409
    #     )

    # try to get the file from s3
    pdf_content = get_file_from_s3(pdf_uuid)

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

    # call grobid api
    grobid_response = call_grobid_api(pdf_content)

    # create a new uuid and save the file
    xml_uuid = str(uuid.uuid4())
    xml_content = grobid_response.content.decode('utf-8')

    # validate the xml content
    if not xml_content:
        raise PDFProcessingError(
            message="GROBID did not return any content.",
            status_code=500
        )

    # save
    save_grobid_response_to_s3(xml_content, xml_uuid, pdf_url, native_id, native_id_namespace)
    save_grobid_metadata_to_dynamodb(xml_uuid, pdf_uuid, pdf_url, native_id, native_id_namespace)
    return {
        "id": xml_uuid,
        "status": "success",
        "xml_content": xml_content,
        "s3_key": f"{xml_uuid}.xml.gz",
        "s3_path": f"s3://{GROBID_XML_BUCKET}/{xml_uuid}.xml.gz"
    }


def previous_parse(pdf_uuid):
    # check if the pdf has already been parsed by seeing if the source_pdf_key exists in the grobid-xml table
    table = dynamodb.Table("grobid-xml")
    response = table.query(
        IndexName="by_source_pdf_id",
        KeyConditionExpression=Key("source_pdf_id").eq(pdf_uuid)
    )

    # return the xml uuid if it exists
    if response["Items"]:
        return response["Items"][0]["id"]
    return None


def get_file_from_s3(pdf_uuid):
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
        f"{GROBID_URL}/api/processFulltextDocument",
        files=files,
        data=data,
        timeout=60
    )
    response.raise_for_status()
    return response


def save_grobid_response_to_s3(xml_content, xml_uuid, pdf_url, native_id, native_id_namespace):
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


def save_grobid_metadata_to_dynamodb(xml_uuid, pdf_uuid, pdf_url, native_id, native_id_namespace):
    table = dynamodb.Table("grobid-xml")
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


def normalize_native_id(native_id):
    return native_id.lower().strip()