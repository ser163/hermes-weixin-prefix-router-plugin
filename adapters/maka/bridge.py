"""
wechat-bridge — fake iLink server for Maka bot channel integration.

Acts as a local iLink-compatible endpoint that Maka's wechat bot channel
connects to instead of ``ilinkai.weixin.qq.com``. Bridges messages between
Maka's agent and Hermes's WeChat gateway.

Usage:
    python bridge.py              # start on default port 19890
    python bridge.py --port 9090
    python bridge.py --port 9090 --onboard  # generate onboarding verification code

Architecture::

    Hermes WeChat → plugin → adapter → POST /bridge/inbound ─┐
                                                                 v
    ┌──────────────── wechat-bridge (port 19890) ─────────────────┐
    │  inbound_queue: [msg1, msg2, ...]           outbound_store │
    │  ┌─ iLink endpoints (for Maka) ───┐  ┌─ internal endpoints ┐│
    │  │ POST /ilink/bot/getconfig      │  │ POST /bridge/inbound  ││
    │  │ POST /ilink/bot/getupdates     │  │ GET  /bridge/outbound ││
    │  │ POST /ilink/bot/sendmessage    │  │ POST /bridge/onboard  ││
    │  └────────────────────────────────┘  └──────────────────────┘│
    └──────────────────────────────────────────────────────────────┘
              │
              v (Maka polls getupdates → gets message)
    Maka agent processes → sends reply via sendmessage
              │
              v (bridge captures reply → Hermes polls outbound)
    Hermes plugin → WeChat gateway → user
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import string
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    aiohttp = None
    web = None

logger = logging.getLogger("wechat-bridge")

# ── defaults ──────────────────────────────────────────────────────────────
DEFAULT_PORT = 19890
ONBOARD_TIMEOUT = 300          # 5 minutes for verification code
POLL_TIMEOUT = 30              # getupdates long-poll wait
ACCOUNT_ID = "wechat-bridge"   # Maka's bot identity (like b5b33621)


# ── data types ────────────────────────────────────────────────────────────

@dataclass
class InboundMessage:
    """Message from Hermes WeChat gateway, waiting for Maka to process."""
    text: str
    chat_id: str
    user_id: str
    request_id: str = ""
    queued_at: float = 0.0

    def to_ilink(self) -> Dict[str, Any]:
        """Convert to iLink getupdates message format."""
        return {
            "from_user_id": self.user_id or self.chat_id,
            "to_user_id": self.chat_id,
            "client_id": self.request_id or "hermes",
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": self.text}}],
            "context_token": f"bridge-ctx-{self.request_id}",
        }


@dataclass
class OutboundResponse:
    """Maka's reply, waiting for Hermes plugin to pick up."""
    text: str
    request_id: str
    replied_at: float = 0.0


# ── bridge state ──────────────────────────────────────────────────────────

class BridgeState:
    """Shared state for the bridge server."""

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: Dict[str, asyncio.Future] = {}
        self._onboard_code: Optional[str] = None
        self._onboard_created: float = 0
        self._bot_token: Optional[str] = None
        self._context_tokens: Dict[str, str] = {}

    # ── onboarding ────────────────────────────────────────────────────────

    def generate_onboard_code(self) -> str:
        """Generate a 6-digit verification code for Maka onboarding."""
        code = "".join(random.choices(string.digits, k=6))
        self._onboard_code = code
        self._onboard_created = time.time()
        logger.info("onboarding code generated: %s", code)
        return code

    def verify_onboard_code(self, code: str) -> bool:
        """Verify the onboarding code and promote it to the bot token.

        Maka's WeChat channel is configured with the verification code as
        its bot token. When Maka calls getconfig with
        ``Authorization: Bearer <code>``, we validate the code and then
        accept it as the long-lived bot token for all subsequent calls.
        """
        if not self._onboard_code:
            return False
        if time.time() - self._onboard_created > ONBOARD_TIMEOUT:
            logger.warning("onboarding code expired")
            return False
        if self._onboard_code != code.strip():
            return False
        # Code verified — it becomes the bot token itself
        self._bot_token = code.strip()
        self._onboard_code = None  # one-time use
        logger.info("onboarding verified — verification code promoted to bot token")
        return True

    @property
    def is_authorized(self) -> bool:
        return self._bot_token is not None

    def check_auth(self, auth_header: str = "") -> bool:
        """Check if the request is authorized (Bearer token match)."""
        if not self._bot_token:
            return False
        expected = f"Bearer {self._bot_token}"
        return auth_header.strip() == expected


# ── iLink endpoints (for Maka) ────────────────────────────────────────────

