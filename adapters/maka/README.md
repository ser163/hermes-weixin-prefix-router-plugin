# Maka × Hermes 微信桥接（wechat-bridge）

> 通过本地桥接服务，让 **Maka 桌面版** 复用 **Hermes 微信通道** 收发消息。
> 版本：v0.3.1 · 端口：19860 · Maka 本地 bridge 协议

---

## 1. 背景与目标

Hermes 通过腾讯 iLink Bot API（`ilinkai.weixin.qq.com`）连接微信个人号。
Maka 桌面版的 WeChat Bot 通道支持 **local bridge 模式**：不连腾讯服务器，
而是连一个本地 wechat-bridge 进程。

本方案在本地实现该 bridge（`bridge.py`），让 Maka 的 Agent 能接收 Hermes
微信通道的消息、处理后经同一会话回复。**Maka 不需要任何真实微信凭据**，
微信连接完全由 Hermes 持有。

```
┌──────────────┐   @maka 消息    ┌───────────────────────────┐
│  微信用户     │ ───────────────▶ │ Hermes Gateway + 插件     │
└──────────────┘                  └─────────────┬─────────────┘
      ▲                                         │ POST /bridge/inbound
      │                                         │ (阻塞等回复, 600s)
      │                                         ▼
      │                              ┌─────────────────────┐
      │                              │  wechat-bridge       │
      │                              │  127.0.0.1:19860     │
      │                              │  (本地 bridge 服务)   │
      │                              └─────────────┬─────────┘
      │                                            │ SSE /messages/stream
      │                                            ▼
      │                              ┌─────────────────────┐
      │                              │ Maka WeChat Bot 通道 │ ──▶ Maka Agent
      │                              └─────────────┬─────────┘
      │                                            ▲ POST /send (回复)
      │                                            │
      └──────────── 回复经 Hermes 微信发出 ◄────────┘
```

**扫码 → 持久 token**：Maka 官方通道需要扫码登录；本方案启动时自动生成
32 位随机 token（持久化到磁盘），填入 Maka 通道即可授权，重启不变。

---

## 2. 目录结构

```
adapters/maka/  （运行时镜像：E:\test\ai\maka\）
├── __init__.py           # Hermes 插件适配器：提交消息 + 等回复
├── bridge.py             # wechat-bridge 服务器（Maka 本地 bridge 协议）
├── test_bridge_e2e.py    # 端到端测试（8 项断言）
├── wechat-bridge.token   # 持久 token（自动生成，勿提交）
└── README.md             # 本文档
```

---

## 3. 组件说明

### 3.1 bridge.py —— Maka 本地 bridge 服务器

| 端点 | 方向 | 用途 |
|------|------|------|
| `GET /health` | Maka → bridge | 连接测试 + 身份信息 |
| `GET /messages/stream` | Maka → bridge | SSE 长连接，投递微信消息 |
| `POST /send` | Maka → bridge | Maka 回复（`{wxid, text}`）→ 捕获 |
| `GET /qrcode` | Maka → bridge | 扫码登录桩（实际用 token 授权） |
| `POST /bridge/inbound` | Hermes → bridge | 插件提交消息，阻塞等回复（600s） |
| `POST /bridge/onboard` | 管理 → bridge | 查看持久 token |
| `GET /bridge/status` | 管理 → bridge | 健康检查 + 队列深度 + SSE 连接数 |

**认证**：所有端点要求 `Authorization: Bearer <token>`。token 为启动时
生成的持久 32 位字符串（存于 `wechat-bridge.token`）。

**设计要点**：
- `senderName`/`chatId` 取自微信消息（无昵称时用 ID，iLink 协议限制）
- 消息经 SSE 流推送；inbound 请求阻塞等待 Maka 回复（超时 600s，适配
  Maka 深度思考模型的慢响应）
- 回复通过 `_pending_by_chat`（chat_id → request_id）匹配回原始请求

### 3.2 __init__.py —— Hermes 插件适配器

标准适配器接口，向 bridge 提交消息并等待回复：

```python
async def handle(text: str, chat_id: str = "", user_id: str = "") -> str
```

bridge 未启动时返回友好提示，不抛异常。超时 620s（必须大于 bridge 的
600s 等待）。

