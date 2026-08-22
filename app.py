import os
import time
import base64
import hashlib
import secrets
import subprocess
import threading
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, redirect, render_template, request, session

app = Flask(__name__)

# =========================================================
# CONFIG
# =========================================================

APP_SECRET = os.getenv("APP_SECRET")

if not APP_SECRET:
    raise RuntimeError("APP_SECRET is not configured.")

app.secret_key = APP_SECRET

KICK_CLIENT_ID = os.getenv("KICK_CLIENT_ID", "").strip()
KICK_CLIENT_SECRET = os.getenv("KICK_CLIENT_SECRET", "").strip()
KICK_REDIRECT_URI = os.getenv(
    "KICK_REDIRECT_URI",
    "https://kick-crka.onrender.com/callback",
).strip()

KICK_AUTH_URL = "https://id.kick.com/oauth/authorize"
KICK_TOKEN_URL = "https://id.kick.com/oauth/token"
KICK_INTROSPECT_URL = "https://id.kick.com/oauth/token/introspect"
KICK_API_BASE = "https://api.kick.com/public/v1"

# KICK documented scopes
KICK_SCOPES = [
    "user:read",
    "channel:read",
    "streamkey:read",
]

# Default KICK ingest URL shown by KICK help documentation.
# The channel API can also return the URL for the authenticated
# broadcaster when the account/scope permits it.
DEFAULT_KICK_STREAM_URL = os.getenv(
    "KICK_STREAM_URL",
    "rtmps://fa723fc1b171.global-contribute.live-video.net:443/app",
).strip()

# Current encoder process
ffmpeg_process = None
ffmpeg_lock = threading.Lock()


# =========================================================
# UTILS
# =========================================================

def redact(value):
    """
    Prevent secrets/tokens from appearing in error responses.
    """
    if isinstance(value, dict):
        result = {}

        for key, item in value.items():
            key_lower = str(key).lower()

            if any(
                secret_name in key_lower
                for secret_name in [
                    "access_token",
                    "refresh_token",
                    "client_secret",
                    "authorization",
                    "token",
                ]
            ):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact(item)

        return result

    if isinstance(value, list):
        return [redact(item) for item in value]

    return value


def config_ok():
    missing = []

    if not KICK_CLIENT_ID:
        missing.append("KICK_CLIENT_ID")

    if not KICK_CLIENT_SECRET:
        missing.append("KICK_CLIENT_SECRET")

    if not KICK_REDIRECT_URI:
        missing.append("KICK_REDIRECT_URI")

    if not APP_SECRET:
        missing.append("APP_SECRET")

    return missing


def api_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }


def safe_json_response(response):
    try:
        return response.json()
    except Exception:
        return None


# =========================================================
# HOME
# =========================================================

@app.route("/")
def index():
    return render_template(
        "index.html",
        logged_in=bool(session.get("access_token")),
        user=session.get("user"),
        error=session.get("flash_error"),
    )


# =========================================================
# LOGIN
# =========================================================

@app.route("/login")
def login():
    missing = config_ok()

    if missing:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Missing environment variables.",
                    "missing": missing,
                }
            ),
            500,
        )

    # -----------------------------------------------------
    # PKCE verifier
    # -----------------------------------------------------

    code_verifier = secrets.token_urlsafe(64)

    digest = hashlib.sha256(
        code_verifier.encode("utf-8")
    ).digest()

    code_challenge = (
        base64.urlsafe_b64encode(digest)
        .decode("utf-8")
        .rstrip("=")
    )

    # -----------------------------------------------------
    # OAuth state
    # -----------------------------------------------------

    state = secrets.token_urlsafe(32)

    session["oauth_state"] = state
    session["oauth_code_verifier"] = code_verifier

    params = {
        "response_type": "code",
        "client_id": KICK_CLIENT_ID,
        "redirect_uri": KICK_REDIRECT_URI,
        "scope": " ".join(KICK_SCOPES),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }

    auth_url = (
        KICK_AUTH_URL
        + "?"
        + urlencode(params)
    )

    return redirect(auth_url)


# =========================================================
# CALLBACK
# =========================================================

