## OpenAlex Grobid

REST API to parse PDFs with Grobid and store the results in S3.

### Test locally

```bash
docker-compose up --build

post to http://0.0.0.0:8080/parse

{
    "url": "http://arxiv.org/pdf/2502.14867",
    "native_id": "oai:arxiv.org:2502.14867",
    "native_id_namespace": "pmh",
    "pdf_uuid": "dc967f71-1a3f-4d70-a869-ec85bf34faa4"
}
```