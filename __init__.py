"""
WeChat prefix router plugin.

Intercepts inbound WeChat messages and routes them to different Hermes
profiles based on a fixed prefix in the message text.

Configuration (routes.json in same directory):
    {
        "@coder": "coder",
        "/zcode": "zcode"
    }

Each key is a prefix; each value is the target profile name.
Messages starting with a prefix are routed to that profile's session.
Messages without a matching prefix go to the default Hermes agent.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────
ROUTES_FILE = Path(__file__).parent / "routes.json"


def _load_routes() -> Dict[str, str]:
    """Load prefix→profile mapping from routes.json."""
    try:
        if ROUTES_FILE.exists():
            data = json.loads(ROUTES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        logger.warning("[weixin-prefix-router] failed to load routes.json: %s", exc)
    return {}


# ── plugin entry point ────────────────────────────────────────────────────

def register(ctx) -> None:
    """Register the pre_gateway_dispatch hook."""
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    routes = _load_routes()
    if routes:
        logger.info(
            "[weixin-prefix-router] loaded %d route(s): %s",
            len(routes),
            list(routes.keys()),
        )
    else:
        logger.info(
            "[weixin-prefix-router] no routes configured — create %s with prefix→profile mappings",
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

    Fired BEFORE auth/pairing and agent dispatch for every inbound message.
    If the message starts with a configured prefix, we:
      1. Set event.source.profile → target profile name
      2. Return {"action": "rewrite", "text": <stripped text>}

    The rewritten text (without the prefix) is what the target profile's
    agent sees. The source.profile change drives session-key namespacing
    and profile-scoped agent runtime (requires multiplex_profiles: true).
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

    for prefix, profile_name in routes.items():
        if not prefix or not profile_name:
            continue
        if text.startswith(prefix):
            remainder = text[len(prefix) :].lstrip(" \t\n\r")
            # Route to target profile
            source.profile = profile_name
            logger.info(
                "[weixin-prefix-router] %s → profile=%s prefix=%r text=%r",
                source.user_id or "?",
                profile_name,
                prefix,
                remainder[:80],
            )
            return {"action": "rewrite", "text": remainder}

    # No prefix matched → normal dispatch
    return None