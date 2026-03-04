#!/usr/bin/env python3
"""Fetch key financial data (income statement, ROE, EPS) for an A-share stock.

Usage:
    python get_financial.py --symbol 600111 [--periods 4]

Outputs JSON with recent quarterly/annual financial highlights:
  revenue, net_profit, gross_margin, roe, eps, yoy growth, etc.
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
import pandas as pd  # noqa: E402


def _safe(val: object) -> str | float | None:
    """Convert a cell value to a JSON-safe scalar."""
    if val is None:
        return None
    try:
        f = float(val)  # type: ignore[arg-type]
        return None if pd.isna(f) else round(f, 4)
    except (ValueError, TypeError):
        return str(val).strip() or None


def get_financial(symbol: str, periods: int = 4) -> dict:
    result: dict = {"symbol": symbol}
    errors: list[str] = []

    # --- Financial abstract (同花顺摘要) ---
    try:
        df = ak.stock_financial_abstract_ths(symbol=symbol, indicator="按报告期")
        if not df.empty:
            rows = []
            for _, row in df.head(periods).iterrows():
                rows.append({str(k): _safe(v) for k, v in row.items()})
            result["financial_abstract"] = rows
    except Exception as e:
        errors.append(f"财务摘要（同花顺）获取失败：{e}")

    # --- Key per-share indicators (每股指标) ---
    try:
        df2 = ak.stock_per_share_indicators_em(symbol=symbol)
        if not df2.empty:
            rows2 = []
            for _, row in df2.head(periods).iterrows():
                rows2.append({str(k): _safe(v) for k, v in row.items()})
            result["per_share_indicators"] = rows2
    except Exception as e:
        errors.append(f"每股指标获取失败：{e}")

    # --- Profit sheet from East Money as fallback ---
    if "financial_abstract" not in result and "per_share_indicators" not in result:
        try:
            df3 = ak.stock_profit_sheet_by_report_em(symbol=symbol)
            if not df3.empty:
                # Transpose: columns are periods, rows are items
                records = []
                for col in df3.columns[:periods]:
                    entry = {"period": str(col)}
                    for idx in df3.index:
                        entry[str(idx)] = _safe(df3.loc[idx, col])
                    records.append(entry)
                result["profit_sheet"] = records
        except Exception as e:
            errors.append(f"利润表获取失败：{e}")

    if errors:
        result["warnings"] = errors

    if not any(k in result for k in ("financial_abstract", "per_share_indicators", "profit_sheet")):
        result["error"] = "所有财务数据接口均失败：" + "；".join(errors)

    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True, help="6位股票代码")
    parser.add_argument("--periods", type=int, default=4, help="返回报告期数（默认4期）")
    args = parser.parse_args()
    result = get_financial(args.symbol, args.periods)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
