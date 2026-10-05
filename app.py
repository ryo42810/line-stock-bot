import calendar
import datetime
import json
import os
import re
import sys
import traceback
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import anthropic
import jpholiday
import requests
import yfinance as yf
from bs4 import BeautifulSoup

# GitHubのSecretsから取得し、先頭・末尾の余計な空白や改行を除去（strip）
LINE_ACCESS_TOKEN = (os.environ.get("LINE_ACCESS_TOKEN") or "").strip()
USER_ID = (os.environ.get("USER_ID") or "").strip()

MODEL = "claude-haiku-4-5"
JST = ZoneInfo("Asia/Tokyo")
MIN_PICKS = 5
MAX_PICKS = 15
BUBBLES_PER_CAROUSEL = 12  # LINEのカルーセルは1つにつき最大12枚

HISTORY_FILE = "history.json"
HISTORY_KEEP_DAYS = 60  # 成績集計用に60日分残す
WEEKLY_LIMIT = 2  # 同じ銘柄は直近7日間で2回まで
NOTIFIED_FLAG = "notified.flag"  # エラー通知済みの目印（ワークフロー側の二重通知防止）

CATEGORY_STYLE = {
    "好材料": {"color": "#1DB446", "label": "📈 好材料"},
    "懸念材料": {"color": "#E0352B", "label": "📉 懸念材料"},
    "決算": {"color": "#2D7FF9", "label": "📊 今日決算"},
    "優待": {"color": "#F5A623", "label": "🎁 今月の優待"},
}

# AIが使えないとき（案A方式）のキーワード判定用
GOOD_WORDS = ["上方修正", "増配", "自社株買い", "自己株式の取得", "株式分割", "優待新設", "優待拡充", "復配", "最高益", "黒字転換"]
BAD_WORDS = ["下方修正", "減配", "無配", "赤字", "優待廃止", "特別損失", "減損", "業務停止", "不適切", "上場廃止"]

# 料金の目安（Claude Haiku 4.5、USD）
PRICE_INPUT = 1.00 / 1_000_000
PRICE_OUTPUT = 5.00 / 1_000_000

usage_total = {"input": 0, "output": 0}


def now_jst():
    return datetime.datetime.now(JST)


def track_usage(response):
    usage_total["input"] += response.usage.input_tokens or 0
    usage_total["output"] += response.usage.output_tokens or 0


def print_cost(usd_jpy=None):
    usd = usage_total["input"] * PRICE_INPUT + usage_total["output"] * PRICE_OUTPUT
    rate = usd_jpy or 150
    print("===== API料金（推定） =====")
    print(f"入力 {usage_total['input']:,} / 出力 {usage_total['output']:,} トークン")
    print(f"今回: 約${usd:.3f}（約{usd * rate:.1f}円） / 30日換算: 約${usd * 30:.2f}（約{usd * rate * 30:,.0f}円）")


# ---------------------------------------------------------------
# 0. 営業日カレンダー（土日・祝日・年末年始は休場）
# ---------------------------------------------------------------
def is_market_holiday(d):
    if d.weekday() >= 5 or jpholiday.is_holiday(d):
        return True
    return (d.month, d.day) in [(12, 31), (1, 1), (1, 2), (1, 3)]


def prev_business_day(d):
    d -= datetime.timedelta(days=1)
    while is_market_holiday(d):
        d -= datetime.timedelta(days=1)
    return d


def business_days_between(start, end):
    """start の翌日から end まで（end含む）の営業日数"""
    count = 0
    d = start
    while d < end:
        d += datetime.timedelta(days=1)
        if not is_market_holiday(d):
            count += 1
    return count


def kenri_last_day(year, month, day=0):
    """権利付き最終日（権利確定日の2営業日前）。day=0 は月末"""
    last = calendar.monthrange(year, month)[1]
    d = datetime.date(year, month, min(day, last) if day else last)
    while is_market_holiday(d):
        d -= datetime.timedelta(days=1)
    for _ in range(2):
        d = prev_business_day(d)
    return d


def kenri_countdown(today, months, day):
    """今月か来月の権利付き最終日までのカウントダウン文言。対象外なら空文字"""
    if not months:
        return ""
    for offset in range(2):
        y, m = (today.year, today.month + offset) if today.month + offset <= 12 else (today.year + 1, 1)
        if m not in months:
            continue
        last_day = kenri_last_day(y, m, day)
        if last_day < today:
            continue
        if last_day == today:
            return f"⏰ 今日が権利付き最終日（{m}月権利）"
        return f"⏰ 権利付き最終日 {last_day:%m/%d}（あと{business_days_between(today, last_day)}営業日）"
    return ""


