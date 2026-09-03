# Hermes WeChat Prefix Router Plugin

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Hermes Agent](https://img.shields.io/badge/Hermes%20Agent-Plugin-blue)](https://hermes-agent.nousresearch.com)
![Version](https://img.shields.io/badge/version-0.3.1-orange)

Route WeChat (weixin) messages by message prefix to **different Hermes profiles** or **external AI agents** (Maka, Pi, or any agent with a programmatic interface). Messages without a configured prefix go to the default Hermes agent.

## Architecture

```
WeChat → Hermes Gateway → [weixin-prefix-router] ──┬─ "@coder message"  → coder Hermes profile
                                                     ├─ "@maka message"  → Maka adapter (external agent)
                                                     ├─ "@pi message"    → Pi adapter (external agent)
                                                     └─ "normal message" → default Hermes agent
```

The plugin hooks into Hermes's `pre_gateway_dispatch` lifecycle event — fired before authorization and agent dispatch for every inbound message.

Two routing modes:

| Mode | Behavior |
|------|----------|
| **profile** | Sets `event.source.profile` and rewrites the text (strips prefix). The message flows into the target profile's session via Hermes's `multiplex_profiles` runtime. |
| **adapter** | Calls an external agent adapter (`handle(text, chat_id, user_id) -> str`), sends the response back over WeChat, and skips Hermes dispatch entirely. |

## Features

- **Prefix-based routing** — any prefix → any target
- **Dual routing modes** — Hermes profile multiplexing *or* external agent adapters
- **Plugin-driven adapters** — each external agent is a standalone directory with its own `__init__.py`; different agents can use completely different API/SDK/CLI conventions
- **Zero core modification** — implemented as a standard Hermes plugin, survives upgrades
- **Hot-reload routing table** — `routes.json` is read on every message, no restart needed
- **WeChat-only** — other platforms pass through untouched

## Requirements

- Hermes Agent with WeChat gateway configured (iLink Bot API)
- `gateway.multiplex_profiles: true` in `config.yaml` (for profile routes)
- Target Hermes profile(s) must exist (for profile routes)
- External agent API/SDK/CLI reachable (for adapter routes)

## Installation

### 1. Clone the plugin

```bash
git clone https://github.com/ser163/hermes-weixin-prefix-router-plugin.git \
  ~/AppData/Local/hermes/plugins/weixin-prefix-router
```

Or copy the files manually:

```bash
mkdir -p ~/AppData/Local/hermes/plugins/weixin-prefix-router
cp plugin.yaml __init__.py routes.json ~/AppData/Local/hermes/plugins/weixin-prefix-router/
```

### 2. Enable multiplex profiles (for profile routes)

```bash
hermes config set gateway.multiplex_profiles true
```

### 3. Enable the plugin

```bash
hermes plugins enable weixin-prefix-router
```

### 4. Restart the gateway

```bash
hermes gateway restart
```

### 5. Verify

```bash
tail -f ~/AppData/Local/hermes/logs/gateway.log
# Expect: "weixin: restored 1 context token(s)" → gateway connected
# And in plugin load output: "loaded N route(s)"
```

## Configuration

Edit `routes.json` in the plugin directory:

```json
{
  "@coder": "coder",
  "@maka": {"type": "adapter", "path": "maka"},
  "@pi": {"type": "adapter", "path": "pi"}
}
```

### Route types

#### Profile route (string shorthand)

```json
"@coder": "coder"
```

Full form: `{"type": "profile", "profile": "coder"}`. The prefix is stripped and the message is routed to the named Hermes profile.

#### Adapter route (external agent)

```json
"@maka": {"type": "adapter", "path": "maka"}
```

`path` resolves relative to the adapter base directory `E:/test/ai/` — so `"maka"` loads `E:/test/ai/maka/__init__.py`. Absolute paths also work.

## Writing an Adapter

Each external agent lives in its own directory under `E:/test/ai/`:

```
E:/test/ai/
├── maka/__init__.py   # Maka agent adapter
└── pi/__init__.py     # Pi agent adapter
```

Every adapter must export a `handle` function:

```python
async def handle(text: str, chat_id: str = "", user_id: str = "") -> str:
    """Process the message and return the response text."""
    return "response text"
```

- `text` — the message with the prefix already stripped
- `chat_id` / `user_id` — WeChat source identifiers (useful for per-user session state)
- Return value — the response sent back over WeChat
- Both sync and async handlers are supported
- Errors are caught by the plugin; an error notice is sent back over WeChat

Adapters are free to use any transport: HTTP API (aiohttp/httpx/requests), subprocess CLI, SDK, etc. The canonical adapter template:

```python
from __future__ import annotations
import aiohttp

async def handle(text: str, chat_id: str = "", user_id: str = "") -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://agent.example.com/api/chat",
            json={"message": text, "session_id": f"weixin-{chat_id or user_id}"},
        ) as resp:
            data = await resp.json()
            return data.get("response") or str(data)
```

## Usage

In WeChat, send a message to your bot:

- `@coder 帮我写一个 Python 排序函数` → **coder profile agent**
- `@maka 分析这个项目结构` → **Maka external agent**
- `@pi 帮我重构这段代码` → **Pi external agent**
- `你好，今天天气怎么样？` → **default Hermes agent** (no prefix)

## How It Works

1. **Plugin hook**: `pre_gateway_dispatch` fires for every inbound message
2. **Platform check**: only `weixin` platform messages are processed
3. **Prefix match**: iterates `routes.json`, checks `text.startswith(prefix)`
4. **Profile route**: sets `event.source.profile`, returns `{"action": "rewrite", "text": stripped}` → Hermes multiplex routes the session to the target profile home
5. **Adapter route**: schedules `_forward_to_adapter()` (async → external agent → response → WeChat reply), returns `{"action": "skip"}` to bypass Hermes dispatch
6. **No match**: returns `None` → normal Hermes dispatch

## Maka Integration (wechat-bridge)

The Maka adapter (`adapters/maka/`) bridges Hermes's WeChat gateway to
**Apache Maka** — a local-first AI agent workspace. Maka ships a WeChat bot
channel that supports **local bridge mode**: it connects to a local
wechat-bridge process instead of Tencent's servers. We implement that
bridge, so Maka's agent can receive WeChat messages from Hermes and reply
back over the same conversation — **no real WeChat credentials needed for
Maka**, Hermes owns the WeChat connection.

### Architecture

```
WeChat user
   │
   ▼
Hermes WeChat gateway ── @maka message ──► weixin-prefix-router plugin
   │                                              │ POST /bridge/inbound (600s wait)
   │                                              ▼
   │                                    wechat-bridge (127.0.0.1:19860)
   │                                              │ SSE /messages/stream
   │                                              ▼
   │                                    Maka WeChat bot channel ──► Maka agent
   │                                              ▲ POST /send (reply)
   │                                              │
   └─────────────── reply via WeChat ◄────────────┘
```

### 1. Start the bridge

```bash
cd adapters/maka
python bridge.py                # listens on 127.0.0.1:19860
```

On first start the bridge generates a **persistent 32-char token** and
stores it in `wechat-bridge.token` next to `bridge.py`. The token survives
restarts — configure Maka once and forget it. Startup prints:

```
[wechat-bridge] TOKEN: 5FF1EA83Cfd04164bEE8bB9b48B6fAf5
[wechat-bridge] 在 Maka WeChat 通道填入：webhookUrl=http://127.0.0.1:19860, Bot Token=...
```

To rotate the token, delete `wechat-bridge.token` and restart.

### 2. Configure Maka's WeChat bot channel

In Maka settings → Remote access (远程接入) → WeChat channel:

| Field | Value |
|-------|-------|
| **webhookUrl** | `http://127.0.0.1:19860` |
| **Bot Token** | the token from step 1 |

Maka's connection test calls `GET /health` with
`Authorization: Bearer <token>` → bridge validates → channel shows
connected. **Enable the channel** so Maka starts listening on the SSE
message stream.

### 3. Route WeChat messages to Maka

`routes.json` (plugin directory):

```json
{
  "@coder": "coder",
  "@maka": {"type": "adapter", "path": "maka"}
}
```

Send `@maka <message>` in WeChat → routed to Maka's agent → the reply
comes back in the same WeChat chat.

### Bridge endpoints

| Endpoint | Direction | Purpose |
|----------|-----------|---------|
| `GET /health` | Maka → bridge | Connection test + identity (Bearer token) |
| `GET /messages/stream` | Maka → bridge | SSE long-poll: deliver WeChat messages |
| `POST /send` | Maka → bridge | Maka replies (`{wxid, text}`) → captured |
| `GET /qrcode` | Maka → bridge | QR-code onboarding stub (auth via token instead) |
| `POST /bridge/inbound` | Hermes → bridge | Plugin submits message, blocks for reply (600s) |
| `POST /bridge/onboard` | Admin → bridge | Show the persistent token |
| `GET /bridge/status` | Admin → bridge | Health check + queue depth + SSE conns |

**Auth model**: all endpoints require `Authorization: Bearer <token>`.
The token is the persistent 32-char string shown at startup.

### Debugging the message flow

```bash
# 1. Bridge healthy? (expect authorized: true, sse_connections ≥ 1)
curl http://127.0.0.1:19860/bridge/status

# 2. Send @maka in WeChat → watch bridge log for:
#    POST /bridge/inbound ... 200     ← Hermes submitted
#    reply captured for request=...   ← Maka replied
#    inbound reply ready: request=... ← delivered back to Hermes

# 3. If "inbound timeout" appears → Maka's model took >600s, or the
#    channel is disabled (check enabled: true in Maka settings)
```

## Development

### Repository structure

```
hermes-weixin-prefix-router-plugin/
├── plugin.yaml          # Hermes plugin manifest
├── __init__.py          # Plugin implementation (v0.3.x dual routing)
├── routes.json          # Default routing configuration
├── adapters/            # External agent adapter examples
│   └── maka/            # Maka wechat-bridge integration
│       ├── __init__.py  # Adapter: submits to bridge, returns reply
│       ├── bridge.py    # Local wechat-bridge server (port 19860)
│       ├── test_bridge_e2e.py  # End-to-end test (8 checks)
│       └── README.md    # Maka integration guide
├── README.md            # This file
├── LICENSE              # MIT License
├── CHANGELOG.md         # Version history
└── .gitignore
```

### Testing locally

```bash
# From Hermes source root
python -c "
from hermes_cli.plugins import PluginManager
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

pm = PluginManager()
pm.discover_and_load(force=True)

# Profile route
source = SessionSource(platform=Platform.WEIXIN, chat_id='t', user_id='u')
event = MessageEvent(text='@coder hello', source=source)
pm.invoke_hook('pre_gateway_dispatch', event=event, gateway=None, session_store=None)
print(f'Profile: {event.source.profile}')  # 'coder'

# Adapter route (skips dispatch)
source2 = SessionSource(platform=Platform.WEIXIN, chat_id='t', user_id='u')
event2 = MessageEvent(text='@maka hello', source=source2)
results = pm.invoke_hook('pre_gateway_dispatch', event=event2, gateway=None, session_store=None)
print(f'Action: {results[0][\"action\"]}')  # 'skip'
"
```

## License

MIT