"""決算発表の予定日（日本取引所グループ＝JPXが公開しているExcel）を読む。

JPXのページには「翌営業日に発表する会社の一覧」と「月ごとの一覧」のExcelが置いてある。
ファイル名は時期で変わるので、ページからExcelのリンクを毎回探して、全部読む。
取れなかったときは空のリストを返す（呼び出し側で別の方法に切り替える）。
"""
import datetime
import io
import re
import unicodedata
from urllib.parse import urljoin

import requests
from openpyxl import load_workbook

INDEX_URL = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (line-stock-bot; personal use)"}

_cache = None  # 1回の実行で何度もダウンロードしない


def _to_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return None


def parse_workbook(content):
    """Excelの中身から [{date, code, name, kind, market}] を取り出す"""
    rows = []
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            if not row or len(row) < 3:
                continue
            day = _to_date(row[0])
            code = str(row[1]).strip().upper() if row[1] is not None else ""
            if code.endswith(".0"):
                code = code[:-2]
            if not day or not re.fullmatch(r"\d{3}[0-9A-Z]", code) or not row[2]:
                continue  # 見出しや注記の行
            rows.append(
                {
                    "date": day,
                    "code": code,
                    # 会社名は全角英数（ＦＲＯＮＴＥＯ）や全角スペース入りなので、ふつうの表記に直す
                    "name": re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(row[2]))),
                    "kind": str(row[7]).strip() if len(row) > 7 and row[7] else "",
                    "market": str(row[9]).strip() if len(row) > 9 and row[9] else "",
                }
            )
    return rows


def load_all():
    """JPXのページにあるExcelを全部読んで、発表予定の一覧を返す（同じ会社・同じ日の重複は除く）"""
    global _cache
    if _cache is not None:
        return _cache
    rows = {}
    try:
        res = requests.get(INDEX_URL, headers=HEADERS, timeout=30)
        res.raise_for_status()
        links = list(dict.fromkeys(re.findall(r'href="([^"]+\.xlsx?)"', res.text)))
        for link in links:
            if not link.lower().endswith(".xlsx"):
                continue
            try:
                file_res = requests.get(urljoin(INDEX_URL, link), headers=HEADERS, timeout=60)
                file_res.raise_for_status()
                for r in parse_workbook(file_res.content):
                    rows.setdefault((r["date"], r["code"]), r)
            except Exception as e:
                print(f"決算予定のExcelを読めませんでした ({link}):", e)
        print(f"JPXの決算発表予定: Excel {len(links)}件 / {len(rows)}行")
    except Exception as e:
        print("JPXの決算発表予定ページを取得できませんでした:", e)
    _cache = list(rows.values())
    return _cache


def scheduled_on(day, priority_codes=()):
    """その日に決算発表を予定している会社。優先したい銘柄（ウォッチ・配信済み）→プライム→その他の順に並べる"""
    priority = set(priority_codes)
    found = [r for r in load_all() if r["date"] == day]
    found.sort(key=lambda r: (r["code"] not in priority, r["market"] != "プライム", r["code"]))
    return found


def as_text(rows, priority_codes=(), limit=80):
    """AIに渡す一覧の文章。★は優先したい銘柄"""
    priority = set(priority_codes)
    lines = [
        f"- {'★' if r['code'] in priority else ''}{r['name']}({r['code']}) {r['kind']} / {r['market']}".rstrip(" /")
        for r in rows[:limit]
    ]
    if len(rows) > limit:
        lines.append(f"（ほか{len(rows) - limit}社）")
    return "\n".join(lines)
