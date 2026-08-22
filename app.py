import os
import secrets
import subprocess
import threading
import time
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, redirect, render_template, request, session

app = Flask(__name__)

app.secret_key = os.environ.get("APP_SECRET", secrets.token_hex(32))

# ============================================================
# CONFIG
# ============================================================

KICK_CLIENT_ID = os.environ.get("KICK_CLIENT_ID", "")
KICK_CLIENT_SECRET = os.environ.get("KICK_CLIENT_SECRET", "")
KICK_REDIRECT_URI = os.environ.get(
    "KICK_REDIRECT_URI",
    "http://localhost:5000/callback"
)

# KICK OAuth
KICK_AUTH_URL = "https://id.kick.com/oauth/authorize"
KICK_TOKEN_URL = "https://id.kick.com/oauth/token"

# KICK API
KICK_API_BASE = "https://api.kick.com/public/v1"

# Required scopes
SCOPES = [
    "user:read",
    "channel:read",
    "streamkey:read",
]

# Current FFmpeg process
ffmpeg_process = None
ffmpeg_lock = threading.Lock()


# ============================================================
# BASIC HELPERS
# ============================================================

def config_error():
    missing = []

    if not KICK_CLIENT_ID:
        missing.append("KICK_CLIENT_ID")

    if not KICK_CLIENT_SECRET:
        missing.append("KICK_CLIENT_SECRET")

    if not KICK_REDIRECT_URI:
        missing.append("KICK_REDIRECT_URI")

    if missing:
        return ", ".join(missing)

    return None


def get_access_token():
    return session.get("access_token")


def kick_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


# ============================================================
# HOME
# ============================================================

@app.route("/")
def index():
    logged_in = bool(session.get("access_token"))

    return render_template(
        "index.html",
        logged_in=logged_in,
        user=session.get("user"),
        error=session.get("error"),
    )


# ============================================================
# LOGIN
# ============================================================

