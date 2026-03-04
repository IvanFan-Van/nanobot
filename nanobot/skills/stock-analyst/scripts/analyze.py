#!/usr/bin/env python3
"""
Stock analysis script for the stock-analyst nanobot skill.

Usage:
    python analyze.py --symbol 000001 [--days 90] [--output-dir /tmp/stock]

Fetches A-share data via akshare, computes technical indicators via pandas-ta,
generates a chart via matplotlib, and prints a JSON summary for the agent to narrate.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Dependency check with user-friendly messages
# ---------------------------------------------------------------------------

import requests  # noqa: E402 — must precede akshare to patch Session

# Spoof a browser User-Agent so eastmoney endpoints don't reject the request.
# akshare uses `requests` internally; we monkeypatch Session.request so that
# every call automatically injects the header regardless of which Session
# instance is used.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_orig_session_request = requests.Session.request


def _patched_request(
    self: requests.Session, method: str, url: str, **kwargs: object
) -> requests.Response:
    headers = kwargs.pop("headers", None) or {}
    if "User-Agent" not in headers:
        headers["User-Agent"] = _BROWSER_UA
    kwargs["headers"] = headers
    return _orig_session_request(self, method, url, **kwargs)


requests.Session.request = _patched_request  # type: ignore[method-assign]

import akshare as ak
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import pandas_ta as ta

# ---------------------------------------------------------------------------
# Chinese font setup — try common CJK fonts; fall back gracefully
# ---------------------------------------------------------------------------

import matplotlib.font_manager as _fm

_CJK_FONT_CANDIDATES = [
    "SimHei",  # Windows
    "Microsoft YaHei",
    "WenQuanYi Micro Hei",  # Linux
    "Noto Sans CJK SC",
    "PingFang SC",  # macOS
    "Heiti SC",
    "Arial Unicode MS",
]


def _find_cjk_font() -> str | None:
    available = {f.name for f in _fm.fontManager.ttflist}
    for name in _CJK_FONT_CANDIDATES:
        if name in available:
            return name
    return None


_cjk_font = _find_cjk_font()
if _cjk_font:
    plt.rcParams["font.family"] = [_cjk_font, "DejaVu Sans"]
else:
    # Last-resort: set font.sans-serif and disable the minus-sign workaround
    plt.rcParams["font.sans-serif"] = ["SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False  # Fix minus-sign rendering


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def resolve_symbol(symbol: str) -> tuple[str, str]:
    """Return (6-digit code, stock name). Accepts code or Chinese name."""
    symbol = symbol.strip()
    if symbol.isdigit() and len(symbol) == 6:
        # Already a code — look up the name
        try:
            info_df = ak.stock_individual_info_em(symbol=symbol)
            name = info_df.set_index("item").loc["股票简称", "value"]
        except Exception:
            name = symbol
        return symbol, str(name)

    # Try to look up by name
    try:
        all_stocks = ak.stock_info_a_code_name()
        match = all_stocks[all_stocks["name"].str.contains(symbol, na=False)]
        if match.empty:
            print(json.dumps({"error": f"找不到股票：{symbol}，请提供6位数字代码或准确的股票简称"}))
            sys.exit(1)
        code = match.iloc[0]["code"]
        name = match.iloc[0]["name"]
        return str(code), str(name)
    except Exception as e:
        print(json.dumps({"error": f"股票查询失败：{e}"}))
        sys.exit(1)


def fetch_kline(symbol: str, days: int) -> pd.DataFrame:
    """Fetch daily K-line (前复权) for the past *days* calendar days.

    We request an extra 120 calendar days (~85 trading days) of history so that
    MA60 and Bollinger Band (20) calculations have enough warmup data even after
    trimming to the requested display window.  The previous buffer of 60 calendar
    days was often insufficient — A-share markets are open ~22 days/month, so 60
    calendar days only yields ~43 trading days, which is not enough to warm up
    MA60.
    """
    end = datetime.today()
    # 120 calendar days ≈ 85 trading days — sufficient warmup for MA60 + BB20
    start = end - timedelta(days=days + 120)
    df = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust="qfq",
    )
    df = df.rename(
        columns={
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "换手率": "turnover",
            "涨跌幅": "pct_change",
        }
    )
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    # Trim to requested display window (keep only the last `days` calendar days)
    cutoff = end - timedelta(days=days)
    df = df[df["date"] >= pd.Timestamp(cutoff)].reset_index(drop=True)
    return df


def fetch_realtime(symbol: str) -> dict:
    """Return a dict with live price, PE, PB, market cap etc."""
    try:
        spot = ak.stock_zh_a_spot_em()
        row = spot[spot["代码"] == symbol]
        if row.empty:
            return {}
        r = row.iloc[0]
        return {
            "price": float(r.get("最新价", 0)),
            "pct_change_today": float(r.get("涨跌幅", 0)),
            "pe_dynamic": float(r.get("市盈率-动态", 0) or 0),
            "pb": float(r.get("市净率", 0) or 0),
            "total_market_cap": float(r.get("总市值", 0) or 0),
            "float_market_cap": float(r.get("流通市值", 0) or 0),
            "volume": float(r.get("成交量", 0) or 0),
            "turnover_rate": float(r.get("换手率", 0) or 0),
            "high_60d_pct": float(r.get("60日涨跌幅", 0) or 0),
        }
    except Exception:
        return {}


def fetch_company_info(symbol: str) -> dict:
    """Return basic company info: industry, listing date."""
    try:
        info_df = ak.stock_individual_info_em(symbol=symbol)
        info = info_df.set_index("item")["value"].to_dict()
        return {
            "industry": str(info.get("行业", "未知")),
            "listing_date": str(info.get("上市时间", "未知")),
            "total_shares": str(info.get("总股本", "未知")),
            "float_shares": str(info.get("流通股", "未知")),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Technical indicator calculation
# ---------------------------------------------------------------------------


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute MA, MACD, RSI, KDJ, Bollinger Bands on df in-place."""
    # Trend: moving averages
    for length in (5, 10, 20, 60):
        df[f"ma{length}"] = df["close"].rolling(length).mean().round(3)

    # MACD (12, 26, 9)
    macd = df.ta.macd(fast=12, slow=26, signal=9)
    if macd is not None:
        df["macd"] = macd["MACD_12_26_9"]
        df["macd_signal"] = macd["MACDs_12_26_9"]
        df["macd_hist"] = macd["MACDh_12_26_9"]

    # RSI (14)
    rsi = df.ta.rsi(length=14)
    if rsi is not None:
        df["rsi14"] = rsi

    # KDJ (9, 3, 3)
    stoch = df.ta.stoch(k=9, d=3, smooth_k=3)
    if stoch is not None:
        df["kdj_k"] = stoch.iloc[:, 0]
        df["kdj_d"] = stoch.iloc[:, 1]
        df["kdj_j"] = 3 * df["kdj_k"] - 2 * df["kdj_d"]

    # Bollinger Bands (20, 2)
    bb = df.ta.bbands(length=20, std=2)
    if bb is not None:
        # pandas-ta column names vary by version; find them dynamically
        bb_upper_col = next((c for c in bb.columns if c.startswith("BBU_")), None)
        bb_mid_col = next((c for c in bb.columns if c.startswith("BBM_")), None)
        bb_lower_col = next((c for c in bb.columns if c.startswith("BBL_")), None)
        # Only assign if ALL three columns were found; partial assignment would
        # leave some BB columns missing and break downstream code
        if bb_upper_col and bb_mid_col and bb_lower_col:
            df["bb_upper"] = bb[bb_upper_col]
            df["bb_mid"] = bb[bb_mid_col]
            df["bb_lower"] = bb[bb_lower_col]

    # Volume MA (5-day)
    df["vol_ma5"] = df["volume"].rolling(5).mean()

    return df


