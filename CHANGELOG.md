# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-09-03

### Added

- **External agent adapter routing** — route WeChat messages by prefix to
  external AI agents (Maka, Pi, or any agent with an HTTP/SDK/CLI interface)
- **Dual route types** in `routes.json`:
  - `profile` — route to a Hermes profile (multiplex_profiles), unchanged from v0.1.0
  - `adapter` — forward to an external agent via a pluggable adapter module
- **Adapter loader** — dynamically imports `E:/test/ai/<name>/__init__.py`
  (or absolute paths); each adapter exports `handle(text, chat_id, user_id)`
- **Async forwarding** — adapters run in the gateway event loop; responses are
  sent back over WeChat; errors surface as WeChat notices
- **Template adapters** — `E:/test/ai/maka/` (aiohttp HTTP template) and
  `E:/test/ai/pi/` (CLI subprocess template)

### Changed

- `routes.json` values now accept string (profile shorthand) or dict with
  `type` (`"profile"` | `"adapter"`) and target (`profile` | `path`)
- Plugin log line now reports route types and adapter base directory
- README documents both route modes and the adapter authoring guide

[0.2.0]: https://github.com/ser163/hermes-weixin-prefix-router-plugin/releases/tag/v0.2.0

## [0.1.0] - 2026-09-03

### Added

- `pre_gateway_dispatch` plugin hook for WeChat (weixin) platform
- Prefix-based routing to Hermes agent profiles (`routes.json` configuration)
- Message prefix stripping before forwarding to target profile agent
- Hot-reload routing table (routes read per-message, no gateway restart required)
- Platform isolation (only `weixin` messages are routed; other platforms unaffected)
- Documented plugin manifest (`plugin.yaml`), implementation, and tests

[0.1.0]: https://github.com/ser163/hermes-weixin-prefix-router-plugin/releases/tag/v0.1.0