
import os
import uuid
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request

app = Flask(__name__)

JOBS = {}
JOBS_LOCK = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": "HTML to APK Builder API",
        "status": "online",
        "version": "1.0.0"
    }), 200


@app.route("/api/build", methods=["POST"])
def create_build():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({
            "error": "A JSON request body is required."
        }), 400

    build_id = str(uuid.uuid4())

    job = {
        "buildId": build_id,
        "status": "queued",
        "message": "Build request received. Build engine is not configured yet.",
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
        "progress": 0,
        "downloadUrl": None
    }

    with JOBS_LOCK:
        JOBS[build_id] = job

    return jsonify(job), 202


@app.route("/api/build/<build_id>", methods=["GET"])
def get_build_status(build_id):
    with JOBS_LOCK:
        job = JOBS.get(build_id)

        if job is None:
            return jsonify({
                "error": "Build job not found."
            }), 404

        return jsonify(dict(job)), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "time": utc_now()
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
