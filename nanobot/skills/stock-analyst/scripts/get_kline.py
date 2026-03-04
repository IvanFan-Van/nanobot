#!/usr/bin/env python3
"""Fetch historical K-line data and compute technical indicators for an A-share stock.

Usage:
    python get_kline.py --symbol 600111 [--days 90]

Outputs JSON with:
  - recent_bars: last N trading days of OHLCV + indicators
  - indicators_latest: snapshot of all indicator values for the most recent bar
  - support_resistance: key support and resistance price levels
  - ma_alignment: plain-text description of MA arrangement
  - summary_stats: high/low/avg over the requested period
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

import requests  # noqa: E402

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_orig = requests.Session.request


def _patched(self: requests.Session, method: str, url: str, **kw: object) -> requests.Response:
    headers = dict(kw.pop("headers", None) or {})  # type: ignore[arg-type]
    headers.setdefault("User-Agent", _BROWSER_UA)
    kw["headers"] = headers
    return _orig(self, method, url, **kw)  # type: ignore[arg-type]


requests.Session.request = _patched  # type: ignore[method-assign]

import akshare as ak  # noqa: E402
import pandas as pd  # noqa: E402
import pandas_ta as ta  # noqa: E402


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def _fetch_kline(symbol: str, days: int) -> pd.DataFrame:
    """Fetch daily front-adjusted (前复权) K-line with enough warmup for MA60."""
    end = datetime.today()
    # Extra 120 calendar days (~85 trading days) of warmup for MA60
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
    return df


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    for n in (5, 10, 20, 60):
        df[f"ma{n}"] = df["close"].rolling(n).mean().round(3)

    macd = df.ta.macd(fast=12, slow=26, signal=9)
    if macd is not None:
        df["macd"] = macd["MACD_12_26_9"].round(4)
        df["macd_signal"] = macd["MACDs_12_26_9"].round(4)
        df["macd_hist"] = macd["MACDh_12_26_9"].round(4)

    rsi = df.ta.rsi(length=14)
    if rsi is not None:
        df["rsi14"] = rsi.round(2)

    stoch = df.ta.stoch(k=9, d=3, smooth_k=3)
    if stoch is not None:
        df["kdj_k"] = stoch.iloc[:, 0].round(2)
        df["kdj_d"] = stoch.iloc[:, 1].round(2)
        df["kdj_j"] = (3 * df["kdj_k"] - 2 * df["kdj_d"]).round(2)

    bb = df.ta.bbands(length=20, std=2)
    if bb is not None:
        upper = next((c for c in bb.columns if c.startswith("BBU_")), None)
        mid = next((c for c in bb.columns if c.startswith("BBM_")), None)
        lower = next((c for c in bb.columns if c.startswith("BBL_")), None)
        if upper and mid and lower:
            df["bb_upper"] = bb[upper].round(3)
            df["bb_mid"] = bb[mid].round(3)
            df["bb_lower"] = bb[lower].round(3)

    df["vol_ma5"] = df["volume"].rolling(5).mean().round(0)
    return df


# ---------------------------------------------------------------------------
# Support / Resistance
# ---------------------------------------------------------------------------


def _find_support_resistance(df: pd.DataFrame, window: int = 10) -> dict:
    max_w = max(3, (len(df) - 1) // 2)
    w = min(window, max_w)
    highs, lows = [], []
    for i in range(w, len(df) - w):
        if df["high"].iloc[i] == df["high"].iloc[i - w : i + w + 1].max():
            highs.append(round(float(df["high"].iloc[i]), 3))
        if df["low"].iloc[i] == df["low"].iloc[i - w : i + w + 1].min():
            lows.append(round(float(df["low"].iloc[i]), 3))
    price = float(df["close"].iloc[-1])
    return {
        "current_price": round(price, 3),
        "resistances": sorted(h for h in highs if h > price)[:3],
        "supports": sorted((l for l in lows if l < price), reverse=True)[:3],
    }


# ---------------------------------------------------------------------------
# Helper: safe float
# ---------------------------------------------------------------------------


def _sf(val: object) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)  # type: ignore[arg-type]
        return None if pd.isna(f) else round(f, 4)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def get_kline(symbol: str, days: int) -> dict:
    try:
        df_full = _fetch_kline(symbol, days)
    except Exception as e:
        return {"error": f"K线数据获取失败：{e}"}

    if len(df_full) < 30:
        return {"error": f"数据不足（仅 {len(df_full)} 条），无法计算技术指标"}

    df_full = _compute_indicators(df_full)

    # Trim to the requested display window
    cutoff = datetime.today() - timedelta(days=days)
    df = df_full[df_full["date"] >= pd.Timestamp(cutoff)].reset_index(drop=True)

    # Support/resistance on full warm-up data so swing detection has enough range
    sr = _find_support_resistance(df_full)

    # Latest indicator snapshot
    last = df_full.iloc[-1]
    snap: dict = {
        "date": str(last["date"].date()),
        "close": _sf(last["close"]),
        "ma5": _sf(last.get("ma5")),
        "ma10": _sf(last.get("ma10")),
        "ma20": _sf(last.get("ma20")),
        "ma60": _sf(last.get("ma60")),
        "macd": _sf(last.get("macd")),
        "macd_signal": _sf(last.get("macd_signal")),
        "macd_hist": _sf(last.get("macd_hist")),
        "rsi14": _sf(last.get("rsi14")),
        "kdj_k": _sf(last.get("kdj_k")),
        "kdj_d": _sf(last.get("kdj_d")),
        "kdj_j": _sf(last.get("kdj_j")),
        "bb_upper": _sf(last.get("bb_upper")),
        "bb_mid": _sf(last.get("bb_mid")),
        "bb_lower": _sf(last.get("bb_lower")),
        "volume": _sf(last.get("volume")),
        "vol_ma5": _sf(last.get("vol_ma5")),
        "turnover": _sf(last.get("turnover")),
    }

    # MA alignment description (raw facts, model decides the narrative)
    close = float(last["close"])
    ma_positions: list[str] = []
    for n in (5, 10, 20, 60):
        v = _sf(last.get(f"ma{n}"))
        if v is not None:
            rel = "上方" if close > v else "下方"
            ma_positions.append(f"MA{n}({v}){rel}")
    snap["ma_positions"] = ma_positions

    # Summary stats for the display window
    if not df.empty:
        summary_stats = {
            "period_days": days,
            "trading_days": len(df),
            "period_high": round(float(df["high"].max()), 3),
            "period_low": round(float(df["low"].min()), 3),
            "period_avg_close": round(float(df["close"].mean()), 3),
            "period_pct_change": round(
                (float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1) * 100, 2
            ),
        }
    else:
        summary_stats = {}

    # Recent bars (last 10 trading days) — raw data for the model to reference
    bar_cols = [
        c
        for c in ["date", "open", "high", "low", "close", "volume", "pct_change", "turnover"]
        if c in df.columns
    ]
    recent_bars = (
        df[bar_cols]
        .tail(10)
        .assign(date=lambda x: x["date"].dt.strftime("%Y-%m-%d"))
        .round(3)
        .to_dict(orient="records")
    )

    return {
        "symbol": symbol,
        "indicators_latest": snap,
        "support_resistance": sr,
        "summary_stats": summary_stats,
        "recent_bars": recent_bars,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True, help="6位股票代码")
    parser.add_argument("--days", type=int, default=90, help="分析天数（默认90）")
    args = parser.parse_args()
    result = get_kline(args.symbol, args.days)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
