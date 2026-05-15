"""
mymockllm — Manual LLM mock server
Run: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir app
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

log = logging.getLogger("mymockllm")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Project root (parent of the `app/` package). We keep `.histories/` at the
# repo root so it survives any further restructuring of the source tree.
BASE_DIR = Path(__file__).resolve().parent.parent
HISTORIES_DIR = BASE_DIR / ".histories"
ACTIVE_FILE = HISTORIES_DIR / ".active"

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# pending requests waiting for human reply: req_id -> {"id", "body", "future", "session_id", "protocol"}
# protocol is one of "openai" | "anthropic"
_pending: dict[str, dict] = {}

# SSE subscribers
_sse_queues: list[asyncio.Queue] = []

# session index: session_id -> session dict (without asyncio.Future)
_sessions: dict[str, dict] = {}

# current active session id (most recent)
_active_session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

def _session_file(session_id: str) -> Path:
    return HISTORIES_DIR / f"{session_id}.json"


def _save_session(session: dict):
    """Persist a session to disk (without the asyncio future)."""
    data = {k: v for k, v in session.items() if k != "future"}
    _session_file(session["session_id"]).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _save_active(session_id: str):
    ACTIVE_FILE.write_text(session_id, encoding="utf-8")


def _abandon_pending(req_id: str, session: dict, reason: str = "client_disconnected"):
    """Mark a request as abandoned: drop it from _pending and flip the
    session out of "pending" if nothing else has finalised it yet."""
    _pending.pop(req_id, None)
    if session.get("status") == "pending":
        session["reply"] = {"abandoned": True, "reason": reason}
        session["status"] = "error"
        try:
            _save_session(session)
        except Exception:
            pass
        try:
            _broadcast("session_updated", {
                "session_id": session["session_id"],
                "status": "error",
                "preview": _session_preview(session),
                "created_at": session["created_at"],
            })
        except Exception:
            pass




def _load_histories():
    """Load all session files from disk into _sessions on startup."""
    global _active_session_id
    HISTORIES_DIR.mkdir(exist_ok=True)
    for f in HISTORIES_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            # Any session left in "pending" on disk is a ghost from a previous
            # run (the server died / the client disconnected before we got a
            # reply). Flip it to "error" so the UI doesn't keep showing it as
            # in-flight forever.
            if data.get("status") == "pending":
                data["status"] = "error"
                data["reply"] = {"abandoned": True, "reason": "server_restart"}
                try:
                    f.write_text(
                        json.dumps(data, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception as e:
                    log.warning("Could not rewrite stale pending session %s: %s", f.name, e)
            _sessions[data["session_id"]] = data
        except Exception as e:
            log.warning("Skipping invalid history file %s: %s", f.name, e)
    if ACTIVE_FILE.exists():
        sid = ACTIVE_FILE.read_text(encoding="utf-8").strip()
        if sid in _sessions:
            _active_session_id = sid


def _session_preview(session: dict) -> str:
    """Return a short preview string from the first non-system user message."""
    for msg in session.get("messages", []):
        if msg.get("role") == "user":
            content = msg.get("content") or ""
            if isinstance(content, list):
                # content blocks
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            return content[:60]
    return "(no user message)"


# Load on startup
_load_histories()


# ---------------------------------------------------------------------------
# SSE broadcast
# ---------------------------------------------------------------------------

def _broadcast(event: str, data: dict):
    for q in _sse_queues:
        q.put_nowait({"event": event, "data": json.dumps(data)})


def _rebroadcast_next_pending(exclude_req_id: Optional[str] = None):
    """If other pending requests remain, push the oldest one back to the frontend
    so the user can continue replying to it.
    """
    global _active_session_id
    candidates = [
        item for rid, item in _pending.items() if rid != exclude_req_id
    ]
    if not candidates:
        return
    # Pick the oldest pending (insertion order in dict)
    nxt = candidates[0]
    _active_session_id = nxt["session_id"]
    _save_active(nxt["session_id"])
    _broadcast("new_request", {
        "id": nxt["id"],
        "body": nxt["body"],
        "session_id": nxt["session_id"],
    })


# ---------------------------------------------------------------------------
# SWE-agent endpoint
# ---------------------------------------------------------------------------

@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    global _active_session_id

    body = await request.json()
    req_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())

    # Create session record
    session = {
        "session_id": session_id,
        "req_id": req_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": body.get("model", "mock-model"),
        "messages": body.get("messages", []),
        "tools": body.get("tools", []),
        "reply": None,
        "status": "pending",
        "protocol": "openai",
        "raw_request": body,
    }
    _sessions[session_id] = session
    _active_session_id = session_id
    _save_active(session_id)
    # Save pending state immediately so a crash doesn't lose the request
    _save_session(session)

    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    _pending[req_id] = {"id": req_id, "body": body, "future": future, "session_id": session_id}

    # Notify browser
    _broadcast("new_request", {"id": req_id, "body": body, "session_id": session_id})

    # Wait for human reply (may be a normal reply, or an injected error)
    try:
        result = await future
    except asyncio.CancelledError:
        _abandon_pending(req_id, session, reason="client_disconnected")
        raise
    except BaseException:
        _abandon_pending(req_id, session, reason="server_error")
        raise
    finally:
        _pending.pop(req_id, None)

    # ── Error injection branch ──────────────────────────────────────────
    if isinstance(result, dict) and result.get("__error__"):
        err = result["__error__"]
        status_code = int(err.get("status", 500))
        error_body = {
            "error": {
                "message": err.get("message", "Internal server error"),
                "type": err.get("type", "server_error"),
                "param": err.get("param"),
                "code": err.get("code"),
            }
        }
        session["reply"] = {"injected_error": err}
        session["status"] = "error"
        _save_session(session)
        _broadcast("session_updated", {
            "session_id": session_id,
            "status": "error",
            "preview": _session_preview(session),
            "created_at": session["created_at"],
        })
        headers = err.get("headers") or {}
        return JSONResponse(content=error_body, status_code=status_code, headers=headers)

    reply = result
    # Persist completed session
    session["reply"] = reply
    session["status"] = "complete"
    _save_session(session)

    # Notify browser that history list changed
    _broadcast("session_updated", {
        "session_id": session_id,
        "status": "complete",
        "preview": _session_preview(session),
        "created_at": session["created_at"],
    })

    # Build OpenAI-compatible response
    if reply.get("tool_calls"):
        message = {
            "role": "assistant",
            "content": reply.get("content") or None,
            "tool_calls": [
                {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]),
                    },
                }
                for tc in reply["tool_calls"]
            ],
        }
        finish_reason = "tool_calls"
    else:
        message = {
            "role": "assistant",
            "content": reply.get("content", ""),
        }
        finish_reason = "stop"

    return JSONResponse(content={
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "mock-model"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


# ---------------------------------------------------------------------------
# Anthropic <-> internal (OpenAI-style) format helpers
# ---------------------------------------------------------------------------
#
# The browser UI in index.html only knows how to render the OpenAI-style
# `messages` array (roles: system / user / assistant[+tool_calls] / tool).
# To keep the UI completely untouched, we normalise every Anthropic request
# into that shape BEFORE storing it into _sessions. When we serialise the
# human reply back over the wire we then re-encode it in Anthropic shape.


def _anthropic_text(content) -> str:
    """Flatten an Anthropic content field (str | list[block]) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                out.append(block.get("text", ""))
            elif btype == "tool_use":
                # Render as readable hint; the real tool-call rendering
                # happens via the dedicated assistant branch below.
                name = block.get("name", "?")
                args = block.get("input", {})
                out.append(f"[tool_use {name} {json.dumps(args, ensure_ascii=False)}]")
            elif btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    out.append(_anthropic_text(inner))
                elif isinstance(inner, str):
                    out.append(inner)
            elif btype == "image":
                out.append("[image]")
        return "\n".join(s for s in out if s)
    return str(content)


