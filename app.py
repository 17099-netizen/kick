import os
import time
import base64
import hashlib
import secrets
import queue
import threading
import subprocess
import json

from urllib.parse import urlencode

import requests

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    session,
    request,
)

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

APP_SECRET = os.getenv(
    "APP_SECRET",
    ""
).strip()

if not APP_SECRET:
    APP_SECRET = secrets.token_hex(32)

app.secret_key = APP_SECRET

app.config.update(
    SESSION_COOKIE_NAME="kick_ai_session",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_PERMANENT=False,
)

# ------------------------------------------------------------
# MISTRAL
# ------------------------------------------------------------

MISTRAL_API_KEY = os.getenv(
    "MISTRAL_API_KEY",
    ""
).strip()

# ใช้โมเดลที่เหมาะกับงานตอบแชตสั้น ๆ
# และเปลี่ยนได้จาก Render Environment Variables
MISTRAL_MODEL = os.getenv(
    "MISTRAL_MODEL",
    "mistral-small-latest",
).strip()

MISTRAL_API_URL = (
    "https://api.mistral.ai/v1/chat/completions"
)

# ------------------------------------------------------------
# GIVEDONATE
# ------------------------------------------------------------

GIVEDONATE_URL = os.getenv(
    "GIVEDONATE_URL",
    "https://givedonate.ct.ws/api/live_donations.php",
).strip()

GIVEDONATE_TOKEN = os.getenv(
    "GIVEDONATE_TOKEN",
    ""
).strip()

# ------------------------------------------------------------
# TTS
# ------------------------------------------------------------

TTS_URL = os.getenv(
    "TTS_URL",
    "https://donateplus.onrender.com/tts",
).strip()

TTS_VOICE = os.getenv(
    "TTS_VOICE",
    "th-female",
).strip()

# ------------------------------------------------------------
# KICK
# ------------------------------------------------------------

KICK_CLIENT_ID = os.getenv(
    "KICK_CLIENT_ID",
    ""
).strip()

KICK_CLIENT_SECRET = os.getenv(
    "KICK_CLIENT_SECRET",
    ""
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

# ============================================================
# GLOBAL STATE
# ============================================================

ffmpeg_process = None
ffmpeg_lock = threading.Lock()

audio_write_lock = threading.Lock()

event_queue = queue.Queue()

background_threads_started = False
background_threads_lock = threading.Lock()


# ============================================================
# SECURITY HELPERS
# ============================================================

def redact(value):

    secret_keys = {
        "access_token",
        "refresh_token",
        "client_secret",
        "authorization",
        "stream_key",
        "streamKey",
        "token",
    }

    if isinstance(value, dict):

        output = {}

        for key, item in value.items():

            if str(key).lower() in {
                x.lower()
                for x in secret_keys
            }:

                if str(key).lower() in {
                    "stream_key",
                    "streamkey",
                }:

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
            redact(item)
            for item in value
        ]

    return value


def first_object(data):

    if isinstance(data, list):

        return (
            data[0]
            if data
            else None
        )

    if isinstance(data, dict):

        value = data.get("data")

        if isinstance(value, list):

            return (
                value[0]
                if value
                else None
            )

        if isinstance(value, dict):
            return value

        return data

    return None


# ============================================================
# KICK API
# ============================================================

def kick_headers(token):

    return {
        "Authorization":
            f"Bearer {token}",
        "Accept":
            "application/json",
    }


def kick_get(
    path,
    token,
    params=None,
):

    try:

        response = requests.get(
            KICK_API_BASE + path,
            headers=kick_headers(token),
            params=params,
            timeout=30,
        )

    except requests.RequestException as exc:

        return {
            "ok": False,
            "status": 0,
            "error": str(exc),
            "data": None,
        }

    try:
        data = response.json()
    except Exception:
        data = None

    return {
        "ok": response.ok,
        "status":
            response.status_code,
        "error": None,
        "data": data,
        "text": response.text,
    }


# ============================================================
# KICK STREAM
# ============================================================

def normalize_kick_stream_url(
    url
):

    url = (
        str(url or "")
        .strip()
        .rstrip("/")
    )

    if not url:

        url = (
            KICK_STREAM_URL
            .rstrip("/")
        )

    if url.endswith("/app"):
        return url

    if "://" in url:

        scheme, rest = (
            url.split(
                "://",
                1
            )
        )

        host = rest.split(
            "/",
            1
        )[0]

        return (
            f"{scheme}://"
            f"{host}/app"
        )

    return (
        KICK_STREAM_URL
        .rstrip("/")
    )


