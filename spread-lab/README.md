# spread-lab — 价差研究

这里回答的是「**为什么 V2 是这个形状**」。只依赖标准库 + 公开行情接口，
没有密钥、没有账户数据。

```
标的：一条记忆体主题 ETF（DRAM）和它成分股在 Arcus 上的永续
      能交易的成分约占 ETF 权重 54%（MU / SKHY / SNDK）
方向：多 MU+SKHY+SNDK（按 ETF 内权重归一） / 空等额 DRAM
单位：1 unit = 多 $1 DRAM 持仓权重归一的多腿 / 空 $1 ETF ⇒ 美元中性
```

数据是**报价驱动**的：这类标的只有少数分钟有真实成交（DRAM 约 10%、SKHY 8%、MU 17%），
所以所有统计都按 bps 而不是按金额表达，并且对「成交稀疏」保持警惕。

---

## 复现

```bash
# 0. 市场参数快照（保证金率 / 精密度 / 价格带 / regularTradingHours）
mkdir -p data
curl -s https://api.arcus.xyz/v1/markets > data/markets.json

# 1. 行情（公开接口 /v1/candles，按 `to` 倒序分页，每页 1500 根）
python3 fetch_candles.py --timeframe 1h --days 400 --symbols DRAM-USD,MU-USD,SKHY-USD,SNDK-USD
python3 fetch_candles.py --timeframe 1m --days  60 --symbols DRAM-USD,MU-USD,SKHY-USD,SNDK-USD

# 2. 统计
python3 analyze.py            # -> results.json      走势 / 相关 / β / ADF / Hurst / 半衰期 / z
python3 extras.py             # -> extras.json       样本内外稳定性 / 多腿对冲 / 容量 / tick
python3 grid2.py              # -> grid_results.json 价差网格模拟（多档距 × 多锚）
python3 sessions.py           # -> sessions_stats.json 时段 vs 非时段
python3 leverage_windows.py   # -> leverage_windows.json 加成窗口排班
python3 leverage_envelope.py  # -> leverage_envelope.json 杠杆包络
python3 plan_L2.py            # -> plan_L2.json      某套排班的保证金/回撤/积分/止损
python3 risk_mm.py            # -> risk_mm.json      R = equity / 维持保证金
python3 points.py             # -> points_stats.json 持有 vs 网格的 OI-time 与积分

# 3. 报告（HTML）
python3 report.py             # 多 DRAM / 空 MU+SKHY 方向
python3 report2.py            # 做空 ETF 里非 Arcus 成分的那一页
python3 strategy_v2.py        # 策略 v2 参数快照（读 plan_L2.json）
```

输出默认写到 `spread-lab/out/`；`LAB_OUT` 可改目录，`LAB_EQUITY` 可改测算用的权益：

```bash
LAB_EQUITY=50000 python3 strategy_v2.py
LAB_EQUITY=50000 python3 risk_mm.py
```

`holdings.json` 是 ETF 的持仓快照（含各成分权重），`basket.py` / `report2.py` 用它把
「做多可交易成分 / 做空 ETF」拆成「做空那 45.86% 的非交易篮子」。

---

## 几个关键结论

**时段划分（`sessions.py`）**

- Arcus 的常规时段 = **04:00–20:00 ET**（`/v1/markets` 的 `regularTradingHours`），
  **盘前盘后都算常规**：无加成、初始保证金 10%。
- 加成窗口 = 20:00–04:00 ET + 周末/假期，初始保证金提到 15%。
- 一周切成 REG / BOOST_EDGE（韩股开收盘 ±1.5h）/ BOOST_CORE；加成核心窗口的
  小时波动率明显低于被排除的窗口，所以**避让韩股开收盘是值的**。

**杠杆排班（`leverage_windows.py`）**

按三条约束取最小定倍数：回撤预算、等波动率、保证金 70% 守卫。
典型结论是**保证金约束比波动率约束更紧**（初始保证金打到 70% 线只允许较低倍数），
这也是把 `base_per_side` 定在 3.0× 并把 `boost_multiplier` 压回 1.0 的原因。

**风险口径（`risk_mm.py`）**

真正决定强平的是**维持保证金**（股票类 6.667%），不是初始保证金占用。
安全指标 `R = 权益 / (总名义 × 6.667%)`，`R = 1` 才是强平；止损阶梯因此按
「历史最深回撤 + 结构性尾部（±5% 价格带）」定义，而不是按保证金占用定义。

**持有 vs 网格（`points.py`）**

同一份资金预算下，直接持有拿到的是 `一份资本 → 一份 OI`，
而不是「一份资本 → 一堆轮次」；网格只有在**震荡收入**这一项上有优势，
在 OI-time 与积分上都不如持有。

---

## 依赖与注意事项

- 只依赖标准库（`stats_lib.py` 里是纯 Python 的 OLS / ADF / Hurst / 方差比实现）。
- 行情与 `markets.json` 都来自公开接口，可随时重拉；不要把大文件提交进来。
- 所有脚本的默认窗口、锚长、档距都写在文件头部常量里，改参数前先看它们。