def _anthropic_to_openai_messages(system, messages) -> list[dict]:
    """Convert an Anthropic-style request into our internal OpenAI-style list.

    `system` may be missing, a string, or a list of text blocks.
    `messages` is a list with roles user/assistant whose `content` may be a
    plain string or an array of typed content blocks.
    """
    out: list[dict] = []

    # System prompt -> a single system message (string or joined blocks).
    if system:
        if isinstance(system, list):
            sys_text = "\n".join(
                b.get("text", "") for b in system if isinstance(b, dict)
            )
        else:
            sys_text = str(system)
        if sys_text:
            out.append({"role": "system", "content": sys_text})

    for m in messages or []:
        role = m.get("role")
        content = m.get("content")

        if role == "user":
            # user content can carry tool_result blocks (== OpenAI "tool" role)
            if isinstance(content, list):
                # split into a possibly-pure-text user message PLUS one
                # synthetic tool message per tool_result block
                text_parts = []
                tool_msgs = []
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        tool_msgs.append({
                            "role": "tool",
                            "tool_call_id": b.get("tool_use_id", ""),
                            "name": b.get("tool_use_id", "tool"),
                            "content": _anthropic_text(b.get("content")),
                        })
                    elif b.get("type") == "text":
                        text_parts.append(b.get("text", ""))
                    else:
                        text_parts.append(_anthropic_text([b]))
                if text_parts:
                    out.append({"role": "user", "content": "\n".join(text_parts)})
                out.extend(tool_msgs)
            else:
                out.append({"role": "user", "content": str(content or "")})

        elif role == "assistant":
            if isinstance(content, list):
                text_parts = []
                tool_calls = []
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        text_parts.append(b.get("text", ""))
                    elif b.get("type") == "tool_use":
                        tool_calls.append({
                            "id": b.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                            "type": "function",
                            "function": {
                                "name": b.get("name", ""),
                                "arguments": json.dumps(b.get("input", {}), ensure_ascii=False),
                            },
                        })
                msg: dict = {"role": "assistant"}
                msg["content"] = "\n".join(text_parts) if text_parts else None
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)
            else:
                out.append({"role": "assistant", "content": str(content or "")})

        else:
            # Unknown role — pass through best-effort.
            out.append({"role": role or "user", "content": _anthropic_text(content)})

    return out


