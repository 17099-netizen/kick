import os
import time
import base64
import hashlib
import secrets
import subprocess
import threading
from urllib.parse import urlencode

import requests
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

APP_SECRET = os.getenv("APP_SECRET", "").strip()

if not APP_SECRET:
    APP_SECRET = secrets.token_hex(32)

app.secret_key = APP_SECRET

app.config.update(
    SESSION_COOKIE_NAME="kick_session",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_PERMANENT=False,
)

KICK_CLIENT_ID = os.getenv(
    "KICK_CLIENT_ID",
    "",
).strip()

KICK_CLIENT_SECRET = os.getenv(
    "KICK_CLIENT_SECRET",
    "",
).strip()

KICK_REDIRECT_URI = os.getenv(
    "KICK_REDIRECT_URI",
    "https://kick-crka.onrender.com/callback",
).strip()

KICK_AUTH_URL = (
    "https://id.kick.com/oauth/authorize"
)

KICK_TOKEN_URL = (
    "https://id.kick.com/oauth/token"
)

KICK_INTROSPECT_URL = (
    "https://id.kick.com/oauth/token/introspect"
)

KICK_API_BASE = (
    "https://api.kick.com/public/v1"
)

KICK_STREAM_URL = os.getenv(
    "KICK_STREAM_URL",
    "rtmps://fa723fc1b171.global-contribute.live-video.net:443/app",
).strip()

KICK_SCOPES = [
    "user:read",
    "channel:read",
    "streamkey:read",
]

ffmpeg_process = None
ffmpeg_lock = threading.Lock()


# ============================================================
# HELPERS
# ============================================================

def redact(value):

    sensitive = [
        "access_token",
        "refresh_token",
        "client_secret",
        "authorization",
        "token",
        "stream_key",
        "streamKey",
    ]

    if isinstance(value, dict):

        output = {}

        for key, item in value.items():

            if str(key).lower() in [
                x.lower()
                for x in sensitive
            ]:

                if key in [
                    "stream_key",
                    "streamKey",
                ]:

                    output[key] = (
                        "[REDACTED: key exists]"
                        if item
                        else ""
                    )

                else:
                    output[key] = "[REDACTED]"

            else:
                output[key] = redact(item)

        return output

    if isinstance(value, list):
        return [
            redact(x)
            for x in value
        ]

    return value


def first_object(data):

    if isinstance(data, list):

        if len(data) > 0:
            return data[0]

        return None

    if isinstance(data, dict):

        if isinstance(
            data.get("data"),
            list,
        ):

            if data["data"]:
                return data["data"][0]

        if isinstance(
            data.get("data"),
            dict,
        ):

            return data["data"]

        return data

    return None


def kick_headers(token):

    return {
        "Authorization":
            f"Bearer {token}",
        "Accept":
            "application/json",
    }


# ============================================================
# HOME
# ============================================================

@app.route("/")
def index():

    if session.get("authenticated"):

        return redirect("/dashboard")

    return render_template(
        "index.html"
    )


# ============================================================
# LOGIN
# ============================================================

@app.route("/login")
def login():

    if not KICK_CLIENT_ID:
        return (
            "KICK_CLIENT_ID is missing",
            500,
        )

    state = secrets.token_urlsafe(
        32
    )

    verifier = secrets.token_urlsafe(
        64
    )

    digest = hashlib.sha256(
        verifier.encode()
    ).digest()

    challenge = (
        base64.urlsafe_b64encode(
            digest
        )
        .decode()
        .rstrip("=")
    )

    session["oauth_state"] = state
    session["oauth_verifier"] = verifier

    params = {
        "response_type": "code",
        "client_id": KICK_CLIENT_ID,
        "redirect_uri": KICK_REDIRECT_URI,
        "scope": " ".join(
            KICK_SCOPES
        ),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }

    url = (
        KICK_AUTH_URL
        + "?"
        + urlencode(params)
    )

    return redirect(url)


# ============================================================
# CALLBACK
# ============================================================

