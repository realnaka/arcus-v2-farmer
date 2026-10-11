# Arcus 协议事实（实测记录）

写代码最容易踩的坑。每条都标注了验证方式 —— **标注「实测」的都真的在测试网或主网跑过**，
不是从文档抄的。

---

## 1. 签名与时间

| 事实 | 细节 |
|---|---|
| API key | Ed25519。注册走 `POST /v1/createApiKey`，需要 EIP-712 签名 |
| `validUntil` | 必须落在 `now + 1d` ~ `now + 180d`。**API key 最长 180 天，要轮换** |
| key 绑定 | 创建时绑定 `accountIndex`（0–9 单子账户，255 全部子账户） |
| 时间戳 | `X-Timestamp` 用**纳秒**，与服务器漂移必须 ≤ 30s |
| 签名 payload | key 排序 + 紧凑 JSON；`placeOrder` 签 `{ad,ai,c,ct,g,m,op,p,q,r,s,t,v}` |
| `p` / `q` | 必须是 tick / quantum 的**整数倍**，不是小数 |

## 2. `goodTilTime` 是硬门槛（实测）

- 每个订单**必填**，且必须是 **≥ 1 个月**的未来时间，否则一律被拒。
- 签名 payload 里的 `g` 是**纳秒**（= 请求体里的微秒 × 1000）。搞错单位会得到「签名无效」，
  而不是「时间不合法」—— 很费时间的一个坑。

## 3. WebSocket 订阅语义（实测）

**订阅帧的 `id` 就是标的或地址，没有独立的 `market` / `address` 字段：**

```json
{"type":"subscribe","channel":"l2Orderbook","id":"QQQ-USD","nLevels":5}
{"type":"subscribe","channel":"orders","id":"0x<address>"}
```

传一个 `market` 字段会让服务端把 `id` 当标的解析并报 `Invalid market`。

其他：

- **下单是异步的**：返回 `202`，订单终态只在 `orders` / `userFills` 频道出现
  ⇒ **WS 是必需项，不是优化**。
- `markets` 快照是**字典**（key 为 marketId 字符串）；
  `orders` / `userFills` 快照是 `openOrders` / `fills` **数组**。
- 断线要**重订阅 + 重建快照**，并检查序列缺口。

## 4. REST 与限流

| 事实 | 细节 |
|---|---|
| 限流端点 | `/v1/rateLimit`（**不是** `rateLimitUsage`） |
| 预算 | 每 IP 1500 tokens，25/s 回血 ⇒ **多账号同机共享同一预算** |
| `openOrders` | 单次读就扣 20 权重；多进程轮询会在 30 秒内打满 |
| 下单/撤单 | IP 权重 0 ⇒ 这正是一个「WS 订阅 + REST 下单」架构的原因 |
| `cancelAllOrders` | 固定扣撤单池 1,000 |
| 429 | 读 `retryAfterMs` / `reason` 后按池重试 |

## 5. 死手开关：文档矛盾已实测解决

官方两份文档互相矛盾：接口文档详细描述 `POST /v1/scheduleCancel`，
WebSocket 概览页却写「Arcus has no cancel-on-disconnect or dead-man's-switch」。

用全新钱包在测试网实测（2026-10-04）：

```
scheduleCancel arm    -> {"status": "scheduled", "time": 1791123162197000,
                          "rateLimit": {"pool": "cancel", "remaining": 39999}}
scheduleCancel disarm -> {"status": "disarmed", "rateLimit": {"remaining": 39998}}
cancelAllOrders       -> {"status": "CANCEL_ALL_ACKNOWLEDGED", "remaining": 38998}
```

**结论：功能存在且可用，WebSocket 概览页那句是过期文本。** 成本也被实测确认：

- 每次武装/解除扣撤单池 1；
- 触发时扣 1,000；
- 每日每子账户最多自动触发 10 次。

`time` 是**绝对 epoch 微秒**，提前量 5s–5min。

> 同一次演练还顺带验证了签名正确性：在**未入金**的账户上 `placeOrder` 返回了 `orderId`
> （而不是签名错误），`setLeverage` 返回 `APPLIED`。

### 护栏怎么分工

| 层 | 覆盖的场景 |
|---|---|
| **外部看门狗**（主防线） | 进程死亡：心跳文件超过阈值未更新 → `cancelAllOrders` |
| **死手开关**（第二层） | 进程卡死但还活着：每 60s 续期（`now + 180s`） |

续期失败要连续计数并升级告警 —— 因为**一旦它自己触发，会撤掉包括平仓梯在内的所有挂单**。

## 6. 费率：beta 优惠不是永久事实

从 API 实时读回来的费率表（`/v1/feeTiers`）：

| 环境 | maker | taker | 一轮往返 maker 成本 |
|---|---|---|---|
| 主网（beta 期） | 0 ppm | 225 ppm | 0 bps |
| 测试网 / 主网 beta 结束后 | 150 ppm | 450 ppm | **3 bps** |

⇒ **不要硬编码止盈**。代码里的有效止盈是
`tp_bps = max(配置值, maker 费率 × 2)`，启动时从 `/v1/feeTiers` 读。
测试网日志里会看到 `maker 1.5 bps ... -> effective take profit 3 bps`。

## 7. 资金费

RWA 永续的**多头持续付费**，实测约 **0.000506%/小时 ≈ 4.4%/年**，
而且**每小时结算一次**。

⇒ 基线资金费大约每 59 小时吃光一轮 3 bps 的毛利。**持仓时间是真实的成本项**，
不是可以忽略的噪声。设计多腿结构时，应优先做**收**资金费的那一边。

## 8. ADL 与强平

**Arcus 有 ADL，而且是 production 代码路径**。官方只在 API 层暴露，概念页里连提都没提：

| 证据 | 位置 |
|---|---|
| 订单状态枚举含 `ADL` | `GET /v1/orders` 的 `status` 过滤器、WS `orders` |
| 成交带 `liquidation.method = ADL` + `liquidatedUser` | `GET /v1/fills`、WS `userFills` |
| ADL 腿不收 fee；被强平腿的 `fee` 是进保险基金的清算罚金 | fills 字段说明 |
| 标记持久化，快照里也有 | changelog |

**没公开的两件事**：ADL 的触发条件、挑对手方的排序规则。
通行规则是「按盈利 × 杠杆排序」，所以浮盈大 = 危险大
（见 [strategy-v2.md](strategy-v2.md) 的 ADL 复位）。

强平单在公开成交流里带 `liq:` 前缀的 orderId ⇒ **可以从公开数据统计强平压力**。

## 9. 其他实测坑

| 事实 | 影响 |
|---|---|
| **TP/SL 未上线**（`placeOrder` 返回 501） | 平仓只能自己挂 |
| `setLeverage` **只降不升** | 有持仓时降杠杆可能返回 `UNDERCOLLATERALIZED`；维持保证金与强平价不受影响 |
| 股票永续有 **±5% 价格带**（`upperTradingBound` / `lowerTradingBound`） | 加档位时新档可能落在带外，永远不成交 |
| 常规时段 = **04:00–20:00 ET**（含盘前盘后） | 「盘前盘后算不算常规」是个真问题；官方字段写明算 |
| 常规 IMF 10% / 加成 IMF 15% | 加成窗口的占用上限比常规更低 |
| 保险池余额查不到 | `/v1/insurance*`、`/v1/fund*` 全 404；口径是「交易所一个池」 |
| 多账号 | 每账号一份 YAML；账户级 WS 频道**无需认证**，任意地址都能订阅 |