### 3.3 test_bridge_e2e.py —— 端到端测试

8 项断言：token 签发、无 token 401、带 token 200、错误 token 拒绝、
inbound→SSE→send 全链路往返、回复匹配。

---

## 4. 安装与配置

### 4.1 前置条件

| 依赖 | 说明 |
|------|------|
| Hermes Agent | 已配置微信网关（iLink），`hermes gateway status` 正常 |
| Maka 桌面版 | 已安装运行（Apache Maka Incubating，0.2.x） |
| Python 3.10+ | 需 `aiohttp` |
| weixin-prefix-router 插件 | v0.3.x，已启用 |

### 4.2 启动 wechat-bridge

```bash
cd E:\test\ai\maka
python bridge.py                  # 默认端口 19860
python bridge.py --port 19860     # 指定端口
python bridge.py --debug          # 调试日志
```

首次启动生成 token：

```
[wechat-bridge] TOKEN: 5FF1EA83Cfd04164bEE8bB9b48B6fAf5
[wechat-bridge] 在 Maka WeChat 通道填入：webhookUrl=http://127.0.0.1:19860, Bot Token=5FF1EA83Cfd04164bEE8bB9b48B6fAf5
```

健康检查：

```bash
curl http://127.0.0.1:19860/bridge/status
# {"ret":0,"status":"running","authorized":true,"sse_connections":1,...}
```

### 4.3 配置 Maka WeChat Bot 通道

Maka 桌面版 → 设置 → 远程接入（Remote access）→ WeChat 通道：

| 字段 | 值 |
|------|-----|
| **webhookUrl** | `http://127.0.0.1:19860` |
| **Bot Token** | 上一步的 token |

保存后 Maka 调 `GET /health`（带 Bearer token）→ bridge 校验 → 通道
显示 connected。**务必启用（enable）通道**，Maka 才会开始监听 SSE 消息流。

### 4.4 启用插件路由

`routes.json`：

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
| `@maka 帮我分析今天的任务` | Maka Agent 处理，回复经 bridge 回微信 |
| `@coder 写一个排序函数` | Hermes coder profile |
| `你好` | 默认 Hermes agent（scnet/DeepSeek） |

---

## 6. 验证清单

| 检查项 | 方法 | 预期 |
|--------|------|------|
| bridge 存活 | `curl /bridge/status` | `status: running, authorized: true` |
| Maka 已连接 | 同上 | `sse_connections ≥ 1` |
| 端到端（模拟） | `python test_bridge_e2e.py` | `RESULT: 8 passed, 0 failed` |
| 微信实链 | 微信发 `@maka 你好` | Maka 思考后回复送达微信 |
| 无前缀消息 | 微信发普通消息 | Hermes 默认 agent 正常回复 |
| 慢模型回复 | Maka 深度思考（数分钟） | 600s 内正常送达（不再 timeout） |

---

## 7. 故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| 适配器提示"桥接服务未运行" | bridge 未启动 | `python bridge.py` |
| Maka 连接测试失败 | token 不匹配 / bridge 未运行 | 重启 bridge；核对 token |
| `sse_connections: 0` | Maka 通道未启用 | Maka 设置里 enable WeChat 通道 |
| `inbound timeout`（600s） | Maka 模型超时 / 通道禁用 | 检查 Maka enabled；重发 |
| `unmatched reply` | bridge 重启后旧请求被清 | 重新发送消息 |
| Maka 显示 `[微信:长ID]` | iLink 协议无昵称字段 | 预期行为（协议限制） |
| 微信无响应 + provider 401 | 默认 profile provider 配置错 | `hermes config set model.provider scnet` |
| 端口占用 | 旧 bridge 残留 | 杀进程后重启 |

**重启 bridge**：

```bash
netstat -ano | findstr 19860
taskkill /PID <pid> /F
python bridge.py
```

---

## 8. 版本历史

| 版本 | 变更 |
|------|------|
| v0.3.1 | 文档修正（本地 bridge 协议 + 持久 token） |
| v0.3.0 | wechat-bridge 上线；随后修正为 Maka 本地 bridge 协议、持久 token、Bearer 认证、600s 超时 |

仓库：https://github.com/ser163/hermes-weixin-prefix-router-plugin
