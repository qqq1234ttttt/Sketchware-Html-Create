
import os
import re
import json
import uuid
import base64
import shutil
import threading
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

logging.basicConfig(level=logging.INFO)

SERVICE_NAME = "HTML to APK Builder API"
SERVICE_VERSION = "1.3.0"

SDK = os.environ.get("ANDROID_SDK_ROOT", "/opt/android-sdk")
DATA = Path(os.environ.get("BUILD_DATA_DIR", "/tmp/html-apk-builder"))

JOBS_DIR = DATA / "jobs"
APK_DIR = DATA / "apks"
WORK_DIR = DATA / "work"

for directory in (JOBS_DIR, APK_DIR, WORK_DIR):
    directory.mkdir(parents=True, exist_ok=True)

JOBS = {}
LOCK = threading.RLock()
BUILD_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=1)

PACKAGE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$"
)
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,49}$")

MAX_FILES = 500
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 40 * 1024 * 1024
BUILD_TIMEOUT = 600


def now():
    return datetime.now(timezone.utc).isoformat()


def err(message, status=400):
    return jsonify({
        "success": False,
        "error": str(message),
        "time": now()
    }), status


def save_job(job):
    path = JOBS_DIR / (job["build_id"] + ".json")
    temp = JOBS_DIR / (job["build_id"] + ".tmp")

    with LOCK:
        temp.write_text(
            json.dumps(job, ensure_ascii=False),
            encoding="utf-8"
        )
        temp.replace(path)


def get_job(build_id):
    with LOCK:
        cached = JOBS.get(build_id)
        if cached is not None:
            return dict(cached)

    path = JOBS_DIR / (build_id + ".json")

    if not path.is_file():
        return None

    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        app.logger.exception("Could not read job: %s", build_id)
        return None

    with LOCK:
        JOBS[build_id] = job
        return dict(job)


def update_job(build_id, **fields):
    with LOCK:
        job = JOBS.get(build_id)

        if job is None:
            path = JOBS_DIR / (build_id + ".json")

            if not path.is_file():
                return

            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                app.logger.exception("Could not restore job: %s", build_id)
                return

        job.update(fields)
        job["updated_at"] = now()
        JOBS[build_id] = job
        snapshot = dict(job)

    save_job(snapshot)


def safe_path(raw):
    if not isinstance(raw, str):
        raise ValueError("Every file must have a string path.")

    value = raw.replace("\\", "/").strip()
    parts = value.split("/")

    if (
        not value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or any(part in ("", ".", "..") for part in parts)
        or ":" in value
        or "\x00" in value
    ):
        raise ValueError("Invalid or unsafe file path: " + value)

    if value in ("project.json", "draft.html"):
        raise ValueError("Reserved project metadata cannot be uploaded.")

    return PurePosixPath(value)


def receive_files(data, dest):
    entries = data.get("files")

    if not isinstance(entries, list) or not entries:
        raise ValueError(
            "HTML projects require a files array containing "
            "path and content_base64."
        )

    if len(entries) > MAX_FILES:
        raise ValueError("Too many files; maximum is 500.")

    seen = set()
    total = 0
    dest.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each file entry must be an object.")

        rel = safe_path(entry.get("path"))
        name = rel.as_posix()

        if name in seen:
            raise ValueError("Duplicate file path: " + name)

        seen.add(name)
        encoded = entry.get("content_base64")

        if not isinstance(encoded, str):
            raise ValueError("Missing content_base64 for " + name)

        try:
            content = base64.b64decode(encoded, validate=True)
        except Exception:
            raise ValueError("Invalid base64 content for " + name)

        if len(content) > MAX_FILE_BYTES:
            raise ValueError("File exceeds 10 MB: " + name)

        total += len(content)

        if total > MAX_TOTAL_BYTES:
            raise ValueError("Total project files exceed 40 MB.")

        output = dest.joinpath(*rel.parts)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)

    if not (dest / "index.html").is_file():
        raise ValueError("Project must contain index.html.")

    return len(entries), total


def xml_escape(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("'", "&apos;")
    )


