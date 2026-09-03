# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-03

### Added

- `pre_gateway_dispatch` plugin hook for WeChat (weixin) platform
- Prefix-based routing to Hermes agent profiles (`routes.json` configuration)
- Message prefix stripping before forwarding to target profile agent
- Hot-reload routing table (routes read per-message, no gateway restart required)
- Platform isolation (only `weixin` messages are routed; other platforms unaffected)
- Documented plugin manifest (`plugin.yaml`), implementation, and tests

[0.1.0]: https://github.com/ser163/hermes-weixin-prefix-router-plugin/releases/tag/v0.1.0