def get_stream_credentials():

    token = session.get(
        "access_token"
    )

    if not token:

        return {
            "ok": False,
            "error":
                "Not logged in.",
        }

    result = kick_get(
        "/channels",
        token,
    )

    if not result["ok"]:

        return {
            "ok": False,
            "error":
                "KICK /channels failed.",
            "status":
                result["status"],
            "response":
                redact(
                    result["data"]
                ),
        }

    channel = first_object(
        result["data"]
    )

    if not isinstance(
        channel,
        dict,
    ):

        return {
            "ok": False,
            "error":
                "No channel data.",
            "response":
                redact(
                    result["data"]
                ),
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
            ""
        ) or ""
    ).strip()

    stream_url = str(
        stream.get(
            "url",
            ""
        ) or ""
    ).strip()

    if not stream_key:

        return {
            "ok": False,
            "error":
                "KICK returned empty stream key.",
            "response":
                redact(
                    result["data"]
                ),
        }

    stream_url = (
        normalize_kick_stream_url(
            stream_url
        )
    )

    target = (
        stream_url.rstrip("/")
        + "/"
        + stream_key
    )

    return {
        "ok": True,
        "stream_key":
            stream_key,
        "stream_url":
            stream_url,
        "target":
            target,
        "slug":
            channel.get("slug"),
        "broadcaster_user_id":
            channel.get(
                "broadcaster_user_id"
            ),
    }


# ============================================================
# MISTRAL AI
# ============================================================

def mistral_generate(
    user_text
):

    if not MISTRAL_API_KEY:

        raise RuntimeError(
            "MISTRAL_API_KEY is missing."
        )

    system_prompt = """
คุณคือ AI สตรีมเมอร์ภาษาไทย

หน้าที่:
- ตอบคำถามของคนดูแบบเป็นธรรมชาติ
- ตอบสั้น กระชับ เหมาะกับการอ่านออกเสียง
- ปกติประมาณ 1 ถึง 3 ประโยค
- ใช้ภาษาไทยเป็นหลัก
- ห้ามใช้ Markdown
- ห้ามใช้ bullet
- ห้ามใส่คำอธิบายเกี่ยวกับระบบ
- ถ้ามี Donate ให้ขอบคุณอย่างเป็นธรรมชาติ
- ถ้ามีคำถาม ให้ตอบคำถามก่อน
- อย่าเขียนคำตอบยาวเกินความจำเป็น
"""

    payload = {
        "model":
            MISTRAL_MODEL,

        "messages": [
            {
                "role":
                    "system",
                "content":
                    system_prompt,
            },
            {
                "role":
                    "user",
                "content":
                    user_text,
            },
        ],

        "temperature":
            0.8,

        "max_tokens":
            180,
    }

    response = requests.post(
        MISTRAL_API_URL,
        headers={
            "Authorization":
                f"Bearer {MISTRAL_API_KEY}",
            "Content-Type":
                "application/json",
        },
        json=payload,
        timeout=60,
    )

    try:
        data = response.json()
    except Exception:
        data = {}

    if not response.ok:

        raise RuntimeError(
            f"Mistral API error "
            f"{response.status_code}: "
            f"{redact(data)}"
        )

    choices = data.get(
        "choices",
        []
    )

    if not choices:

        raise RuntimeError(
            "Mistral returned no choices."
        )

    message = (
        choices[0]
        .get(
            "message",
            {}
        )
    )

    answer = (
        message.get(
            "content",
            ""
        )
        or ""
    ).strip()

    if not answer:

        raise RuntimeError(
            "Mistral returned empty text."
        )

    return answer


# ============================================================
# TTS
# ============================================================

def download_tts(
    text
):

    response = requests.get(
        TTS_URL,
        params={
            "text":
                text,
            "voice":
                TTS_VOICE,
        },
        timeout=90,
    )

    response.raise_for_status()

    content_type = (
        response.headers
        .get(
            "Content-Type",
            ""
        )
        .lower()
    )

    if not (
        "audio/" in content_type
        or
        response.content[:3] == b"ID3"
        or
        response.content[:2] == b"\xff\xfb"
    ):

        raise RuntimeError(
            "TTS endpoint did not return audio. "
            f"Content-Type: {content_type}"
        )

    return response.content


# ============================================================
# AUDIO → PCM
# ============================================================