# ---------------------------------------------------------------
# 1. 配信履歴（同じ銘柄は1週間で2回まで）
# ---------------------------------------------------------------
def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def history_status(history, today):
    """直近7日で上限に達した銘柄と、前回配信した銘柄を返す"""
    counts = {}
    prev_codes = []
    prev_date = ""
    for date_str, entries in history.items():
        codes = [e["code"] if isinstance(e, dict) else e for e in entries]
        d = datetime.date.fromisoformat(date_str)
        if d >= today:
            continue  # 当日分（手動実行のやり直し）は数えない
        if (today - d).days < 7:
            for c in codes:
                counts[c] = counts.get(c, 0) + 1
        if date_str > prev_date:
            prev_date, prev_codes = date_str, codes
    blocked = {c for c, n in counts.items() if n >= WEEKLY_LIMIT}
    return blocked, set(prev_codes)


def save_history(history, today, picks):
    history[today.isoformat()] = [
        {"code": p["code"], "name": p["name"], "category": p["category"], "price": p.get("price")} for p in picks
    ]
    cutoff = (today - datetime.timedelta(days=HISTORY_KEEP_DAYS)).isoformat()
    history = {k: v for k, v in sorted(history.items()) if k >= cutoff}
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------
# 2. 適時開示（TDnet）の取得
# ---------------------------------------------------------------
def fetch_tdnet(days):
    """当日・前日などの適時開示を取得。取れなくても処理は続ける"""
    items = []
    for day in days:
        url = f"https://webapi.yanoshin.jp/webapi/tdnet/list/{day:%Y%m%d}.json?limit=500"
        try:
            res = requests.get(url, timeout=20)
            res.raise_for_status()
            for row in res.json().get("items", []):
                t = row.get("Tdnet", {})
                code = str(t.get("company_code", ""))[:4]
                if code and t.get("title"):
                    items.append(
                        {
                            "code": code,
                            "name": t.get("company_name", ""),
                            "title": t.get("title", ""),
                            "pubdate": t.get("pubdate", ""),
                            "url": t.get("document_url", ""),
                        }
                    )
        except Exception as e:
            print(f"TDnet取得失敗 ({day:%Y-%m-%d}):", e)
    return items


def pick_material_disclosures(tdnet_items):
    """キーワードで材料になりそうな開示だけに絞る"""
    picked = []
    for item in tdnet_items:
        title = item["title"]
        if "取得状況" in title:
            continue  # 自己株式の取得状況などの定期報告は除外
        if any(w in title for w in GOOD_WORDS):
            picked.append({**item, "category": "好材料"})
        elif any(w in title for w in BAD_WORDS):
            picked.append({**item, "category": "懸念材料"})
    return picked


# ---------------------------------------------------------------
# 3. Yahoo!ファイナンス・日経のページ取得（スクレイピング）
# ---------------------------------------------------------------
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.8",
}

# 話題の銘柄を探すときに読むページ
# （みんかぶは403、株探は405でGitHub Actionsから取れないため、Yahoo!ファイナンスに寄せた）
TOPIC_PAGES = [
    "https://finance.yahoo.co.jp/",
    "https://finance.yahoo.co.jp/stocks/ranking/hot",
    "https://finance.yahoo.co.jp/stocks/ranking/up",
    "https://finance.yahoo.co.jp/stocks/ranking/down",
]

# 今日の決算発表予定を探すページの候補（取れたものだけ使う）
EARNINGS_PAGES = [
    "https://www.nikkei.com/markets/kigyo/money-schedule/kessan/",
]

# 優待一覧ページの候補（取れたものだけ使う）
YUTAI_LIST_PAGES = [
    "https://finance.yahoo.co.jp/stocks/incentive",
]

# サイト内のリンクから、決算予定・優待一覧のページを自動で探すための入口
# （優待のある銘柄の優待ページから、優待ランキング・権利月別一覧へのリンクを探す）
INDEX_PAGES = [
    "https://finance.yahoo.co.jp/",
    "https://finance.yahoo.co.jp/quote/2702.T/incentive",
]

# 各銘柄の優待ページ（上から順に試す）
YUTAI_STOCK_PAGES = [
    "https://finance.yahoo.co.jp/quote/{code}.T/incentive",
]


