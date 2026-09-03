# Hermes WeChat Prefix Router Plugin

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Hermes Agent](https://img.shields.io/badge/Hermes%20Agent-Plugin-blue)](https://hermes-agent.nousresearch.com)

Route WeChat (weixin) messages to different Hermes agent profiles by message prefix. Messages starting with a configured prefix (e.g., `@coder`) are forwarded to the corresponding profile's agent; all other messages go to the default Hermes agent.

## Architecture

```
WeChat → Hermes Gateway → [weixin-prefix-router] ──┬─ "@coder message" → coder profile agent
                                                      ├─ "/zcode message" → zcode profile agent
                                                      └─ "normal message" → default Hermes agent
```

The plugin hooks into Hermes's `pre_gateway_dispatch` lifecycle event — fired before authorization and agent dispatch for every inbound message. It inspects the message text, rewrites it (stripping the prefix), and stamps `event.source.profile` to route the message to the target profile's session namespace and runtime.

## Features

- **Prefix-based routing** — configure any prefix to map to any profile
- **Multi-profile support** — works with Hermes's `gateway.multiplex_profiles` feature
- **Zero core modification** — implemented as a standard Hermes plugin, survives upgrades
- **Hot-reload routing table** — routes are read from `routes.json` on every message, no restart needed
- **WeChat-only** — only affects weixin platform messages, other platforms pass through unchanged

## Requirements

- Hermes Agent with WeChat gateway configured (iLink Bot API)
- `gateway.multiplex_profiles: true` in `config.yaml`
- Target profile(s) must exist and be configured

## Installation

### 1. Clone the plugin

```bash
git clone https://github.com/ser163/hermes-weixin-prefix-router-plugin.git \
  ~/AppData/Local/hermes/plugins/weixin-prefix-router
```

Alternatively, copy the files manually:

```bash
mkdir -p ~/AppData/Local/hermes/plugins/weixin-prefix-router
cp plugin.yaml __init__.py routes.json ~/AppData/Local/hermes/plugins/weixin-prefix-router/
```

### 2. Enable multiplex profiles

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
# Look for: "weixin: restored 1 context token(s)" → gateway connected
# Check plugin loaded: hermes plugins list | grep weixin-prefix
```

## Configuration

Edit `routes.json` in the plugin directory:

```json
{
  "@coder": "coder",
  "/zcode": "zcode"
}
```

Each key is a **message prefix** and each value is the **target Hermes profile name**:

| Key       | Value  | Effect                             |
|-----------|--------|------------------------------------|
| `@coder`  | coder  | `@coder <message>` → coder profile |
| `/zcode`  | zcode  | `/zcode <message>` → zcode profile |

The prefix is **stripped** from the message before it reaches the target profile's agent. The profile name must match a valid Hermes profile directory name.

## Usage

In WeChat, send a message to your bot:

- `@coder 帮我写一个 Python 排序函数` → routed to **coder profile** agent, receives `帮我写一个 Python 排序函数`
- `你好，今天天气怎么样？` → routed to **default Hermes agent** (no prefix match)

## How It Works

1. **Plugin hook**: `pre_gateway_dispatch` fires for every inbound message
2. **Platform check**: only `weixin` platform messages are processed
3. **Prefix match**: iterates `routes.json` entries, checks if `text.startswith(prefix)`
4. **Route**: if match, sets `event.source.profile = target_profile` and returns `{"action": "rewrite", "text": stripped_text}`
5. **Fallback**: no match → returns `None`, normal Hermes dispatch

The gateway's `_resolve_profile_home_for_source` method picks up the `source.profile` field and routes the session to the target profile's home directory, loading its model, tools, skills, and memory configuration.

## Dependencies

None. The plugin uses only Python standard library and Hermes's built-in plugin API (`hermes_cli.plugins.PluginContext`).

## Development

### Repository structure

```
hermes-weixin-prefix-router-plugin/
├── plugin.yaml          # Hermes plugin manifest
├── __init__.py          # Plugin implementation
├── routes.json          # Default routing configuration
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

source = SessionSource(platform=Platform.WEIXIN, chat_id='test', user_id='user1')
event = MessageEvent(text='@coder hello', source=source)
results = pm.invoke_hook('pre_gateway_dispatch', event=event, gateway=None, session_store=None)

print(f'Profile: {event.source.profile}')  # Should be 'coder'
print(f'Text: {event.text}')               # Should be rewritten to 'hello'
"
```

## License

MIT