def audio_to_pcm(
    audio_bytes
):

    process = subprocess.Popen(
        [
            "ffmpeg",

            "-hide_banner",
            "-loglevel",
            "error",

            "-i",
            "pipe:0",

            "-f",
            "s16le",

            "-ar",
            "48000",

            "-ac",
            "2",

            "pipe:1",
        ],

        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout, stderr = (
        process.communicate(
            audio_bytes
        )
    )

    if process.returncode != 0:

        raise RuntimeError(
            "Audio decode failed: "
            + stderr.decode(
                "utf-8",
                errors="replace"
            )
        )

    return stdout


# ============================================================
# FFMPEG LIVE
# ============================================================

def start_ffmpeg_stream():

    global ffmpeg_process

    credentials = (
        get_stream_credentials()
    )

    if not credentials["ok"]:

        raise RuntimeError(
            credentials["error"]
        )

    target = credentials[
        "target"
    ]

    command = [
        "ffmpeg",

        "-hide_banner",
        "-loglevel",
        "warning",

        # Background
        "-f",
        "lavfi",

        "-i",
        "color="
        "c=0x080D0A:"
        "s=1280x720:"
        "r=30",

        # Raw PCM
        "-f",
        "s16le",

        "-ar",
        "48000",

        "-ac",
        "2",

        "-i",
        "pipe:0",

        # Visualizer
        "-filter_complex",

        (
            "[0:v]"
            "drawtext="
            "fontcolor=white:"
            "fontsize=42:"
            "text='AI LIVE':"
            "x=(w-text_w)/2:"
            "y=70"
            "[bg];"

            "[1:a]"
            "showwaves="
            "s=1100x300:"
            "mode=cline:"
            "rate=30:"
            "colors=0x53FF9D:"
            "draw=full"
            "[wave];"

            "[bg][wave]"
            "overlay="
            "(W-w)/2:"
            "(H-h)/2"
        ),

        "-map",
        "0:v",

        "-map",
        "1:a",

        # H264
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

        # AAC
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

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    with ffmpeg_lock:
        ffmpeg_process = process

    return process


# ============================================================
# WRITE PCM
# ============================================================

def write_pcm(
    pcm
):

    with ffmpeg_lock:
        process = ffmpeg_process

    if not process:
        return

    if process.poll() is not None:
        return

    with audio_write_lock:

        try:

            process.stdin.write(
                pcm
            )

            process.stdin.flush()

        except (
            BrokenPipeError,
            OSError,
        ):

            pass


# ============================================================
# SILENCE
# ============================================================

def silence_loop():

    # 100ms:
    # 4800 samples
    # 2 channels
    # 16-bit = 4 bytes/sample-frame
    silence = (
        b"\x00\x00"
        * 2
        * 4800
    )

    while True:

        with ffmpeg_lock:
            process = ffmpeg_process

        if (
            process
            and process.poll() is None
        ):

            write_pcm(
                silence
            )

        else:

            time.sleep(
                0.2
            )


# ============================================================
# AI EVENT QUEUE
# ============================================================

def enqueue_event(
    event
):

    event_queue.put(
        event
    )


def event_worker():

    while True:

        event = event_queue.get()

        try:

            process_event(
                event
            )

        except Exception as exc:

            print(
                "AI event error:",
                repr(exc)
            )

        finally:

            event_queue.task_done()


def process_event(
    event
):

    event_type = event.get(
        "type"
    )

    username = str(
        event.get(
            "username",
            "ผู้ชม"
        )
    )

    if event_type == "donation":

        amount = str(
            event.get(
                "amount",
                "0"
            )
        )

        message = str(
            event.get(
                "message",
                ""
            )
        )

        prompt = (
            f"ผู้ชมชื่อ {username} "
            f"โดเนท {amount} บาท"
        )

        if message:

            prompt += (
                f" และเขียนข้อความว่า "
                f"{message}"
            )

        reply = (
            mistral_generate(
                prompt
            )
        )

    else:

        message = str(
            event.get(
                "message",
                ""
            )
        )

        reply = (
            mistral_generate(
                (
                    f"ผู้ชมชื่อ {username} "
                    f"ถามว่า {message}"
                )
            )
        )

    print(
        "AI:",
        reply
    )

    # --------------------------------------------------------
    # TTS
    # --------------------------------------------------------

    audio = download_tts(
        reply
    )

    # --------------------------------------------------------
    # MP3 → PCM
    # --------------------------------------------------------

    pcm = audio_to_pcm(
        audio
    )

    # --------------------------------------------------------
    # Send to live encoder
    # --------------------------------------------------------

    write_pcm(
        pcm
    )


# ============================================================
# GIVEDONATE SSE
# ============================================================

def parse_sse_block(
    block
):

    data_lines = []

    for line in block.splitlines():

        if line.startswith(
            "data:"
        ):

            data_lines.append(
                line[5:].strip()
            )

    if not data_lines:
        return None

    raw = "\n".join(
        data_lines
    )

    try:

        return json.loads(
            raw
        )

    except Exception:

        return {
            "message":
                raw
        }


def normalize_donation(
    event
):

    username = (
        event.get(
            "username"
        )
        or event.get(
            "name"
        )
        or event.get(
            "donor"
        )
        or "ผู้สนับสนุน"
    )

    amount = (
        event.get(
            "amount"
        )
        or event.get(
            "price"
        )
        or event.get(
            "value"
        )
        or 0
    )

    message = (
        event.get(
            "message"
        )
        or event.get(
            "note"
        )
        or event.get(
            "comment"
        )
        or ""
    )

    return {
        "type":
            "donation",

        "username":
            str(username),

        "amount":
            str(amount),

        "message":
            str(message),
    }


def donation_listener():

    if not GIVEDONATE_TOKEN:

        print(
            "GIVEDONATE_TOKEN is missing."
        )

        return

    while True:

        try:

            response = requests.get(
                GIVEDONATE_URL,

                params={
                    "token":
                        GIVEDONATE_TOKEN
                },

                headers={
                    "Accept":
                        "text/event-stream",
                    "Cache-Control":
                        "no-cache",
                },

                stream=True,

                timeout=90,
            )

            response.raise_for_status()

            buffer = ""

            for line in response.iter_lines(
                decode_unicode=True
            ):

                if line is None:
                    continue

                if line == "":

                    event = (
                        parse_sse_block(
                            buffer
                        )
                    )

                    buffer = ""

                    if event:

                        donation = (
                            normalize_donation(
                                event
                            )
                        )

                        enqueue_event(
                            donation
                        )

                    continue

                buffer += (
                    line
                    + "\n"
                )

        except Exception as exc:

            print(
                "GiveDonate SSE error:",
                repr(exc)
            )

            time.sleep(
                3
            )


# ============================================================
# BACKGROUND THREADS
# ============================================================

def start_background_workers():

    global background_threads_started

    with background_threads_lock:

        if background_threads_started:
            return

        background_threads_started = True

    threading.Thread(
        target=event_worker,
        daemon=True,
    ).start()

    threading.Thread(
        target=donation_listener,
        daemon=True,
    ).start()

    threading.Thread(
        target=silence_loop,
        daemon=True,
    ).start()


# ============================================================
# PAGES
# ============================================================

@app.route("/")
def index():

    if session.get(
        "authenticated"
    ):

        return redirect(
            "/dashboard"
        )

    return render_template(
        "index.html"
    )


# ============================================================
# LOGIN
# ============================================================

@app.route("/login")
def login():

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

    session[
        "oauth_state"
    ] = state

    session[
        "oauth_verifier"
    ] = verifier

    params = {
        "response_type":
            "code",

        "client_id":
            KICK_CLIENT_ID,

        "redirect_uri":
            KICK_REDIRECT_URI,

        "scope":
            " ".join(
                KICK_SCOPES
            ),

        "state":
            state,

        "code_challenge":
            challenge,

        "code_challenge_method":
            "S256",
    }

    return redirect(
        KICK_AUTH_URL
        + "?"
        + urlencode(params)
    )


# ============================================================
# CALLBACK
# ============================================================

@app.route("/callback")
def callback():

    error = request.args.get(
        "error"
    )

    if error:

        return (
            f"""
            <h2>KICK Login Error</h2>
            <p>{error}</p>
            <a href="/login">Login ใหม่</a>
            """,
            400,
        )

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

        return (
            "ไม่มี authorization code",
            400,
        )

    if not state or not saved_state:

        return (
            "OAuth session หาย "
            "กรุณา Login ใหม่",
            400,
        )

    if not secrets.compare_digest(
        state,
        saved_state,
    ):

        return (
            "OAuth state ไม่ตรงกัน",
            400,
        )

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

        data = response.json()

    except Exception as exc:

        return (
            f"Token exchange failed: "
            f"{exc}",
            502,
        )

    if not response.ok:

        return (
            f"""
            <h2>KICK Token Error</h2>
            <pre>{redact(data)}</pre>
            <a href="/login">Login ใหม่</a>
            """,
            400,
        )

    access_token = data.get(
        "access_token"
    )

    if not access_token:

        return (
            "KICK ไม่ส่ง Access Token",
            400,
        )

    session.clear()

    session[
        "authenticated"
    ] = True

    session[
        "access_token"
    ] = access_token

    session[
        "scope"
    ] = data.get(
        "scope",
        ""
    )

    user_result = kick_get(
        "/users",
        access_token,
    )

    if user_result["ok"]:

        user = first_object(
            user_result["data"]
        )

        if user:

            session[
                "user"
            ] = user

    start_background_workers()

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
    )


# ============================================================
# START LIVE
# ============================================================

@app.route(
    "/api/start",
    methods=["POST"],
)
def api_start():

    if not session.get(
        "authenticated"
    ):

        return jsonify({
            "ok": False,
            "error":
                "กรุณา Login KICK ก่อน"
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
                    "Live กำลังทำงานอยู่แล้ว"
            }), 409

    try:

        process = (
            start_ffmpeg_stream()
        )

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error":
                str(exc),
        }), 500

    start_background_workers()

    time.sleep(3)

    if process.poll() is not None:

        stderr = ""

        try:

            stderr = (
                process.stderr
                .read()
                .decode(
                    "utf-8",
                    errors="replace"
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
                stderr[-6000:],
        }), 502

    return jsonify({
        "ok": True,
        "message":
            "เริ่ม AI Live แล้ว",
    })


