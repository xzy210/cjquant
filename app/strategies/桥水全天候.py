# -*- coding: utf-8 -*-
"""
桥水全天候策略 (Bridgewater All Weather Portfolio)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
由桥水基金（Bridgewater Associates）的瑞·达利欧（Ray Dalio）设计。

核心逻辑：经济由"增长"与"通胀"两个维度驱动，各有上行/下行，
构成四个象限，每个象限配置一类资产，以"风险平价"理念分配权重，
使得每个象限对组合的风险贡献相等。

四象限配置思路（桥水公开版本参考比例）:
  ┌──────────────────────┬──────────────────────┐
  │  经济增长↑ / 通胀↑   │  经济增长↑ / 通胀↓   │
  │  大宗商品  + 通胀债  │  股票（权益）         │
  │  (TIPS/黄金)  约15%  │          约30%        │
  ├──────────────────────┼──────────────────────┤
  │  经济增长↓ / 通胀↑   │  经济增长↓ / 通胀↓   │
  │  黄金  +  大宗商品   │  长期国债（利率债）   │
  │          约7.5%      │          约55%        │
  └──────────────────────┴──────────────────────┘

→ 汇总为四类资产，风险平价方式动态分配：
    权益类（股票）    ~30%  → 110020.OF (沪深300)
    黄金/通胀对冲     ~15%  → 000216.OF (华安黄金ETF联接)
    长端利率债        ~40%  → 012752.OF (中债7-10年国开债)
    中短期信用/货基   ~15%  → 161115.OF (此处示例用，可替换为债基)

策略机制：
  - 每季（3/6/9/12 月）首个交易日/工作日以风险平价优化器重新计算权重
  - 历史数据不足时退化为静态目标权重
  - 回测：handle_bar(context) + context.rebalance()
  - 实盘：run_strategy() → 读取 portfolio.json → OTCExecutor 导出订单
  - 基金池：app/strategies/pools/桥水全天候.json（回测/实盘共用）

使用说明:
  回测 — 选择本策略后点击「刷新基金池」，或自动加载配置中的标的
  实盘 — 任务调度 → 策略文件选本文件，建议 interval=86400（每天一次）
  修改标的 — 编辑 pools/桥水全天候.json，回测界面点刷新即可同步
"""

import os
import sys
import json
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Dict, Optional

import pandas as pd

# 调度器以子进程运行时需手动加入项目根目录
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from cjquant.optimizer.traditional import RiskParityOptimizer
from cjquant.data.providers.public import PublicFundProvider
from cjquant.execution import OTCExecutor
from cjquant.strategies.fund_pool import load_fund_pool_for_script, get_static_weights, get_fund_codes

# 季度再平衡触发月份
REBALANCE_MONTHS = {3, 6, 9, 12}

# 风险平价历史窗口（交易日数）
HISTORY_WINDOW = 120

# 多取一段历史用于吸收基金净值日期不完全一致造成的对齐损耗
HISTORY_ALIGNMENT_BUFFER = 40

# ── 实盘配置 ──────────────────────────────────────────
STRATEGY_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(STRATEGY_DIR, "..")
PROJECT_ROOT = os.path.join(STRATEGY_DIR, "..", "..")
PORTFOLIO_JSON = os.path.join(APP_DIR, "portfolio.json")
STATE_FILE = os.path.join(STRATEGY_DIR, ".all_weather_state.json")
MIN_RUN_HOUR = 15          # 15:00 后才执行，避免盘中误触发
REBALANCE_THRESHOLD = 100  # 最小调仓金额（元），低于此值不下单


@lru_cache(maxsize=1)
def _load_strategy_pool() -> dict:
    pool = load_fund_pool_for_script(__file__)
    if pool is None:
        raise RuntimeError(
            "未找到基金池配置文件: strategies/pools/桥水全天候.json"
        )
    return pool


