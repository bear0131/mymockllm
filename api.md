# mymockllm — API Reference

This service exposes an **OpenAI-compatible** HTTP API so that SWE-agent (via litellm)
can route requests to any backend model.

---

## Base URL

```
http://localhost:8000
```

---

## Endpoints

### `POST /chat/completions`

The only endpoint SWE-agent calls. Accepts a standard OpenAI Chat Completions request
and returns a standard OpenAI Chat Completions response.

#### Request headers

| Header          | Required | Description                          |
|-----------------|----------|--------------------------------------|
| `Authorization` | No       | `Bearer <api_key>` — validated or ignored depending on config |
| `Content-Type`  | Yes      | `application/json`                   |

#### Request body

```json
{
  "model": "my-model",
  "messages": [
    { "role": "system",    "content": "You are a helpful assistant..." },
    { "role": "user",      "content": "...task description..." },
    { "role": "assistant", "content": "..." },
    { "role": "user",      "content": "OBSERVATION:\n..." }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "bash",
        "description": "Run a bash command in the sandbox shell.",
        "parameters": {
          "type": "object",
          "properties": {
            "command": { "type": "string", "description": "The bash command to run." }
          },
          "required": ["command"]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "submit",
        "description": "Submit the current patch as the final answer.",
        "parameters": { "type": "object", "properties": {} }
      }
    }
  ],
  "tool_choice": "auto",
  "temperature": 0.0,
  "top_p": 1.0,
  "stream": false
}
```

> **Note:** `tools` and `tool_choice` are only present when
> `agent.tools.parse_function.type = function_calling` (the default).
> If you set `type: thought_action`, these fields are omitted and the model
> should return a plain-text response with the action inside a fenced code block.

#### Response body (function-calling mode)

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "my-model",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_xyz",
            "type": "function",
            "function": {
              "name": "bash",
              "arguments": "{\"command\": \"ls -la /repo\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ],
  "usage": {
    "prompt_tokens": 512,
    "completion_tokens": 32,
    "total_tokens": 544
  }
}
```

#### Response body (thought_action mode)

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "my-model",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "I'll list the repository contents first.\n\n```\nls -la /repo\n```"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 512,
    "completion_tokens": 24,
    "total_tokens": 536
  }
}
```

---

## Minimal Python implementation

```python
# main.py  —  run with: uvicorn main:app --host 0.0.0.0 --port 8000
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx, time, uuid

app = FastAPI()

# Replace with the real backend you want to route to
BACKEND_URL = "http://your-real-model-backend/v1/chat/completions"
BACKEND_API_KEY = "your-real-api-key"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()

    # --- routing logic goes here ---
    # e.g. inspect body["model"] or body["messages"] to pick a backend
    # --------------------------------

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            BACKEND_URL,
            json=body,
            headers={"Authorization": f"Bearer {BACKEND_API_KEY}"},
        )

    return JSONResponse(content=resp.json(), status_code=resp.status_code)
```

---

## SWE-agent config reference

The relevant fields in `config/local_router.yaml`:

```yaml
agent:
  model:
    name: openai/my-model        # "openai/" prefix → litellm uses OpenAI-compatible protocol
    api_base: http://localhost:8000
    api_key: "local-router-key"  # any non-empty string if the router ignores auth
    per_instance_cost_limit: 0   # must be 0 — litellm can't price unknown models
    total_cost_limit: 0
    per_instance_call_limit: 100
    max_input_tokens: 0          # 0 = disable context-window check
  tools:
    parse_function:
      type: function_calling     # or "thought_action" if backend lacks tool-call support
  history_processors: []         # remove cache_control — it's Claude-only
```
