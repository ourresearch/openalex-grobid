import os
import shutil

from flask import Flask, jsonify, request

from exceptions import PDFProcessingError
from grobid import check_grobid_health, parse_pdf

app = Flask(__name__)
app.json.sort_keys = False


@app.route("/")
def index():
    disk_usage = shutil.disk_usage("/")
    free_percentage = disk_usage.free / disk_usage.total * 100

    # If free space is less than 15%, return unhealthy status
    if free_percentage < 15:
        return jsonify({
            "status": "critical",
            "error": "Disk space critically low",
            "free_percentage": free_percentage
        }), 503

    return jsonify({"status": "ok"})


@app.route("/grobid-health")
def grobid_health():
    grobid_status = check_grobid_health()
    if grobid_status:
        return jsonify({"status": "grobid is alive"})
    else:
        return jsonify({"status": "grobid is dead :("}), 503


@app.route("/parse", methods=["POST"])
def parse():
    # fetch and validate request body
    data = request.get_json()
    pdf_url = data.get("url")
    pdf_uuid = data.get("pdf_uuid")
    native_id = data.get("native_id")
    native_id_namespace = data.get("native_id_namespace")

    if not pdf_url or not pdf_uuid or not native_id or not native_id_namespace:
        return jsonify({
            "error": "Missing required fields in request body: url, pdf_uuid, native_id, native_id_namespace"
        }), 400

    # parse pdf
    try:
        response = parse_pdf(pdf_url, pdf_uuid, native_id, native_id_namespace)
    except PDFProcessingError as e:
        return jsonify({"error": e.message}), e.status_code
    return jsonify(response), 201


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_ENV", "production") == "development"
    app.run(host="0.0.0.0", port=8080, debug=debug_mode)