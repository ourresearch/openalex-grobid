import os

from flask import Flask, jsonify
import requests
from requests.exceptions import RequestException

app = Flask(__name__)
app.json.sort_keys = False

GROBID_URL = os.getenv("GROBID_URL", "http://grobid:8070")

def check_grobid_status():
    """Check if GROBID service is running by calling its health endpoint"""
    try:
        response = requests.get(f"{GROBID_URL}/api/health", timeout=5)
        return response.status_code == 200
    except RequestException:
        return False

@app.route("/")
def index():
    grobid_status = check_grobid_status()
    if grobid_status:
        return jsonify({
            "status": "ok",
            "message": "GROBID service is running!"
        })
    else:
        return jsonify({
            "status": "error",
            "message": "GROBID service is not available"
        }), 503

if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_ENV", "production") == "development"
    app.run(host="0.0.0.0", port=8080, debug=debug_mode)
