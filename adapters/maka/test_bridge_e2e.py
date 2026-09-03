"""Concurrent end-to-end test for wechat-bridge (simulates both sides)."""
import asyncio
import json

import aiohttp

BASE = "http://127.0.0.1:19890"


async def hermes_plugin_submit(session, text: str) -> dict:
    """Simulate Hermes plugin: submit message, wait for reply (blocks)."""
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{BASE}/bridge/inbound",
            json={"text": text, "chat_id": "wx_user1", "user_id": "wx_user1"},
            timeout=60,
        ) as resp:
            return await resp.json()


async def maka_poll_and_reply(session, auth_header: str) -> None:
    """Simulate Maka: poll getupdates, then send reply via sendmessage."""
    async with aiohttp.ClientSession() as s:
        headers = {"Authorization": auth_header}
        # Poll getupdates until a message arrives
        for attempt in range(30):
            async with s.post(f"{BASE}/ilink/bot/getupdates", json={}, headers=headers) as resp:
                data = await resp.json()
                messages = data.get("messages", [])
                if messages:
                    ctx = messages[0].get("context_token", "")
                    for item in messages[0].get("item_list", []):
                        if item.get("type") == 1:
                            received = item["text_item"]["text"]
                    reply = {
                        "msg": {
                            "from_user_id": "maka-test",
                            "to_user_id": "wx_user1",
                            "client_id": "maka-test",
                            "message_type": 2,
                            "message_state": 2,
                            "item_list": [{
                                "type": 1,
                                "text_item": {"text": f"Maka收到: {received}"},
                            }],
                            "context_token": ctx,
                        }
                    }
                    async with s.post(f"{BASE}/ilink/bot/sendmessage", json=reply, headers=headers) as r2:
                        await r2.json()
                    print(f"[maka] polled msg: {received!r}, replied OK")
                    return
            await asyncio.sleep(0.5)
        print("[maka] poll timed out - no messages in 15s")


async def main():
    async with aiohttp.ClientSession() as session:
        # 1. Onboard → get code → authorize
        async with session.post(f"{BASE}/bridge/onboard") as resp:
            code = (await resp.json())["verification_code"]
            print(f"[setup] onboard code: {code}")

        async with session.post(
            f"{BASE}/ilink/bot/getconfig",
            json={"ilink_user_id": "maka-test"},
            headers={"Authorization": f"Bearer {code}"},
        ) as resp:
            auth = await resp.json()
            print(f"[setup] authorize: {auth}")
            assert auth["ret"] == 0

        # 2. Concurrent: Hermes submits (waits for reply) + Maka polls/replies
        submit_task = asyncio.create_task(
            hermes_plugin_submit(session, "请分析一下今天的任务")
        )
        await asyncio.sleep(0.5)  # let inbound land
        poll_task = asyncio.create_task(
            maka_poll_and_reply(session, f"Bearer {code}")
        )

        # Wait for both
        results = await asyncio.gather(submit_task, poll_task)
        print(f"\n[hermes] reply received: {results[0]}")
        assert results[0]["ret"] == 0
        assert "Maka收到" in results[0]["text"]
        print("\n=== E2E TEST PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())