#!/usr/bin/env python3
"""Fetch recent news and announcements for an A-share stock.

Usage:
    python get_news.py --symbol 600111 [--count 10]

Outputs JSON with a list of recent news items (title, date, source, url).
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


def get_news(symbol: str, count: int = 10) -> dict:
    items: list[dict] = []
    errors: list[str] = []

    # Primary: East Money individual stock news
    try:
        df = ak.stock_news_em(symbol=symbol)
        if not df.empty:
            for _, row in df.head(count).iterrows():
                items.append(
                    {
                        "title": str(row.get("新闻标题", row.get("title", ""))),
                        "date": str(row.get("发布时间", row.get("date", ""))),
                        "source": str(row.get("新闻来源", row.get("source", "东方财富"))),
                        "url": str(row.get("新闻链接", row.get("url", ""))),
                    }
                )
    except Exception as e:
        errors.append(f"东方财富新闻获取失败：{e}")

    if not items:
        # Fallback: CNINF announcements (公告)
        try:
            df2 = ak.stock_notice_report(symbol=symbol)
            if not df2.empty:
                for _, row in df2.head(count).iterrows():
                    items.append(
                        {
                            "title": str(row.get("公告标题", "")),
                            "date": str(row.get("公告日期", "")),
                            "source": "上交所/深交所公告",
                            "url": str(row.get("公告链接", "")),
                        }
                    )
        except Exception as e:
            errors.append(f"公告获取失败：{e}")

    result: dict = {"symbol": symbol, "news": items}
    if errors and not items:
        result["error"] = "；".join(errors)
    elif errors:
        result["warnings"] = errors
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True, help="6位股票代码")
    parser.add_argument("--count", type=int, default=10, help="返回条数（默认10）")
    args = parser.parse_args()
    result = get_news(args.symbol, args.count)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
