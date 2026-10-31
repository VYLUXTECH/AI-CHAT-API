# AI-CHAT-API

OpenAI-compatible API bridge over **chat.qwen.ai** and **chatgpt.com** — pure HTTP.
Chrome is used only to bootstrap/refresh sessions (cookies + tokens); chat traffic is
plain HTTPS replaying the exact requests the web apps make. Derived from the
Deepseek-Bridge engine architecture.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# visible Chrome with remote debugging (session bootstrap):
./aichatapi.sh
.venv/bin/uvicorn aichatapi:app --host 127.0.0.1 --port 3080
```

## Endpoints

| Route | Description |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible chat (stream & non-stream) |
| `GET /v1/models` | Combined Qwen + ChatGPT catalog |
| `GET /v1/health` | Token/session status for both backends |

Verified live 2026-10-30: both backends answer end-to-end through `/v1/chat/completions`
(Qwen `qwen3.7-plus` and ChatGPT `gpt-6-mini` echo tests).

Models: `qwen3.7-plus`, `qwen3.8-max`, `qwen3.8-omni-flash` · `gpt-6`, `gpt-6-mini`,
`gpt-5-6`, `gpt-5-6-mini`, `auto`. Route selection is by model id prefix.

## Config (env)

`AICHAT_PORT` (3080) · `BRIDGE_API_KEY` (optional Bearer) · `AICHAT_CDP_URL` (127.0.0.1:9222)
· `AICHAT_QWEN_MODEL` · `AICHAT_GPT_MODEL` · `AICHAT_TIMEOUT` (240s)

## How it works

- **Qwen**: full app-order replay in page context — `POST /api/v2/users/status` →
  `POST /api/v2/chats/new` → `users/status` (with chat id) → `POST /api/v2/chat/completions?chat_id=` (SSE).
  (Skipping the second `users/status` registration ping yields `CHAT_NOT_FOUND`.)
- **ChatGPT**: pure HTTP is blocked by OpenAI's *sentinel* proof-of-work handshake, so the
  driver fills the live composer via CDP (`#prompt-textarea`, retry-until-mounted), clicks
  send, and lets the page perform its own handshake. The reply is read from the rendered
  transcript (`body.innerText` after our prompt's echo → "ChatGPT said:…" block), polled
  until the streaming text stabilizes. In-page network hooks were removed on purpose —
  the app's own stream reader locks the ReadableStream and a second `getReader()` races it
  (the capture came back empty).
- Session bootstrap: Bearer JWT from `localStorage.token` (Qwen) / `GET /api/auth/session`
  (ChatGPT), cookie jar harvested from Chrome via CDP — auto-refreshed near expiry.

`main.py` is the legacy DeepSeek engine kept for reference.
`tokens/fallback-routes.json`-style free relays are planned as the next fallback layer.

---

# Container port (`container/`) — headless Node

Pure-HTTP bridge to **chat.qwen.ai** rewritten for the box (runs as a mux child,
zero npm deps). Chrome is gone from the runtime: the JWT (+ optional cookie jar)
is read per-request from `state.json` — refresh it whenever it nears expiry
(tokens last ~15 min).

## Run

```bash
node container/server.js          # PORT env (container mux assigns :21690)
```

| Route | Description |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible chat (stream & non-stream), models `qwen3.7-plus`, `qwen3.8-max`, `qwen3.8-omni-flash` |
| `GET /v1/models` | Catalog (gpt ids listed but unavailable headless) |
| `GET /v1/health` | Token expiry + session status |

`container/state.json` (see `state.example.json`): `{ token, cookies, ua }` —
read lazily on every request, so updating it needs no restart.

## Session keep-alive (`scripts/`)

`harvest.js` pulls `localStorage.token` + cookies + UA from a logged-in Chrome
via CDP (`:9222`); `refresh-token.sh` harvests and pushes `state.json` to the
container over SFTP. Deployed pattern: Chrome under Xvfb with full VNC login
(`aichatapi.sh`), then a cron `*/5 * * * * /root/aichat/refresh-token.sh`.

Public endpoint (deployed): `https://aichat.vyluxtech.com/v1/{models,health,chat/completions}`
— Cloudflare `apix-bridge` → container mux (host `aichat.*`) → AICHAT child.
