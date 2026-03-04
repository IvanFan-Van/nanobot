#!/usr/bin/env python3
"""Fetch company profile for an A-share stock.

Usage:
    python get_company_info.py --symbol 600111

Outputs JSON with industry, listing date, share structure, and a brief business description.
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


def get_company_info(symbol: str) -> dict:
    result: dict = {"symbol": symbol}
    try:
        info_df = ak.stock_individual_info_em(symbol=symbol)
        info = info_df.set_index("item")["value"].to_dict()
        result.update(
            {
                "name": str(info.get("股票简称", "")),
                "full_name": str(info.get("公司名称", "")),
                "industry": str(info.get("行业", "未知")),
                "listing_date": str(info.get("上市时间", "未知")),
                "total_shares": str(info.get("总股本", "未知")),
                "float_shares": str(info.get("流通股", "未知")),
                "region": str(info.get("地区", "未知")),
            }
        )
    except Exception as e:
        result["error_individual_info"] = str(e)

    # Try to get a brief business introduction
    try:
        desc_df = ak.stock_profile_cninfo(symbol=symbol)
        if not desc_df.empty:
            # The dataframe typically has columns like 主营业务, 经营范围, etc.
            for col in desc_df.columns:
                result[str(col)] = str(desc_df.iloc[0][col])
    except Exception:
        pass  # description is optional

    if "name" not in result or not result.get("name"):
        return {"error": f"无法获取 {symbol} 的公司信息"}

    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True, help="6位股票代码")
    args = parser.parse_args()
    result = get_company_info(args.symbol)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