# ============================================================
# STOP LIVE
# ============================================================

@app.route(
    "/api/stop",
    methods=["POST"],
)
def api_stop():

    global ffmpeg_process

    with ffmpeg_lock:

        process = ffmpeg_process

        ffmpeg_process = None

    if not process:

        return jsonify({
            "ok": False,
            "message":
                "ไม่มี Live"
        })

    try:

        if process.poll() is None:

            process.terminate()

            try:

                process.wait(
                    timeout=5
                )

            except subprocess.TimeoutExpired:

                process.kill()

    except Exception:
        pass

    return jsonify({
        "ok": True,
        "message":
            "หยุด AI Live แล้ว"
    })


# ============================================================
# STATUS
# ============================================================

@app.route(
    "/api/status"
)
def api_status():

    running = False

    with ffmpeg_lock:

        if (
            ffmpeg_process
            and ffmpeg_process.poll()
            is None
        ):

            running = True

    return jsonify({

        "ok":
            True,

        "live":
            running,

        "authenticated":
            bool(
                session.get(
                    "authenticated"
                )
            ),

        "mistral":
            bool(
                MISTRAL_API_KEY
            ),

        "tts":
            bool(
                TTS_URL
            ),

        "givedonate":
            bool(
                GIVEDONATE_TOKEN
            ),
    })