async def _require_auth(request: web.Request, state: BridgeState) -> bool:
    """Check that the request carries a valid bot token.

    Returns True if authorized. If not, the response has already been sent.
    """
    if not state.is_authorized:
        # Already handled by the calling handler — just return False
        return False
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {state._bot_token}"
    if auth.strip() == expected:
        return True
    return False


async def handle_getconfig(request: web.Request) -> web.Response:
    """GETCONFIG: validate token and return config."""
    state: BridgeState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        body = {}

    ilink_user_id = body.get("ilink_user_id", "")

    # If not yet authorized, try to onboard using the verification code
    # as the bearer token (Maka sets the code as the bot token)
    auth = request.headers.get("Authorization", "")
    if not state.is_authorized:
        if auth.startswith("Bearer "):
            code = auth[len("Bearer "):]
            if state.verify_onboard_code(code):
                return web.json_response({
                    "ret": 0,
                    "typing_ticket": f"bridge-onboard-ok-{ACCOUNT_ID}",
                })
        # Check if we're still in onboarding — return "need code" info
        return web.json_response({
            "ret": -2,
            "errmsg": "onboarding_required",
            "bridge_info": {
                "onboarding_url": f"http://127.0.0.1:{request.app['port']}/bridge/onboard",
                "method": "POST",
                "description": "Send POST to /bridge/onboard to get a verification code, "
                               "then enter it as the bot token in Maka's WeChat channel settings.",
            },
        })

    # Authorized — validate token
    if not await _require_auth(request, state):
        return web.json_response({"ret": -2, "errmsg": "invalid_token"})

    # Normal getconfig
    return web.json_response({
        "ret": 0,
        "typing_ticket": f"bridge-tt-{ACCOUNT_ID}-{int(time.time())}",
        "account_id": ACCOUNT_ID,
    })


async def handle_getupdates(request: web.Request) -> web.Response:
    """GETUPDATES: long-poll, wait for inbound messages from Hermes."""
    state: BridgeState = request.app["state"]
    if not state.is_authorized:
        return web.json_response({"ret": -2, "errmsg": "not_authorized"})
    if not await _require_auth(request, state):
        return web.json_response({"ret": -2, "errmsg": "invalid_token"})

    try:
        # Wait for a message from the inbound queue (with timeout)
        message = await asyncio.wait_for(
            state.inbound.get(), timeout=POLL_TIMEOUT
        )
    except asyncio.TimeoutError:
        # No messages — return empty (Maka will re-poll)
        return web.json_response({"ret": 0, "messages": []})

    # Return the message in iLink format
    msg_data = message.to_ilink()
    # Store context_token for later use
    state._context_tokens[message.request_id] = msg_data["context_token"]

    return web.json_response({
        "ret": 0,
        "messages": [msg_data],
    })


async def handle_sendmessage(request: web.Request) -> web.Response:
    """SENDMESSAGE: capture Maka's reply for the Hermes plugin."""
    state: BridgeState = request.app["state"]
    if not state.is_authorized:
        return web.json_response({"ret": -2, "errmsg": "not_authorized"})
    if not await _require_auth(request, state):
        return web.json_response({"ret": -2, "errmsg": "invalid_token"})

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ret": -2, "errmsg": "invalid_json"})

    msg = body.get("msg", {})
    item_list = msg.get("item_list", [])
    reply_text = ""
    for item in item_list:
        if item.get("type") == 1:  # text
            text_item = item.get("text_item", {})
            reply_text = text_item.get("text", "")

    # Extract the context_token to match request_id
    context_token = msg.get("context_token", "")
    request_id = ""
    for rid, ctx in state._context_tokens.items():
        if ctx == context_token:
            request_id = rid
            break

    if not request_id:
        # Try to find from message content
        request_id = msg.get("client_id", f"reply-{int(time.time())}")

    # Store the response
    if request_id in state.outbound:
        future = state.outbound.pop(request_id)
        if not future.done():
            future.set_result(OutboundResponse(
                text=reply_text,
                request_id=request_id,
                replied_at=time.time(),
            ))
        logger.info("reply captured for request=%s: %d chars", request_id, len(reply_text))

    return web.json_response({
        "message_id": int(time.time() * 1000),
    })


# ── internal endpoints (for Hermes plugin) ────────────────────────────────

