import base64
import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('APP_SECRET', secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true',
)

KICK_AUTH = 'https://id.kick.com/oauth/authorize'
KICK_TOKEN = 'https://id.kick.com/oauth/token'
KICK_API = 'https://api.kick.com/public/v1'
DEFAULT_RTMPS = os.environ.get(
    'KICK_RTMPS_URL',
    'rtmps://fa723fc1b171.global-contribute.live-video.net:443/app'
)
FFMPEG = os.environ.get('FFMPEG_BIN', 'ffmpeg')

# Only request permissions needed by the current phase.
SCOPES = 'user:read channel:read streamkey:read'

_stream_lock = threading.Lock()
_stream_proc = None
_stream_meta = {
    'status': 'offline',
    'started_at': None,
    'last_error': None,
}
_stream_log_thread = None


def env(name, default=None):
    return os.environ.get(name, default)


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode('utf-8')).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return verifier, challenge


def kick_token_request(data):
    response = requests.post(KICK_TOKEN, data=data, timeout=30)
    response.raise_for_status()
    return response.json()


def access_token():
    return session.get('access_token')


def auth_headers():
    token = access_token()
    if not token:
        return {}
    return {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
    }


def refresh_access_token():
    refresh = session.get('refresh_token')
    client_id = env('KICK_CLIENT_ID')
    client_secret = env('KICK_CLIENT_SECRET')
    if not refresh or not client_id:
        return False

    data = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh,
        'client_id': client_id,
    }
    if client_secret:
        data['client_secret'] = client_secret

    try:
        tokens = kick_token_request(data)
    except requests.RequestException:
        return False

    session['access_token'] = tokens['access_token']
    if tokens.get('refresh_token'):
        session['refresh_token'] = tokens['refresh_token']
    if tokens.get('expires_in'):
        session['expires_at'] = int(time.time()) + int(tokens['expires_in'])
    return True


def kick_request(method, path, **kwargs):
    if not access_token():
        raise RuntimeError('Not authenticated')

    headers = kwargs.pop('headers', {})
    merged = {**auth_headers(), **headers}
    response = requests.request(
        method,
        f'{KICK_API}{path}',
        headers=merged,
        timeout=30,
        **kwargs,
    )

    if response.status_code == 401 and refresh_access_token():
        merged = {**auth_headers(), **headers}
        response = requests.request(
            method,
            f'{KICK_API}{path}',
            headers=merged,
            timeout=30,
            **kwargs,
        )

    response.raise_for_status()
    if not response.content:
        return None
    return response.json()


def extract_data(payload):
    if isinstance(payload, dict) and 'data' in payload:
        return payload['data']
    return payload


def current_user():
    return extract_data(kick_request('GET', '/users'))


def current_channel():
    return extract_data(kick_request('GET', '/channels'))


def extract_stream_credentials(payload):
    """
    KICK's documented permission is streamkey:read and it returns a stream URL + key.
    The exact envelope is normalized defensively because API response shapes can evolve.
    """
    data = extract_data(payload)

    candidates = []
    if isinstance(data, dict):
        candidates.append(data)
        for key in ('stream', 'stream_key', 'streamkey', 'credentials'):
            value = data.get(key)
            if isinstance(value, dict):
                candidates.append(value)
    elif isinstance(data, list):
        candidates.extend(x for x in data if isinstance(x, dict))

    stream_url = None
    stream_key = None
    for obj in candidates:
        stream_url = stream_url or obj.get('stream_url') or obj.get('url') or obj.get('streamUrl')
        stream_key = stream_key or obj.get('stream_key') or obj.get('streamKey') or obj.get('key')

    return {
        'stream_url': stream_url,
        'stream_key': stream_key,
        'raw': payload,
    }