@app.route("/callback")
def callback():

    error = request.args.get(
        "error"
    )

    if error:

        description = request.args.get(
            "error_description",
            "",
        )

        return render_template(
            "index.html",
            error=(
                f"KICK OAuth Error: "
                f"{error} "
                f"{description}"
            ),
        ), 400

    code = request.args.get(
        "code"
    )

    state = request.args.get(
        "state"
    )

    saved_state = session.get(
        "oauth_state"
    )

    verifier = session.get(
        "oauth_verifier"
    )

    if not code:

        return render_template(
            "index.html",
            error=(
                "KICK ไม่ส่ง authorization code กลับมา"
            ),
        ), 400

    if not state:

        return render_template(
            "index.html",
            error=(
                "KICK ไม่ส่ง OAuth state กลับมา"
            ),
        ), 400

    if not saved_state:

        return render_template(
            "index.html",
            error=(
                "OAuth session หาย "
                "กรุณากด Login ใหม่"
            ),
        ), 400

    if not secrets.compare_digest(
        state,
        saved_state,
    ):

        return render_template(
            "index.html",
            error=(
                "OAuth state ไม่ตรงกัน"
            ),
        ), 400

    if not verifier:

        return render_template(
            "index.html",
            error=(
                "PKCE verifier หาย"
            ),
        ), 400

    # --------------------------------------------------------
    # TOKEN EXCHANGE
    # --------------------------------------------------------

    payload = {
        "grant_type":
            "authorization_code",

        "client_id":
            KICK_CLIENT_ID,

        "client_secret":
            KICK_CLIENT_SECRET,

        "redirect_uri":
            KICK_REDIRECT_URI,

        "code":
            code,

        "code_verifier":
            verifier,
    }

    try:

        response = requests.post(
            KICK_TOKEN_URL,
            data=payload,
            timeout=30,
        )

    except Exception as exc:

        return render_template(
            "index.html",
            error=(
                f"เชื่อมต่อ KICK ไม่สำเร็จ: "
                f"{exc}"
            ),
        ), 502

    try:

        token_data = response.json()

    except Exception:

        token_data = {}

    if not response.ok:

        return render_template(
            "index.html",
            error=(
                "KICK Token Error "
                f"{response.status_code}: "
                f"{redact(token_data)}"
            ),
        ), 400

    access_token = token_data.get(
        "access_token"
    )

    refresh_token = token_data.get(
        "refresh_token"
    )

    if not access_token:

        return render_template(
            "index.html",
            error=(
                "KICK ไม่ส่ง Access Token กลับมา"
            ),
        ), 400

    # --------------------------------------------------------
    # SAVE SESSION
    # --------------------------------------------------------

    session.clear()

    session["authenticated"] = True

    session["access_token"] = (
        access_token
    )

    if refresh_token:

        session["refresh_token"] = (
            refresh_token
        )

    session["scope"] = token_data.get(
        "scope",
        "",
    )

    session["expires_in"] = (
        token_data.get(
            "expires_in"
        )
    )

    # --------------------------------------------------------
    # GET USER
    # --------------------------------------------------------

    user_result = kick_get(
        "/users",
        access_token,
    )

    user = None

    if user_result["ok"]:

        user = first_object(
            user_result["data"]
        )

    if user:

        session["user"] = user

    return redirect(
        "/dashboard"
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
def dashboard():

    if not session.get(
        "authenticated"
    ):

        return redirect(
            "/login"
        )

    return render_template(
        "dashboard.html",
        user=session.get(
            "user"
        ),
        scope=session.get(
            "scope",
            "",
        ),
    )


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    stop_ffmpeg()

    session.clear()

    return redirect("/")


# ============================================================
# KICK GET
# ============================================================

def kick_get(
    path,
    token,
    params=None,
):

    try:

        response = requests.get(
            KICK_API_BASE + path,
            headers=kick_headers(
                token
            ),
            params=params,
            timeout=30,
        )

    except Exception as exc:

        return {
            "ok": False,
            "status": 0,
            "data": None,
            "error": str(exc),
        }

    try:

        data = response.json()

    except Exception:

        data = None

    return {
        "ok": response.ok,
        "status":
            response.status_code,
        "data": data,
        "error": None,
    }


# ============================================================
# API ME
# ============================================================

@app.route("/api/me")
def api_me():

    token = session.get(
        "access_token"
    )

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
            "error":
                redact(result),
        }), 502

    return jsonify({
        "ok": True,
        "data":
            result["data"],
    })


# ============================================================
# CHANNEL
# ============================================================

@app.route("/api/channel")
def api_channel():

    token = session.get(
        "access_token"
    )

    if not token:

        return jsonify({
            "ok": False,
            "error":
                "Not logged in",
        }), 401

    result = kick_get(
        "/channels",
        token,
    )

    if not result["ok"]:

        return jsonify({
            "ok": False,
            "status":
                result["status"],
            "data":
                redact(
                    result["data"]
                ),
        }), 502

    return jsonify({
        "ok": True,
        "data":
            result["data"],
    })


# ============================================================
# STREAM CREDENTIALS
# ============================================================