def java_escape(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def make_project(project, uploaded, data):
    name = str(data.get("app_name", "HTML App")).strip()[:80]
    name = name or "HTML App"

    package = data.get("package_name", "com.generated.htmlapp")

    if not isinstance(package, str) or not PACKAGE_RE.fullmatch(package):
        raise ValueError(
            "Invalid package_name. Example: com.example.myapp"
        )

    version = str(data.get("version_name", "1.0"))

    if not VERSION_RE.fullmatch(version):
        raise ValueError("Invalid version_name.")

    try:
        version_code = int(data.get("version_code", 1))
    except Exception:
        raise ValueError("version_code must be an integer.")

    if version_code < 1 or version_code > 2100000000:
        raise ValueError("version_code is out of range.")

    source_type = data.get("source_type", "html")
    url = str(data.get("website_url", "")).strip()

    if source_type not in ("html", "url"):
        raise ValueError("source_type must be html or url.")

    if source_type == "url":
        parsed = urlsplit(url)

        if (
            parsed.scheme not in ("https", "http")
            or not parsed.netloc
            or any(c in url for c in ('"', "'", "\r", "\n", "\\"))
        ):
            raise ValueError("A valid website_url is required.")

    appdir = project / "app"
    main = appdir / "src" / "main"
    assets = main / "assets" / "www"
    javadir = main / "java"

    for part in package.split("."):
        javadir = javadir / part

    assets.mkdir(parents=True, exist_ok=True)
    javadir.mkdir(parents=True, exist_ok=True)

    if source_type == "html":
        for file_path in uploaded.rglob("*"):
            if file_path.is_file():
                target = assets / file_path.relative_to(uploaded)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file_path, target)

        start_url = "file:///android_asset/www/index.html"
    else:
        start_url = url

    (project / "settings.gradle").write_text(
        "pluginManagement {\n"
        "    repositories {\n"
        "        google()\n"
        "        mavenCentral()\n"
        "        gradlePluginPortal()\n"
        "    }\n"
        "}\n"
        "dependencyResolutionManagement {\n"
        "    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)\n"
        "    repositories { google(); mavenCentral() }\n"
        "}\n"
        "rootProject.name = 'HtmlGeneratedApp'\n"
        "include ':app'\n",
        encoding="utf-8"
    )

    (project / "build.gradle").write_text(
        "plugins { id 'com.android.application' version '8.7.3' apply false }\n",
        encoding="utf-8"
    )

    # Limit Gradle memory and parallel workers.
    (project / "gradle.properties").write_text(
        "org.gradle.jvmargs=-Xmx512m "
        "-XX:MaxMetaspaceSize=192m "
        "-Dfile.encoding=UTF-8\n"
        "org.gradle.parallel=false\n"
        "org.gradle.workers.max=1\n"
        "org.gradle.daemon=false\n"
        "android.useAndroidX=true\n",
        encoding="utf-8"
    )

    (appdir / "build.gradle").write_text(
        "plugins { id 'com.android.application' }\n"
        "android {\n"
        "    namespace '" + package + "'\n"
        "    compileSdk 35\n"
        "    defaultConfig {\n"
        "        applicationId '" + package + "'\n"
        "        minSdk 23\n"
        "        targetSdk 28\n"
        "        versionCode " + str(version_code) + "\n"
        "        versionName '" + version + "'\n"
        "    }\n"
        "}\n",
        encoding="utf-8"
    )

    java_code = (
        "package " + package + ";\n"
        "import android.app.Activity;\n"
        "import android.os.Bundle;\n"
        "import android.webkit.WebSettings;\n"
        "import android.webkit.WebView;\n"
        "import android.webkit.WebViewClient;\n"
        "import android.webkit.WebChromeClient;\n"
        "public class MainActivity extends Activity {\n"
        "    private WebView webView;\n"
        "    @Override public void onCreate(Bundle b) {\n"
        "        super.onCreate(b);\n"
        "        webView = new WebView(this);\n"
        "        setContentView(webView);\n"
        "        WebSettings s = webView.getSettings();\n"
        "        s.setJavaScriptEnabled(true);\n"
        "        s.setDomStorageEnabled(true);\n"
        "        s.setAllowFileAccess(true);\n"
        "        s.setAllowContentAccess(true);\n"
        "        s.setMediaPlaybackRequiresUserGesture(false);\n"
        "        webView.setWebViewClient(new WebViewClient());\n"
        "        webView.setWebChromeClient(new WebChromeClient());\n"
        '        webView.loadUrl("' + java_escape(start_url) + '");\n'
        "    }\n"
        "    @Override public void onBackPressed() {\n"
        "        if (webView != null && webView.canGoBack()) {\n"
        "            webView.goBack();\n"
        "        } else {\n"
        "            super.onBackPressed();\n"
        "        }\n"
        "    }\n"
        "}\n"
    )

    (javadir / "MainActivity.java").write_text(
        java_code, encoding="utf-8"
    )

    manifest = (
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <uses-permission android:name="android.permission.INTERNET" />\n'
        '    <application android:label="' + xml_escape(name) + '"\n'
        '        android:theme="@android:style/Theme.Material.Light.NoActionBar"\n'
        '        android:usesCleartextTraffic="true">\n'
        '        <activity android:name=".MainActivity" android:exported="true">\n'
        '            <intent-filter>\n'
        '                <action android:name="android.intent.action.MAIN" />\n'
        '                <category android:name="android.intent.category.LAUNCHER" />\n'
        '            </intent-filter>\n'
        '        </activity>\n'
        '    </application>\n'
        '</manifest>\n'
    )

    (main / "AndroidManifest.xml").write_text(
        manifest, encoding="utf-8"
    )


