"""Pulse AI — Vercel serverless API."""
from __future__ import annotations

import base64
import hmac
import json
import os
import time
import urllib.request

from flask import Flask, jsonify, request

app = Flask(__name__)

_CRON_SECRET = os.environ.get("CRON_SECRET", "")
_KV_URL      = os.environ.get("KV_REST_API_URL", "")
_KV_TOKEN    = os.environ.get("KV_REST_API_TOKEN", "")
_VAPID_PUB   = os.environ.get("VAPID_PUBLIC_KEY", "")
_VAPID_PRIV  = os.environ.get("VAPID_PRIVATE_KEY", "")
_VAPID_SUB   = os.environ.get("VAPID_SUBJECT", "mailto:pulse@local")


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


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


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


@app.after_request
def _add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Cron-Secret"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/api/config")
def config():
    return jsonify({"vapid_public_key": _VAPID_PUB})


@app.route("/api/debug")
def debug():
    return jsonify({"ok": True, "kv_set": bool(_KV_URL), "path": request.path})


@app.route("/api/messages", methods=["GET", "OPTIONS"])
def messages():
    if request.method == "OPTIONS":
        return "", 204
    raw = kv_get("pulse_messages") or "[]"
    return jsonify(json.loads(raw))


@app.route("/api/subscribe", methods=["POST", "OPTIONS"])
def subscribe():
    if request.method == "OPTIONS":
        return "", 204
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
