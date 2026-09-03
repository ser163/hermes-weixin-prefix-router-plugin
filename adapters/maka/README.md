# Maka × Hermes 微信桥接（wechat-bridge）

> 通过本地 iLink 兼容服务，让 **Maka 桌面版** 复用 **Hermes 微信通道** 收发消息。
> 版本：v0.3.0 · 端口：19860

---

## 1. 背景与目标

Hermes 通过腾讯 iLink Bot API（`ilinkai.weixin.qq.com`）连接微信个人号。
Maka 桌面版内置的 WeChat Bot 通道使用的**同一套 iLink 协议**。

本桥接方案的核心思路：在本地起一个**伪 iLink 服务器**（wechat-bridge），
让 Maka 的 WeChat Bot 通道连接它而非真实腾讯端点。Hermes 收到的微信消息
经插件前缀路由（`@maka`）投递给 bridge，Maka 长轮询取走消息、Agent 处理后
回复，bridge 捕获回复并原路送回微信。

```
┌──────────────┐   @maka 消息    ┌───────────────────────────┐
│  微信用户     │ ───────────────▶ │ Hermes Gateway + 插件     │
└──────────────┘                  └─────────────┬─────────────┘
      ▲                                         │ POST /bridge/inbound
      │                                         ▼
      │                              ┌─────────────────────┐
      │                              │  wechat-bridge       │
      │                              │  127.0.0.1:19860     │
      │                              │  (伪 iLink 服务器)    │
      │                              └─────────────┬─────────┘
      │                                            │ getupdates 长轮询
      │                                            ▼
      │                              ┌─────────────────────┐
      │                              │ Maka WeChat Bot 通道 │ ──▶ Maka Agent
      │                              └─────────────┬─────────┘
      │                                            ▲ sendmessage 回复
      └──────────── 回复经 Hermes 微信发出 ◄────────┘
```

**扫码 → 验证码**：Maka 官方微信通道原本需要扫码注册，本方案将其替换为
6 位随机验证码（由 bridge 签发），把验证码填入 Maka 通道的 bot token 即可
完成授权——无需真实扫码，全程本地闭环。

---

## 2. 目录结构

```
E:\test\ai\maka\
├── __init__.py           # Hermes 插件适配器：handle(text, chat_id, user_id)
├── bridge.py             # wechat-bridge 服务器（伪 iLink，端口 19860）
├── test_bridge_e2e.py    # 端到端测试（模拟 Hermes + Maka 两侧）
└── README.md             # 本文档
```

GitHub 仓库对应：`adapters/maka/`（https://github.com/ser163/hermes-weixin-prefix-router-plugin）

---

## 3. 组件说明

### 3.1 bridge.py —— 伪 iLink 服务器

| 端点 | 方向 | 用途 |
|------|------|------|
| `POST /ilink/bot/getconfig` | Maka → bridge | 授权 / onboarding（验证码即 token） |
| `POST /ilink/bot/getupdates` | Maka → bridge | 长轮询，取走 Hermes 投递的消息 |
| `POST /ilink/bot/sendmessage` | Maka → bridge | Maka 的回复，捕获后送回 Hermes |
| `POST /bridge/inbound` | Hermes → bridge | 插件提交微信消息，阻塞等待 Maka 回复 |
| `POST /bridge/onboard` | 人工 → bridge | 生成 6 位验证码（替代扫码） |
| `GET /bridge/status` | 人工 → bridge | 健康检查 |

**授权模型**（安全）：未授权时，`getconfig` 携带的 Bearer token 若匹配
最新验证码则授权成功，该验证码**晋升为长期 bot token**；此后所有 iLink
端点均需携带同一 token，错误/缺失 token 一律拒绝（`ret=-2`）。

### 3.2 __init__.py —— Hermes 插件适配器

标准适配器接口，向 bridge 的 `/bridge/inbound` 提交消息并等待回复：

```python
async def handle(text: str, chat_id: str = "", user_id: str = "") -> str
```

bridge 未启动时返回友好提示，不抛异常。

### 3.3 test_bridge_e2e.py —— 端到端测试