def worker(build_id, data):
    work = WORK_DIR / build_id
    uploaded = work / "uploaded"
    project = work / "project"

    try:
        update_job(
            build_id,
            status="building",
            progress=5,
            message="Project ဖိုင်များ ပြင်ဆင်နေပါသည်။"
        )

        count = 0
        total = 0

        if data.get("source_type", "html") == "html":
            count, total = receive_files(data, uploaded)

        make_project(project, uploaded, data)

        gradle = shutil.which("gradle")

        if not gradle:
            raise RuntimeError("Gradle was not found in the container.")

        if not Path(SDK).is_dir():
            raise RuntimeError("Android SDK directory was not found.")

        env = os.environ.copy()
        env["ANDROID_SDK_ROOT"] = SDK
        env["ANDROID_HOME"] = SDK
        env["GRADLE_OPTS"] = (
            "-Dorg.gradle.daemon=false "
            "-Dorg.gradle.parallel=false"
        )

        update_job(
            build_id,
            progress=15,
            message="Gradle ဖြင့် APK တည်ဆောက်နေပါသည်။"
        )

        command = [
            gradle,
            "--no-daemon",
            "--max-workers=1",
            "--stacktrace",
            "assembleDebug"
        ]

        app.logger.info("Starting Gradle build: %s", build_id)

        proc = subprocess.run(
            command,
            cwd=str(project),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=BUILD_TIMEOUT
        )

        output = proc.stdout or ""

        if proc.returncode != 0:
            app.logger.error(
                "Gradle failed for %s:\n%s",
                build_id,
                output[-12000:]
            )

            detail = output[-2500:].strip()

            raise RuntimeError(
                "Gradle build failed. "
                + (detail if detail else "See Render runtime logs.")
            )

        apk = (
            project / "app" / "build" / "outputs"
            / "apk" / "debug" / "app-debug.apk"
        )

        if not apk.is_file() or apk.stat().st_size == 0:
            raise RuntimeError(
                "Gradle completed but APK output was not found."
            )

        final_apk = APK_DIR / (build_id + ".apk")
        shutil.copy2(apk, final_apk)

        base = os.environ.get(
            "PUBLIC_BASE_URL",
            "https://sketchware-html-create.onrender.com"
        ).rstrip("/")

        update_job(
            build_id,
            status="completed",
            progress=100,
            message="APK တည်ဆောက်ပြီးပါပြီ။",
            download_url=base + "/api/build/" + build_id + "/download",
            apk_size_bytes=final_apk.stat().st_size,
            file_count=count,
            total_file_bytes=total
        )

        app.logger.info("Build completed: %s", build_id)

    except subprocess.TimeoutExpired:
        app.logger.exception("Gradle timeout: %s", build_id)
        update_job(
            build_id,
            status="failed",
            progress=100,
            message="Gradle Build သည် 600 စက္ကန့်ကျော်သွားပါသည်။"
        )

    except ValueError as exc:
        update_job(
            build_id,
            status="failed",
            progress=100,
            message=str(exc)[:3000]
        )

    except Exception as exc:
        app.logger.exception("Build failed: %s", build_id)
        update_job(
            build_id,
            status="failed",
            progress=100,
            message=str(exc)[:3000] or "Unknown build error."
        )

    finally:
        try:
            if work.exists():
                shutil.rmtree(work)
        except Exception:
            app.logger.exception("Temporary folder cleanup failed: %s", build_id)

        with BUILD_LOCK:
            BUILD_LOCK.release()