def _anthropic_tools_to_openai(tools) -> list[dict]:
    """Convert Anthropic tool definitions to the OpenAI tool definitions the
    UI already knows how to render (`{type:'function', function:{...}}`)."""
    if not tools:
        return []
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return out


def _build_anthropic_response(reply: dict, model: str) -> dict:
    """Render a non-stream Anthropic Messages response from the human reply."""
    blocks: list[dict] = []
    text = reply.get("content")
    if text:
        blocks.append({"type": "text", "text": text})
    for tc in reply.get("tool_calls") or []:
        blocks.append({
            "type": "tool_use",
            "id": f"toolu_{uuid.uuid4().hex[:16]}",
            "name": tc["name"],
            "input": tc.get("arguments") or {},
        })
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    stop_reason = "tool_use" if reply.get("tool_calls") else "end_turn"
    return {
        "id": f"msg_{uuid.uuid4().hex[:16]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


async def _anthropic_stream(reply: dict, model: str):
    """Yield raw SSE bytes that follow Anthropic's streaming protocol."""
    msg_id = f"msg_{uuid.uuid4().hex[:16]}"

    def sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # message_start
    yield sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    block_index = 0
    text = reply.get("content")
    if text:
        yield sse("content_block_start", {
            "type": "content_block_start",
            "index": block_index,
            "content_block": {"type": "text", "text": ""},
        })
        # emit the whole text as a single delta — the UI doesn't care about
        # token-by-token granularity for a mock.
        yield sse("content_block_delta", {
            "type": "content_block_delta",
            "index": block_index,
            "delta": {"type": "text_delta", "text": text},
        })
        yield sse("content_block_stop", {"type": "content_block_stop", "index": block_index})
        block_index += 1

    for tc in reply.get("tool_calls") or []:
        tool_id = f"toolu_{uuid.uuid4().hex[:16]}"
        yield sse("content_block_start", {
            "type": "content_block_start",
            "index": block_index,
            "content_block": {
                "type": "tool_use",
                "id": tool_id,
                "name": tc["name"],
                "input": {},
            },
        })
        # Send the arguments as one input_json_delta chunk.
        yield sse("content_block_delta", {
            "type": "content_block_delta",
            "index": block_index,
            "delta": {
                "type": "input_json_delta",
                "partial_json": json.dumps(tc.get("arguments") or {}, ensure_ascii=False),
            },
        })
        yield sse("content_block_stop", {"type": "content_block_stop", "index": block_index})
        block_index += 1

    stop_reason = "tool_use" if reply.get("tool_calls") else "end_turn"
    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": 0},
    })
    yield sse("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# Anthropic Messages endpoint  (Claude Code talks to this)
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    global _active_session_id

    body = await request.json()
    req_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())

    # Normalise to OpenAI-style for the existing UI / history pipeline.
    norm_messages = _anthropic_to_openai_messages(body.get("system"), body.get("messages", []))
    norm_tools = _anthropic_tools_to_openai(body.get("tools", []))

    session = {
        "session_id": session_id,
        "req_id": req_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": body.get("model", "mock-model"),
        "messages": norm_messages,
        "tools": norm_tools,
        "reply": None,
        "status": "pending",
        "protocol": "anthropic",
        # Keep the original request body too — useful for debugging.
        "raw_request": body,
    }
    _sessions[session_id] = session
    _active_session_id = session_id
    _save_active(session_id)
    _save_session(session)

    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    # Carry over all metadata (max_tokens, temperature, stream, tool_choice, …)
    # except the parts we've already normalised (system/messages/tools).
    forwarded_meta = {
        k: v for k, v in body.items()
        if k not in ("system", "messages", "tools")
    }
    forwarded_body = {
        **forwarded_meta,
        "model": session["model"],
        "messages": norm_messages,
        "tools": norm_tools,
    }
    _pending[req_id] = {
        "id": req_id,
        "body": forwarded_body,
        "future": future,
        "session_id": session_id,
        "protocol": "anthropic",
    }

    # Notify browser using the OpenAI-shaped body it already understands.
    _broadcast("new_request", {
        "id": req_id,
        "body": _pending[req_id]["body"],
        "session_id": session_id,
    })

    try:
        result = await future
    except asyncio.CancelledError:
        _abandon_pending(req_id, session, reason="client_disconnected")
        raise
    except BaseException:
        _abandon_pending(req_id, session, reason="server_error")
        raise
    finally:
        _pending.pop(req_id, None)

    stream = bool(body.get("stream"))
    model = body.get("model", "mock-model")

    # ── Error injection branch ──────────────────────────────────────────
    if isinstance(result, dict) and result.get("__error__"):
        err = result["__error__"]
        status_code = int(err.get("status", 500))
        # Anthropic-style error envelope.
        anth_type = err.get("type") or "api_error"
        # Map a couple of common OpenAI-ish types to Anthropic-ish ones so
        # client SDKs that special-case the type still react sensibly.
        anth_type_map = {
            "invalid_request_error": "invalid_request_error",
            "rate_limit_exceeded": "rate_limit_error",
            "insufficient_quota": "permission_error",
            "server_error": "api_error",
        }
        anth_type = anth_type_map.get(anth_type, anth_type)
        error_body = {
            "type": "error",
            "error": {
                "type": anth_type,
                "message": err.get("message", "Internal server error"),
            },
        }
        session["reply"] = {"injected_error": err}
        session["status"] = "error"
        _save_session(session)
        _broadcast("session_updated", {
            "session_id": session_id,
            "status": "error",
            "preview": _session_preview(session),
            "created_at": session["created_at"],
        })
        headers = err.get("headers") or {}
        if stream:
            # When streaming, errors come back as a single SSE error event
            # with HTTP 200 — that's what real Anthropic does mid-stream.
            # But if the error happens BEFORE any bytes are sent we just
            # return the JSON envelope with the proper HTTP status; that's
            # also legal and easier to test against.
            async def err_stream():
                yield f"event: error\ndata: {json.dumps(error_body)}\n\n"
            return StreamingResponse(
                err_stream(),
                media_type="text/event-stream",
                status_code=status_code,
                headers=headers,
            )
        return JSONResponse(content=error_body, status_code=status_code, headers=headers)

    reply = result
    session["reply"] = reply
    session["status"] = "complete"
    _save_session(session)

    _broadcast("session_updated", {
        "session_id": session_id,
        "status": "complete",
        "preview": _session_preview(session),
        "created_at": session["created_at"],
    })

    if stream:
        return StreamingResponse(
            _anthropic_stream(reply, model),
            media_type="text/event-stream",
        )
    return JSONResponse(content=_build_anthropic_response(reply, model))


