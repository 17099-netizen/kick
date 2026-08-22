import os
import time
import json
import base64
import hashlib
import secrets
import queue
import threading
import subprocess
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
    SESSION_COOKIE_NAME="kick_ai_session",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_PERMANENT=False,
)

# ============================================================
# MISTRAL
# ============================================================

MISTRAL_API_KEY = os.getenv(
    "MISTRAL_API_KEY",
    ""
).strip()

MISTRAL_MODEL = os.getenv(
    "MISTRAL_MODEL",
    "mistral-small-latest"
).strip()

MISTRAL_API_URL = (
    "https://api.mistral.ai/v1/chat/completions"
)

# ============================================================
# GIVEDONATE
# ============================================================

GIVEDONATE_URL = os.getenv(
    "GIVEDONATE_URL",
    "https://givedonate.ct.ws/api/live_donations.php"
).strip()

GIVEDONATE_TOKEN = os.getenv(
    "GIVEDONATE_TOKEN",
    ""
).strip()

# ============================================================
# TTS
# ============================================================

TTS_URL = os.getenv(
    "TTS_URL",
    "https://donateplus.onrender.com/tts"
).strip()

TTS_VOICE = os.getenv(
    "TTS_VOICE",
    "th-female"
).strip()

# ============================================================
# KICK
# ============================================================

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
    "https://kick-crka.onrender.com/callback"
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
    "rtmps://fa723fc1b171.global-contribute.live-video.net:443/app"
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
# SECURITY
# ============================================================

def redact(value):
    sensitive_keys = {
        "access_token",
        "refresh_token",
        "client_secret",
        "authorization",
        "stream_key",
        "streamkey",
        "token",
    }

    if isinstance(value, dict):
        output = {}

        for key, item in value.items():

            if str(key).lower() in {
                x.lower() for x in sensitive_keys
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
        return [redact(x) for x in value]

    return value


def first_object(data):

    if isinstance(data, list):
        return data[0] if data else None

    if isinstance(data, dict):

        value = data.get("data")

        if isinstance(value, list):
            return value[0] if value else None

        if isinstance(value, dict):
            return value

        return data

    return None


# ============================================================
# KICK API
# ============================================================

def kick_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def kick_get(path, token, params=None):

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
            "text": "",
        }

    try:
        data = response.json()
    except Exception:
        data = None

    return {
        "ok": response.ok,
        "status": response.status_code,
        "error": None,
        "data": data,
        "text": response.text,
    }


# ============================================================
# KICK STREAM URL
# ============================================================

def normalize_kick_stream_url(url):
    # KICK API already returns the ingest URL.
    # Keep it intact, including :443.
    url = str(url or "").strip().rstrip("/")

    if url:
        return url

    return KICK_STREAM_URL.rstrip("/")


def get_stream_credentials():

    token = session.get("access_token")

    if not token:
        return {
            "ok": False,
            "error": "Not logged in.",
        }

    result = kick_get(
        "/channels",
        token,
    )

    if not result["ok"]:
        return {
            "ok": False,
            "error": "KICK /channels failed.",
            "status": result["status"],
            "response": redact(
                result["data"]
                if result["data"] is not None
                else result["text"]
            ),
        }

    channel = first_object(result["data"])

    if not isinstance(channel, dict):
        return {
            "ok": False,
            "error": "No channel data.",
            "response": redact(result["data"]),
        }

    stream = channel.get("stream")

    if not isinstance(stream, dict):
        stream = {}

    stream_key = str(
        stream.get("key", "") or ""
    ).strip()

    stream_url = str(
        stream.get("url", "") or ""
    ).strip()

    if not stream_key:
        return {
            "ok": False,
            "error": "KICK returned empty stream key.",
            "response": redact(result["data"]),
        }

    stream_url = normalize_kick_stream_url(stream_url)

    target = (
        stream_url.rstrip("/")
        + "/"
        + stream_key
    )

    return {
        "ok": True,
        "stream_key": stream_key,
        "stream_url": stream_url,
        "target": target,
        "slug": channel.get("slug"),
        "broadcaster_user_id": channel.get(
            "broadcaster_user_id"
        ),
    }


# ============================================================
# MISTRAL
# ============================================================