def get_stream_credentials():
    """Try documented stream credential operation and normalize the result.

    The public Swagger exposes the streamkey:read permission but the endpoint naming
    has changed across documentation revisions. We keep the endpoint isolated here so
    it is easy to update without changing the rest of the app.
    """
    paths = [
        '/channels/stream-key',
        '/stream-key',
        '/stream/key',
    ]
    last = None
    for path in paths:
        try:
            payload = kick_request('GET', path)
            result = extract_stream_credentials(payload)
            if result['stream_key']:
                if not result['stream_url']:
                    result['stream_url'] = DEFAULT_RTMPS
                return result
            last = {'path': path, 'payload': payload}
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 405):
                continue
            raise
    raise RuntimeError(
        'The KICK API did not return a stream key. '
        'Check that streamkey:read is enabled for your KICK Developer App and authorized by the account.'
    ) from (None if last is None else Exception(str(last)))


def make_rtmps_target(stream_url, stream_key):
    base = (stream_url or DEFAULT_RTMPS).rstrip('/')
    return f'{base}/{stream_key}'


def build_test_ffmpeg(target):
    # Generated video/audio are only a verification source for Phase 1.
    # They prove the full path to KICK works before we attach AI/Avatar inputs.
    return [
        FFMPEG,
        '-hide_banner',
        '-loglevel', 'warning',
        '-re',
        '-f', 'lavfi',
        '-i', 'testsrc2=size=1280x720:rate=30',
        '-f', 'lavfi',
        '-i', 'sine=frequency=440:sample_rate=48000',
        '-c:v', 'libx264',
        '-preset', env('X264_PRESET', 'veryfast'),
        '-tune', 'zerolatency',
        '-pix_fmt', 'yuv420p',
        '-r', '30',
        '-b:v', '4500k',
        '-maxrate', '4500k',
        '-bufsize', '9000k',
        '-g', '60',
        '-keyint_min', '60',
        '-sc_threshold', '0',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-ar', '48000',
        '-ac', '2',
        '-f', 'flv',
        target,
    ]


def stop_stream():
    global _stream_proc, _stream_meta
    with _stream_lock:
        proc = _stream_proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        _stream_proc = None
        _stream_meta = {
            'status': 'offline',
            'started_at': None,
            'last_error': None,
        }


def _drain_stderr(proc):
    global _stream_meta
    try:
        for line in proc.stderr:
            line = line.strip()
            if not line:
                continue
            if 'error' in line.lower() or 'failed' in line.lower():
                with _stream_lock:
                    _stream_meta['last_error'] = line[-1000:]
    finally:
        code = proc.wait()
        with _stream_lock:
            if _stream_proc is proc:
                _stream_meta['status'] = 'ended' if code == 0 else 'error'
                if code != 0 and not _stream_meta.get('last_error'):
                    _stream_meta['last_error'] = f'FFmpeg exited with code {code}'


def start_stream(target):
    global _stream_proc, _stream_meta, _stream_log_thread
    stop_stream()
    command = build_test_ffmpeg(target)
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f'FFmpeg was not found: {FFMPEG}. Install FFmpeg or use the Dockerfile included in this project.'
        ) from exc

    with _stream_lock:
        _stream_proc = proc
        _stream_meta = {
            'status': 'starting',
            'started_at': int(time.time()),
            'last_error': None,
        }

    _stream_log_thread = threading.Thread(target=_drain_stderr, args=(proc,), daemon=True)
    _stream_log_thread.start()


def is_stream_alive():
    with _stream_lock:
        return bool(_stream_proc and _stream_proc.poll() is None)


@app.get('/')
def index():
    return render_template(
        'index.html',
        connected=bool(access_token()),
        user=session.get('user'),
        channel=session.get('channel'),
    )


@app.get('/login')
def login():
    client_id = env('KICK_CLIENT_ID')
    redirect_uri = env('KICK_REDIRECT_URI') or url_for('callback', _external=True)
    if not client_id:
        return 'Missing KICK_CLIENT_ID environment variable.', 500

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    session['pkce_verifier'] = verifier

    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': SCOPES,
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }
    return redirect(f'{KICK_AUTH}?{urlencode(params)}')