# ---------------------------------------------------------------------------
# Frontend reply endpoint
# ---------------------------------------------------------------------------

@app.post("/reply/{req_id}")
async def reply(req_id: str, request: Request):
    data = await request.json()
    if req_id not in _pending:
        return JSONResponse({"error": "request not found"}, status_code=404)
    _pending[req_id]["future"].set_result(data)
    _rebroadcast_next_pending(exclude_req_id=req_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Error injection
# ---------------------------------------------------------------------------

ERROR_PRESETS = [
    {
        "name": "context_length_exceeded",
        "label": "Context length exceeded (400)",
        "status": 400,
        "type": "invalid_request_error",
        "code": "context_length_exceeded",
        "message": "This model's maximum context length is 128000 tokens. However, your messages resulted in 135000 tokens. Please reduce the length of the messages.",
    },
    {
        "name": "rate_limit_exceeded",
        "label": "Rate limit exceeded (429)",
        "status": 429,
        "type": "rate_limit_exceeded",
        "code": "rate_limit_exceeded",
        "message": "Rate limit reached for requests. Please try again in 20s.",
        "headers": {"Retry-After": "20"},
    },
    {
        "name": "tokens_per_minute_exceeded",
        "label": "TPM rate limit (429)",
        "status": 429,
        "type": "tokens",
        "code": "rate_limit_exceeded",
        "message": "Rate limit reached for tokens per minute (TPM). Limit: 30000 / min.",
        "headers": {"Retry-After": "30"},
    },
    {
        "name": "insufficient_quota",
        "label": "Insufficient quota (429)",
        "status": 429,
        "type": "insufficient_quota",
        "code": "insufficient_quota",
        "message": "You exceeded your current quota, please check your plan and billing details.",
    },
    {
        "name": "invalid_api_key",
        "label": "Invalid API key (401)",
        "status": 401,
        "type": "invalid_request_error",
        "code": "invalid_api_key",
        "message": "Incorrect API key provided. You can find your API key at https://platform.openai.com/account/api-keys.",
    },
    {
        "name": "model_not_found",
        "label": "Model not found (404)",
        "status": 404,
        "type": "invalid_request_error",
        "code": "model_not_found",
        "message": "The model `gpt-x` does not exist or you do not have access to it.",
    },
    {
        "name": "invalid_tool_arguments",
        "label": "Invalid tool arguments (400)",
        "status": 400,
        "type": "invalid_request_error",
        "code": "invalid_value",
        "message": "Invalid 'tools[0].function.parameters': schema must be a valid JSON Schema object.",
    },
    {
        "name": "content_filter",
        "label": "Content policy violation (400)",
        "status": 400,
        "type": "invalid_request_error",
        "code": "content_filter",
        "message": "Your input was blocked by our content policy. Please modify your prompt and try again.",
    },
    {
        "name": "server_error",
        "label": "Internal server error (500)",
        "status": 500,
        "type": "server_error",
        "code": "internal_error",
        "message": "The server had an error while processing your request. Sorry about that!",
    },
    {
        "name": "service_unavailable",
        "label": "Service unavailable (503)",
        "status": 503,
        "type": "server_error",
        "code": "service_unavailable",
        "message": "The engine is currently overloaded, please try again later.",
    },
    {
        "name": "gateway_timeout",
        "label": "Gateway timeout (504)",
        "status": 504,
        "type": "server_error",
        "code": "gateway_timeout",
        "message": "Gateway timeout. Upstream did not respond in time.",
    },
    {
        "name": "bad_gateway",
        "label": "Bad gateway (502)",
        "status": 502,
        "type": "server_error",
        "code": "bad_gateway",
        "message": "Bad gateway.",
    },
]


@app.get("/error-presets")
async def error_presets():
    return JSONResponse(ERROR_PRESETS)


@app.post("/reply/{req_id}/error")
async def reply_with_error(req_id: str, request: Request):
    data = await request.json()
    if req_id not in _pending:
        return JSONResponse({"error": "request not found"}, status_code=404)
    _pending[req_id]["future"].set_result({"__error__": data})
    _rebroadcast_next_pending(exclude_req_id=req_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# History API
# ---------------------------------------------------------------------------

@app.get("/histories")
async def list_histories():
    summaries = [
        {
            "session_id": s["session_id"],
            "created_at": s["created_at"],
            "preview": _session_preview(s),
            "status": s["status"],
        }
        for s in sorted(_sessions.values(), key=lambda x: x["created_at"], reverse=True)
    ]
    return JSONResponse(summaries)


@app.get("/histories/{session_id}")
async def get_history(session_id: str):
    if session_id not in _sessions:
        return JSONResponse({"error": "not found"}, status_code=404)
    data = {k: v for k, v in _sessions[session_id].items() if k != "future"}
    return JSONResponse(data)


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------

@app.get("/events")
async def events(request: Request):
    global _active_session_id
    q: asyncio.Queue = asyncio.Queue()
    _sse_queues.append(q)

    # On connect: replay active session state
    if _active_session_id and _active_session_id in _sessions:
        active = _sessions[_active_session_id]
        if active["status"] == "pending":
            # Find the matching pending req
            for item in list(_pending.values()):
                if item["session_id"] == _active_session_id:
                    await q.put({
                        "event": "new_request",
                        "data": json.dumps({"id": item["id"], "body": item["body"], "session_id": _active_session_id}),
                    })
                    break
        else:
            # complete — restore read-only
            data = {k: v for k, v in active.items() if k != "future"}
            await q.put({"event": "session_restored", "data": json.dumps(data)})

    async def generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield msg
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            if q in _sse_queues:
                _sse_queues.remove(q)

    return EventSourceResponse(generator())


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))
