
import os
import uuid
import shutil
import threading
import subprocess
from datetime import datetime, timezone

from flask import Flask, jsonify, request

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

JOBS = {}
JOBS_LOCK = threading.Lock()

SERVICE_NAME = "HTML to APK Builder API"
SERVICE_VERSION = "1.1.0"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def json_error(message, status_code):
    return jsonify({
        "success": False,
        "error": message,
        "time": utc_now()
    }), status_code


def get_android_sdk_root():
    return os.environ.get(
        "ANDROID_SDK_ROOT",
        "/opt/android-sdk"
    )


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": SERVICE_NAME,
        "status": "online",
        "version": SERVICE_VERSION,
        "endpoints": {
            "health": "/health",
            "engine_check": "/api/build/engine-check",
            "create_build": "POST /api/build",
            "build_status": "GET /api/build/<build_id>"
        },
        "build_engine": "not_implemented"
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": SERVICE_NAME,
        "time": utc_now()
    }), 200


@app.route("/api/build/engine-check", methods=["GET"])
def engine_check():
    sdk_root = get_android_sdk_root()

    java_path = shutil.which("java")
    gradle_path = shutil.which("gradle")
    sdkmanager_path = shutil.which("sdkmanager")

    result = {
        "success": True,
        "java": java_path is not None,
        "java_path": java_path,
        "gradle": gradle_path is not None,
        "gradle_path": gradle_path,
        "sdkmanager": sdkmanager_path is not None,
        "sdkmanager_path": sdkmanager_path,
        "android_sdk_root": sdk_root,
        "sdk_directory_exists": os.path.isdir(sdk_root),
        "platform_android_35_exists": os.path.isdir(
            os.path.join(sdk_root, "platforms", "android-35")
        ),
        "build_tools_35_exists": os.path.isdir(
            os.path.join(sdk_root, "build-tools", "35.0.0")
        ),
        "ready_for_apk_build": False,
        "time": utc_now()
    }

    if java_path:
        try:
            process = subprocess.run(
                [java_path, "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=10
            )
            result["java_version_output"] = process.stdout[:1000]
            result["java_exit_code"] = process.returncode
        except Exception as exc:
            result["java_error"] = str(exc)

    if sdkmanager_path:
        try:
            process = subprocess.run(
                [sdkmanager_path, "--list_installed"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=30
            )
            result["sdkmanager_exit_code"] = process.returncode
            result["sdkmanager_output"] = process.stdout[-3000:]
        except Exception as exc:
            result["sdkmanager_error"] = str(exc)

    result["ready_for_apk_build"] = (
        result["java"]
        and result["sdkmanager"]
        and result["sdk_directory_exists"]
        and result["platform_android_35_exists"]
        and result["build_tools_35_exists"]
    )

    result["note"] = (
        "Android tools were detected. A working Gradle project "
        "template and actual APK build pipeline still need testing."
    )

    return jsonify(result), 200


@app.route("/api/build", methods=["POST"])
def create_build():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return json_error(
            "A JSON request body is required.",
            400
        )

    build_id = str(uuid.uuid4())
    now = utc_now()

    job = {
        "buildId": build_id,
        "status": "queued",
        "message": (
            "Build request received. "
            "The actual APK compilation pipeline is not implemented yet."
        ),
        "createdAt": now,
        "updatedAt": now,
        "progress": 0,
        "downloadUrl": None
    }

    with JOBS_LOCK:
        JOBS[build_id] = job

    return jsonify(dict(job)), 202


@app.route("/api/build/<build_id>", methods=["GET"])
def get_build_status(build_id):
    with JOBS_LOCK:
        job = JOBS.get(build_id)

        if job is None:
            return json_error(
                "Build job not found.",
                404
            )

        return jsonify(dict(job)), 200


@app.errorhandler(400)
def handle_bad_request(error):
    return json_error("Bad request.", 400)


@app.errorhandler(404)
def handle_not_found(error):
    return json_error("Endpoint not found.", 404)


@app.errorhandler(413)
def handle_too_large(error):
    return json_error(
        "Request exceeds the 50 MB limit.",
        413
    )


@app.errorhandler(500)
def handle_internal_error(error):
    app.logger.exception("Internal server error")
    return json_error(
        "Internal server error.",
        500
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