@app.route("/login")
def login():

    error = config_error()

    if error:
        return (
            f"Missing Render Environment Variables: {error}",
            500,
        )

    # --------------------------------------------------------
    # PKCE
    # --------------------------------------------------------

    code_verifier = secrets.token_urlsafe(64)

    # SHA256 + base64url
    import hashlib
    import base64

    challenge_bytes = hashlib.sha256(
        code_verifier.encode("utf-8")
    ).digest()

    code_challenge = base64.urlsafe_b64encode(
        challenge_bytes
    ).decode("utf-8").rstrip("=")

    state = secrets.token_urlsafe(32)

    session["oauth_state"] = state
    session["code_verifier"] = code_verifier

    params = {
        "client_id": KICK_CLIENT_ID,
        "redirect_uri": KICK_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    return redirect(
        KICK_AUTH_URL + "?" + urlencode(params)
    )


# ============================================================
# OAUTH CALLBACK
# ============================================================

@app.route("/callback")
def callback():

    error = request.args.get("error")

    if error:
        return (
            f"KICK OAuth error: {error}",
            400,
        )

    code = request.args.get("code")
    state = request.args.get("state")

    saved_state = session.get("oauth_state")
    code_verifier = session.get("code_verifier")

    if not code:
        return "No authorization code received from KICK.", 400

    if not state or state != saved_state:
        return "Invalid OAuth state.", 400

    if not code_verifier:
        return "Missing PKCE verifier.", 400

    # --------------------------------------------------------
    # Exchange code for token
    # --------------------------------------------------------

    payload = {
        "grant_type": "authorization_code",
        "client_id": KICK_CLIENT_ID,
        "client_secret": KICK_CLIENT_SECRET,
        "redirect_uri": KICK_REDIRECT_URI,
        "code": code,
        "code_verifier": code_verifier,
    }

    try:
        response = requests.post(
            KICK_TOKEN_URL,
            data=payload,
            timeout=20,
        )
    except requests.RequestException as exc:
        return f"Could not connect to KICK token endpoint: {exc}", 502

    if not response.ok:
        return (
            "KICK token request failed.<br><br>"
            f"HTTP {response.status_code}<br>"
            f"<pre>{response.text}</pre>",
            400,
        )

    try:
        token_data = response.json()
    except ValueError:
        return (
            "KICK returned invalid token JSON.",
            502,
        )

    access_token = token_data.get("access_token")

    if not access_token:
        return (
            "KICK did not return an access token.<br><br>"
            f"<pre>{safe_json(token_data)}</pre>",
            400,
        )

    session["access_token"] = access_token

    # Keep refresh token if supplied
    if token_data.get("refresh_token"):
        session["refresh_token"] = token_data["refresh_token"]

    # --------------------------------------------------------
    # Get current user
    # --------------------------------------------------------

    user_result = kick_get(
        "/users",
        access_token,
    )

    if user_result["ok"]:
        user_data = user_result["data"]

        # Different API responses can wrap the user differently
        user = extract_first_object(user_data)

        if user:
            session["user"] = user

    # Clear OAuth temporary values
    session.pop("oauth_state", None)
    session.pop("code_verifier", None)
    session.pop("error", None)

    return redirect("/")


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    stop_ffmpeg()

    session.clear()

    return redirect("/")


# ============================================================
# KICK API GET
# ============================================================

def kick_get(path, token, params=None):

    url = KICK_API_BASE + path

    try:
        response = requests.get(
            url,
            headers=kick_headers(token),
            params=params,
            timeout=20,
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status": 0,
            "error": str(exc),
            "data": None,
            "text": "",
        }

    try:
        data = response.json()
    except ValueError:
        data = None

    return {
        "ok": response.ok,
        "status": response.status_code,
        "error": None,
        "data": data,
        "text": response.text,
    }


# ============================================================
# CURRENT USER
# ============================================================

@app.route("/api/me")
def api_me():

    token = get_access_token()

    if not token:
        return jsonify({
            "ok": False,
            "logged_in": False,
        }), 401

    result = kick_get(
        "/users",
        token,
    )

    if not result["ok"]:
        return jsonify({
            "ok": False,
            "status": result["status"],
            "error": result["text"],
        }), 502

    return jsonify({
        "ok": True,
        "data": result["data"],
    })


# ============================================================
# CHANNEL
# ============================================================

@app.route("/api/channel")
def api_channel():

    token = get_access_token()

    if not token:
        return jsonify({
            "ok": False,
            "error": "Not logged in.",
        }), 401

    result = kick_get(
        "/channels",
        token,
    )

    if not result["ok"]:
        return jsonify({
            "ok": False,
            "status": result["status"],
            "error": result["text"],
        }), 502

    return jsonify({
        "ok": True,
        "data": result["data"],
    })


# ============================================================
# STREAM KEY
# ============================================================

def get_stream_key():

    token = get_access_token()

    if not token:
        return {
            "ok": False,
            "error": "Not logged in.",
        }

    # --------------------------------------------------------
    # IMPORTANT
    #
    # KICK's stream-key response can vary depending on API
    # version / account / authorization.
    #
    # Therefore we intentionally keep the raw response here
    # and inspect multiple possible field names.
    # --------------------------------------------------------

    possible_endpoints = [
        "/channels",
        "/livestreams",
    ]

    results = []

    for endpoint in possible_endpoints:

        result = kick_get(
            endpoint,
            token,
        )

        results.append({
            "endpoint": endpoint,
            "status": result["status"],
            "ok": result["ok"],
            "data": result["data"],
        })

        if not result["ok"]:
            continue

        found = find_stream_credentials(
            result["data"]
        )

        if found.get("stream_key"):

            return {
                "ok": True,
                "stream_key": found["stream_key"],
                "stream_url": found.get("stream_url"),
                "source_endpoint": endpoint,
            }

    # --------------------------------------------------------
    # No key found
    #
    # Return diagnostic information WITHOUT exposing token.
    # --------------------------------------------------------

    return {
        "ok": False,
        "error": (
            "KICK API responded, but no stream key was found."
        ),
        "diagnostics": results,
    }


# ============================================================
# FIND STREAM CREDENTIALS
# ============================================================

def find_stream_credentials(data):

    result = {
        "stream_key": None,
        "stream_url": None,
    }

    if data is None:
        return result

    if isinstance(data, dict):

        # Common possible names
        key_names = [
            "stream_key",
            "streamKey",
            "streamkey",
            "key",
        ]

        url_names = [
            "stream_url",
            "streamUrl",
            "stream_url",
            "url",
        ]

        for name in key_names:

            value = data.get(name)

            if isinstance(value, str) and value.strip():
                result["stream_key"] = value.strip()
                break

        for name in url_names:

            value = data.get(name)

            if isinstance(value, str) and value.strip():

                # Avoid confusing ordinary URLs with stream URLs
                if (
                    "rtmp" in value.lower()
                    or "rtmps" in value.lower()
                ):
                    result["stream_url"] = value.strip()
                    break

        # Nested objects
        for value in data.values():

            if isinstance(value, (dict, list)):

                nested = find_stream_credentials(value)

                if nested.get("stream_key"):
                    return nested

                if (
                    nested.get("stream_url")
                    and not result.get("stream_url")
                ):
                    result["stream_url"] = nested["stream_url"]

    elif isinstance(data, list):

        for item in data:

            nested = find_stream_credentials(item)

            if nested.get("stream_key"):
                return nested

            if (
                nested.get("stream_url")
                and not result.get("stream_url")
            ):
                result["stream_url"] = nested["stream_url"]

    return result


# ============================================================
# START LIVE
# ============================================================

@app.route("/api/start", methods=["POST"])
def start_live():

    token = get_access_token()

    if not token:
        return jsonify({
            "ok": False,
            "error": "กรุณา Login KICK ก่อน",
        }), 401

    with ffmpeg_lock:

        global ffmpeg_process

        if ffmpeg_process is not None:

            if ffmpeg_process.poll() is None:

                return jsonify({
                    "ok": False,
                    "error": "Live is already running.",
                }), 409

            ffmpeg_process = None

    # --------------------------------------------------------
    # Get stream credentials
    # --------------------------------------------------------

    stream_result = get_stream_key()

    if not stream_result["ok"]:

        # Important diagnostic response
        return jsonify({
            "ok": False,
            "error": stream_result["error"],
            "diagnostics": stream_result.get(
                "diagnostics",
                [],
            ),
        }), 502

    stream_key = stream_result["stream_key"]
    stream_url = stream_result.get("stream_url")

    # --------------------------------------------------------
    # If API didn't provide URL, use standard RTMPS endpoint.
    #
    # IMPORTANT:
    # Verify the exact ingest URL shown by KICK for the
    # account before production use.
    # --------------------------------------------------------

    if not stream_url:

        stream_url = os.environ.get(
            "KICK_STREAM_URL",
            "rtmps://fa723fc1b171.global-contribute.live-video.net:443/app"
        )

    target = (
        stream_url.rstrip("/")
        + "/"
        + stream_key
    )

    # --------------------------------------------------------
    # FFmpeg test source
    #
    # Replace this later with AI video/audio.
    # --------------------------------------------------------

    command = [
        "ffmpeg",

        "-hide_banner",
        "-loglevel",
        "warning",

        # Video test source
        "-f",
        "lavfi",

        "-i",
        "testsrc2="
        "size=1280x720:"
        "rate=30",

        # Audio test source
        "-f",
        "lavfi",

        "-i",
        "sine="
        "frequency=440:"
        "sample_rate=48000",

        # Video
        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-tune",
        "zerolatency",

        "-pix_fmt",
        "yuv420p",

        "-r",
        "30",

        "-g",
        "60",

        "-keyint_min",
        "60",

        "-b:v",
        "4500k",

        "-maxrate",
        "4500k",

        "-bufsize",
        "9000k",

        # Audio
        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-ar",
        "48000",

        "-ac",
        "2",

        # Streaming
        "-f",
        "flv",

        target,
    ]

    try:

        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    except FileNotFoundError:

        return jsonify({
            "ok": False,
            "error": (
                "FFmpeg ไม่ได้ติดตั้งบน Render "
                "ให้ตรวจ Dockerfile/Runtime"
            ),
        }), 500

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500

    with ffmpeg_lock:
        ffmpeg_process = process

    # --------------------------------------------------------
    # Give FFmpeg a moment to establish connection
    # --------------------------------------------------------

    time.sleep(3)

    if process.poll() is not None:

        stderr_output = ""

        try:
            stderr_output = (
                process.stderr.read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )
        except Exception:
            pass

        with ffmpeg_lock:
            ffmpeg_process = None

        return jsonify({
            "ok": False,
            "error": "FFmpeg stopped immediately.",
            "ffmpeg": stderr_output[-4000:],
        }), 502

    return jsonify({
        "ok": True,
        "message": "Live stream process started.",
        "stream_endpoint": stream_url,
    })


# ============================================================
# STOP LIVE
# ============================================================

@app.route("/api/stop", methods=["POST"])
def stop_live():

    stopped = stop_ffmpeg()

    if stopped:
        return jsonify({
            "ok": True,
            "message": "Live stopped.",
        })

    return jsonify({
        "ok": False,
        "message": "No running Live process.",
    })


def stop_ffmpeg():

    global ffmpeg_process

    with ffmpeg_lock:

        process = ffmpeg_process
        ffmpeg_process = None

    if process is None:
        return False

    try:

        if process.poll() is None:

            process.terminate()

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:

                process.kill()
                process.wait(timeout=3)

    except Exception:
        pass

    return True


# ============================================================
# STATUS
# ============================================================

@app.route("/api/status")
def status():

    with ffmpeg_lock:

        process = ffmpeg_process

        running = (
            process is not None
            and process.poll() is None
        )

    return jsonify({
        "ok": True,
        "logged_in": bool(
            session.get("access_token")
        ),
        "live": running,
    })


# ============================================================
# DEBUG KICK RESPONSE
# ============================================================

@app.route("/api/debug/stream")
def debug_stream():

    token = get_access_token()

    if not token:
        return jsonify({
            "ok": False,
            "error": "Not logged in.",
        }), 401

    result = get_stream_key()

    return jsonify(
        remove_sensitive_data(result)
    )


# ============================================================
# UTILITIES
# ============================================================

def extract_first_object(data):

    if isinstance(data, dict):

        # Prefer common wrappers
        for key in [
            "data",
            "user",
            "users",
            "results",
        ]:

            value = data.get(key)

            if isinstance(value, list) and value:
                if isinstance(value[0], dict):
                    return value[0]

            if isinstance(value, dict):
                return value

        return data

    if isinstance(data, list) and data:

        if isinstance(data[0], dict):
            return data[0]

    return None


def remove_sensitive_data(value):

    # This is defensive in case API diagnostics ever contain
    # something sensitive.

    sensitive_keys = {
        "access_token",
        "refresh_token",
        "client_secret",
        "authorization",
        "token",
    }

    if isinstance(value, dict):

        cleaned = {}

        for key, item in value.items():

            if key.lower() in sensitive_keys:
                cleaned[key] = "***REDACTED***"
            else:
                cleaned[key] = remove_sensitive_data(item)

        return cleaned

    if isinstance(value, list):

        return [
            remove_sensitive_data(item)
            for item in value
        ]

    return value


def safe_json(value):

    import json

    return json.dumps(
        remove_sensitive_data(value),
        indent=2,
        ensure_ascii=False,
    )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "service": "kick-direct-live",
    })


# ============================================================
# CLEANUP
# ============================================================

@app.teardown_appcontext
def cleanup(exception=None):
    # Don't stop FFmpeg here.
    #
    # Flask creates/destroys application contexts frequently,
    # and stopping the stream here would accidentally terminate
    # Live requests.
    pass


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "5000",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