def _fund_codes() -> list:
    return get_fund_codes(_load_strategy_pool())


def _static_weights() -> Dict[str, float]:
    return get_static_weights(_load_strategy_pool())


# ══════════════════════════════════════════════════════
#  共用核心逻辑
# ══════════════════════════════════════════════════════

def _get_current_quarter(date):
    """获取当前季度标识 (year, quarter)"""
    return (date.year, (date.month - 1) // 3 + 1)


def _quarter_rebalance_pending(date, last_rebalance_quarter) -> bool:
    """3/6/9/12 月内且本季度尚未调仓（回测/实盘共用判断）。"""
    if date.month not in REBALANCE_MONTHS:
        return False
    if last_rebalance_quarter is None:
        return True
    return _get_current_quarter(date) != last_rebalance_quarter


def _first_weekday_of_month(date: datetime) -> datetime:
    """当月首个工作日（周六日顺延）。"""
    d = date.replace(day=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _compute_rp_weights_from_returns_df(df_rets: pd.DataFrame) -> Optional[Dict[str, float]]:
    """从对齐后的日收益率矩阵计算风险平价权重。"""
    if len(df_rets) < HISTORY_WINDOW - 1:
        print(
            f"  [历史数据不足] 对齐后仅有 {len(df_rets)} 条收益率，"
            f"需 {HISTORY_WINDOW - 1} 条"
        )
        return None
    df_rets = df_rets.tail(HISTORY_WINDOW - 1)

    try:
        opt = RiskParityOptimizer(df_rets)
        weights_series = opt.optimize()
        weights = {}
        for i, fund in enumerate(df_rets.columns):
            weights[fund] = float(weights_series.iloc[i])
        total = sum(weights.values())
        return {k: v / total for k, v in weights.items()} if total > 0 else None
    except Exception as e:
        print(f"  [风险平价优化失败] {e}，回退到静态权重")
        return None


def _compute_rp_weights(context):
    """
    回测入口：用 context 提供的历史净值计算风险平价权重。
    若历史数据不足，返回 None（由调用方回退到静态权重）。
    """
    funds = _fund_codes()
    hist_returns = {}

    for fund in funds:
        navs = context.get_history_navs(fund, count=HISTORY_WINDOW + HISTORY_ALIGNMENT_BUFFER)
        if len(navs) < HISTORY_WINDOW:
            print(f"  [历史数据不足] {fund} 仅有 {len(navs)} 条净值，需 {HISTORY_WINDOW} 条")
            return None
        hist_returns[fund] = navs.pct_change().dropna()

    df_rets = pd.DataFrame(hist_returns).dropna()
    return _compute_rp_weights_from_returns_df(df_rets)


def _fetch_hist_returns() -> Optional[pd.DataFrame]:
    """实盘入口：从 PublicFundProvider 拉取历史净值并返回对齐后的收益率矩阵。"""
    provider = PublicFundProvider()
    hist_returns = {}

    for fund in _fund_codes():
        clean_code = fund.split(".")[0]
        try:
            df = provider.fetch(clean_code)
        except Exception as e:
            print(f"  [数据拉取失败] {fund}: {e}")
            return None
        if df.empty or len(df) < HISTORY_WINDOW:
            print(f"  [历史数据不足] {fund} 仅有 {len(df)} 条净值，需 {HISTORY_WINDOW} 条")
            return None
        hist_returns[fund] = df["adj_nav"].pct_change().dropna()

    df_rets = pd.DataFrame(hist_returns).dropna()
    return df_rets if len(df_rets) >= HISTORY_WINDOW - 1 else None


def _resolve_target_weights() -> Dict[str, float]:
    """计算目标权重，数据不足时回退静态权重。"""
    df_rets = _fetch_hist_returns()
    if df_rets is not None:
        rp_weights = _compute_rp_weights_from_returns_df(df_rets)
        if rp_weights is not None:
            return rp_weights
    print("  使用静态权重回退")
    return dict(_static_weights())


# ══════════════════════════════════════════════════════
#  回测入口
# ══════════════════════════════════════════════════════

def init(context):
    """初始化：记录上次再平衡季度"""
    context.last_rebalance_quarter = None
    print("=" * 60)
    print("  桥水全天候策略 (All Weather Portfolio) 已初始化")
    pool = _load_strategy_pool()
    print("  基金池:", pool.get("name") or "桥水全天候")
    print("  静态目标权重 (数据不足时回退):")
    for code, w in _static_weights().items():
        print(f"    {code}  →  {w:.0%}")
    print("  再平衡频率: 每季 (3/6/9/12 月) 首个交易日")
    print(f"  风险平价历史窗口: {HISTORY_WINDOW} 个交易日")
    print("=" * 60)


def handle_bar(context):
    """每个交易日驱动函数（回测）"""
    current_date = context.current_date
    is_first_day = (context.current_date_idx == 0)

    # ── 期初建仓 ──────────────────────────────────────
    if is_first_day:
        rp_weights = _compute_rp_weights(context)
        if rp_weights is not None:
            print(f"[{current_date.strftime('%Y-%m-%d')}] 期初建仓（风险平价）")
            for code, w in rp_weights.items():
                print(f"    {code}: {w:.2%}")
            context.rebalance(rp_weights)
        else:
            print(f"[{current_date.strftime('%Y-%m-%d')}] 期初建仓（静态权重回退）")
            for code, w in _static_weights().items():
                print(f"    {code}: {w:.0%}")
            context.rebalance(_static_weights())
        context.last_rebalance_quarter = _get_current_quarter(current_date)
        return

    # ── 季度再平衡：季月首个交易日触发（与实盘逻辑一致）──
    current_quarter = _get_current_quarter(current_date)
    if not _quarter_rebalance_pending(current_date, context.last_rebalance_quarter):
        return

    rp_weights = _compute_rp_weights(context)
    if rp_weights is not None:
        print(
            f"[{current_date.strftime('%Y-%m-%d')}] 季度再平衡（风险平价）"
            f"  Q{current_quarter[1]}/{current_quarter[0]}"
        )
        for code, w in rp_weights.items():
            print(f"    {code}: {w:.2%}")
        context.rebalance(rp_weights)
    else:
        print(
            f"[{current_date.strftime('%Y-%m-%d')}] 季度再平衡（静态权重回退）"
            f"  Q{current_quarter[1]}/{current_quarter[0]}"
        )
        context.rebalance(_static_weights())

    context.last_rebalance_quarter = current_quarter


# ══════════════════════════════════════════════════════
#  实盘入口
# ══════════════════════════════════════════════════════

def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _load_portfolio() -> dict:
    if not os.path.exists(PORTFOLIO_JSON):
        return {"cash": 0.0, "positions": [], "total_value": 0.0, "account_id": "ALL_WEATHER"}
    try:
        with open(PORTFOLIO_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"cash": 0.0, "positions": [], "total_value": 0.0, "account_id": "ALL_WEATHER"}


def _is_initial_build(portfolio: dict) -> bool:
    positions = portfolio.get("positions", [])
    return not positions or all(p.get("shares", 0) < 0.01 for p in positions)


def _should_rebalance_today(today: datetime, state: dict) -> bool:
    """
    实盘调仓日：季月内、本季度尚未调仓、且不早于当月首个工作日。
    若首个工作日软件未运行，后续工作日补跑（回测按交易日逐日推进，等价于首个交易日）。
    """
    if today.weekday() >= 5:
        return False
    last_q = state.get("last_rebalance_quarter")
    last_q_tuple = tuple(last_q) if last_q else None
    if not _quarter_rebalance_pending(today, last_q_tuple):
        return False
    return today.date() >= _first_weekday_of_month(today).date()


def _portfolio_total_value(portfolio: dict) -> float:
    cash = portfolio.get("cash", 0.0)
    total = cash
    for pos in portfolio.get("positions", []):
        mv = pos.get("market_value")
        if mv is None:
            mv = pos.get("shares", 0) * pos.get("current_nav", 0)
        total += mv
    return total


def _execute_rebalance_live(target_weights: Dict[str, float], portfolio: dict) -> bool:
    """读取当前持仓，按目标权重生成买卖订单并导出。"""
    total_value = _portfolio_total_value(portfolio)
    if total_value < REBALANCE_THRESHOLD:
        print(f"  [警告] 组合总市值 {total_value:.2f} 元过低，跳过调仓")
        return False

    positions = {p["fund_code"]: p for p in portfolio.get("positions", [])}

    executor = OTCExecutor(
        portfolio_id=str(portfolio.get("account_id", "ALL_WEATHER")),
        asset_unit="AW_UNIT",
        output_dir=os.path.abspath(PROJECT_ROOT),
        strip_suffix=True,
    )

    print(f"  组合总市值: {total_value:,.2f} 元")
    for fund, target_w in target_weights.items():
        target_value = total_value * target_w
        pos = positions.get(fund, {})
        current_value = pos.get("market_value")
        if current_value is None:
            current_value = pos.get("shares", 0) * pos.get("current_nav", 0)
        delta = target_value - current_value

        if delta > REBALANCE_THRESHOLD:
            print(f"  BUY  {fund}: {delta:,.2f} 元  (目标 {target_w:.2%}, 当前 {current_value:,.2f})")
            executor.buy(fund, delta)
        elif delta < -REBALANCE_THRESHOLD:
            nav = pos.get("current_nav", 0)
            shares = pos.get("shares", 0)
            if nav > 0 and shares > 0:
                shares_to_sell = min(shares, abs(delta) / nav)
                if shares_to_sell >= 0.01:
                    print(f"  SELL {fund}: {shares_to_sell:.4f} 份  (目标 {target_w:.2%}, 当前 {current_value:,.2f})")
                    executor.sell(fund, shares_to_sell)

    if not executor.orders:
        print("  持仓已接近目标权重，无需下单")
        return True

    print("\n  导出订单文件...")
    exported = executor.execute(channels=["o32", "wechat"], file_prefix="all_weather")
    for channel, path in exported.items():
        print(f"  [{channel.upper()}] {os.path.abspath(path)}")
    return True


def run_strategy():
    """实盘调度入口：季末调仓 / 首次建仓 → 导出 O32 & 微信订单。"""
    now = datetime.now()
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 桥水全天候策略（实盘）启动")

    if now.hour < MIN_RUN_HOUR:
        print(f"  当前时间早于 {MIN_RUN_HOUR}:00，跳过（等待收盘后执行）")
        return

    portfolio = _load_portfolio()
    state = _load_state()
    initial = _is_initial_build(portfolio)

    if initial:
        print("  检测到空持仓，执行期初建仓")
    elif not _should_rebalance_today(now, state):
        print("  今日非季月调仓窗口或本季度已调仓，跳过")
        return

    current_q = _get_current_quarter(now)
    label = "期初建仓" if initial else f"季度再平衡 Q{current_q[1]}/{current_q[0]}"
    print(f"  [{label}] 计算目标权重...")

    target_weights = _resolve_target_weights()
    for code, w in target_weights.items():
        print(f"    {code}: {w:.2%}")

    print(f"  [{label}] 生成调仓订单...")
    if _execute_rebalance_live(target_weights, portfolio):
        state["last_rebalance_quarter"] = list(current_q)
        state["last_run_date"] = now.strftime("%Y-%m-%d %H:%M:%S")
        _save_state(state)
        print(f"  [{label}] 完成")
    else:
        print(f"  [{label}] 未完成（请检查组合数据）")


if __name__ == "__main__":
    run_strategy()
