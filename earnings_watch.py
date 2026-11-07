"""ウォッチ銘柄の決算チェック

スプシ「気になるテーマ株_一覧表」の「マスター」タブ（リンク共有・閲覧のみ）から銘柄を読み、
前営業日にTDnetへ出た「決算短信」「業績予想の修正」「配当予想の修正」を拾って、
決算短信PDFの1〜2ページ目（サマリー表）をAIに要約させ、LINE用の1メッセージにまとめる。

スプシへの書き戻しはしない（閲覧のみの共有のため）。
"""

import csv
import io
import os
import re
from urllib.parse import quote

import requests
from pypdf import PdfReader

# GitHubのVariables（WATCHLIST_SHEET_ID）で指定。空なら何もしない
SHEET_ID = (os.environ.get("WATCHLIST_SHEET_ID") or "").strip()
SHEET_NAME = (os.environ.get("WATCHLIST_SHEET_NAME") or "マスター").strip()

# タイトルがこれらに当てはまる開示だけ対象にする
TITLE_PATTERNS = (
    ("決算短信", re.compile(r"決算短信")),
    ("業績予想の修正", re.compile(r"業績予想.*(修正|上方|下方)|(上方|下方)修正")),
    ("配当予想の修正", re.compile(r"配当予想.*修正|配当.*(増配|減配|修正)")),
)
PDF_PAGES = 2  # 決算短信は1ページ目にサマリー表、2ページ目に注記や修正の有無が載る
PDF_MAX_CHARS = 6000


# ---------------------------------------------------------------
# 1. スプシから銘柄一覧（証券コード・企業名・状況）を読む
# ---------------------------------------------------------------
def load_watchlist():
    """マスターのCSVを読み、{証券コード: {name, status}} を返す。状況が「見送り」の銘柄は除く"""
    if not SHEET_ID:
        print("WATCHLIST_SHEET_ID が未設定のため、ウォッチ銘柄の決算チェックはしません")
        return {}
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet={quote(SHEET_NAME)}"
    res = requests.get(url, timeout=30)
    res.raise_for_status()
    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    if not rows:
        return {}
    header = rows[0]

    def col(name):
        return header.index(name) if name in header else None

    i_name, i_code, i_status = col("企業名"), col("証券コード"), col("状況")
    if i_name is None or i_code is None:
        raise RuntimeError(f"スプシの列が見つかりません: {header[:10]}")
    watch = {}
    for row in rows[1:]:
        if len(row) <= max(i_name, i_code):
            continue
        code = row[i_code].strip().upper()
        name = row[i_name].strip()
        status = row[i_status].strip() if i_status is not None and len(row) > i_status else ""
        if not code or not name or status == "見送り":
            continue
        watch[code] = {"name": name, "status": status}
    print(f"ウォッチ銘柄: {len(watch)}件")
    return watch


# ---------------------------------------------------------------
# 2. TDnetの開示から、ウォッチ銘柄の決算関連だけを拾う
# ---------------------------------------------------------------
def find_earnings_disclosures(tdnet_items, watch):
    """{code: [{kind, title, pubdate, url}, ...]} を返す"""
    found = {}
    for item in tdnet_items:
        code = item["code"]
        if code not in watch:
            continue
        kind = next((k for k, pat in TITLE_PATTERNS if pat.search(item["title"])), None)
        if not kind:
            continue
        found.setdefault(code, []).append({**item, "kind": kind})
    for code, docs in found.items():
        docs.sort(key=lambda d: (d["kind"] != "決算短信", d["pubdate"]))
    return found


# ---------------------------------------------------------------
# 3. PDFの先頭ページを文字にする
# ---------------------------------------------------------------
def extract_pdf_text(url, pages=PDF_PAGES, max_chars=PDF_MAX_CHARS):
    res = requests.get(url, timeout=60)
    res.raise_for_status()
    reader = PdfReader(io.BytesIO(res.content))
    text = "\n".join((reader.pages[i].extract_text() or "") for i in range(min(pages, len(reader.pages))))
    text = re.sub(r"[ \t　]+", " ", text)
    return text[:max_chars]