def fetch_page_text(url, max_chars=10000, start_word=None, keep_links=True, require_word=None):
    """ページを取ってきてテキストにする。失敗・中身なしなら空文字"""
    try:
        res = requests.get(url, headers=HTTP_HEADERS, timeout=20)
        res.raise_for_status()
        res.encoding = res.apparent_encoding or res.encoding
    except Exception as e:
        print(f"ページ取得失敗 ({url}):", e)
        return ""

    soup = BeautifulSoup(res.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    if keep_links:
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            if text:
                a.replace_with(f"{text} <{urljoin(url, a['href'])}>")
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    text = "\n".join(line for line in lines if line)

    if require_word and require_word not in text:
        print(f"ページに「{require_word}」が見つからず ({url})")
        return ""
    if start_word and start_word in text:
        text = text[max(0, text.index(start_word) - 200):]
    return text[:max_chars]


def fetch_pages(urls, limit=None, **kwargs):
    blocks = []
    for url in dict.fromkeys(urls):
        if limit and len(blocks) >= limit:
            break
        text = fetch_page_text(url, **kwargs)
        if text:
            blocks.append(f"<page url=\"{url}\">\n{text}\n</page>")
    return blocks


def discover_links(index_urls):
    """入口ページのリンク一覧（文字, URL）を返す。ログにも出す"""
    links = []
    for url in index_urls:
        try:
            res = requests.get(url, headers=HTTP_HEADERS, timeout=20)
            res.raise_for_status()
            res.encoding = res.apparent_encoding or res.encoding
        except Exception as e:
            print(f"入口ページ取得失敗 ({url}):", e)
            continue
        soup = BeautifulSoup(res.text, "html.parser")
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            href = urljoin(url, a["href"])
            if text and href.startswith("http"):
                links.append((text, href))
    found = [(t, h) for t, h in links if any(w in t for w in ["優待", "決算", "ランキング"])]
    print("見つかったリンク（優待・決算・ランキング）:")
    for t, h in dict.fromkeys(found):
        print(f"  {t[:30]} -> {h}")
    return links


def links_matching(links, must, any_of=()):
    return [h for t, h in links if must in t and (not any_of or any(w in t for w in any_of))]


# ---------------------------------------------------------------
# 4. 地合い（日経平均・ドル円・米国株）
# ---------------------------------------------------------------
MARKET_TICKERS = [
    ("^N225", "日経平均", "{:,.0f}"),
    ("JPY=X", "ドル円", "{:,.2f}"),
    ("^DJI", "NYダウ", "{:,.0f}"),
    ("^IXIC", "ナスダック", "{:,.0f}"),
    ("^GSPC", "S&P500", "{:,.0f}"),
]


def fetch_close_change(symbol):
    """直近の終値と前日比(%)。取れなければ (None, None)"""
    closes = yf.Ticker(symbol).history(period="10d")["Close"].dropna()
    if len(closes) < 2:
        return None, None
    last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
    return last, (last - prev) / prev * 100


def fetch_market_data():
    rows = []
    for symbol, name, fmt in MARKET_TICKERS:
        try:
            last, change = fetch_close_change(symbol)
            if last is not None:
                rows.append({"symbol": symbol, "name": name, "value": fmt.format(last), "raw": last, "change": change})
        except Exception as e:
            print(f"地合いデータ取得失敗 ({symbol}):", e)
    return rows


# ---------------------------------------------------------------
# 5. Claudeで銘柄の選定（Web検索なし。取ってきたページだけで判断）
# ---------------------------------------------------------------
PICKS_SCHEMA = {
    "type": "object",
    "properties": {
        "market_comment": {"type": "string", "description": "今日の地合いのまとめ（80字程度）"},
        "x_post": {"type": "string", "description": "X（旧Twitter）投稿用の下書き。130字以内"},
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "4桁の証券コード（例: 7203, 130A）"},
                    "name": {"type": "string"},
                    "category": {"type": "string", "enum": ["好材料", "懸念材料", "決算", "優待"]},
                    "headline": {"type": "string", "description": "選んだ理由の要約（40字程度）"},
                    "source_url": {"type": "string", "description": "根拠記事のURL（ページ内のリンクから）。なければ空文字"},
                },
                "required": ["code", "name", "category", "headline", "source_url"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_comment", "x_post", "picks"],
    "additionalProperties": False,
}


def pick_with_claude(client, today, market_closed, disclosures, blocked_codes, market_rows):
    weekday_str = "月火水木金土日"[today.weekday()]
    disclosure_text = "\n".join(
        f"- [{d['category']}] {d['code']} {d['name']}: {d['title']} ({d['pubdate']})"
        for d in disclosures[:60]
    ) or "（取得できず）"

    topic_blocks = fetch_pages(TOPIC_PAGES)
    print(f"話題ページ取得: {len(topic_blocks)}/{len(TOPIC_PAGES)}件")

    links = discover_links(INDEX_PAGES)
    months = [today.month, today.month % 12 + 1]

    # 優待一覧（固定の候補＋サイト内で見つけた「優待」リンク）
    yutai_urls = YUTAI_LIST_PAGES + [
        u for u in links_matching(links, "優待") if "/news/" not in u and "/quote/" not in u
    ]
    yutai_blocks = fetch_pages(yutai_urls, limit=2, max_chars=6000, require_word="優待")
    print(f"優待一覧ページ取得: {len(yutai_blocks)}件（候補{len(set(yutai_urls))}件）")

    earnings_blocks = []
    if not market_closed:
        earnings_urls = EARNINGS_PAGES + links_matching(links, "決算", ["予定", "スケジュール", "カレンダー", "発表日"])
        earnings_blocks = fetch_pages(earnings_urls, limit=2, max_chars=6000, require_word="決算")
        print(f"決算予定ページ取得: {len(earnings_blocks)}件（候補{len(set(earnings_urls))}件）")

    market_text = "\n".join(f"- {r['name']}: {r['value']}（前日比 {r['change']:+.2f}%）" for r in market_rows) or "（取得できず）"
    blocked_text = "、".join(sorted(blocked_codes)) or "なし"

    if market_closed:
        market_note = "今日は休場日（土日祝）。株価は動かないので、前営業日〜今日までに出たニュースと、権利確定が近い優待株を中心に選ぶ。決算銘柄は選ばない。"
        earnings_rule = ""
    else:
        market_note = "今日は営業日。前営業日の引け後〜今朝までに出たニュース・開示で、今日の株価に影響しそうな銘柄を優先する。"
        earnings_rule = f"4. 「決算発表予定のページ」に今日（{today:%m月%d日}）発表予定の銘柄があれば、注目度の高いものを2〜4件選ぶ。分類は「決算」。ページがなければ選ばない\n"

    def section(blocks):
        return "\n\n".join(blocks) or "（取得できず）"

    prompt = f"""今日は{today:%Y年%m月%d日}（{weekday_str}曜）です。毎朝自動でLINEに送る「今日の注目株」リストの銘柄を選んでください。
これは自動処理です。下に渡した情報だけを使い、書いていないことは推測しないでください。

{market_note}

## 選び方
1. 「話題・ニュースのページ」で話題・トピックになっている銘柄を拾う（ニュース見出し、ランキング、注目銘柄など）
   - 好材料（上方修正、増配、自社株買い、大型受注、提携など）
   - 懸念材料（下方修正、減配、不祥事、優待廃止など）
2. TDnetの適時開示も参考にする
3. 枠が余るときは「優待一覧のページ」から、{months[0]}月か{months[1]}月が権利確定月で優待が魅力的な銘柄を加える。分類は「優待」
{earnings_rule}5. 次の銘柄は直近1週間で配信済みなので選ばない: {blocked_text}
6. 合計{MIN_PICKS}〜{MAX_PICKS}件。10件以上を目標にする。根拠の弱い銘柄は入れない
7. 証券コードはページに書いてあるものだけを使う。わからない銘柄は入れない
8. market_comment には、地合いデータとニュースから今日の日本株の見通しを80字程度で書く
9. x_post には、X（旧Twitter）にそのまま投稿できる文章を130字以内で書く
   - 地合いを一言＋注目銘柄2〜3件（銘柄名とコード、理由を短く）
   - 最後に「※投資判断はご自身で」と、ハッシュタグ #日本株 を付ける
   - 「必ず上がる」などの断定や煽る表現は使わない

## 今日の地合いデータ（前営業日終値ベース）
{market_text}

## 話題・ニュースのページ
{section(topic_blocks)}

## 決算発表予定のページ
{section(earnings_blocks)}

## 優待一覧のページ
{section(yutai_blocks)}

## TDnetの適時開示（キーワードで抽出したもの）
{disclosure_text}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        output_config={"format": {"type": "json_schema", "schema": PICKS_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    track_usage(response)
    if response.stop_reason == "refusal":
        raise RuntimeError("Claudeの選定が拒否されました")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    print("地合いコメント:", data["market_comment"])

    # 4. AIが返したURLが、実際に読んだページ・開示の中にあるかを確認（なければリンクを出さない）
    allowed_urls = set(re.findall(r"<(https?://[^>\s]+)>", "\n".join(topic_blocks + earnings_blocks + yutai_blocks)))
    allowed_urls |= {d["url"] for d in disclosures if d.get("url")}
    for pick in data["picks"]:
        if pick.get("source_url") and pick["source_url"] not in allowed_urls:
            print(f"根拠URLが取得ページに無いため除外 ({pick.get('code')}): {pick['source_url']}")
            pick["source_url"] = ""
    return data["market_comment"], data["picks"], data["x_post"]


def clean_picks(picks, blocked_codes):
    """コードの形式チェック・重複除去・配信上限の除外・件数上限"""
    seen = set()
    result = []
    for p in picks:
        code = p.get("code", "").strip().upper()
        if not re.fullmatch(r"\d{3}[0-9A-Z]", code) or code in seen or code in blocked_codes:
            continue
        seen.add(code)
        result.append({"yutai": "不明", "kenri_month": "不明", **p, "code": code})
    return result[:MAX_PICKS]


def fallback_picks(disclosures):
    """AIが使えないとき：TDnetのキーワード判定だけで選ぶ（案A方式）"""
    return [
        {
            "code": d["code"],
            "name": d["name"],
            "category": d["category"],
            "headline": d["title"][:60],
            "source_url": d["url"],
        }
        for d in disclosures
    ]


# ---------------------------------------------------------------
# 6. 優待内容・権利確定月（Yahoo!ファイナンスの優待ページから）
# ---------------------------------------------------------------
YUTAI_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "yutai": {"type": "string", "description": "優待内容を60字以内で要約（例: 自社店舗で使える買物券3,000円分）。優待がない銘柄は「なし」、ページから読み取れなければ「不明」"},
                    "kenri_month": {"type": "string", "description": "権利確定月（例: 3月・9月）。優待がない場合は「-」、読み取れなければ「不明」"},
                    "kenri_months": {"type": "array", "items": {"type": "integer"}, "description": "権利確定月を数字で（例: [3, 9]）。不明なら空配列"},
                    "kenri_day": {"type": "integer", "description": "権利確定日の日付。月末なら0。20日なら20。不明なら0"},
                    "min_shares": {"type": "integer", "description": "優待を受け取れる最低株数（例: 100）。不明なら100"},
                    "yutai_value_yen": {"type": "integer", "description": "最低株数で1年間にもらえる優待の金額換算（円）。権利が年2回なら2回分の合計。金額換算できない・不明なら0"},
                },
                "required": ["code", "yutai", "kenri_month", "kenri_months", "kenri_day", "min_shares", "yutai_value_yen"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def add_yutai_info(client, picks):
    """各銘柄のYahoo!ファイナンス優待ページを取ってきて、AIで優待内容と権利確定月を抜き出す"""
    page_blocks = []
    for p in picks:
        for template in YUTAI_STOCK_PAGES:
            url = template.format(code=p["code"])
            text = fetch_page_text(url, max_chars=3000, start_word="優待", keep_links=False, require_word="優待")
            if text:
                page_blocks.append(f"<page code=\"{p['code']}\" name=\"{p['name']}\" url=\"{url}\">\n{text}\n</page>")
                break
    print(f"優待ページ取得: {len(page_blocks)}/{len(picks)}件")
    if not page_blocks:
        return picks

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            output_config={"format": {"type": "json_schema", "schema": YUTAI_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": "次は各銘柄の株主優待ページです。銘柄ごとに、現在の優待内容・権利確定月・最低株数・優待の金額換算を抜き出してください。"
                    "ページに書いていないことは推測せず「不明」や0にしてください。\n\n" + "\n\n".join(page_blocks),
                }
            ],
        )
        track_usage(response)
        if response.stop_reason == "refusal":
            raise RuntimeError("拒否されました")
        text = next(b.text for b in response.content if b.type == "text")
        found = {item["code"].upper(): item for item in json.loads(text)["items"]}
    except Exception as e:
        print("優待情報の抜き出しに失敗:", e)
        return picks

    for p in picks:
        item = found.get(p["code"])
        if item:
            p["yutai"] = item["yutai"] or "不明"
            p["kenri_month"] = item["kenri_month"] or "不明"
            p["kenri_months"] = [m for m in item["kenri_months"] if 1 <= m <= 12]
            p["kenri_day"] = item["kenri_day"] if 0 <= item["kenri_day"] <= 31 else 0
            p["min_shares"] = item["min_shares"] if item["min_shares"] > 0 else 100
            p["yutai_value_yen"] = max(item["yutai_value_yen"], 0)
    return picks


# ---------------------------------------------------------------
# 7. 株価・前日比・利回り
# ---------------------------------------------------------------
def technical_signals(hist, today):
    """出来高急増・年初来高値/安値更新・25日線乖離のマーク"""
    signals = []
    closes = hist["Close"].dropna()
    volumes = hist["Volume"].dropna()

    if len(volumes) >= 21:
        avg20 = float(volumes.iloc[-21:-1].mean())
        if avg20 > 0 and volumes.iloc[-1] >= avg20 * 2:
            signals.append(f"🔥 出来高急増（20日平均の{volumes.iloc[-1] / avg20:.1f}倍）")

    this_year = hist[hist.index.year == hist.index[-1].year]
    if len(this_year) >= 2:
        if float(this_year["High"].iloc[-1]) >= float(this_year["High"].iloc[:-1].max()):
            signals.append("📈 年初来高値更新")
        elif float(this_year["Low"].iloc[-1]) <= float(this_year["Low"].iloc[:-1].min()):
            signals.append("📉 年初来安値更新")

    if len(closes) >= 25:
        ma25 = float(closes.iloc[-25:].mean())
        gap = (float(closes.iloc[-1]) - ma25) / ma25 * 100
        note = "（過熱気味）" if gap >= 10 else "（売られすぎ）" if gap <= -10 else ""
        signals.append(f"25日線から{gap:+.1f}%{note}")
    return signals


def add_price_info(picks, today=None):
    valid = []
    for p in picks:
        p["price_fmt"] = "取得できず"
        p["change_fmt"] = ""
        p["change"] = None
        p["yield_fmt"] = "取得できず"
        p["signals"] = []
        try:
            ticker = yf.Ticker(f"{p['code']}.T")
            hist = ticker.history(period="1y").dropna(subset=["Close"])
            price, change = None, None
            if len(hist) >= 2:
                price = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2])
                change = (price - prev) / prev * 100
            if not price:
                print(f"株価なしのため除外 ({p['code']} {p['name']})")
                continue
            valid.append(p)
            p["price"] = price
            try:
                p["signals"] = technical_signals(hist, today)
            except Exception as e:
                print(f"テクニカル計算失敗 ({p['code']}):", e)
            p["price_fmt"] = f"約{price * 100 / 10000:.1f}万円 ({price:,.0f}円)"
            if change is not None:
                p["change"] = change
                p["change_fmt"] = f"{change:+.1f}%"

            # 配当利回りは「年間配当÷株価」で自前計算（yfinanceのdividendYieldは版によって単位が違うため）
            info = ticker.info
            rate = info.get("dividendRate") or info.get("trailingAnnualDividendRate")
            div_yield = rate / price * 100 if rate else 0.0

            value = p.get("yutai_value_yen", 0)
            if value:
                yutai_yield = value / (price * p.get("min_shares", 100)) * 100
                p["yield_fmt"] = f"配当{div_yield:.1f}%＋優待{yutai_yield:.1f}%＝{div_yield + yutai_yield:.1f}%"
            elif rate:
                p["yield_fmt"] = f"配当{div_yield:.1f}%"
            else:
                p["yield_fmt"] = "配当なし／不明"
        except Exception as e:
            if p not in valid:
                print(f"株価取得失敗のため除外 ({p['code']} {p['name']}):", e)
            else:
                print(f"配当情報の取得失敗 ({p['code']}):", e)
    return valid


# ---------------------------------------------------------------
# 8. LINE送信
# ---------------------------------------------------------------
def change_color(change):
    if change is None:
        return "#555555"
    return "#1DB446" if change >= 0 else "#E0352B"


def build_market_bubble(market_rows, comment, today):
    rows = [
        {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {"type": "text", "text": r["name"], "size": "sm", "color": "#555555", "flex": 3},
                {"type": "text", "text": r["value"], "size": "sm", "align": "end", "flex": 3},
                {"type": "text", "text": f"{r['change']:+.2f}%", "size": "sm", "align": "end", "flex": 2, "color": change_color(r["change"])},
            ],
        }
        for r in market_rows
    ]
    contents = [{"type": "text", "text": f"{today:%m/%d} 今日の地合い", "weight": "bold", "size": "lg"}]
    if rows:
        contents.append({"type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm", "contents": rows})
    if comment:
        contents.append({"type": "text", "text": comment, "size": "sm", "wrap": True, "margin": "lg", "color": "#333333"})
    contents.append({"type": "text", "text": "※前営業日終値ベース", "size": "xxs", "color": "#999999", "margin": "md"})
    return {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#333333",
            "paddingAll": "md",
            "contents": [{"type": "text", "text": "🌏 今日の地合い", "color": "#FFFFFF", "weight": "bold", "size": "sm"}],
        },
        "body": {"type": "box", "layout": "vertical", "contents": contents},
    }


def build_bubble(stock, today, market_closed):
    style = CATEGORY_STYLE.get(stock["category"], CATEGORY_STYLE["優待"])
    label = style["label"] + ("　🔁 継続" if stock.get("continued") else "")

    footer_buttons = [
        {
            "type": "button",
            "action": {
                "type": "uri",
                "label": "チャートを見る",
                "uri": f"https://finance.yahoo.co.jp/quote/{stock['code']}.T",
            },
            "style": "link",
            "height": "sm",
        }
    ]
    if stock.get("source_url", "").startswith("http"):
        footer_buttons.append(
            {
                "type": "button",
                "action": {"type": "uri", "label": "ニュースを見る", "uri": stock["source_url"]},
                "style": "link",
                "height": "sm",
            }
        )

    details = [
        {"type": "text", "text": f"最低買付額: {stock['price_fmt']}", "size": "xs", "color": "#555555", "wrap": True},
    ]
    if stock.get("change_fmt"):
        details.append(
            {"type": "text", "text": f"前日比: {stock['change_fmt']}", "size": "sm", "weight": "bold", "color": change_color(stock.get("change"))}
        )
    details += [
        {"type": "text", "text": f"利回り: {stock['yield_fmt']}", "size": "xs", "color": "#555555", "wrap": True},
        {"type": "text", "text": f"優待: {stock['yutai'] or '不明'}", "size": "xs", "color": "#666666", "wrap": True},
        {"type": "text", "text": f"権利確定月: {stock['kenri_month'] or '不明'}", "size": "xs", "color": "#666666"},
    ]
    for signal in stock.get("signals", []):
        details.append({"type": "text", "text": signal, "size": "xs", "color": "#8E44AD", "wrap": True})
    countdown = kenri_countdown(today, stock.get("kenri_months", []), stock.get("kenri_day", 0))
    if countdown:
        details.append({"type": "text", "text": countdown, "size": "xs", "color": "#F5A623", "weight": "bold", "wrap": True})

    return {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": style["color"],
            "paddingAll": "md",
            "contents": [{"type": "text", "text": label, "color": "#FFFFFF", "weight": "bold", "size": "sm"}],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": f"{stock['name']} ({stock['code']})",
                    "weight": "bold",
                    "size": "lg",
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": stock["headline"] or "-",
                    "size": "sm",
                    "color": "#333333",
                    "wrap": True,
                    "margin": "md",
                },
                {"type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm", "contents": details},
            ],
        },
        "footer": {"type": "box", "layout": "vertical", "contents": footer_buttons},
    }


def push_line(messages):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
    }
    res = requests.post(url, headers=headers, data=json.dumps({"to": USER_ID, "messages": messages}))
    print("送信ステータス:", res.status_code)
    if res.status_code != 200:
        print("レスポンス:", res.text)
        raise RuntimeError(f"LINE送信失敗: {res.status_code}")


def send_line_flex_message(stocks, market_bubble, today, market_closed, x_post=""):
    weekday_str = "月火水木金土日"[today.weekday()]

    bubbles = ([market_bubble] if market_bubble else []) + [build_bubble(s, today, market_closed) for s in stocks]
    chunks = [bubbles[i : i + BUBBLES_PER_CAROUSEL] for i in range(0, len(bubbles), BUBBLES_PER_CAROUSEL)]

    messages = []
    for i, chunk in enumerate(chunks):
        suffix = f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        messages.append(
            {
                "type": "flex",
                "altText": f"【{today:%m/%d}({weekday_str})】今日の注目株 {len(stocks)}銘柄{suffix}",
                "contents": {"type": "carousel", "contents": chunk},
            }
        )
    if x_post:
        messages.append({"type": "text", "text": f"📝 X投稿用の下書き（{len(x_post)}字）\n\n{x_post}"})
    push_line(messages)


def send_line_text(text):
    push_line([{"type": "text", "text": text}])


# ---------------------------------------------------------------
# メイン
# ---------------------------------------------------------------
def run():
    now = now_jst()
    today = now.date()
    market_closed = is_market_holiday(today)

    # 前営業日から今日までのニュースを拾う（土日・祝日明けも漏れないように）
    start = prev_business_day(today)
    days = [start + datetime.timedelta(days=i) for i in range((today - start).days + 1)]
    disclosures = pick_material_disclosures(fetch_tdnet(days))
    print(f"材料になりそうな開示: {len(disclosures)}件")

    history = load_history()
    blocked_codes, prev_codes = history_status(history, today)
    print(f"配信上限で除外: {sorted(blocked_codes)}")

    market_rows = fetch_market_data()
    usd_jpy = next((r["raw"] for r in market_rows if r["symbol"] == "JPY=X"), None)

    picks = []
    market_comment = ""
    x_post = ""
    client = None
    try:
        client = anthropic.Anthropic()
        market_comment, raw_picks, x_post = pick_with_claude(client, today, market_closed, disclosures, blocked_codes, market_rows)
        picks = clean_picks(raw_picks, blocked_codes)
    except Exception as e:
        print("AIでの選定に失敗。キーワード判定に切り替えます:", e)

    if len(picks) < MIN_PICKS:
        # 足りない分をキーワード判定の結果で補う
        known = {p["code"] for p in picks}
        extra = clean_picks(fallback_picks(disclosures), blocked_codes | known)
        picks = (picks + extra)[:MAX_PICKS]

    if not picks:
        print_cost(usd_jpy)
        raise RuntimeError("注目株を1件も選べませんでした")

    if client is not None:
        picks = add_yutai_info(client, picks)
    picks = add_price_info(picks, today)
    if not picks:
        raise RuntimeError("株価を取得できた銘柄が1件もありませんでした")
    for p in picks:
        p["continued"] = p["code"] in prev_codes

    market_bubble = build_market_bubble(market_rows, market_comment, today) if (market_rows or market_comment) else None
    send_line_flex_message(picks, market_bubble, today, market_closed, x_post)
    save_history(history, today, picks)
    print_cost(usd_jpy)


# ---------------------------------------------------------------
# 夕方の振り返り・週間成績（AIは使わない）
# ---------------------------------------------------------------
_close_cache = {}


def recent_closes(code):
    if code not in _close_cache:
        try:
            _close_cache[code] = yf.Ticker(f"{code}.T").history(period="1mo")["Close"].dropna()
        except Exception as e:
            print(f"株価取得失敗 ({code}):", e)
            _close_cache[code] = None
    return _close_cache[code]


def performance(entries):
    """配信時の株価（前営業日終値）から、直近終値までの騰落率を計算"""
    rows = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("price"):
            continue
        closes = recent_closes(e["code"])
        if closes is None or closes.empty:
            continue
        last = float(closes.iloc[-1])
        rows.append({**e, "last": last, "last_date": closes.index[-1].date(), "change": (last - e["price"]) / e["price"] * 100})
    return sorted(rows, key=lambda r: r["change"], reverse=True)


def format_row(r):
    mark = "📈" if r["change"] >= 0 else "📉"
    return f"{mark} {r['change']:+.1f}%  {r['name']}({r['code']}) [{r['category']}]"


def summary_line(rows):
    avg = sum(r["change"] for r in rows) / len(rows)
    ups = sum(1 for r in rows if r["change"] >= 0)
    return f"平均 {avg:+.1f}%（上昇 {ups} / 下落 {len(rows) - ups}）"


def daily_review_text(history, today):
    rows = performance(history.get(today.isoformat(), []))
    if not rows:
        return ""
    lines = [f"🌇 {today:%m/%d} 朝の注目株の結果", summary_line(rows)]
    if any(r["last_date"] != today for r in rows):
        lines.append("※一部、今日の終値がまだ反映されていません")
    lines.append("")
    lines += [format_row(r) for r in rows]
    return "\n".join(lines)


def weekly_review_text(history, today):
    entries = []
    for date_str, day_entries in history.items():
        d = datetime.date.fromisoformat(date_str)
        if 0 <= (today - d).days < 7:
            entries += [{**e, "date": d} for e in day_entries if isinstance(e, dict)]
    rows = performance(entries)
    if not rows:
        return ""
    lines = [f"📊 今週の成績（{today - datetime.timedelta(days=6):%m/%d}〜{today:%m/%d}、配信日から今日まで）", summary_line(rows), ""]
    for category in CATEGORY_STYLE:
        cat_rows = [r for r in rows if r["category"] == category]
        if cat_rows:
            lines.append(f"・{category}: {summary_line(cat_rows)}")
    lines += ["", "🏆 ベスト3"] + [format_row(r) for r in rows[:3]]
    lines += ["", "💧 ワースト3"] + [format_row(r) for r in rows[-3:][::-1]]
    return "\n".join(lines)


def is_last_business_day_of_week(today):
    d = today + datetime.timedelta(days=1)
    while is_market_holiday(d):
        d += datetime.timedelta(days=1)
    return d.isocalendar()[1] != today.isocalendar()[1]


def run_evening():
    today = now_jst().date()
    if is_market_holiday(today):
        print("今日は休場日のため振り返りなし")
        return
    history = load_history()
    messages = []
    daily = daily_review_text(history, today)
    if daily:
        messages.append({"type": "text", "text": daily})
    if is_last_business_day_of_week(today):
        weekly = weekly_review_text(history, today)
        if weekly:
            messages.append({"type": "text", "text": weekly})
    if not messages:
        print("振り返る配信履歴がありません")
        return
    push_line(messages)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    try:
        run_evening() if mode == "evening" else run()
    except Exception as e:
        traceback.print_exc()
        try:
            send_line_text(f"⚠️ 注目株botでエラーが起きました\n{type(e).__name__}: {str(e)[:300]}\n\nGitHub Actionsのログを確認してください。")
            open(NOTIFIED_FLAG, "w").close()
        except Exception as notify_error:
            print("エラー通知も失敗:", notify_error)
        raise


if __name__ == "__main__":
    main()
