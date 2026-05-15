# mymockllm — API Reference

A manual mock server. The browser is the "model": every client request hangs
until you type a reply in the UI.

Two client-facing protocols are accepted on the same server, plus a small set
of internal endpoints used by the browser UI.

```
http://localhost:8000
```

---

## Client-facing endpoints

### `POST /chat/completions` &nbsp;·&nbsp; OpenAI-compatible

Also exposed at `POST /v1/chat/completions` (same handler) so clients that
include the `/v1` prefix in their `base_url` work without reconfiguration.

Standard OpenAI Chat Completions request and response. Both streaming
(`"stream": true`, `text/event-stream`) and non-streaming responses are
supported — the response shape is decided by your reply in the UI plus the
`stream` flag in the request.

Request body fields recognised: `model`, `messages`, `tools`, `tool_choice`,
`temperature`, `top_p`, `stream`, plus arbitrary extras (forwarded to the UI
as request metadata).

Reply shape (function-calling):

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "<echoed from request>",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "<text you typed, may be null>",
      "tool_calls": [
        {
          "id": "call_xyz",
          "type": "function",
          "function": { "name": "bash", "arguments": "{\"command\":\"ls\"}" }
        }
      ]
    },
    "finish_reason": "tool_calls"
  }]
}
```

### `POST /v1/messages` &nbsp;·&nbsp; Anthropic-compatible

Standard Anthropic Messages request and response. Streaming and non-streaming
are both supported (Anthropic's own SSE event sequence is emitted when
`"stream": true`). Tool use is returned as `content` blocks of type
`tool_use`.

### `GET /`

Serves the browser UI (`app/index.html`). Open it in a browser to act as the
model.

---

## Error injection

Instead of a normal reply, the UI can return a structured error envelope. The
server applies the right status code and protocol-specific error shape
(OpenAI vs. Anthropic) automatically.

### `GET /error-presets`

Returns the list of available presets:

```json
[
  { "name": "context_length_exceeded", "label": "Context length exceeded (400)",
    "status": 400, "type": "invalid_request_error",
    "code": "context_length_exceeded", "message": "..." },
  { "name": "rate_limit_exceeded",     "label": "Rate limit exceeded (429)",
    "status": 429, "headers": { "Retry-After": "20" }, ... },
  ...
]
```

Built-in presets cover: context-length exceeded, rate limit (RPM/TPM),
insufficient quota, invalid API key, model not found, invalid tool arguments,
server overload, internal server error, gateway timeout.

---

## Internal endpoints (used by the browser UI)

You normally don't call these yourself — they're how `index.html` drives the
server.

### `GET /events` &nbsp;·&nbsp; SSE

Server-sent events stream. On connect, replays the active session if one
exists. Event types:

| Event              | Payload                                                |
| ------------------ | ------------------------------------------------------ |
| `new_request`      | `{ "id", "body", "session_id" }` — a request is hung   |
| `session_restored` | full session record (read-only replay on reconnect)    |
| `ping`             | `{}` — keep-alive every 15s                            |

### `POST /reply/{req_id}`

Resolve a hung request with a normal assistant reply.

```json
{
  "content": "<text or null>",
  "tool_calls": [
    { "name": "bash", "arguments": { "command": "ls" } }
  ]
}
```

`tool_calls` is optional. The server formats the response in whichever
protocol the original request came in on.

### `POST /reply/{req_id}/error`

Resolve a hung request with an error envelope. Body is one of the preset
objects from `/error-presets` (or any compatible shape).

### `GET /histories`

List past sessions, newest first:

```json
[
  { "session_id": "...", "created_at": "...", "preview": "...", "status": "complete" }
]
```

### `GET /histories/{session_id}`

Full session record (request body, reply, metadata, timestamps). Persisted
on disk under `.histories/<session_id>.json`.

---

## Run

```bash
./start.sh
# or
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir app
```

Source code lives in `app/`. Histories live in `.histories/` at the repo
root.