@app.route("/callback")
def callback():

    # -----------------------------------------------------
    # KICK may return OAuth errors directly in the callback.
    # Never redirect silently back to login.
    # -----------------------------------------------------

    oauth_error = request.args.get("error")

    if oauth_error:

        description = request.args.get(
            "error_description",
            "",
        )

        return (
            f"""
            <!doctype html>
            <html lang="th">
            <head>
                <meta charset="utf-8">
                <title>KICK OAuth Error</title>
                <style>
                    body {{
                        font-family: Arial, sans-serif;
                        background: #111;
                        color: #fff;
                        padding: 40px;
                    }}
                    .box {{
                        max-width: 760px;
                        margin: auto;
                        background: #1c1c1c;
                        padding: 24px;
                        border-radius: 16px;
                    }}
                    a {{
                        color: #53ff9d;
                    }}
                    code {{
                        background: #000;
                        padding: 3px 6px;
                        border-radius: 6px;
                    }}
                </style>
            </head>
            <body>
                <div class="box">
                    <h1>เข้าสู่ระบบ KICK ไม่สำเร็จ</h1>
                    <p><b>Error:</b> <code>{oauth_error}</code></p>
                    <p>{description}</p>
                    <p>
                        <a href="/login">ลอง Login ใหม่</a>
                    </p>
                </div>
            </body>
            </html>
            """,
            400,
        )

    code = request.args.get("code")
    returned_state = request.args.get("state")

    saved_state = session.get("oauth_state")
    code_verifier = session.get(
        "oauth_code_verifier"
    )

    if not code:
        return (
            """
            <h2>KICK Login Error</h2>
            <p>ไม่มี authorization code กลับมาจาก KICK</p>
            <a href="/login">ลองใหม่</a>
            """,
            400,
        )

    if not returned_state:
        return (
            """
            <h2>KICK Login Error</h2>
            <p>ไม่มี OAuth state กลับมา</p>
            <a href="/login">ลองใหม่</a>
            """,
            400,
        )

    if not saved_state:
        return (
            """
            <h2>KICK Login Error</h2>
            <p>OAuth session หายไปก่อน callback</p>
            <p>
                สาเหตุที่พบบ่อย:
                cookie/session ของ Render ไม่ถูกเก็บไว้
                หรือเปิด callback ด้วยคนละ URL
            </p>
            <a href="/login">ลองใหม่</a>
            """,
            400,
        )

    if not secrets.compare_digest(
        returned_state,
        saved_state,
    ):
        return (
            """
            <h2>KICK Login Error</h2>
            <p>OAuth state ไม่ตรงกัน</p>
            <a href="/login">ลองใหม่</a>
            """,
            400,
        )

    if not code_verifier:
        return (
            """
            <h2>KICK Login Error</h2>
            <p>PKCE verifier หายไปจาก session</p>
            <a href="/login">ลองใหม่</a>
            """,
            400,
        )

    # -----------------------------------------------------
    # Exchange code -> token
    # -----------------------------------------------------

    token_payload = {
        "code": code,
        "client_id": KICK_CLIENT_ID,
        "client_secret": KICK_CLIENT_SECRET,
        "redirect_uri": KICK_REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }

    try:
        response = requests.post(
            KICK_TOKEN_URL,
            data=token_payload,
            headers={
                "Content-Type":
                    "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        return (
            f"""
            <h2>KICK Token Error</h2>
            <p>{str(exc)}</p>
            <a href="/login">ลองใหม่</a>
            """,
            502,
        )

    token_data = safe_json_response(response)

    if not response.ok:

        return (
            f"""
            <!doctype html>
            <html lang="th">
            <head>
                <meta charset="utf-8">
                <title>KICK Token Error</title>
            </head>
            <body>
                <h2>KICK Token Error</h2>
                <p>HTTP {response.status_code}</p>
                <pre>{redact(token_data if token_data is not None else response.text)}</pre>
                <a href="/login">ลองใหม่</a>
            </body>
            </html>
            """,
            400,
        )

    if not isinstance(token_data, dict):
        return (
            """
            <h2>KICK Token Error</h2>
            <p>KICK ส่งข้อมูล token ที่ไม่ใช่ JSON object</p>
            <a href="/login">ลองใหม่</a>
            """,
            502,
        )

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")

    if not access_token:
        return (
            f"""
            <h2>KICK Token Error</h2>
            <pre>{redact(token_data)}</pre>
            <a href="/login">ลองใหม่</a>
            """,
            400,
        )

    # -----------------------------------------------------
    # Save login session
    # -----------------------------------------------------

    session["access_token"] = access_token

    if refresh_token:
        session["refresh_token"] = refresh_token

    session["token_scope"] = token_data.get(
        "scope",
        "",
    )

    session["expires_in"] = token_data.get(
        "expires_in"
    )

    # Temporary OAuth values no longer needed
    session.pop(
        "oauth_state",
        None,
    )

    session.pop(
        "oauth_code_verifier",
        None,
    )

    # -----------------------------------------------------
    # Read current user
    # -----------------------------------------------------

    user_result = kick_api_get(
        "/users",
        access_token,
    )

    if user_result["ok"]:

        user_data = user_result["data"]

        user = first_object(
            user_data
        )

        if user:
            session["user"] = user

    # -----------------------------------------------------
    # Go to dashboard
    # -----------------------------------------------------

    session.pop(
        "flash_error",
        None,
    )

    return redirect("/")


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():
    stop_ffmpeg()

    session.clear()

    return redirect("/")


# =========================================================
# TOKEN INTROSPECT
# =========================================================

@app.route("/api/token-info")
def token_info():

    access_token = session.get(
        "access_token"
    )

    if not access_token:
        return jsonify(
            {
                "ok": False,
                "logged_in": False,
            }
        ), 401

    try:
        response = requests.post(
            KICK_INTROSPECT_URL,
            headers={
                "Authorization":
                    f"Bearer {access_token}",
                "Accept":
                    "application/json",
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
            }
        ), 502

    data = safe_json_response(response)

    if not response.ok:
        return jsonify(
            {
                "ok": False,
                "status":
                    response.status_code,
                "data":
                    redact(
                        data
                        if data is not None
                        else response.text
                    ),
            }
        ), 502

    return jsonify(
        {
            "ok": True,
            "data": redact(data),
        }
    )


# =========================================================
# KICK API REQUEST
# =========================================================

def kick_api_get(
    path,
    access_token,
    params=None,
):

    url = KICK_API_BASE + path

    try:

        response = requests.get(
            url,
            headers=api_headers(
                access_token
            ),
            params=params,
            timeout=30,
        )

    except requests.RequestException as exc:

        return {
            "ok": False,
            "status": 0,
            "data": None,
            "text": "",
            "error": str(exc),
        }

    data = safe_json_response(
        response
    )

    return {
        "ok": response.ok,
        "status": response.status_code,
        "data": data,
        "text": response.text,
        "error": None,
    }


# =========================================================
# CURRENT USER
# =========================================================

@app.route("/api/me")
def api_me():

    access_token = session.get(
        "access_token"
    )

    if not access_token:
        return jsonify(
            {
                "ok": False,
                "logged_in": False,
            }
        ), 401

    result = kick_api_get(
        "/users",
        access_token,
    )

    if not result["ok"]:
        return jsonify(
            {
                "ok": False,
                "status":
                    result["status"],
                "error":
                    redact(
                        result["data"]
                        if result["data"] is not None
                        else result["text"]
                    ),
            }
        ), 502

    return jsonify(
        {
            "ok": True,
            "data": result["data"],
        }
    )


# =========================================================
# CURRENT CHANNEL
# =========================================================

@app.route("/api/channel")
def api_channel():

    access_token = session.get(
        "access_token"
    )

    if not access_token:
        return jsonify(
            {
                "ok": False,
                "error": "Not logged in.",
            }
        ), 401

    # KICK /channels without broadcaster ID
    # can return the channel for the authenticated user.
    result = kick_api_get(
        "/channels",
        access_token,
    )

    if not result["ok"]:
        return jsonify(
            {
                "ok": False,
                "status":
                    result["status"],
                "error":
                    redact(
                        result["data"]
                        if result["data"] is not None
                        else result["text"]
                    ),
            }
        ), 502

    return jsonify(
        {
            "ok": True,
            "data": result["data"],
        }
    )


# =========================================================
# GET STREAM CREDENTIALS
# =========================================================

def get_stream_credentials():

    access_token = session.get(
        "access_token"
    )

    if not access_token:
        return {
            "ok": False,
            "error":
                "Not logged in.",
        }

    # -----------------------------------------------------
    # KICK's channels response is where the authenticated
    # broadcaster's stream object is exposed.
    #
    # Expected shape:
    #
    # data: [
    #   {
    #      "stream": {
    #          "url": "...",
    #          "key": "..."
    #      }
    #   }
    # ]
    #
    # The key is available only when the authenticated user
    # owns the channel and streamkey:read is authorized.
    # -----------------------------------------------------

    result = kick_api_get(
        "/channels",
        access_token,
    )

    if not result["ok"]:

        return {
            "ok": False,
            "error":
                "KICK /channels request failed.",
            "status":
                result["status"],
            "response":
                redact(
                    result["data"]
                    if result["data"] is not None
                    else result["text"]
                ),
        }

    data = result["data"]

    channel = first_object(data)

    if not isinstance(
        channel,
        dict,
    ):

        return {
            "ok": False,
            "error":
                "KICK /channels returned no channel data.",
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

    stream_url = str(
        stream.get(
            "url",
            ""
        ) or ""
    ).strip()

    stream_key = str(
        stream.get(
            "key",
            ""
        ) or ""
    ).strip()

    broadcaster_user_id = channel.get(
        "broadcaster_user_id"
    )

    slug = channel.get(
        "slug"
    )

    # -----------------------------------------------------
    # If empty, return the REAL KICK response.
    # This is especially useful because KICK has documented
    # that the stream object can contain empty values while
    # offline in some API cases.
    # -----------------------------------------------------

    if not stream_key:

        return {
            "ok": False,
            "error":
                "KICK returned an empty stream.key.",
            "status":
                200,
            "channel": {
                "broadcaster_user_id":
                    broadcaster_user_id,
                "slug":
                    slug,
            },
            "stream": {
                "is_live":
                    stream.get("is_live"),
                "url":
                    stream_url,
                "key":
                    "",
            },
            "raw_response":
                redact(data),
            "hint": (
                "Check that this OAuth token includes "
                "streamkey:read and belongs to the broadcaster. "
                "If KICK returns an empty key while the channel "
                "is offline, verify the key from Creator Dashboard "
                "and check the API response again."
            ),
        }

    return {
        "ok": True,
        "stream_key": stream_key,
        "stream_url":
            stream_url or DEFAULT_KICK_STREAM_URL,
        "broadcaster_user_id":
            broadcaster_user_id,
        "slug":
            slug,
        "is_live":
            stream.get(
                "is_live"
            ),
    }


# =========================================================
# DEBUG STREAM
# =========================================================

@app.route("/api/debug/stream")
def debug_stream():

    if not session.get(
        "access_token"
    ):
        return jsonify(
            {
                "ok": False,
                "error": "Not logged in.",
            }
        ), 401

    result = get_stream_credentials()

    # Never expose actual stream key.
    clean_result = redact(
        result
    )

    if result.get("stream_key"):
        clean_result["stream_key"] = (
            "[REDACTED: key exists]"
        )

    return jsonify(
        clean_result
    )


# =========================================================
# START LIVE
# =========================================================

@app.route(
    "/api/start",
    methods=["POST"],
)
def start_live():

    if not session.get(
        "access_token"
    ):
        return jsonify(
            {
                "ok": False,
                "error":
                    "กรุณา Login KICK ก่อน",
            }
        ), 401

    global ffmpeg_process

    # -----------------------------------------------------
    # Check existing process
    # -----------------------------------------------------

    with ffmpeg_lock:

        if (
            ffmpeg_process
            and ffmpeg_process.poll() is None
        ):
            return jsonify(
                {
                    "ok": False,
                    "error":
                        "Live กำลังทำงานอยู่แล้ว",
                }
            ), 409

        ffmpeg_process = None

    # -----------------------------------------------------
    # Get stream credentials
    # -----------------------------------------------------

    credentials = get_stream_credentials()

    if not credentials["ok"]:

        return jsonify(
            {
                "ok": False,
                "error":
                    credentials.get(
                        "error",
                        "ไม่สามารถอ่าน Stream Key ได้",
                    ),
                "details":
                    redact(
                        credentials
                    ),
            }
        ), 502

    stream_key = credentials[
        "stream_key"
    ]

    stream_url = (
        credentials.get(
            "stream_url"
        )
        or DEFAULT_KICK_STREAM_URL
    )

    # -----------------------------------------------------
    # Build RTMPS target
    #
    # KICK examples use:
    #   rtmps://host:443/app
    #
    # and the stream key is appended after /app/.
    # -----------------------------------------------------

    target = (
        stream_url.rstrip("/")
        + "/"
        + stream_key
    )

    # -----------------------------------------------------
    # TEST SOURCE
    #
    # Replace this later with:
    # AI video renderer + AI TTS/audio
    # -----------------------------------------------------

    ffmpeg_command = [
        "ffmpeg",

        "-hide_banner",
        "-loglevel",
        "warning",

        # -------------------------------------------------
        # VIDEO TEST
        # -------------------------------------------------

        "-f",
        "lavfi",

        "-re",

        "-i",
        "testsrc2="
        "size=1280x720:"
        "rate=30",

        # -------------------------------------------------
        # AUDIO TEST
        # -------------------------------------------------

        "-f",
        "lavfi",

        "-re",

        "-i",
        "sine="
        "frequency=440:"
        "sample_rate=48000",

        # -------------------------------------------------
        # VIDEO ENCODE
        # -------------------------------------------------

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-tune",
        "zerolatency",

        "-profile:v",
        "main",

        "-pix_fmt",
        "yuv420p",

        "-r",
        "30",

        "-g",
        "60",

        "-keyint_min",
        "60",

        "-sc_threshold",
        "0",

        "-b:v",
        "4500k",

        "-maxrate",
        "4500k",

        "-bufsize",
        "9000k",

        # -------------------------------------------------
        # AUDIO
        # -------------------------------------------------

        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-ar",
        "48000",

        "-ac",
        "2",

        # -------------------------------------------------
        # OUTPUT
        # -------------------------------------------------

        "-f",
        "flv",

        target,
    ]

    try:

        process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    except FileNotFoundError:

        return jsonify(
            {
                "ok": False,
                "error":
                    "ไม่พบ FFmpeg บน Render",
                "hint":
                    "ตรวจ Dockerfile หรือ build command",
            }
        ), 500

    except Exception as exc:

        return jsonify(
            {
                "ok": False,
                "error": str(exc),
            }
        ), 500

    with ffmpeg_lock:
        ffmpeg_process = process

    # -----------------------------------------------------
    # Wait for encoder to establish the connection.
    # -----------------------------------------------------

    time.sleep(4)

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

        return jsonify(
            {
                "ok": False,
                "error":
                    "FFmpeg หยุดทำงานทันที",
                "ffmpeg":
                    stderr_output[-6000:],
            }
        ), 502

    return jsonify(
        {
            "ok": True,
            "message":
                "ส่งสัญญาณ Live ไป KICK แล้ว",
            "channel":
                credentials.get("slug"),
            "broadcaster_user_id":
                credentials.get(
                    "broadcaster_user_id"
                ),
        }
    )


# =========================================================
# STOP LIVE
# =========================================================

@app.route(
    "/api/stop",
    methods=["POST"],
)
def stop_live_api():

    stopped = stop_ffmpeg()

    if not stopped:
        return jsonify(
            {
                "ok": False,
                "message":
                    "ไม่มี Live ที่กำลังทำงาน",
            }
        )

    return jsonify(
        {
            "ok": True,
            "message":
                "หยุด Live แล้ว",
        }
    )


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


# =========================================================
# LIVE STATUS
# =========================================================

@app.route("/api/status")
def api_status():

    with ffmpeg_lock:

        process = ffmpeg_process

        process_running = bool(
            process
            and process.poll() is None
        )

    logged_in = bool(
        session.get("access_token")
    )

    return jsonify(
        {
            "ok": True,
            "logged_in":
                logged_in,
            "encoder_running":
                process_running,
        }
    )


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():

    return jsonify(
        {
            "ok": True,
            "service":
                "kick-direct-live",
        }
    )


# =========================================================
# FIRST OBJECT
# =========================================================

def first_object(data):

    if isinstance(
        data,
        dict,
    ):

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

    if isinstance(
        data,
        list,
    ):

        if data:
            return data[0]

    return None


# =========================================================
# CLEANUP
# =========================================================

@app.teardown_appcontext
def teardown_appcontext(
    exception=None
):
    # Do NOT stop FFmpeg here.
    # Flask application context cleanup can run during
    # normal requests and would kill the Live process.
    pass


# =========================================================
# MAIN
# =========================================================

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
