#!/usr/bin/env python3
"""Fetch real-time quote data for an A-share stock.

Usage:
    python get_realtime.py --symbol 600111

Outputs JSON with live price, change, PE, PB, market cap, turnover rate, etc.
"""

from __future__ import annotations

import argparse
import json
import sys

import requests

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


def get_realtime(symbol: str) -> dict:
    try:
        spot = ak.stock_zh_a_spot_em()
        row = spot[spot["代码"] == symbol]
        if row.empty:
            return {"error": f"未找到股票 {symbol} 的实时数据，市场可能已收盘或代码有误"}
        r = row.iloc[0]

        def _f(key: str) -> float | None:
            v = r.get(key)
            try:
                return float(v) if v is not None and str(v) not in ("", "-", "nan") else None
            except (ValueError, TypeError):
                return None

        market_cap = _f("总市值")
        return {
            "symbol": symbol,
            "name": str(r.get("名称", "")),
            "price": _f("最新价"),
            "pct_change_today": _f("涨跌幅"),
            "change_amount": _f("涨跌额"),
            "open": _f("今开"),
            "high": _f("最高"),
            "low": _f("最低"),
            "prev_close": _f("昨收"),
            "volume_lot": _f("成交量"),  # 手
            "amount_yuan": _f("成交额"),  # 元
            "turnover_rate": _f("换手率"),  # %
            "pe_dynamic": _f("市盈率-动态"),
            "pb": _f("市净率"),
            "total_market_cap_yi": round(market_cap / 1e8, 2) if market_cap else None,  # 亿元
            "float_market_cap_yi": (
                round(float(r.get("流通市值")) / 1e8, 2)
                if r.get("流通市值") not in (None, "", "-")
                else None
            ),
            "pct_60d": _f("60日涨跌幅"),
            "pct_ytd": _f("年初至今涨跌幅"),
        }
    except Exception as e:
        return {"error": f"实时行情获取失败：{e}"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True, help="6位股票代码")
    args = parser.parse_args()
    result = get_realtime(args.symbol)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