async def handle_inbound(request: web.Request) -> web.Response:
    """INBOUND: Hermes plugin submits a message to be processed by Maka."""
    state: BridgeState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ret": -1, "errmsg": "invalid_json"})

    text = body.get("text", "")
    chat_id = body.get("chat_id", "")
    user_id = body.get("user_id", "")
    request_id = f"{chat_id}-{int(time.time() * 1000)}"

    if not text:
        return web.json_response({"ret": -1, "errmsg": "text_required"})

    # Create a future for the outbound response
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    state.outbound[request_id] = future

    # Queue the message for Maka to pick up
    msg = InboundMessage(
        text=text,
        chat_id=chat_id,
        user_id=user_id,
        request_id=request_id,
        queued_at=time.time(),
    )
    await state.inbound.put(msg)

    # Wait for Maka's reply (with timeout)
    try:
        response = await asyncio.wait_for(future, timeout=120)
        logger.info("inbound reply ready: request=%s text=%r", request_id, response.text[:80])
        return web.json_response({
            "ret": 0,
            "text": response.text,
            "request_id": request_id,
        })
    except asyncio.TimeoutError:
        state.outbound.pop(request_id, None)
        logger.warning("inbound timeout: request=%s", request_id)
        return web.json_response({"ret": -1, "errmsg": "timeout"})


async def handle_outbound(request: web.Request) -> web.Response:
    """OUTBOUND: Hermes plugin polls for a specific request's reply."""
    state: BridgeState = request.app["state"]
    request_id = request.match_info.get("request_id", "")

    if not request_id or request_id not in state.outbound:
        return web.json_response({"ret": -1, "errmsg": "not_found"})

    future = state.outbound[request_id]
    try:
        response = await asyncio.wait_for(future, timeout=120)
        return web.json_response({
            "ret": 0,
            "text": response.text,
            "request_id": request_id,
        })
    except asyncio.TimeoutError:
        state.outbound.pop(request_id, None)
        return web.json_response({"ret": -1, "errmsg": "timeout"})


async def handle_onboard(request: web.Request) -> web.Response:
    """ONBOARD: generate a verification code for Maka onboarding."""
    state: BridgeState = request.app["state"]
    code = state.generate_onboard_code()
    return web.json_response({
        "ret": 0,
        "verification_code": code,
        "expires_in": ONBOARD_TIMEOUT,
        "instructions": (
            f"Enter the verification code '{code}' in Maka's WeChat bot "
            f"channel onboarding to complete authorization."
        ),
    })


async def handle_status(request: web.Request) -> web.Response:
    """STATUS: bridge health check."""
    state: BridgeState = request.app["state"]
    return web.json_response({
        "ret": 0,
        "status": "running",
        "authorized": state.is_authorized,
        "inbound_queue_size": state.inbound.qsize(),
        "outbound_pending": len(state.outbound),
        "port": request.app["port"],
    })


# ── server setup ──────────────────────────────────────────────────────────

async def create_app(port: int = DEFAULT_PORT) -> web.Application:
    """Create and configure the aiohttp web application."""
    app = web.Application()
    app["state"] = BridgeState()
    app["port"] = port

    # iLink endpoints (Maka calls these)
    app.router.add_post("/ilink/bot/getconfig", handle_getconfig)
    app.router.add_post("/ilink/bot/getupdates", handle_getupdates)
    app.router.add_post("/ilink/bot/sendmessage", handle_sendmessage)

    # Internal endpoints (Hermes plugin calls these)
    app.router.add_post("/bridge/inbound", handle_inbound)
    app.router.add_get("/bridge/outbound/{request_id}", handle_outbound)
    app.router.add_post("/bridge/onboard", handle_onboard)
    app.router.add_get("/bridge/status", handle_status)

    return app


def run_bridge(port: int = DEFAULT_PORT) -> None:
    """Run the bridge server."""
    if aiohttp is None:
        print("ERROR: aiohttp is required. Install with: pip install aiohttp")
        return

    app = asyncio.run(create_app(port))
    print(f"[wechat-bridge] listening on http://127.0.0.1:{port}")
    print(f"[wechat-bridge] iLink endpoints: /ilink/bot/{{getconfig,getupdates,sendmessage}}")
    print(f"[wechat-bridge] internal endpoints: /bridge/{{inbound,outbound,onboard,status}}")
    web.run_app(app, host="127.0.0.1", port=port)


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="wechat-bridge: fake iLink server for Maka")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Listen port (default: {DEFAULT_PORT})")
    parser.add_argument("--onboard", action="store_true", help="Generate onboarding code and exit")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if args.onboard:
        state = BridgeState()
        code = state.generate_onboard_code()
        print(f"Verification code: {code}")
        print(f"Valid for: {ONBOARD_TIMEOUT}s")
        print(f"POST /bridge/onboard on the running server for new codes")
        return

    run_bridge(port=args.port)


if __name__ == "__main__":
    main()