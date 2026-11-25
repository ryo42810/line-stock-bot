"""値下がりアラートの銘柄一覧（alert_watchlist.json）を、スプシのExcel形式のファイルから作る

スプシ「気になるテーマ株_一覧表」は会社のGoogleアカウントにあり、社外に公開できない（botからは読めない）。
そのため、銘柄を入れかえたときは、スプシをExcel形式で書き出して、このスクリプトで一覧を作り直す。
  python scripts/make_alert_watchlist.py 書き出したファイル.xlsx
一覧に入れるのは、コード・銘柄名・Diego式か成長株か・1株配当だけ（保有株数や取得単価は入れない）
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import openpyxl  # noqa: E402

import price_alert  # noqa: E402


def main(path):
    stocks = price_alert.parse_workbook(openpyxl.load_workbook(path, read_only=True, data_only=True))
    rows = [{"code": code, "name": s["name"], "groups": s["groups"], "div": s["div"]} for code, s in sorted(stocks.items())]
    with open(price_alert.FALLBACK_FILE, "w", encoding="utf-8") as f:
        f.write("[\n" + ",\n".join(" " + json.dumps(r, ensure_ascii=False) for r in rows) + "\n]\n")
    print(f"{price_alert.FALLBACK_FILE} を作りました: {len(rows)}銘柄")


if __name__ == "__main__":
    main(sys.argv[1])