# ---------------------------------------------------------------
# 4. AIに要約させる
# ---------------------------------------------------------------
SUMMARY_PROMPT = """あなたは個人投資家向けに決算を要約するアシスタントです。
次の開示資料（決算短信や業績予想・配当予想の修正）の本文から、{name}（{code}）の決算を要約してください。

## 書き方
- 下の形式に合わせ、全体で6行以内。LINEで読むので短く
- 数字は資料にあるものだけ使う。資料に無い項目は行ごと省く。推測で数字を作らない
- 金額は「億円」に直す（百万円÷100。小数1桁まで）。増減率は資料の％をそのまま
- 通期進捗率は「累計の経常利益 ÷ 通期予想の経常利益」で計算し、整数％で書く。経常利益が無い会社は営業利益で代用し「(営業益)」と添える
- 修正の有無は、資料の「修正の有無： 有／無」や修正のお知らせから判断する。修正があれば「前→後」の数字を書く
- 一言は、決算の良し悪しを事実ベースで30文字以内（例：「増収増益・増配。進捗も順調」「減益だが通期は据え置き」）
- 株の売買をすすめる表現はしない

## 形式
📊 {name}({code}) ＜決算の種類：第1四半期／中間／第3四半期／通期＞
・売上 ○○億円(±○%) 営業益 ○○億円(±○%) 経常 ○○億円(±○%) 純利益 ○○億円(±○%)
・通期進捗：経常 ○%（通期予想○○億円）
・通期予想：据え置き／上方修正（経常○→○億円）／下方修正（…）
・配当：年○円で据え置き／増配（○→○円）／減配（○→○円）
・一言：…

## 資料
{docs}
"""


def summarize(client, model, code, info, docs, on_usage=None):
    """1銘柄分の開示をまとめてAIに渡し、要約テキストを返す"""
    parts = []
    for d in docs:
        try:
            body = extract_pdf_text(d["url"])
        except Exception as e:
            print(f"PDF取得失敗 ({code} {d['title']}):", e)
            continue
        parts.append(f"### {d['kind']}：{d['title']}（{d['pubdate']}）\n{body}")
    if not parts:
        return ""
    prompt = SUMMARY_PROMPT.format(name=info["name"], code=code, docs="\n\n".join(parts))
    res = client.messages.create(model=model, max_tokens=600, messages=[{"role": "user", "content": prompt}])
    if on_usage:
        on_usage(res)
    return res.content[0].text.strip()


# ---------------------------------------------------------------
# 5. まとめて1メッセージにする
# ---------------------------------------------------------------
def run_earnings_watch(client, model, tdnet_items, day, on_usage=None):
    """LINEに送る本文を返す。該当がなければ空文字"""
    watch = load_watchlist()
    if not watch:
        return ""
    found = find_earnings_disclosures(tdnet_items, watch)
    if not found:
        print("ウォッチ銘柄の決算開示はありませんでした")
        return ""
    print(f"ウォッチ銘柄の決算開示: {[(c, len(d)) for c, d in found.items()]}")

    blocks = []
    for code, docs in found.items():
        info = watch[code]
        try:
            text = summarize(client, model, code, info, docs, on_usage)
        except Exception as e:
            print(f"決算要約に失敗 ({code}):", e)
            text = ""
        if not text:
            titles = "／".join(d["title"][:40] for d in docs)
            text = f"📊 {info['name']}({code})\n・開示あり（要約できず）：{titles}"
        if info.get("status") == "購入済":
            text = text.replace("📊", "📊★保有", 1)
        blocks.append(text)

    header = f"【{day:%m/%d}の決算】ウォッチ銘柄 {len(blocks)}社\n※スプシ「マスター」の備考は手で更新してください"
    return (header + "\n\n" + "\n\n".join(blocks))[:4900]