# ---------------------------------------------------------------------------
# Support / Resistance detection (simple swing-high/low method)
# ---------------------------------------------------------------------------


def find_support_resistance(df: pd.DataFrame, window: int = 10) -> dict:
    """Identify key support and resistance levels using recent swing highs/lows.

    The *window* parameter is automatically clamped so that we never need more
    data than is actually available (minimum 3 bars on each side).
    """
    # Adapt window to available data so we always produce some results
    max_window = max(3, (len(df) - 1) // 2)
    window = min(window, max_window)

    highs = []
    lows = []
    for i in range(window, len(df) - window):
        if df["high"].iloc[i] == df["high"].iloc[i - window : i + window + 1].max():
            highs.append(float(df["high"].iloc[i]))
        if df["low"].iloc[i] == df["low"].iloc[i - window : i + window + 1].min():
            lows.append(float(df["low"].iloc[i]))

    current_price = float(df["close"].iloc[-1])

    # Filter: resistance above current price, support below
    resistances = sorted([h for h in highs if h > current_price])[:3]
    supports = sorted([l for l in lows if l < current_price], reverse=True)[:3]

    return {
        "supports": supports,
        "resistances": resistances,
    }


# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------


def generate_chart(df: pd.DataFrame, symbol: str, name: str, output_path: Path) -> None:
    """Generate a 3-panel chart: K-line + MAs + BB, Volume, MACD + RSI."""
    fig, axes = plt.subplots(
        3,
        1,
        figsize=(14, 10),
        gridspec_kw={"height_ratios": [3, 1, 1.5]},
        facecolor="#1a1a2e",
    )
    plt.subplots_adjust(hspace=0.05)

    dates = df["date"]
    c_up = "#ef5350"  # red for up candles (Chinese convention)
    c_down = "#26a69a"  # green for down candles

    # ---- Panel 1: K-line + MAs + Bollinger Bands ----
    ax1 = axes[0]
    ax1.set_facecolor("#1a1a2e")

    # Draw candles
    for _, row in df.iterrows():
        color = c_up if row["close"] >= row["open"] else c_down
        ax1.plot([row["date"], row["date"]], [row["low"], row["high"]], color=color, linewidth=0.8)
        ax1.bar(
            row["date"],
            abs(row["close"] - row["open"]),
            bottom=min(row["open"], row["close"]),
            color=color,
            width=0.6,
            alpha=0.9,
        )

    # MAs
    ma_styles = {
        "ma5": ("#ffd700", "MA5"),
        "ma10": ("#1e90ff", "MA10"),
        "ma20": ("#ff8c00", "MA20"),
        "ma60": ("#da70d6", "MA60"),
    }
    for col, (color, label) in ma_styles.items():
        if col in df.columns:
            ax1.plot(dates, df[col], color=color, linewidth=1.0, label=label)

    # Bollinger Bands
    if "bb_upper" in df.columns:
        ax1.fill_between(dates, df["bb_lower"], df["bb_upper"], alpha=0.07, color="#90caf9")
        ax1.plot(dates, df["bb_upper"], color="#90caf9", linewidth=0.7, linestyle="--", alpha=0.7)
        ax1.plot(dates, df["bb_lower"], color="#90caf9", linewidth=0.7, linestyle="--", alpha=0.7)

    ax1.set_title(f"{name}（{symbol}）技术分析", color="white", fontsize=13, pad=8)
    ax1.legend(
        loc="upper left", fontsize=7, facecolor="#2a2a4a", labelcolor="white", framealpha=0.8
    )
    ax1.tick_params(colors="gray", labelbottom=False)
    ax1.spines[:].set_color("#444466")
    ax1.yaxis.label.set_color("white")

    # ---- Panel 2: Volume ----
    ax2 = axes[1]
    ax2.set_facecolor("#1a1a2e")
    colors_vol = [c_up if r["close"] >= r["open"] else c_down for _, r in df.iterrows()]
    ax2.bar(dates, df["volume"], color=colors_vol, alpha=0.7, width=0.6)
    if "vol_ma5" in df.columns:
        ax2.plot(dates, df["vol_ma5"], color="#ffd700", linewidth=0.9, label="Vol MA5")
    ax2.set_ylabel("成交量", color="gray", fontsize=8)
    ax2.tick_params(colors="gray", labelbottom=False)
    ax2.spines[:].set_color("#444466")
    ax2.legend(
        loc="upper left", fontsize=7, facecolor="#2a2a4a", labelcolor="white", framealpha=0.8
    )

    # ---- Panel 3: MACD + RSI ----
    ax3 = axes[2]
    ax3.set_facecolor("#1a1a2e")

    if "macd" in df.columns:
        ax3.plot(dates, df["macd"], color="#1e90ff", linewidth=0.9, label="MACD")
        ax3.plot(dates, df["macd_signal"], color="#ff8c00", linewidth=0.9, label="Signal")
        hist_colors = ["#ef5350" if v >= 0 else "#26a69a" for v in df["macd_hist"].fillna(0)]
        ax3.bar(dates, df["macd_hist"], color=hist_colors, alpha=0.6, width=0.6)
        ax3.axhline(0, color="#666688", linewidth=0.6)

    # RSI on secondary y-axis
    if "rsi14" in df.columns:
        ax3_rsi = ax3.twinx()
        ax3_rsi.plot(
            dates, df["rsi14"], color="#da70d6", linewidth=0.9, linestyle=":", label="RSI(14)"
        )
        ax3_rsi.axhline(70, color="#da70d6", linewidth=0.5, linestyle="--", alpha=0.5)
        ax3_rsi.axhline(30, color="#da70d6", linewidth=0.5, linestyle="--", alpha=0.5)
        ax3_rsi.set_ylim(0, 100)
        ax3_rsi.tick_params(colors="gray")
        ax3_rsi.set_ylabel("RSI", color="#da70d6", fontsize=8)
        ax3_rsi.legend(
            loc="upper right", fontsize=7, facecolor="#2a2a4a", labelcolor="white", framealpha=0.8
        )

    ax3.set_ylabel("MACD", color="gray", fontsize=8)
    ax3.legend(
        loc="upper left", fontsize=7, facecolor="#2a2a4a", labelcolor="white", framealpha=0.8
    )
    ax3.tick_params(colors="gray")
    ax3.spines[:].set_color("#444466")
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax3.xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.setp(ax3.xaxis.get_majorticklabels(), rotation=30, ha="right", color="gray", fontsize=7)

    plt.savefig(output_path, dpi=130, bbox_inches="tight", facecolor="#1a1a2e")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Analysis narrative builders
# ---------------------------------------------------------------------------


def _trend_desc(df: pd.DataFrame) -> str:
    last = df.iloc[-1]
    close = last["close"]
    parts = []

    ma_vals = {f"ma{n}": last.get(f"ma{n}") for n in (5, 10, 20, 60)}
    valid = {k: v for k, v in ma_vals.items() if pd.notna(v)}
    above = [k.upper() for k, v in valid.items() if close > v]
    below = [k.upper() for k, v in valid.items() if close <= v]

    if len(above) == len(valid):
        parts.append("价格位于所有均线上方，均线多头排列，趋势偏强")
    elif len(below) == len(valid):
        parts.append("价格位于所有均线下方，均线空头排列，趋势偏弱")
    elif above:
        parts.append(f"价格位于 {', '.join(above)} 上方，{', '.join(below)} 下方，趋势中性偏震荡")
    else:
        # above is empty but not all below (shouldn't happen, but handle gracefully)
        parts.append(f"价格受均线 {', '.join(below)} 压制，趋势偏弱")

    if pd.notna(last.get("macd")) and pd.notna(last.get("macd_signal")):
        if last["macd"] > last["macd_signal"]:
            parts.append("MACD 金叉（DIF > DEA），短期动能向上")
        else:
            parts.append("MACD 死叉（DIF < DEA），短期动能偏弱")
        parts.append(
            "MACD 处于零轴上方，中期趋势为多"
            if last["macd"] > 0
            else "MACD 处于零轴下方，中期趋势为空"
        )

    return "；".join(parts) + "。"


def _momentum_desc(df: pd.DataFrame) -> str:
    last = df.iloc[-1]
    parts = []

    rsi = last.get("rsi14")
    if pd.notna(rsi):
        rsi_val = round(float(rsi), 1)
        if rsi_val >= 80:
            parts.append(f"RSI(14)={rsi_val}，严重超买，回调风险较大")
        elif rsi_val >= 70:
            parts.append(f"RSI(14)={rsi_val}，超买区间，注意高位风险")
        elif rsi_val <= 20:
            parts.append(f"RSI(14)={rsi_val}，严重超卖，可能存在反弹机会")
        elif rsi_val <= 30:
            parts.append(f"RSI(14)={rsi_val}，超卖区间，关注企稳信号")
        else:
            parts.append(f"RSI(14)={rsi_val}，处于中性区间（30–70），动能平衡")

    k, d, j = last.get("kdj_k"), last.get("kdj_d"), last.get("kdj_j")
    if all(pd.notna(v) for v in (k, d, j)):
        k, d, j = round(float(k), 1), round(float(d), 1), round(float(j), 1)
        parts.append(f"KDJ K({k})>D({d}) 呈金叉形态" if k > d else f"KDJ K({k})<D({d}) 呈死叉形态")
        if j > 80:
            parts.append(f"J 值={j}，超买区间")
        elif j < 20:
            parts.append(f"J 值={j}，超卖区间")

    return "；".join(parts) + "。" if parts else "动量指标数据不足。"


def _volume_desc(df: pd.DataFrame) -> str:
    if len(df) < 6:
        return "数据不足。"
    last = df.iloc[-1]
    recent5_avg = df["volume"].iloc[-6:-1].mean()
    ratio = float(last["volume"]) / recent5_avg if recent5_avg > 0 else 1.0
    pct_raw = last.get("pct_change")
    pct = float(pct_raw) if pd.notna(pct_raw) else 0.0

    if ratio > 2.0 and pct > 0:
        desc = f"今日成交量是近5日均量的 {ratio:.1f} 倍，放量上涨，买方积极，量价配合良好。"
    elif ratio > 2.0 and pct < 0:
        desc = f"今日成交量是近5日均量的 {ratio:.1f} 倍，放量下跌，抛压较重，需警惕。"
    elif ratio < 0.5 and pct > 0:
        desc = f"今日缩量上涨（成交量仅为近5日均量的 {ratio:.1%}），涨势缺乏量能支撑，持续性存疑。"
    elif ratio < 0.5 and pct < 0:
        desc = f"今日缩量下跌（成交量仅为近5日均量的 {ratio:.1%}），卖压有限，可能为技术性回调。"
    else:
        desc = f"今日成交量与近期均量基本持平（{ratio:.1f} 倍），量能无明显异常。"

    turnover_raw = last.get("turnover")
    turnover = float(turnover_raw) if pd.notna(turnover_raw) else 0.0
    if turnover:
        desc += f" 换手率 {turnover:.2f}%。"
    return desc


def _bollinger_desc(df: pd.DataFrame) -> str:
    if "bb_upper" not in df.columns:
        return ""
    last = df.iloc[-1]
    close = float(last["close"])
    upper = last.get("bb_upper")
    lower = last.get("bb_lower")
    mid = last.get("bb_mid")
    if any(pd.isna(v) for v in (upper, lower, mid)):
        return ""
    upper, lower, mid = float(upper), float(lower), float(mid)
    width = (upper - lower) / mid * 100

    if close >= upper:
        return f"价格触及布林带上轨（{upper:.2f}），短期可能面临压力；带宽 {width:.1f}%。"
    elif close <= lower:
        return f"价格触及布林带下轨（{lower:.2f}），可能存在超跌反弹机会；带宽 {width:.1f}%。"
    elif close > mid:
        return f"价格位于布林带中轨（{mid:.2f}）上方，多方占优；带宽 {width:.1f}%。"
    else:
        return f"价格位于布林带中轨（{mid:.2f}）下方，空方稍强；带宽 {width:.1f}%。"


def _overall_verdict(df: pd.DataFrame) -> str:
    """Produce a concise overall technical verdict via a simple scoring model."""
    last = df.iloc[-1]
    close = float(last["close"])
    score = 0

    # MA alignment (4 signals)
    for n in (5, 10, 20, 60):
        v = last.get(f"ma{n}")
        if pd.notna(v):
            score += 1 if close > float(v) else -1

    # MACD (2 signals)
    if pd.notna(last.get("macd")) and pd.notna(last.get("macd_signal")):
        score += 1 if last["macd"] > last["macd_signal"] else -1
        score += 1 if last["macd"] > 0 else -1

    # RSI (1 signal)
    rsi = last.get("rsi14")
    if pd.notna(rsi):
        rsi_val = float(rsi)
        if rsi_val > 60:
            score += 1
        elif rsi_val < 40:
            score -= 1

    # KDJ (1 signal)
    if pd.notna(last.get("kdj_k")) and pd.notna(last.get("kdj_d")):
        score += 1 if float(last["kdj_k"]) > float(last["kdj_d"]) else -1

    if score >= 5:
        return "技术面整体强势偏多，多项指标共振向上，可关注逢低买入机会。"
    elif score >= 2:
        return "技术面偏多，趋势仍在确认中，建议结合量能变化判断介入时机。"
    elif score <= -5:
        return "技术面整体偏空，多项指标同步走弱，建议谨慎操作或等待趋势反转确认。"
    elif score <= -2:
        return "技术面偏空，短期承压，可等待企稳信号后再行决策。"
    else:
        return "技术面多空势均力敌，市场处于震荡整理阶段，暂无明确方向信号，以观望为主。"


# ---------------------------------------------------------------------------
# Summary assembler
# ---------------------------------------------------------------------------


def build_summary(
    df: pd.DataFrame,
    symbol: str,
    name: str,
    realtime: dict,
    company: dict,
    sr: dict,
    chart_path: str | None,
) -> dict:
    last = df.iloc[-1]
    close = float(last["close"])
    prev_close = float(df["close"].iloc[-2]) if len(df) >= 2 else close

    ma_snapshot = {
        f"MA{n}": round(float(last[f"ma{n}"]), 3) if pd.notna(last.get(f"ma{n}")) else None
        for n in (5, 10, 20, 60)
    }

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "symbol": symbol,
        "name": name,
        "fundamentals": {
            "price": realtime.get("price") or close,
            "pct_change_today": realtime.get("pct_change_today")
            or round((close / prev_close - 1) * 100, 2),
            "pe_dynamic": realtime.get("pe_dynamic"),
            "pb": realtime.get("pb"),
            "total_market_cap_bn": round((realtime.get("total_market_cap") or 0) / 1e9, 2),
            "turnover_rate": realtime.get("turnover_rate"),
            "industry": company.get("industry"),
            "listing_date": company.get("listing_date"),
        },
        "indicators": {
            **ma_snapshot,
            "MACD": round(float(last["macd"]), 4) if pd.notna(last.get("macd")) else None,
            "MACD_Signal": round(float(last["macd_signal"]), 4)
            if pd.notna(last.get("macd_signal"))
            else None,
            "RSI14": round(float(last["rsi14"]), 2) if pd.notna(last.get("rsi14")) else None,
            "KDJ_K": round(float(last["kdj_k"]), 2) if pd.notna(last.get("kdj_k")) else None,
            "KDJ_D": round(float(last["kdj_d"]), 2) if pd.notna(last.get("kdj_d")) else None,
            "KDJ_J": round(float(last["kdj_j"]), 2) if pd.notna(last.get("kdj_j")) else None,
            "BB_Upper": round(float(last["bb_upper"]), 3)
            if pd.notna(last.get("bb_upper"))
            else None,
            "BB_Lower": round(float(last["bb_lower"]), 3)
            if pd.notna(last.get("bb_lower"))
            else None,
        },
        "analysis": {
            "trend": _trend_desc(df),
            "momentum": _momentum_desc(df),
            "volume": _volume_desc(df),
            "bollinger": _bollinger_desc(df),
            "verdict": _overall_verdict(df),
        },
        "support_resistance": sr,
        "chart_path": chart_path,
        "recent_5d": (
            df[
                [
                    c
                    for c in [
                        "date",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "pct_change",
                        "turnover",
                    ]
                    if c in df.columns
                ]
            ]
            .tail(5)
            .assign(date=lambda x: x["date"].dt.strftime("%Y-%m-%d"))
            .to_dict(orient="records")
        ),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="A-share technical analysis via akshare")
    parser.add_argument("--symbol", required=True, help="股票代码（6位数字）或股票名称")
    parser.add_argument("--days", type=int, default=0, help="分析天数（0=自动，建议90或180）")
    parser.add_argument("--output-dir", default="/tmp/stock_analysis", help="图表输出目录")
    args = parser.parse_args()

    code, name = resolve_symbol(args.symbol)

    # Auto days: 120 calendar days covers MA60 warmup + ~60 display trading days
    days = args.days if args.days > 0 else 120

    try:
        df = fetch_kline(code, days)
    except Exception as e:
        print(json.dumps({"error": f"K线数据获取失败：{e}"}))
        sys.exit(1)

    if len(df) < 30:
        print(json.dumps({"error": f"数据不足（仅 {len(df)} 条），无法进行技术分析"}))
        sys.exit(1)

    realtime = fetch_realtime(code)
    company = fetch_company_info(code)
    df = compute_indicators(df)
    sr = find_support_resistance(df)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    chart_path = output_dir / f"{code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

    try:
        generate_chart(df, code, name, chart_path)
        chart_path_str = str(chart_path)
    except Exception as e:
        chart_path_str = None
        sys.stderr.write(f"Chart generation failed: {e}\n")

    summary = build_summary(df, code, name, realtime, company, sr, chart_path_str)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