# ============================================================
# TEST MISTRAL
# ============================================================

@app.route(
    "/api/test/mistral",
    methods=["POST"],
)
def test_mistral():

    try:

        answer = (
            mistral_generate(
                "พูดทักทายคนดูในไลฟ์แบบสั้น ๆ"
            )
        )

        return jsonify({
            "ok":
                True,
            "answer":
                answer,
            "model":
                MISTRAL_MODEL,
        })

    except Exception as exc:

        return jsonify({
            "ok":
                False,
            "error":
                str(exc),
        }), 500


# ============================================================
# TEST TTS
# ============================================================

@app.route(
    "/api/test/tts"
)
def test_tts():

    text = request.args.get(
        "text",
        "สวัสดีครับ",
    )

    try:

        audio = download_tts(
            text
        )

        return jsonify({

            "ok":
                True,

            "bytes":
                len(audio),

            "content_type":
                "audio/mpeg",

        })

    except Exception as exc:

        return jsonify({

            "ok":
                False,

            "error":
                str(exc),

        }), 500


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

    return jsonify(
        redact(
            get_stream_credentials()
        )
    )


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/health"
)
def health():

    return jsonify({
        "ok":
            True,
        "service":
            "kick-mistral-live",
    })


# ============================================================
# LOGOUT
# ============================================================

@app.route(
    "/logout"
)
def logout():

    global ffmpeg_process

    with ffmpeg_lock:

        process = ffmpeg_process
        ffmpeg_process = None

    if process:

        try:

            if process.poll() is None:

                process.terminate()

        except Exception:
            pass

    session.clear()

    return redirect(
        "/"
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    start_background_workers()

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
