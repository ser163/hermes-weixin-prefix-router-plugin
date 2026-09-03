"""
wechat-bridge — Maka 本地桥接服务器（v2: 本地 bridge 协议 version）

Maka 桌面版的 WeChat 通道支持两种模式：
  1. iLink 模式：webhookUrl 为 https://ilinkai.weixin.qq.com（真实腾讯）
  2. **本地 bridge 模式**（默认）：webhookUrl 为 http://127.0.0.1:PORT

本服务实现模式 2 —— Maka 期望的本地 bridge 协议端点：

  GET  /health                    → 健康检查 + 身份信息
  POST /send                      → 发送消息 ({wxid, text})
  GET  /messages/stream?since=X   → SSE 消息流（接收消息）
  GET  /api/weixin/qrcode         → 二维码信息（mock/可选）
  GET  /qrcode                    → 同上（备选路径）

  POST /bridge/inbound            → Hermes 插件投递消息（内部）
  GET  /bridge/status             → 桥接状态（内部）
  POST /bridge/onboard            → 生成验证码（内部）

认证：X-API-Key: <token> 头（token 即验证码）
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
from typing import Any, AsyncGenerator, Dict, Optional, Set

try:
    from aiohttp import web
except ImportError:
    web = None

logger = logging.getLogger("wechat-bridge")

# ── defaults ──────────────────────────────────────────────────────────────
DEFAULT_PORT = 19860
ONBOARD_TIMEOUT = 300
SSE_PING_INTERVAL = 25  # Maka 的 GET /messages/stream 长连接保活间隔


# ── data types ────────────────────────────────────────────────────────────

@dataclass
class InboundMessage:
    """Message from Hermes WeChat gateway, waiting for Maka to consume."""
    text: str
    chat_id: str
    user_id: str
    request_id: str = ""
    queued_at: float = 0.0

    def to_bridge_message(self) -> Dict[str, Any]:
        """Convert to Maka's bridge message format (SSE stream)."""
        return {
            "chatId": self.chat_id,
            "senderId": self.user_id or self.chat_id,
            "senderName": self.user_id or self.chat_id,
            "messageId": self.request_id,
            "body": self.text,
            "text": self.text,
            "timestamp": int(self.queued_at * 1000),
            "isGroup": False,
            "isMentioned": True,
            "fromSelf": False,
        }


@dataclass
class OutboundResponse:
    """Maka's reply, waiting for Hermes plugin to pick up."""
    text: str
    request_id: str
    replied_at: float = 0.0


# ── token persistence ─────────────────────────────────────────────────────

def _get_token_file() -> str:
    """Path to the persistent token file, next to bridge.py."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "wechat-bridge.token")


def _load_or_create_token() -> str:
    """Load persistent token from disk, or create a new one."""
    token_file = _get_token_file()
    if os.path.exists(token_file):
        with open(token_file, "r") as f:
            token = f.read().strip()
        if token:
            logger.info("loaded persistent token from %s", token_file)
            return token
    # Generate a new 32-char random hex token
    token = "".join(random.choices(string.hexdigits, k=32))
    with open(token_file, "w") as f:
        f.write(token + "\n")
    logger.info("created new persistent token: %s", token)
    return token


# ── bridge state ──────────────────────────────────────────────────────────

class BridgeState:
    """Shared state for the bridge server."""

    def __init__(self, token: str):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: Dict[str, asyncio.Future] = {}
        # Map chat_id → latest pending request_id (Maka's /send echoes wxid)
        self._pending_by_chat: Dict[str, str] = {}
        # Persistent token (loaded from disk or newly created)
        self._bot_token: str = token
        # SSE subscribers: set of asyncio.Queue used to push messages to
        # Maka's /messages/stream long-poll connections
        self._sse_queues: Set[asyncio.Queue] = set()

    @property
    def is_authorized(self) -> bool:
        return True  # Always authorized — token is set at startup

    def check_auth(self, api_key: str = "") -> bool:
        """Check if the token matches the persistent bot token."""
        return api_key.strip() == self._bot_token

    async def broadcast_to_sse(self, message: InboundMessage) -> None:
        """Push a message to all active SSE subscribers."""
        if not self._sse_queues:
            return
        data = json.dumps(message.to_bridge_message(), ensure_ascii=False)
        dead_queues = set()
        for q in self._sse_queues:
            try:
                await q.put(data)
            except Exception:
                dead_queues.add(q)
        self._sse_queues -= dead_queues


# ── auth middleware ───────────────────────────────────────────────────────

def _extract_token(request: web.Request) -> str:
    """Extract token from Authorization header (Maka sends Bearer token)."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    # Fallback: X-API-Key (for test compatibility)
    return request.headers.get("X-API-Key", "").strip()


