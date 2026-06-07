"""Pulse AI — Vercel serverless API."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request

from flask import Flask, jsonify, make_response, request, send_from_directory

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = Flask(__name__)

_PIN         = os.environ.get("APP_PIN", "")
_SECRET      = os.environ.get("APP_SECRET", "change-me").encode()
_CRON_SECRET = os.environ.get("CRON_SECRET", "")
_KV_URL      = os.environ.get("KV_REST_API_URL", "")
_KV_TOKEN    = os.environ.get("KV_REST_API_TOKEN", "")
_VAPID_PUB   = os.environ.get("VAPID_PUBLIC_KEY", "")
_VAPID_PRIV  = os.environ.get("VAPID_PRIVATE_KEY", "")
_VAPID_SUB   = os.environ.get("VAPID_SUBJECT", "mailto:pulse@local")


# ---------------------------------------------------------------------------
# Upstash / Vercel KV  (REST pipeline, no extra deps)
# ---------------------------------------------------------------------------

def _kv(*cmd) -> object:
    body = json.dumps([list(cmd)]).encode()
    req = urllib.request.Request(
        f"{_KV_URL}/pipeline",
        data=body,
        headers={
            "Authorization": f"Bearer {_KV_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())[0]["result"]


def kv_get(key: str) -> str | None:
    return _kv("GET", key)


def kv_set(key: str, value: str) -> None:
    _kv("SET", key, value)


# ---------------------------------------------------------------------------
# Minimal JWT (HS256, no external deps)
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_token() -> str:
    hdr = _b64url(b'{"alg":"HS256","typ":"JWT"}')
    pay = _b64url(json.dumps({"exp": int(time.time()) + 86400 * 30}).encode())
    sig_in = f"{hdr}.{pay}".encode()
    sig = _b64url(hmac.new(_SECRET, sig_in, hashlib.sha256).digest())
    return f"{hdr}.{pay}.{sig}"


def _verify_token(token: str) -> bool:
    try:
        h, p, s = token.split(".")
        sig_in = f"{h}.{p}".encode()
        expected = _b64url(hmac.new(_SECRET, sig_in, hashlib.sha256).digest())
        if not hmac.compare_digest(s, expected):
            return False
        pad = 4 - len(p) % 4
        data = json.loads(base64.urlsafe_b64decode(p + "=" * (pad % 4)))
        return data.get("exp", 0) > int(time.time())
    except Exception:
        return False


def _authenticated() -> bool:
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return bool(token) and _verify_token(token)


# ---------------------------------------------------------------------------
# Web Push
# ---------------------------------------------------------------------------

def _send_push(title: str, body_text: str) -> None:
    raw = kv_get("pulse_sub")
    if not raw:
        return
    sub = json.loads(raw)
    if not sub:
        return
    try:
        from pywebpush import WebPushException, webpush
        webpush(
            subscription_info=sub,
            data=json.dumps({"title": title, "body": body_text}),
            vapid_private_key=_VAPID_PRIV,
            vapid_claims={"sub": _VAPID_SUB},
        )
    except Exception as exc:
        app.logger.warning("push failed: %s", exc)
        try:
            from pywebpush import WebPushException
            if isinstance(exc, WebPushException) and exc.response and exc.response.status_code in (404, 410):
                kv_set("pulse_sub", "")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CORS helper
# ---------------------------------------------------------------------------

@app.after_request
def _add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Cron-Secret"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/config")
def config():
    return jsonify({"vapid_public_key": _VAPID_PUB})


@app.route("/api/debug")
def debug():
    stripped = _PIN.strip()
    return jsonify({
        "pin_set": bool(_PIN),
        "pin_len": len(stripped),
        "pin_is_2137": stripped == "2137",
        "kv_set": bool(_KV_URL),
    })


@app.route("/api/login", methods=["POST", "OPTIONS"])
def login():
    if request.method == "OPTIONS":
        return "", 204
    body = request.get_json(silent=True) or {}
    if str(body.get("pin", "")).strip() == str(_PIN).strip():
        return jsonify({"token": _make_token()})
    return jsonify({"error": "Nieprawidłowy PIN"}), 401


@app.route("/api/messages", methods=["GET", "OPTIONS"])
def messages():
    if request.method == "OPTIONS":
        return "", 204
    if not _authenticated():
        return jsonify({"error": "unauthorized"}), 401
    raw = kv_get("pulse_messages") or "[]"
    return jsonify(json.loads(raw))


@app.route("/api/subscribe", methods=["POST", "OPTIONS"])
def subscribe():
    if request.method == "OPTIONS":
        return "", 204
    if not _authenticated():
        return jsonify({"error": "unauthorized"}), 401
    sub = request.get_json(silent=True)
    if not sub:
        return jsonify({"error": "bad body"}), 400
    kv_set("pulse_sub", json.dumps(sub))
    return jsonify({"ok": True})


@app.route("/api/receive", methods=["POST"])
def receive():
    secret = request.headers.get("X-Cron-Secret", "")
    if not hmac.compare_digest(secret.encode(), _CRON_SECRET.encode()):
        return jsonify({"error": "forbidden"}), 403

    body = request.get_json(silent=True) or {}
    msg = {
        "id":      _b64url(os.urandom(6)),
        "type":    str(body.get("type",    "general")),
        "ts":      body.get("ts",          int(time.time())),
        "title":   str(body.get("title",   "Pulse")),
        "content": str(body.get("content", "")),
    }

    raw  = kv_get("pulse_messages") or "[]"
    msgs = json.loads(raw)
    msgs.insert(0, msg)
    kv_set("pulse_messages", json.dumps(msgs[:50]))

    preview = msg["content"]
    if len(preview) > 150:
        preview = preview[:150] + "…"
    _send_push(msg["title"], preview)

    return jsonify({"ok": True, "id": msg["id"]})


# ---------------------------------------------------------------------------
# Static files (bundled inside api/static/)
# ---------------------------------------------------------------------------

@app.route("/sw.js")
def _sw():
    resp = make_response(send_from_directory(_STATIC, "sw.js"))
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/manifest.json")
def _manifest():
    return send_from_directory(_STATIC, "manifest.json")


@app.route("/icons/<path:filename>")
def _icons(filename):
    return send_from_directory(os.path.join(_STATIC, "icons"), filename)


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def _catch_all(path):
    if request.path.startswith("/api/"):
        return jsonify({"bug": "catch_all_hit", "request_path": request.path, "route_var": path}), 404
    return send_from_directory(_STATIC, "index.html")

