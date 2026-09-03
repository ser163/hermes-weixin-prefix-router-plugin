"""
WeChat prefix router plugin — v0.2.0

Intercepts inbound WeChat messages and routes them to:
  - A different Hermes profile (via multiplex_profiles)
  - An external AI agent adapter (HTTP API, SDK, CLI, etc.)

Each external agent has its own adapter directory under a configurable
base path (default: E:/test/ai/). Adapters export a standard
``handle(text, chat_id, user_id)`` function that returns the response text.

Configuration (routes.json):
    {
        "@coder": "coder",                          # profile route
        "@maka": {"type": "adapter", "path": "maka"},  # adapter route
        "@pi":   {"type": "adapter", "path": "pi"}     # adapter route
    }

Adapter paths are resolved relative to ``ADAPTER_BASE`` (E:/test/ai/)
or as absolute paths. Each adapter directory must contain an ``__init__.py``
with an async ``handle(text, chat_id, user_id) -> str`` function.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────
ROUTES_FILE = Path(__file__).parent / "routes.json"
ADAPTER_BASE = Path("E:/test/ai")  # root for adapter directories


# ── config types ───────────────────────────────────────────────────────────

def _resolve_route(
    value: Union[str, dict],
) -> Dict[str, Any]:
    """Normalise a route entry into a canonical dict.

    String value → profile route (backward-compatible).
    Dict value   → must contain ``type``: ``"profile"`` or ``"adapter"``.
    """
    if isinstance(value, str):
        return {"type": "profile", "profile": value}
    if isinstance(value, dict):
        return value
    raise ValueError(f"Unsupported route value type: {type(value).__name__}")


def _load_routes() -> Dict[str, Dict[str, Any]]:
    """Load prefix→route mapping from routes.json."""
    try:
        if ROUTES_FILE.exists():
            data = json.loads(ROUTES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: _resolve_route(v) for k, v in data.items()}
    except Exception as exc:
        logger.warning("[weixin-prefix-router] failed to load routes.json: %s", exc)
    return {}


# ── adapter loader ─────────────────────────────────────────────────────────

def _load_adapter(path: str) -> Any:
    """Dynamically import an adapter module by path.

    ``path`` can be relative (resolved under ADAPTER_BASE) or absolute.
    The module must export an async ``handle(text, chat_id, user_id)``
    function.
    """
    adapter_path = Path(path)
    if not adapter_path.is_absolute():
        adapter_path = ADAPTER_BASE / adapter_path

    init_file = adapter_path / "__init__.py"
    if not init_file.exists():
        raise FileNotFoundError(
            f"Adapter not found at {adapter_path}/__init__.py"
        )

    module_name = f"_weixin_adapter_{adapter_path.name}"
    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, init_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load adapter: {init_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    if not hasattr(module, "handle"):
        raise AttributeError(
            f"Adapter {adapter_path.name} is missing a 'handle' function"
        )

    return module


# ── forwarder ──────────────────────────────────────────────────────────────

async def _forward_to_adapter(
    text: str,
    chat_id: str,
    user_id: str,
    adapter_path: str,
    gateway: Any,
    source: Any,
) -> None:
    """Call the external adapter and send the response via WeChat."""
    try:
        module = _load_adapter(adapter_path)
        handle_fn = module.handle

        # Support both sync and async handlers
        result = handle_fn(text=text, chat_id=chat_id, user_id=user_id)
        if asyncio.iscoroutine(result):
            response = await result
        else:
            response = result

        if not response or not isinstance(response, str):
            logger.warning(
                "[weixin-prefix-router] adapter %s returned empty/invalid response",
                adapter_path,
            )
            return

        # Send response via the WeChat gateway adapter
        try:
            adapter = gateway._adapter_for_source(source)
            if adapter:
                await adapter.send(chat_id, response)
                logger.info(
                    "[weixin-prefix-router] adapter=%s responded %d chars to %s",
                    adapter_path,
                    len(response),
                    user_id or "?",
                )
            else:
                logger.warning(
                    "[weixin-prefix-router] no adapter for source %s", source
                )
        except Exception as send_err:
            logger.error(
                "[weixin-prefix-router] failed to send response: %s", send_err
            )

    except Exception as exc:
        logger.error(
            "[weixin-prefix-router] adapter %s error: %s", adapter_path, exc
        )
        # Try to send error notice back to user
        try:
            adapter = gateway._adapter_for_source(source)
            if adapter:
                await adapter.send(
                    chat_id,
                    f"⚠️ Agent {adapter_path} 返回错误，请稍后重试",
                )
        except Exception:
            pass


# ── plugin entry point ────────────────────────────────────────────────────

def register(ctx) -> None:
    """Register the pre_gateway_dispatch hook."""
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    routes = _load_routes()
    if routes:
        info = []
        for prefix, cfg in routes.items():
            t = cfg.get("type", "profile")
            target = cfg.get("profile") or cfg.get("path", "?")
            info.append(f"{prefix}→{t}:{target}")
        logger.info(
            "[weixin-prefix-router] v0.2.0 loaded %d route(s): %s",
            len(routes),
            ", ".join(info),
        )
        adapter_routes = [cfg for cfg in routes.values() if cfg.get("type") == "adapter"]
        if adapter_routes:
            logger.info(
                "[weixin-prefix-router] adapter base: %s",
                ADAPTER_BASE.resolve(),
            )
    else:
        logger.info(
            "[weixin-prefix-router] no routes configured — create %s",
            ROUTES_FILE,
        )


# ── hook handler ──────────────────────────────────────────────────────────

def _pre_gateway_dispatch(
    event: Any = None,
    gateway: Any = None,
    session_store: Any = None,
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """
    pre_gateway_dispatch handler.

    Two route types:
      - ``profile``: set ``event.source.profile`` + rewrite text (Hermes multiplex)
      - ``adapter``: forward to external agent via adapter, skip Hermes dispatch
    """
    # ---- 1. Only weixin platform ─────────────────────────────────────────
    source = getattr(event, "source", None)
    if source is None:
        return None

    platform = getattr(source, "platform", None)
    platform_name = ""
    try:
        platform_name = platform.value if hasattr(platform, "value") else str(platform)
    except Exception:
        platform_name = str(getattr(source, "platform", ""))

    if platform_name != "weixin":
        return None

    # ---- 2. Check prefix ─────────────────────────────────────────────────
    text = getattr(event, "text", "") or ""
    if not text:
        return None

    routes = _load_routes()
    if not routes:
        return None

    for prefix, route_cfg in routes.items():
        if not prefix:
            continue
        if not text.startswith(prefix):
            continue

        remainder = text[len(prefix):].lstrip(" \t\n\r")
        route_type = route_cfg.get("type", "profile")

        # ── Profile route (Hermes multiplex) ──────────────────────────────
        if route_type == "profile":
            profile_name = route_cfg.get("profile", "")
            # Safety: profile must be a non-empty string (dicts cause
            # TypeError: unhashable type in authz_mixin._pairing_store_for)
            if not isinstance(profile_name, str) or not profile_name:
                logger.warning(
                    "[weixin-prefix-router] profile route %r has invalid profile=%r, skipping",
                    prefix, profile_name,
                )
                continue
            source.profile = profile_name
            logger.info(
                "[weixin-prefix-router] %s → profile=%s prefix=%r",
                source.user_id or "?",
                profile_name,
                prefix,
            )
            return {"action": "rewrite", "text": remainder}

        # ── Adapter route (forward to external agent) ─────────────────────
        if route_type == "adapter":
            adapter_path = route_cfg.get("path", "")
            if not adapter_path:
                logger.warning(
                    "[weixin-prefix-router] adapter route %r missing 'path'",
                    prefix,
                )
                continue

            logger.info(
                "[weixin-prefix-router] %s → adapter=%s prefix=%r text=%r",
                source.user_id or "?",
                adapter_path,
                prefix,
                remainder[:80],
            )

            # Schedule async forwarding, skip Hermes dispatch
            asyncio.create_task(
                _forward_to_adapter(
                    text=remainder,
                    chat_id=source.chat_id or "",
                    user_id=source.user_id or "",
                    adapter_path=adapter_path,
                    gateway=gateway,
                    source=source,
                )
            )
            return {"action": "skip"}

        logger.warning(
            "[weixin-prefix-router] unknown route type %r for prefix %r",
            route_type,
            prefix,
        )

    # No prefix matched → normal dispatch
    return None