def _require_auth(state: BridgeState, request: web.Request) -> bool:
    """Check if request carries a valid bot token."""
    if not state.is_authorized:
        return False
    token = _extract_token(request)
    return state.check_auth(token)


# ── Maka bridge endpoints (Maka calls these) ──────────────────────────────

async def handle_health(request: web.Request) -> web.Response:
    """GET /health — Maka 连接测试 + 健康检查。

    Maka 的 testWechatBridge() 调用此端点，携带 Authorization: Bearer <token>。
    token 是持久化到磁盘的 32 位随机字符串，bridge 启动时自动生成。
    """
    state: BridgeState = request.app["state"]
    token = _extract_token(request)

    if not state.check_auth(token):
        return web.json_response({"error": "invalid_token", "message": "Token mismatch"}, status=401)

    return web.json_response({
        "wxid": "wechat-bridge",
        "nickname": "WeChat Bridge",
        "alias": "Hermes-Maka Bridge",
        "self": {"wxid": "wechat-bridge"},
        "send_status": "available",
        "status": "running",
    })


async def handle_send(request: web.Request) -> web.Response:
    """POST /send — Maka 发送消息。

    Maka 的 sendMessage() 调用此端点，payload:
      { "wxid": "目标 chatId", "text": "消息内容" }
    返回:
      { "status": "ok", "messageId": "<id>", "svrId": "<id>" }
    """
    state: BridgeState = request.app["state"]
    if not state.is_authorized:
        return web.json_response({"error": "not_authorized"}, status=401)
    if not _require_auth(state, request):
        return web.json_response({"error": "invalid_token"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    target_wxid = body.get("wxid", "")
    text = body.get("text", "")

    if not text:
        return web.json_response({"error": "text_required"}, status=400)

    # Find the pending request for this chat_id (Maka echoes the same
    # wxid/chatId it received the message on)
    request_id = ""
    pending_by_chat = getattr(state, "_pending_by_chat", {})
    if target_wxid in pending_by_chat:
        request_id = pending_by_chat[target_wxid]

    if not request_id:
        request_id = f"reply-{int(time.time() * 1000)}"

    # Resolve the outbound future
    if request_id in state.outbound:
        future = state.outbound.pop(request_id)
        if not future.done():
            future.set_result(OutboundResponse(
                text=text,
                request_id=request_id,
                replied_at=time.time(),
            ))
        logger.info("reply captured for request=%s: %d chars", request_id, len(text))
    else:
        # Unmatched reply — store under a generated key
        logger.info("unmatched reply (no pending request): %s chars", len(text))

    message_id = int(time.time() * 1000)
    return web.json_response({
        "status": "ok",
        "messageId": str(message_id),
        "svrId": str(message_id),
    })


async def handle_messages_stream(request: web.Request) -> web.Response:
    """GET /messages/stream?since=X — SSE 消息流连接。

    Maka 会持续连接此端点，通过 SSE 接收消息。Hermes 插件投递的
    消息会被推送到此流中。
    """
    state: BridgeState = request.app["state"]
    if not state.is_authorized:
        return web.Response(status=401, text="not_authorized")
    if not _require_auth(state, request):
        return web.Response(status=401, text="invalid_token")

    since = request.query.get("since", "0")

    # Create a queue for this SSE subscriber
    queue: asyncio.Queue = asyncio.Queue()
    state._sse_queues.add(queue)

    async def sse_stream() -> AsyncGenerator[bytes, None]:
        try:
            # Send initial connected event
            yield b"event: connected\ndata: {}\n\n"

            # Also deliver any queued messages
            while not state.inbound.empty():
                try:
                    msg = state.inbound.get_nowait()
                    data = json.dumps(msg.to_bridge_message(), ensure_ascii=False)
                    yield f"data: {data}\n\n".encode("utf-8")
                except asyncio.QueueEmpty:
                    break

            # Main loop: wait for new messages or ping
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=SSE_PING_INTERVAL)
                    yield f"data: {data}\n\n".encode("utf-8")
                except asyncio.TimeoutError:
                    # SSE keepalive ping
                    yield b": ping\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            state._sse_queues.discard(queue)

    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)
    async for chunk in sse_stream():
        try:
            await resp.write(chunk)
        except (ConnectionResetError, ConnectionAbortedError):
            break
    return resp


async def handle_qrcode(request: web.Request) -> web.Response:
    """GET /api/weixin/qrcode 或 GET /qrcode — 二维码信息。

    Maka 在 onboarding 时调用，期望返回 QR 码数据。我们的 bridge
    使用验证码替代扫码，所以返回一个 mock 成功的响应。
    """
    state: BridgeState = request.app["state"]
    if not state.is_authorized:
        return web.json_response(
            {"ok": False, "error": "not_authorized",
             "hint": "请先通过 /bridge/onboard 获取验证码并配置到 Maka。"},
            status=401,
        )
    # Mock success — 扫码已由验证码替代
    return web.json_response({
        "ok": True,
        "qrcode": None,
        "expired": False,
        "loggedIn": True,
        "diagnostic": "Maka 已通过验证码授权，无需扫码。",
    })