def mistral_generate(user_text):

    if not MISTRAL_API_KEY:
        raise RuntimeError(
            "MISTRAL_API_KEY is missing."
        )

    system_prompt = """
คุณเป็น AI สตรีมเมอร์ภาษาไทย

หน้าที่:
- ตอบคำถามของคนดูแบบเป็นธรรมชาติ
- ตอบสั้น กระชับ เหมาะกับการพูดออกเสียง
- ปกติ 1 ถึง 3 ประโยค
- ใช้ภาษาไทยเป็นหลัก
- ห้ามใช้ Markdown
- ห้ามใช้ bullet
- ห้ามอธิบายระบบเบื้องหลัง
- ถ้ามี Donate ให้ขอบคุณอย่างเป็นธรรมชาติ
- ถ้ามีคำถาม ให้ตอบคำถามก่อน
- อย่าตอบยาวเกินความจำเป็น
"""

    payload = {
        "model": MISTRAL_MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_text,
            },
        ],
        "temperature": 0.8,
        "max_tokens": 180,
    }

    response = requests.post(
        MISTRAL_API_URL,
        headers={
            "Authorization": (
                f"Bearer {MISTRAL_API_KEY}"
            ),
            "Content-Type": "application/json",
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

    choices = data.get("choices", [])

    if not choices:
        raise RuntimeError(
            "Mistral returned no choices."
        )

    message = choices[0].get(
        "message",
        {}
    )

    content = message.get(
        "content",
        ""
    )

    if isinstance(content, list):
        content = " ".join(
            str(x.get("text", ""))
            for x in content
            if isinstance(x, dict)
        )

    answer = str(content or "").strip()

    if not answer:
        raise RuntimeError(
            "Mistral returned empty text."
        )

    return answer


# ============================================================
# TTS
# ============================================================

def download_tts(text):

    response = requests.get(
        TTS_URL,
        params={
            "text": text,
            "voice": TTS_VOICE,
        },
        timeout=90,
    )

    response.raise_for_status()

    content_type = (
        response.headers
        .get("Content-Type", "")
        .lower()
    )

    if not (
        "audio/" in content_type
        or response.content[:3] == b"ID3"
        or response.content[:2] == b"\xff\xfb"
    ):
        raise RuntimeError(
            "TTS endpoint did not return audio. "
            f"Content-Type: {content_type}"
        )

    return response.content


# ============================================================
# AUDIO → PCM
# ============================================================

def audio_to_pcm(audio_bytes):

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

    stdout, stderr = process.communicate(
        audio_bytes
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

FFMPEG_LOG_PATH = "/tmp/ffmpeg.log"
ffmpeg_last_error = ""
ffmpeg_log_lock = threading.Lock()


def _append_ffmpeg_log(text):
    global ffmpeg_last_error

    if not text:
        return

    with ffmpeg_log_lock:
        try:
            with open(
                FFMPEG_LOG_PATH,
                "a",
                encoding="utf-8"
            ) as f:
                f.write(text)
                if not text.endswith("\n"):
                    f.write("\n")
        except Exception:
            pass

        # Keep the most recent error text available to the API.
        ffmpeg_last_error = (
            text[-15000:]
        )


def _drain_ffmpeg_stderr(process):
    # Continuously drain FFmpeg stderr so the pipe cannot fill.
    # At the same time, persist the real FFmpeg error.
    try:
        while True:
            line = process.stderr.readline()

            if not line:
                break

            decoded = line.decode(
                "utf-8",
                errors="replace"
            )

            print(
                "[FFmpeg]",
                decoded.rstrip()
            )

            _append_ffmpeg_log(decoded)

    except Exception as exc:
        _append_ffmpeg_log(
            "FFmpeg stderr reader error: "
            + repr(exc)
        )


def start_ffmpeg_stream():
    global ffmpeg_process

    credentials = get_stream_credentials()

    if not credentials["ok"]:
        raise RuntimeError(
            credentials["error"]
        )

    target = str(
        credentials["target"]
    ).strip()

    if not target.startswith("rtmps://"):
        raise RuntimeError(
            "KICK returned an invalid RTMPS target."
        )

    print(
        "KICK RTMPS:",
        credentials["stream_url"]
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",

        # Stable 1280x720 video source
        "-re",
        "-f", "lavfi",
        "-i",
        "color=c=0x080D0A:s=1280x720:r=30",

        # PCM audio supplied by the Python process
        "-f", "s16le",
        "-ar", "48000",
        "-ac", "2",
        "-i", "pipe:0",

        # Background + waveform
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
            "s=1100x260:"
            "mode=cline:"
            "rate=30:"
            "colors=0x53FF9D:"
            "draw=full"
            "[wave];"

            "[bg][wave]"
            "overlay="
            "(W-w)/2:"
            "(H-h)/2"
            "[outv]"
        ),

        "-map", "[outv]",
        "-map", "1:a",

        # H.264
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-g", "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",
        "-b:v", "4500k",
        "-maxrate", "4500k",
        "-bufsize", "9000k",

        # AAC
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",

        # KICK RTMPS
        "-rtmp_live", "live",
        "-flvflags", "no_duration_filesize",
        "-f", "flv",
        target,
    ]

    global ffmpeg_last_error

    with ffmpeg_log_lock:
        ffmpeg_last_error = ""
        try:
            with open(
                FFMPEG_LOG_PATH,
                "w",
                encoding="utf-8"
            ) as f:
                f.write(
                    "=== FFmpeg session started ===\\n"
                )
                f.write(
                    "Target: "
                    + redact({"target": target})["target"]
                    + "\\n"
                )
        except Exception:
            pass

    print("Starting FFmpeg -> KICK")

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    with ffmpeg_lock:
        ffmpeg_process = process

    threading.Thread(
        target=_drain_ffmpeg_stderr,
        args=(process,),
        daemon=True,
        name="ffmpeg-stderr",
    ).start()

    return process


def write_pcm(pcm):

    with ffmpeg_lock:
        process = ffmpeg_process

    if not process:
        return

    if process.poll() is not None:
        return

    with audio_write_lock:

        try:
            process.stdin.write(pcm)
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

    # 100 ms silence
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
            write_pcm(silence)
        else:
            time.sleep(0.2)


# ============================================================
# EVENT QUEUE
# ============================================================

def enqueue_event(event):
    event_queue.put(event)


def event_worker():

    while True:

        event = event_queue.get()

        try:
            process_event(event)

        except Exception as exc:
            print(
                "AI event error:",
                repr(exc)
            )

        finally:
            event_queue.task_done()


def process_event(event):

    event_type = event.get("type")

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
                f" และส่งข้อความว่า {message}"
            )

        reply = mistral_generate(
            prompt
        )

    else:

        message = str(
            event.get(
                "message",
                ""
            )
        )

        reply = mistral_generate(
            f"ผู้ชมชื่อ {username} "
            f"ถามว่า {message}"
        )

    print("AI:", reply)

    audio = download_tts(reply)

    pcm = audio_to_pcm(audio)

    write_pcm(pcm)


# ============================================================
# GIVEDONATE SSE
# ============================================================

def parse_sse_block(block):

    data_lines = []

    for line in block.splitlines():

        if line.startswith("data:"):
            data_lines.append(
                line[5:].strip()
            )

    if not data_lines:
        return None

    raw = "\n".join(data_lines)

    try:
        return json.loads(raw)
    except Exception:
        return {
            "message": raw
        }


def normalize_donation(event):

    username = (
        event.get("username")
        or event.get("name")
        or event.get("donor")
        or "ผู้สนับสนุน"
    )

    amount = (
        event.get("amount")
        or event.get("price")
        or event.get("value")
        or 0
    )

    message = (
        event.get("message")
        or event.get("note")
        or event.get("comment")
        or ""
    )

    return {
        "type": "donation",
        "username": str(username),
        "amount": str(amount),
        "message": str(message),
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

                    event = parse_sse_block(
                        buffer
                    )

                    buffer = ""

                    if event:

                        enqueue_event(
                            normalize_donation(
                                event
                            )
                        )

                    continue

                buffer += line + "\n"

        except Exception as exc:

            print(
                "GiveDonate SSE error:",
                repr(exc)
            )

            time.sleep(3)


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
        name="ai-worker",
    ).start()

    threading.Thread(
        target=donation_listener,
        daemon=True,
        name="donation-sse",
    ).start()

    threading.Thread(
        target=silence_loop,
        daemon=True,
        name="audio-silence",
    ).start()


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
            "KICK_CLIENT_ID is missing.",
            500,
        )

    state = secrets.token_urlsafe(32)

    verifier = secrets.token_urlsafe(64)

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
        "scope": " ".join(KICK_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
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

    error = request.args.get("error")

    if error:

        description = request.args.get(
            "error_description",
            ""
        )

        return (
            f"""
            <!doctype html>
            <html lang="th">
            <head>
                <meta charset="utf-8">
                <title>KICK OAuth Error</title>
            </head>
            <body>
                <h2>KICK Login Error</h2>
                <p><b>Error:</b> {error}</p>
                <p>{description}</p>
                <a href="/login">Login ใหม่</a>
            </body>
            </html>
            """,
            400,
        )

    code = request.args.get("code")
    state = request.args.get("state")

    saved_state = session.get(
        "oauth_state"
    )

    verifier = session.get(
        "oauth_verifier"
    )

    if not code:
        return (
            "ไม่มี authorization code จาก KICK.",
            400,
        )

    if not state or not saved_state:
        return (
            "OAuth session หาย กรุณา Login ใหม่.",
            400,
        )

    if not secrets.compare_digest(
        state,
        saved_state
    ):
        return (
            "OAuth state ไม่ตรงกัน.",
            400,
        )

    if not verifier:
        return (
            "PKCE verifier หาย กรุณา Login ใหม่.",
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
            f"Token exchange failed: {exc}",
            502,
        )

    if not response.ok:

        return (
            f"""
            <h2>KICK Token Error</h2>
            <p>HTTP {response.status_code}</p>
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
            "KICK ไม่ส่ง Access Token.",
            400,
        )

    session.clear()

    session["authenticated"] = True
    session["access_token"] = access_token
    session["scope"] = data.get(
        "scope",
        ""
    )

    if data.get("refresh_token"):
        session["refresh_token"] = data.get(
            "refresh_token"
        )

    user_result = kick_get(
        "/users",
        access_token
    )

    if user_result["ok"]:

        user = first_object(
            user_result["data"]
        )

        if user:
            session["user"] = user

    start_background_workers()

    return redirect("/dashboard")


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
def dashboard():

    if not session.get(
        "authenticated"
    ):
        return redirect("/login")

    return render_template(
        "dashboard.html",
        user=session.get("user"),
    )


# ============================================================
# START LIVE
# ============================================================

@app.route(
    "/api/start",
    methods=["POST"]
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
            and ffmpeg_process.poll() is None
        ):
            return jsonify({
                "ok": False,
                "error":
                    "Live กำลังทำงานอยู่แล้ว"
            }), 409

    try:

        process = start_ffmpeg_stream()

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500

    start_background_workers()

    time.sleep(8)

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
    methods=["POST"]
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

@app.route("/api/status")
def api_status():

    running = False

    with ffmpeg_lock:

        if (
            ffmpeg_process
            and ffmpeg_process.poll() is None
        ):
            running = True

    return jsonify({
        "ok": True,
        "live": running,
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
            bool(TTS_URL),
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
    methods=["POST"]
)
def test_mistral():

    try:

        body = request.get_json(
            silent=True
        ) or {}

        text = str(
            body.get(
                "text",
                "พูดทักทายคนดูในไลฟ์แบบสั้น ๆ"
            )
        )

        answer = mistral_generate(text)

        return jsonify({
            "ok": True,
            "answer": answer,
            "model": MISTRAL_MODEL,
        })

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


# ============================================================
# TEST TTS
# ============================================================

@app.route("/api/test/tts")
def test_tts():

    text = request.args.get(
        "text",
        "สวัสดีครับ",
    )

    try:

        audio = download_tts(text)

        return jsonify({
            "ok": True,
            "bytes": len(audio),
            "content_type": "audio/mpeg",
        })

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


# ============================================================
# DEBUG STREAM
# ============================================================

@app.route(
    "/api/debug/ffmpeg"
)
def debug_ffmpeg():

    with ffmpeg_lock:
        process = ffmpeg_process

    returncode = (
        process.poll()
        if process
        else None
    )

    try:
        with open(
            FFMPEG_LOG_PATH,
            "r",
            encoding="utf-8",
            errors="replace"
        ) as f:
            log = f.read()

    except Exception:
        log = ""

    return jsonify({
        "ok": True,

        "running": bool(
            process
            and process.poll() is None
        ),

        "returncode":
            returncode,

        "last_error":
            ffmpeg_last_error,

        "log":
            log[-15000:],
    })


@app.route(
    "/api/debug/ffmpeg-log"
)
def debug_ffmpeg_log():

    try:
        with open(
            FFMPEG_LOG_PATH,
            "r",
            encoding="utf-8",
            errors="replace"
        ) as f:
            log = f.read()

        return jsonify({
            "ok": True,
            "log":
                log[-30000:]
        })

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error":
                str(exc)
        })


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
                "Not logged in.",
        }), 401

    return jsonify(
        redact(
            get_stream_credentials()
        )
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "ok": True,
        "service":
            "kick-mistral-live",
    })


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
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

    return redirect("/")


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
