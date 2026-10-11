# arcus-farmer — 执行层

> **还没开户？先点这个邀请链接再注册：<https://app.arcus.xyz/ref/SKHYGO>**
> （作者的邀请链接，**不影响你的费率**。推荐关系在注册时绑定，注册之后再点无效。）
> 命令行版本：`python3 tools/get_started.py --open`

这是策略 V2 的**执行层**：一个 Arcus 客户端（签名 / REST / WebSocket / 本地状态镜像），
加上开篮子、调杠杆、止损、监控、估分的工具。策略参数在
[`config/spread-v2.yaml`](config/spread-v2.yaml)，设计说明在
[`../docs/strategy-v2.md`](../docs/strategy-v2.md)。

架构是**单进程、单连接、WS 优先**：一条 WebSocket 维护所有标的的盘口 / 订单 / 仓位 / 余额，
REST 只用于下单撤单（IP 权重 0）和低频对账。这不是偏好，是被限流模型逼出来的 ——
见 [`../docs/protocol-notes.md`](../docs/protocol-notes.md) 第 4 节。

---

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                        # 100 项

python3 tools/get_started.py --open                  # 先开户：邀请链接 + 上手清单
cp config/example.yaml config/mainnet.yaml            # 填 address / key 路径 / env
.venv/bin/python tools/ws_probe.py --env testnet      # 连通性
.venv/bin/python tools/screen.py --env mainnet        # 只用公开接口筛标的

# 开一条小篮子（测试网先跑通）
.venv/bin/python tools/spread_basket.py --config config/mainnet.yaml status
.venv/bin/python tools/spread_basket.py --config config/mainnet.yaml open --lev 1.0 --yes

# 策略：只看不动（影子期）
.venv/bin/python tools/spread_schedule.py --config config/mainnet.yaml plan
.venv/bin/python tools/spread_schedule.py --config config/mainnet.yaml adl

