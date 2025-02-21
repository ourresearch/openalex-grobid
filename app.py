from flask import Flask, jsonify

app = Flask(__name__)
app.json.sort_keys = False

@app.route("/")
def index():
    return jsonify({"message": "Hello, world!"})