def get_stream_credentials():

    token = session.get(
        "access_token"
    )

    if not token:

        return {
            "ok": False,
            "error":
                "Not logged in",
        }

    result = kick_get(
        "/channels",
        token,
    )

    if not result["ok"]:

        return {
            "ok": False,
            "error":
                "KICK /channels failed",
            "status":
                result["status"],
            "response":
                redact(
                    result["data"]
                ),
        }

    data = result["data"]

    channel = first_object(
        data
    )

    if not isinstance(
        channel,
        dict,
    ):

        return {
            "ok": False,
            "error":
                "ไม่พบ Channel",
            "response":
                redact(data),
        }

    stream = channel.get(
        "stream"
    )

    if not isinstance(
        stream,
        dict,
    ):

        stream = {}

    stream_key = str(
        stream.get(
            "key",
            "",
        )
        or ""
    ).strip()

    stream_url = str(
        stream.get(
            "url",
            "",
        )
        or ""
    ).strip()

    if not stream_key:

        return {
            "ok": False,
            "error":
                "KICK ไม่ส่ง Stream Key",
            "response":
                redact(data),
        }

    return {
        "ok": True,
        "stream_key":
            stream_key,
        "stream_url":
            stream_url or KICK_STREAM_URL,
        "slug":
            channel.get(
                "slug"
            ),
        "broadcaster_user_id":
            channel.get(
                "broadcaster_user_id"
            ),
    }


# ============================================================
# DEBUG STREAM
# ============================================================

@app.route(
    "/api/debug/stream"
)
def debug_stream():

    if not session.get(
        "authenticated"
    ):

        return jsonify({
            "ok": False,
            "error":
                "Not logged in",
        }), 401

    result = get_stream_credentials()

    result = redact(
        result
    )

    return jsonify(
        result
    )


# ============================================================
# START LIVE
# ============================================================

@app.route(
    "/api/start",
    methods=["POST"],
)
def start_live():

    if not session.get(
        "authenticated"
    ):

        return jsonify({
            "ok": False,
            "error":
                "กรุณา Login KICK ก่อน",
        }), 401

    global ffmpeg_process

    with ffmpeg_lock:

        if (
            ffmpeg_process
            and ffmpeg_process.poll()
            is None
        ):

            return jsonify({
                "ok": False,
                "error":
                    "Live กำลังทำงานอยู่แล้ว",
            }), 409

    credentials = (
        get_stream_credentials()
    )

    if not credentials["ok"]:

        return jsonify(
            credentials
        ), 502

    stream_key = (
        credentials[
            "stream_key"
        ]
    )

    stream_url = (
        credentials[
            "stream_url"
        ]
    )

    target = (
        stream_url.rstrip("/")
        + "/"
        + stream_key
    )

    # --------------------------------------------------------
    # TEST LIVE
    # --------------------------------------------------------

    command = [
        "ffmpeg",

        "-hide_banner",
        "-loglevel",
        "warning",

        "-re",

        "-f",
        "lavfi",

        "-i",
        "testsrc2="
        "size=1280x720:"
        "rate=30",

        "-f",
        "lavfi",

        "-i",
        "sine="
        "frequency=440:"
        "sample_rate=48000",

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

        "-b:v",
        "4500k",

        "-maxrate",
        "4500k",

        "-bufsize",
        "9000k",

        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-ar",
        "48000",

        "-ac",
        "2",

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
            "error":
                "Render ไม่มี FFmpeg",
        }), 500

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error":
                str(exc),
        }), 500

    with ffmpeg_lock:

        ffmpeg_process = process

    time.sleep(4)

    if process.poll() is not None:

        error_text = ""

        try:

            error_text = (
                process.stderr
                .read()
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
            "error":
                "FFmpeg หยุดทำงาน",
            "ffmpeg":
                error_text[-6000:],
        }), 502

    return jsonify({
        "ok": True,
        "message":
            "เริ่มส่ง Live ไป KICK แล้ว",
    })


# ============================================================
# STOP LIVE
# ============================================================

@app.route(
    "/api/stop",
    methods=["POST"],
)
def stop_live():

    if stop_ffmpeg():

        return jsonify({
            "ok": True,
            "message":
                "หยุด Live แล้ว",
        })

    return jsonify({
        "ok": False,
        "message":
            "ไม่มี Live ที่กำลังทำงาน",
    })


def stop_ffmpeg():

    global ffmpeg_process

    with ffmpeg_lock:

        process = ffmpeg_process

        ffmpeg_process = None

    if not process:
        return False

    try:

        if process.poll() is None:

            process.terminate()

            try:

                process.wait(
                    timeout=5
                )

            except subprocess.TimeoutExpired:

                process.kill()

                process.wait(
                    timeout=3
                )

    except Exception:
        pass

    return True


# ============================================================
# STATUS
# ============================================================

@app.route(
    "/api/status"
)
def status():

    running = False

    with ffmpeg_lock:

        if (
            ffmpeg_process
            and ffmpeg_process.poll()
            is None
        ):

            running = True

    return jsonify({
        "ok": True,
        "logged_in":
            bool(
                session.get(
                    "authenticated"
                )
            ),
        "live":
            running,
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "ok": True,
        "service":
            "kick-direct-live",
    })


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "5000",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