# 常驻（15 秒一轮，止损要立刻反应）
.venv/bin/python tools/spread_schedule.py --config config/mainnet.yaml daemon --apply --interval 15
```

---

## 工具一览

| 文件 | 作用 | 关键参数 |
|---|---|---|
| `tools/get_started.py` | **从这里开始**：邀请链接 + 上手清单 | `--open / --json` |
| `tools/referral.py` | 邀请链接的唯一来源（fork 时改这里） | `REFERRAL_CODE` |
| `tools/setup_key.py` | 生成 Ed25519 API key 并向 `POST /v1/createApiKey` 注册 | `--env --name --account-index` |
| `tools/ws_probe.py` | 逐频道验证 WebSocket 有帧 | `--env` |
| `tools/screen.py` | 标的筛选（公开接口），输出 CSV + HTML | `--env` |
| `tools/spread_basket.py` | 篮子的 status / open / close | `--lev --yes --force` |
| `tools/spread_schedule.py` | V2 调度器与状态机 | `plan / tick / daemon / adl / clear-halt`，`--apply` |
| `tools/spread_dashboard.py` | 本地看板（SSE），并每分钟采样 OI-time / 积分 | `--config --port` |
| `tools/margin_watch.py` | 三级保证金告警 | `--once / --test` |
| `tools/adl_watch.py` | ADL 监控（事实层 / 压力层 / 概率代理层），只报告不下单 | `--once --json` |
| `tools/points_report.py` | OI-time 与积分估算（读交易所成交账本） | `--config / --fills-json / --since / --days` |
| `tools/flatten.py` | 应急平仓（可只针对指定标的） | `--symbols --list --cancel-only` |

### 只报告 vs 真的下单

| 命令 | 副作用 |
|---|---|
| `spread_schedule.py ... plan` / `... adl` | **无**。只计算并打印 |
| `spread_schedule.py ... tick --apply` | 按目标调仓（含减仓 / 止损） |
| `spread_schedule.py ... daemon --apply --interval 15` | 常驻，同上 |
| `spread_basket.py ... open` / `close` | 需要显式 `--yes` 才真正下单 |
| `adl_watch.py` | **永远不下单** |

新的自动化**先跑影子期**：不 `--apply` 跑满 N 小时，把它的判断和人工判断逐条对过。

---

## 配置

### 账号配置 `config/<name>.yaml`

从 [`config/example.yaml`](config/example.yaml) 复制。里面只有「哪个账户、哪个 key、
哪些护栏」。字段语义见 `arcus/config.py`。

密钥写在仓库外的 `~/.arcus-farmer/keys/*.pem`（权限 600），配置里只放路径。
**私钥、助记词、`config/*.yaml`、`state/`、`logs/` 都不进 Git。**

### 策略配置 `config/spread-v2.yaml`

这是 V2 的全部策略参数：

| 段 | 内容 |
|---|---|
| `mode` | `hold_rebalance`：冻结调仓（默认 true） |
| `spread` | 长腿权重、空腿、可交易合计 |
| `leverage` | `base_per_side`、`boost_multiplier` |
| `schedule` | ET 时段划分、韩股 / 美股避让窗口 |
| `stops` | 三级止损阶梯 + 逐级恢复 + `HALT` 复位 |
| `execution` | maker 优先、超时改 taker、死区、容量上限 |
| `adl_reset` | ADL 复位的触发口径与冷却 |
| `alerts` | 保证金告警阈值（`margin_watch.py` 覆盖值） |

改这个文件 = 改真金白银的行为。**先跑 `plan` 看输出，再重启 daemon。**

---

## 状态文件

全部在 `state/`（已 gitignore）：

| 文件 | 内容 |
|---|---|
| `STOP` | 存在即全局停机（不下任何新单） |
| `spread_basket.json` | 篮子开仓记录（时间、杠杆、入场权益、各腿成交） |
| `spread_ladder.json` | 止损阶梯已执行到第几档 + 冻结目标尺寸 |
| `spread_peak.json` | 峰值权益（回撤的基准） |
| `spread_halt` | 熔断标记 |
| `spread_adl_reset.json`、`adl_reset_log.jsonl` | ADL 复位阶段机 / 事件流水 |
| `spread_pending.json` | 未成交挂单 |
| `spread_samples.jsonl` | 每分钟采样（OI-time、积分、净入金、各类 PnL） |
| `margin_alert_state.json` | 告警冷却状态 |
| `fills_cache.json` | `points_report.py` 的成交账本缓存 |

---

## 护栏

| 护栏 | 行为 |
|---|---|
| 容量上限 | `max_leg_notional_usd`、`max_rounds_per_day` |
| 账户回撤 | 三级阶梯：12% 降杠杆 → 20% 再降 → 30% 全平 + `HALT` |
| 保证金缓冲 | 缓冲 < 60% / 50% / 40%（即 R < 2.5 / 2.0 / 1.67）时分级告警 |
| 死手开关 | 每 60s 续期（`now + 180s`），失败只记录不致命 |
| 外部看门狗 | 心跳文件超时 → `cancelAllOrders` |
| 手工刹车 | `state/STOP`，不需要登录、不需要 API |

**不自动加仓**：冻结模式下的唯一订单来源是风控规则。

---

## 测试网演练清单

顺序执行，任何一步失败先修再往下：

1. `.venv/bin/python -m pytest -q` 全绿。
2. `tools/ws_probe.py --env testnet` 七个频道都有帧。
3. `setup_key.py` 写好密钥（600 权限）。
4. 在测试网 App 入金。
5. `spread_basket.py ... status`：账户读得到、四腿标的都在。
6. `open --lev 1.0 --yes` 开小篮子 → 四腿成交、方向正确 → `close --yes` 平掉 →
   确认没有残留挂单。
7. `spread_schedule.py ... plan`（不 `--apply`）：窗口判定与目标仓位合理。
8. **死手实验**：武装后停心跳，看是否全撤。
9. **看门狗实验**：杀掉机器人，确认超时后挂单被撤。
10. 挂机 24–48h 只验稳定性。

## 主网上线门槛

- 测试网跑通即可上主网**小资金**。
- 最小实单：确认真实成交、`/v1/fills` 对账、资金费至少一个结算周期。
- **放大之前**：先用 `points_report.py` 确认 OI-time 真的上去了，
  并且 ADL 复位的影子期没有误触发。

---

## 已知未覆盖

- 净 beta / 全局敞口没有具体数字（只有分腿的容量上限）。
- 隔离保证金未实现（启动时是跨保证金，所有标的共享抵押品）。
- `openInterestCapNotional` 未处理（当前为 `null`）。
- `clientId` 的存活上限（文档提到 10,000）未做主动回收。
- 积分异常（连续两周点数 ≈ 0）没有接进告警。
- 公司行动（分红 / 拆股）通过 funding 传导，未做事件级处理。

---

## 免责

仅供研究与学习。**刷量型策略可能触发交易所条款**（wash trading / churning），
积分规则是黑盒且可追溯调整。使用风险自负 —— 见根目录
[README](../README.md) 第 8 节与 [LICENSE](../LICENSE)。