并发模拟双端：Hermes 提交消息（阻塞等待）↔ Maka 轮询 + 回复。

---

## 4. 安装与配置

### 4.1 前置条件

| 依赖 | 说明 |
|------|------|
| Hermes Agent | 已配置微信网关（iLink），见 `hermes gateway status` |
| Maka 桌面版 | 已安装并运行（Apache Maka Incubating） |
| Python 3.10+ | 需 `aiohttp` 库 |
| weixin-prefix-router 插件 | v0.3.0+，已启用 |

### 4.2 启动 wechat-bridge

```bash
cd E:\test\ai\maka
python bridge.py                # 默认端口 19860
python bridge.py --port 19860   # 指定端口
python bridge.py --debug        # 调试日志
```

健康检查：

```bash
curl http://127.0.0.1:19860/bridge/status
# {"ret":0,"status":"running","authorized":false,"port":19860}
```

### 4.3 获取验证码（替代扫码）

```bash
curl -X POST http://127.0.0.1:19860/bridge/onboard
# {"ret":0,"verification_code":"482913","expires_in":300,...}
```

验证码 5 分钟有效、一次性使用。

### 4.4 配置 Maka WeChat Bot 通道

Maka 桌面版 → 设置 → 远程接入（Remote access）→ WeChat 通道：

1. 若 Maka 支持自定义 base URL，填 `http://127.0.0.1:19860`
2. bot token 填入第 4.3 步的**验证码**（如 `482913`）
3. 保存后 Maka 即用该 token 调 `getconfig` → bridge 校验通过 → 通道 connected

> 若 Maka 端扫码流程无法绕过，可将 bridge 的 `/bridge/onboard` 返回的
> 验证码视作"扫码结果"人工输入，效果一致。

### 4.5 启用插件路由

确认插件路由配置（`routes.json`）：

```json
{
  "@coder": "coder",
  "@maka": {"type": "adapter", "path": "maka"}
}
```

```bash
hermes plugins enable weixin-prefix-router
hermes gateway restart
```

---

## 5. 使用

微信中给 Hermes bot 发消息：

| 消息 | 去向 |
|------|------|
| `@maka 帮我分析今天的任务` | Maka Agent 处理，回复经 bridge 回到微信 |
| `@coder 写一个排序函数` | Hermes coder profile |
| `你好` | 默认 Hermes agent |

---

## 6. 验证清单

| 检查项 | 方法 | 预期 |
|--------|------|------|
| bridge 存活 | `curl /bridge/status` | `status: running` |
| 验证码签发 | `curl -X POST /bridge/onboard` | 返回 6 位 code |
| 授权 | `getconfig` + Bearer code | `ret: 0` |
| token 校验 | `getconfig` + 错误 token | `ret: -2 invalid_token` |
| 端到端 | `python test_bridge_e2e.py` | `=== E2E TEST PASSED ===` |
| 微信实链 | 微信发 `@maka 你好` | Maka 回复送达微信 |

---

## 7. 故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| 适配器提示"桥接服务未运行" | bridge 未启动 | `python bridge.py` |
| `inbound` 超时（120s） | Maka 未轮询/未配置 | 检查 Maka 通道 connected |
| `getconfig` 报 `invalid_token` | 验证码过期或已授权锁 | 重新 `onboard`；重启 bridge 重置 |
| Maka 收不到消息 | getupdates 轮询未带 token | 确认通道 token 与 bridge 一致 |
| 端口占用 | 旧 bridge 残留 | 杀进程后重启 |
| 测试残留脏队列 | 中断的 inbound 留在队列 | 重启 bridge 清空状态 |

**重启 bridge 的正确姿势**：

```bash
# 找到占用 19860 的进程并结束
netstat -ano | findstr 19860
taskkill /PID <pid> /F
python bridge.py
```

---

## 8. 版本历史

| 版本 | 变更 |
|------|------|
| v0.3.0 | wechat-bridge 上线：伪 iLink 服务器、验证码 onboarding、token 校验、E2E 测试 |
| v0.3.0+ | 端口 19890 → 19860 |

仓库：https://github.com/ser163/hermes-weixin-prefix-router-plugin
