"""E2E test for wechat-bridge v3 (persistent token).

Simulates the full flow with the persistent token model:
  Hermes: onboard (get token) → submit inbound → wait for reply
  Maka:   GET /health → GET /messages/stream (SSE) → POST /send
"""
import asyncio
import json
import sys

import aiohttp

BASE = "http://127.0.0.1:19860"
PASS = 0
FAIL = 0

async def check(name, ok, detail=""):
    global PASS, FAIL
    if ok: PASS += 1
    else: FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name} {detail}")

async def main():
    # 1. Onboard → get persistent token
    async with aiohttp.ClientSession() as s:
        r = await s.post(f"{BASE}/bridge/onboard", timeout=5)
        d = await r.json()
        token = d.get("token", "")
        await check("onboard: token returned", d.get("ret") == 0 and len(token) >= 16,
                    f"len={len(token)}")

    # 2. /health without auth → 401
    async with aiohttp.ClientSession() as s:
        r = await s.get(f"{BASE}/health", timeout=5)
        await check("health: no auth → 401", r.status == 401)

    # 3. /health with Bearer token → 200 (Maka's real behavior)
    async with aiohttp.ClientSession() as s:
        r = await s.get(f"{BASE}/health",
            headers={"Authorization": f"Bearer {token}"}, timeout=5)
        body = await r.json()
        await check("health: with token → 200", r.status == 200,
                    f"send_status={body.get('send_status')}")
        await check("health: send_status available", body.get("send_status") == "available")

    # 4. Wrong token rejected
    async with aiohttp.ClientSession() as s:
        r = await s.get(f"{BASE}/health",
            headers={"Authorization": "Bearer bad-token"}, timeout=5)
        await check("health: bad token → 401", r.status == 401)

    # 5. Hermes submits inbound; Maka SSE-receives and replies via /send
    async def submit():
        async with aiohttp.ClientSession() as s:
            r = await s.post(f"{BASE}/bridge/inbound",
                json={"text": "测试消息", "chat_id": "wx_user", "user_id": "wx_user"},
                timeout=30)
            return await r.json()

    async def maka_sse_and_reply():
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE}/messages/stream?since=0",
                headers={"Authorization": f"Bearer {token}"}, timeout=30) as resp:
                buffer = ""
                async for chunk in resp.content.iter_any():
                    buffer += chunk.decode("utf-8", errors="replace")
                    for line in buffer.split("\n"):
                        if line.startswith("data: "):
                            try:
                                data = json.loads(line[6:])
                                if data.get("body"):
                                    reply = await s.post(f"{BASE}/send",
                                        json={"wxid": "wx_user", "text": "MakaOK"},
                                        headers={"Authorization": f"Bearer {token}"}, timeout=5)
                                    rdata = await reply.json()
                                    print(f"  [maka] replied: {rdata.get('status')}")
                                    return
                            except json.JSONDecodeError:
                                pass
                    buffer = buffer[-5000:]

    submit_task = asyncio.create_task(submit())
    await asyncio.sleep(0.5)
    sse_task = asyncio.create_task(maka_sse_and_reply())
    results = await asyncio.gather(submit_task, sse_task)

    await check("inbound got reply", results[0].get("ret") == 0,
                f"text={results[0].get('text','')!r}")
    await check("reply text matches", results[0].get("text") == "MakaOK")

    # 6. Bad token rejected on /send
    async with aiohttp.ClientSession() as s:
        r = await s.post(f"{BASE}/send",
            json={"wxid": "x", "text": "test"},
            headers={"Authorization": "Bearer bad-token"}, timeout=5)
        await check("send rejects bad token", r.status == 401)

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    asyncio.run(main())