# ── internal endpoints (for Hermes plugin) ────────────────────────────────

async def handle_inbound(request: web.Request) -> web.Response:
    """POST /bridge/inbound — Hermes 插件投递消息，等待 Maka 回复。"""
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
    state._pending_by_chat[chat_id] = request_id  # for Maka's /send lookup

    # Queue the message for Maka to pick up via SSE stream
    msg = InboundMessage(
        text=text,
        chat_id=chat_id,
        user_id=user_id,
        request_id=request_id,
        queued_at=time.time(),
    )
    await state.inbound.put(msg)
    # Also broadcast to active SSE subscribers
    await state.broadcast_to_sse(msg)

    # Wait for Maka's reply (with timeout)
    try:
        response = await asyncio.wait_for(future, timeout=600)
        logger.info("inbound reply ready: request=%s text=%r", request_id, response.text[:80])
        return web.json_response({
            "ret": 0,
            "text": response.text,
            "request_id": request_id,
        })
    except asyncio.TimeoutError:
        state.outbound.pop(request_id, None)
        state._pending_by_chat.pop(chat_id, None)
        logger.warning("inbound timeout: request=%s", request_id)
        return web.json_response({"ret": -1, "errmsg": "timeout"})


async def handle_onboard(request: web.Request) -> web.Response:
    """POST /bridge/onboard — 返回持久 token（替代扫码/验证码）。

    token 在 bridge 首次启动时生成并持久化到磁盘，重启不变。
    只需配置一次 Maka。如需更换 token，删除 wechat-bridge.token 后重启。
    """
    state: BridgeState = request.app["state"]
    return web.json_response({
        "ret": 0,
        "token": state._bot_token,
        "persistent": True,
        "instructions": (
            f"在 Maka 的 WeChat 通道设置中：\n"
            f"1. webhookUrl: http://127.0.0.1:{request.app['port']}\n"
            f"2. Bot Token: {state._bot_token}\n"
            f"token 持久化保存，重启不换，只需配置一次。"
        ),
    })


async def handle_status(request: web.Request) -> web.Response:
    """GET /bridge/status — 桥接状态。"""
    state: BridgeState = request.app["state"]
    return web.json_response({
        "ret": 0,
        "status": "running",
        "authorized": state.is_authorized,
        "inbound_queue_size": state.inbound.qsize(),
        "outbound_pending": len(state.outbound),
        "sse_connections": len(state._sse_queues),
        "port": request.app["port"],
    })


# ── server setup ──────────────────────────────────────────────────────────

async def create_app(port: int = DEFAULT_PORT, token: str = "") -> web.Application:
    """Create and configure the aiohttp web application."""
    app = web.Application()
    app["state"] = BridgeState(token)
    app["port"] = port

    # Maka bridge protocol endpoints
    app.router.add_get("/health", handle_health)
    app.router.add_post("/send", handle_send)
    app.router.add_get("/messages/stream", handle_messages_stream)
    app.router.add_get("/api/weixin/qrcode", handle_qrcode)
    app.router.add_get("/qrcode", handle_qrcode)

    # Internal endpoints (Hermes plugin)
    app.router.add_post("/bridge/inbound", handle_inbound)
    app.router.add_post("/bridge/onboard", handle_onboard)
    app.router.add_get("/bridge/status", handle_status)

    return app


def run_bridge(port: int = DEFAULT_PORT) -> None:
    """Run the bridge server."""
    if web is None:
        print("ERROR: aiohttp is required. Install with: pip install aiohttp")
        return

    token = _load_or_create_token()
    app = asyncio.run(create_app(port, token))
    print(f"[wechat-bridge] listening on http://127.0.0.1:{port}")
    print(f"[wechat-bridge] Maka bridge endpoints: /health /send /messages/stream /qrcode")
    print(f"[wechat-bridge] internal endpoints: /bridge/{{inbound,onboard,status}}")
    print(f"[wechat-bridge] auth: Authorization: Bearer <token>")
    print(f"[wechat-bridge] TOKEN: {token}")
    print(f"[wechat-bridge] 在 Maka WeChat 通道填入：webhookUrl=http://127.0.0.1:{port}, Bot Token={token}")
    web.run_app(app, host="127.0.0.1", port=port)


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="wechat-bridge — Maka 本地桥接服务器")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口 (default: {DEFAULT_PORT})")
    parser.add_argument("--debug", action="store_true", help="调试日志")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    run_bridge(port=args.port)


if __name__ == "__main__":
    main()