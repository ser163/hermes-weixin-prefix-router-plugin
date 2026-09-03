"""
Maka adapter — weixin-prefix-router v0.3.0 (wechat-bridge mode).

Apache Maka (Incubating): local-first AI agent workspace.
https://github.com/apache/maka

This adapter integrates with Maka through a LOCAL iLink-compatible bridge
(``bridge.py`` in this directory). Maka's built-in WeChat bot channel is
configured to connect to the bridge instead of the real iLink server, so
the QR-code onboarding is replaced by a verification code flow.

Required interface (exported by every adapter):
    async def handle(text: str, chat_id: str = "", user_id: str = "") -> str
"""
from __future__ import annotations

import asyncio
import os

import aiohttp

# ── bridge endpoint config ────────────────────────────────────────────────
BRIDGE_URL = os.getenv("MAKA_BRIDGE_URL", "http://127.0.0.1:19890")
REQUEST_TIMEOUT = 150          # must be longer than the bridge's 120s wait


async def _bridge_alive() -> bool:
    """Quick health check against the local bridge."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BRIDGE_URL}/bridge/status", timeout=5) as resp:
                return resp.status == 200
    except Exception:
        return False


async def handle(text: str, chat_id: str = "", user_id: str = "") -> str:
    """Submit the message to the bridge and wait for Maka's reply."""
    if not await _bridge_alive():
        return "⚠️ Maka 桥接服务未运行。请在 E:\\test\\ai\\maka 下启动: python bridge.py"

    payload = {
        "text": text,
        "chat_id": chat_id,
        "user_id": user_id,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{BRIDGE_URL}/bridge/inbound",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            data = await resp.json()

    if data.get("ret") == 0:
        return data.get("text", "")
    if data.get("errmsg") == "timeout":
        return "⚠️ Maka 处理超时（120秒无响应）"
    return f"⚠️ Maka 桥接错误: {data.get('errmsg', 'unknown')}"


if __name__ == "__main__":
    print(asyncio.run(handle("测试消息", "cli", "cli")))