from flask import Flask, jsonify
import os

app = Flask(__name__)
app.json.sort_keys = False

@app.route("/")
def index():
    return jsonify({"message": "Hello, world!"})

if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_ENV", "production") == "development"
    app.run(host="0.0.0.0", port=8080, debug=debug_mode)
