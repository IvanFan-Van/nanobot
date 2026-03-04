#!/usr/bin/env python3
"""Resolve a stock symbol (6-digit code or Chinese name) to (code, name).

Usage:
    python resolve_symbol.py --symbol 600111
    python resolve_symbol.py --symbol 北方稀土

Outputs JSON:
    {"code": "600111", "name": "北方稀土"}
    {"error": "..."}
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
    headers = kw.pop("headers", None) or {}
    headers.setdefault("User-Agent", _BROWSER_UA)
    kw["headers"] = headers
    return _orig(self, method, url, **kw)


requests.Session.request = _patched  # type: ignore[method-assign]

import akshare as ak  # noqa: E402


def resolve(symbol: str) -> dict[str, str]:
    symbol = symbol.strip()
    if symbol.isdigit() and len(symbol) == 6:
        try:
            info = ak.stock_individual_info_em(symbol=symbol)
            name = str(info.set_index("item").loc["股票简称", "value"])
        except Exception:
            name = symbol
        return {"code": symbol, "name": name}

    try:
        all_stocks = ak.stock_info_a_code_name()
        match = all_stocks[all_stocks["name"].str.contains(symbol, na=False)]
        if match.empty:
            return {"error": f"找不到股票：{symbol}，请提供6位数字代码或准确的股票简称"}
        row = match.iloc[0]
        return {"code": str(row["code"]), "name": str(row["name"])}
    except Exception as e:
        return {"error": f"股票查询失败：{e}"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    result = resolve(args.symbol)
    print(json.dumps(result, ensure_ascii=False))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