@app.route("/")
def home():
    return jsonify({
        "service": SERVICE_NAME,
        "status": "online",
        "version": SERVICE_VERSION,
        "endpoints": {
            "health": "/health",
            "engine_check": "/api/build/engine-check",
            "create_build": "POST /api/build",
            "build_status": "GET /api/build/<build_id>",
            "download_apk": "GET /api/build/<build_id>/download"
        },
        "build_engine": "gradle_debug_apk"
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": SERVICE_NAME,
        "time": now()
    })


@app.route("/api/build/engine-check")
def engine_check():
    sdk = Path(SDK)
    java = shutil.which("java")
    gradle = shutil.which("gradle")
    sdkm = shutil.which("sdkmanager")

    result = {
        "success": True,
        "java": bool(java),
        "java_path": java,
        "gradle": bool(gradle),
        "gradle_path": gradle,
        "sdkmanager": bool(sdkm),
        "sdkmanager_path": sdkm,
        "android_sdk_root": SDK,
        "sdk_directory_exists": sdk.is_dir(),
        "platform_android_35_exists": (
            sdk / "platforms" / "android-35"
        ).is_dir(),
        "build_tools_35_exists": (
            sdk / "build-tools" / "35.0.0"
        ).is_dir(),
        "time": now()
    }

    result["ready_for_apk_build"] = bool(
        java
        and gradle
        and sdkm
        and result["sdk_directory_exists"]
        and result["platform_android_35_exists"]
        and result["build_tools_35_exists"]
    )

    result["note"] = (
        "Tools detected. A real build request is required "
        "to validate compilation."
    )

    return jsonify(result)


@app.route("/api/build", methods=["POST"])
def create_build():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return err("A JSON request body is required.")

    source_type = data.get("source_type", "html")

    if source_type not in ("html", "url"):
        return err("source_type must be html or url.")

    if source_type == "html":
        entries = data.get("files")

        if not isinstance(entries, list) or not entries:
            return err("HTML projects require a non-empty files array.")

        if len(entries) > MAX_FILES:
            return err("Too many files; maximum is 500.")

    # Accept only one build at a time in this server process.
    if not BUILD_LOCK.acquire(blocking=False):
        return err(
            "အခြား APK Build တစ်ခု လုပ်ဆောင်နေပါသည်။ "
            "ပြီးဆုံးသည်အထိ စောင့်ပြီး ထပ်စမ်းပါ။",
            429
        )

    build_id = str(uuid.uuid4())
    stamp = now()

    job = {
        "build_id": build_id,
        "project_id": str(data.get("project_id", "")),
        "status": "queued",
        "message": "Build request accepted.",
        "created_at": stamp,
        "updated_at": stamp,
        "progress": 0,
        "download_url": None
    }

    with LOCK:
        JOBS[build_id] = job

    try:
        save_job(dict(job))
        EXECUTOR.submit(worker, build_id, data)

    except Exception as exc:
        with LOCK:
            JOBS.pop(build_id, None)

        BUILD_LOCK.release()
        app.logger.exception("Could not queue build")

        return err("Could not start build: " + str(exc), 500)

    return jsonify(dict(job)), 202


@app.route("/api/build/<build_id>", methods=["GET"])
def status(build_id):
    try:
        uuid.UUID(build_id)
    except Exception:
        return err("Invalid build ID.")

    job = get_job(build_id)

    if job is None:
        return err("Build job not found.", 404)

    return jsonify(job)


@app.route("/api/build/<build_id>/download", methods=["GET"])
def download(build_id):
    try:
        uuid.UUID(build_id)
    except Exception:
        return err("Invalid build ID.")

    job = get_job(build_id)

    if job is None:
        return err("Build job not found.", 404)

    if job.get("status") != "completed":
        return err("APK is not ready yet.", 409)

    apk = APK_DIR / (build_id + ".apk")

    if not apk.is_file():
        return err(
            "APK is unavailable on this server instance. "
            "Configure persistent storage.",
            404
        )

    return send_file(
        str(apk),
        as_attachment=True,
        download_name="html-app-" + build_id[:8] + ".apk",
        mimetype="application/vnd.android.package-archive"
    )


@app.errorhandler(413)
def too_large(_):
    return err("Request exceeds the 50 MB limit.", 413)


@app.errorhandler(500)
def internal_error(exc):
    app.logger.exception("Internal server error: %s", exc)
    return err("Internal server error.", 500)


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        debug=False
    )
