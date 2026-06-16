#!/usr/bin/env python3
"""
mistral_toolid_proxy
====================

A tiny, transparent reverse proxy that sits in front of ONE OpenAI-compatible
Mistral endpoint (e.g. vLLM with --tokenizer_mode mistral) and rewrites incoming
tool-call IDs so they satisfy Mistral's contract: ^[a-zA-Z0-9]{9}$ (exactly nine
alphanumeric characters, no underscores/dashes/prefixes).

Why this exists
---------------
Clients like GitHub Copilot, Zed, opencode, etc. mint OpenAI-style tool-call IDs
(`call_...`, `toolu_...`, timestamp-based, mixed length). mistral-common rejects
anything that isn't 9 alphanumerics, so multi-turn tool calls intermittently 400.
This proxy translates IDs at the contract boundary — it makes non-compliant
clients comply, rather than asking anyone to loosen a contract that exists for a
real reason (the model was trained on that ID shape).

Design
------
* Dedicated to a single upstream (routing by topology, not by inspecting the
  model name). Deploy one instance per Mistral endpoint; nothing else routes
  through it, so non-Mistral workloads pay no overhead.
* Only POST /v1/chat/completions bodies are touched. Every other path
  (/v1/models, /health, /v1/completions, ...) is forwarded verbatim.
* Rewrite is REQUEST-path only. vLLM already emits compliant 9-char IDs, so
  responses stream straight through untouched (SSE-safe).
* Every ID is normalized unconditionally — no "is this already valid?" branch.
  The transform is deterministic, so the assistant's tool_calls[].id and the
  matching tool message's tool_call_id (same source string) map to the same
  output and stay paired within the request. No state required.

Run
---
    pip install starlette httpx uvicorn

    # In front of a local vLLM (client carries its own auth, if any):
    UPSTREAM=http://127.0.0.1:8001 PORT=8081 python mistral_toolid_proxy.py

    # In front of a public Mistral-shaped API. Auth headers pass through, so the
    # client can hold the key; or set API_KEY to keep the client keyless and let
    # the proxy inject it. HTTPS upstreams work out of the box.
    UPSTREAM=https://api.mistral.ai API_KEY=$MISTRAL_API_KEY python mistral_toolid_proxy.py

Then point your client's base URL at http://<host>:8081/v1 instead of the model.
"""

import os
import json
import string
import hashlib

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from starlette.routing import Route

UPSTREAM = os.environ.get("UPSTREAM", "http://127.0.0.1:8001").rstrip("/")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8081"))
# Optional. If set, the proxy injects `Authorization: Bearer <API_KEY>` toward
# the upstream, so the local client can stay keyless. If unset (default), the
# client's own auth headers pass through untouched.
API_KEY = os.environ.get("API_KEY")

# Hop-by-hop headers (RFC 7230) plus host/content-length, which must not be
# blindly forwarded. content-length is dropped because we may resize the body;
# httpx recomputes it from the content we pass. NOTE: end-to-end auth headers
# (authorization, x-api-key, api-key) are deliberately absent here so they pass
# straight through to the upstream — required when fronting a public, keyed
# Mistral-shaped API. proxy-authorization IS stripped: that one is hop-by-hop.
_STRIP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

_B62 = string.digits + string.ascii_uppercase + string.ascii_lowercase  # 62 symbols


def mistralize(tid: str) -> str:
    """Deterministically map any string to exactly 9 base62 chars (a-zA-Z0-9).

    base62 uses the full allowed alphabet (62^9 ~= 2^53), so birthday collisions
    are negligible at conversation scale. Deterministic => paired IDs stay paired.
    """
    h = int.from_bytes(hashlib.blake2s(tid.encode("utf-8"), digest_size=8).digest(), "big")
    out = ""
    while h:
        h, r = divmod(h, 62)
        out = _B62[r] + out
    return out.rjust(9, _B62[0])[:9]


def normalize_chat_body(raw: bytes) -> bytes:
    """Rewrite every tool_call_id / tool_calls[].id in a chat-completions body."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return raw  # not JSON we understand; forward untouched
    if not isinstance(payload, dict):
        return raw
    for msg in payload.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        if isinstance(msg.get("tool_call_id"), str):
            msg["tool_call_id"] = mistralize(msg["tool_call_id"])
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict) and isinstance(tc.get("id"), str):
                tc["id"] = mistralize(tc["id"])
    return json.dumps(payload).encode("utf-8")


client = httpx.AsyncClient(timeout=httpx.Timeout(None))  # long generations


async def proxy(request):
    body = await request.body()
    if request.method == "POST" and request.url.path == "/v1/chat/completions":
        body = normalize_chat_body(body)

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP}
    if API_KEY:  # keep the local client keyless; the real key lives in proxy env
        headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
        headers["authorization"] = f"Bearer {API_KEY}"
    upstream_req = client.build_request(
        request.method,
        UPSTREAM + request.url.path,
        params=request.query_params,
        headers=headers,
        content=body,
    )
    resp = await client.send(upstream_req, stream=True)

    # Preserve content-type/content-encoding; we forward raw (undecoded) bytes.
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _STRIP}
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers=resp_headers,
        background=BackgroundTask(resp.aclose),
    )


app = Starlette(
    routes=[Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])],
    on_shutdown=[client.aclose],
)


if __name__ == "__main__":
    print(f"mistral_toolid_proxy: :{PORT} -> {UPSTREAM}  (normalizing /v1/chat/completions tool IDs)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