@app.get('/callback')
def callback():
    state = request.args.get('state')
    if not state or state != session.pop('oauth_state', None):
        return 'OAuth state mismatch.', 400

    code = request.args.get('code')
    if not code:
        error = request.args.get('error', 'unknown')
        return f'KICK authorization failed: {error}', 400

    verifier = session.pop('pkce_verifier', None)
    if not verifier:
        return 'Missing PKCE verifier.', 400

    payload = {
        'grant_type': 'authorization_code',
        'client_id': env('KICK_CLIENT_ID'),
        'redirect_uri': env('KICK_REDIRECT_URI') or url_for('callback', _external=True),
        'code': code,
        'code_verifier': verifier,
    }
    if env('KICK_CLIENT_SECRET'):
        payload['client_secret'] = env('KICK_CLIENT_SECRET')

    try:
        tokens = kick_token_request(payload)
    except requests.HTTPError as exc:
        body = exc.response.text[:1500] if exc.response is not None else str(exc)
        return f'Token exchange failed: {body}', 502

    session['access_token'] = tokens['access_token']
    session['refresh_token'] = tokens.get('refresh_token')
    if tokens.get('expires_in'):
        session['expires_at'] = int(time.time()) + int(tokens['expires_in'])

    try:
        session['user'] = extract_data(current_user())
        session['channel'] = extract_data(current_channel())
    except Exception:
        session['user'] = None
        session['channel'] = None

    return redirect(url_for('index'))


@app.get('/logout')
def logout():
    stop_stream()
    session.clear()
    return redirect(url_for('index'))


@app.get('/api/me')
def api_me():
    if not access_token():
        return jsonify({'connected': False}), 200
    try:
        user = extract_data(current_user())
        channel = extract_data(current_channel())
        session['user'] = user
        session['channel'] = channel
        return jsonify({'connected': True, 'user': user, 'channel': channel})
    except Exception as exc:
        return jsonify({'connected': True, 'error': str(exc)}), 502


@app.get('/api/stream-credentials')
def api_stream_credentials():
    if not access_token():
        return jsonify({'error': 'not_connected'}), 401
    try:
        creds = get_stream_credentials()
        # Never log or return the raw credential object.
        return jsonify({
            'ok': True,
            'stream_url': creds['stream_url'],
            'has_stream_key': bool(creds['stream_key']),
        })
    except Exception as exc:
        return jsonify({'error': 'stream_key_unavailable', 'message': str(exc)}), 502


@app.post('/api/live/start')
def api_live_start():
    if not access_token():
        return jsonify({'error': 'not_connected'}), 401

    try:
        creds = get_stream_credentials()
        target = make_rtmps_target(creds['stream_url'], creds['stream_key'])
        start_stream(target)
        return jsonify({'ok': True, 'status': 'starting'})
    except Exception as exc:
        return jsonify({'error': 'start_failed', 'message': str(exc)}), 502


@app.post('/api/live/stop')
def api_live_stop():
    stop_stream()
    return jsonify({'ok': True, 'status': 'offline'})


@app.get('/api/live/status')
def api_live_status():
    alive = is_stream_alive()
    with _stream_lock:
        snapshot = dict(_stream_meta)
    snapshot['process_alive'] = alive
    snapshot['connected'] = bool(access_token())
    return jsonify(snapshot)


@app.get('/healthz')
def healthz():
    return jsonify({'ok': True})


@app.before_request
def add_no_store_for_credentials():
    # Prevent browsers/proxies from caching sensitive-control responses.
    pass


@app.after_request
def security_headers(response):
    response.headers.setdefault('Cache-Control', 'no-store')
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    return response


if __name__ == '__main__':
    host = env('HOST', '0.0.0.0')
    port = int(env('PORT', '10000'))
    app.run(host=host, port=port, debug=False)
