# mymockllm

A minimal OpenAI / Anthropic compatible mock server that lets you manually play the role of the LLM.

Instead of calling a real model, every request is pushed to a browser UI where you type the reply yourself — giving you full control over what the "model" returns, at zero cost.

## Start

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

…or just `./start.sh`. Open `http://localhost:8000` in your browser.

## Endpoints

```
POST /chat/completions     # OpenAI
POST /v1/messages          # Anthropic
```

Point your client's `base_url` here and set any non-empty API key.

## How it works

1. Client sends a request — the server **hangs**.
2. The browser receives the message history, available tools, and request metadata via SSE.
3. You type a reply (plain text and/or tool calls) and hit **Send** — or inject a server-side error.
4. The server wraps your input into a standard response (streaming or not) and returns it to the client.

## UI

| Color  | Role                |
| ------ | ------------------- |
| Blue   | `user`              |
| Green  | `assistant`         |
| Purple | `tool_call`         |
| Orange | `tool` result       |
| Gray   | `system` / metadata |

- **＋** next to the input box adds a tool call. Pick a name from the available tools and the matching JSON Schema floats next to the form.
- **Inject error** sends a preset OpenAI/Anthropic-style error envelope (rate limit, context length exceeded, etc.) instead of a normal reply.
- **History** sidebar lets you replay any past session. Files are stored under `.histories/`.

## Theme

Toggle the moon/sun in the top-right for dark / light mode.
