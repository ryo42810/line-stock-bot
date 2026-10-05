import calendar
import datetime
import json
import logging
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

import x_media

# yfinanceは、ETFなど決算データが無い銘柄で「HTTP Error 404」を表示するので、ログに出さない
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

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
    "買い場候補": {"color": "#16A085", "label": "買い場候補👀"},
}
# X投稿の書き方（朝の通常版・買い場候補版で共通）
X_POST_STYLE = """## X投稿の書き方（共通）
### フック（1行目）
- 1行目で指を止めさせる。地合いの説明から始めない
- 指定された「今日のフックの型」を使う。中身と合わないフックにはしない（好材料と懸念材料が混ざるなら、混ざる前提のフックにする）
- 1行目の最後に「↓↓」を付けて、続きを読ませてもよい

### 中身
- 銘柄は3つ（足りなければあるだけ）。①②③で番号を付け、銘柄名(コード)の次の行から説明を書く
- 具体的な数字を必ず入れる（増益率、出来高○倍、前日比、優待の年間○円分など）
- 口語でテンポよく。体言止めや「控えめに言って強い」「異次元の増益」のような強めのリアクションもOK（事実に対してだけ）
- 「自社株買い」など略さずに伝わる言葉を使う。「アナリスト好評価」のようなあいまいな表現は避け、何が起きたかを書く

### 見た目
- 1銘柄ごとに空行を入れ、スマホで読みやすくする
- 「。」で終わる文や、絵文字で終わる文のあとは必ず改行する（1行に1文）
- 絵文字は多めに使う（1行に1つ程度）。必ず言葉の後に付け、行頭には置かない（例: 「好材料📈」「出来高急増🔥」）
- 最後の行はハッシュタグ #株クラ だけ（免責文は書かない）
- 長さは200字程度まで

### やってはいけないこと
- 「使ってみた」「もらった」「買った」などの体験談は書かない（事実ではないため）
- 実在の人物の発言や口コミを作らない
- 「絶対上がる」「今すぐ買え」など、株価の断定や売買の指示はしない。強い言い切りは、事実や注目度についてだけ使う"""

# フックの型（日替わりで使い分けて、毎日同じ書き出しにならないようにする）
X_HOOK_TYPES = [
    "ぶっちゃけ型（例: 「ぶっちゃけ、今朝いちばん気になったのはこれ」）",
    "タメ型（例: 1行目「はい。」2行目「これが今朝の激アツ決算です。」）",
    "言い切り型（例: 「この3銘柄は見逃し厳禁」）※株価の断定はしない",
    "リアクション型（例: 「控えめに言って、この増益は異次元」）",
    "問いかけ型（例: 「この3つ、どれが一番伸びると思う？👀」）",
    "親近感型（例: 「みんな大好き〇〇に好材料」）※実在の銘柄名で",
]

# アンケート版（日曜の朝だけ）のテーマ。週ごとに順番に使う
SURVEY_DAY = 6  # 日曜
X_SURVEY_THEMES = [
    "株主優待で「これは神！」と思った銘柄",
    "{next_month}月の権利確定で狙っている優待",
    "長く持ち続けたい高配当株",
    "コスパ最強だと思う優待（少ない投資額で満足度が高いもの）",
    "今年いちばん「買ってよかった」と思う銘柄",
    "優待で実際によく使っているお店・サービス",
]

# アンケート版のお手本（雰囲気・形の参考。内容はそのまま使わない）
X_EXAMPLE_SURVEY = """株主優待で「これは神！」って思った銘柄は？🎁

1位＋理由を教えてください👀
実際にもらった人の声が聞きたい！

#株クラ"""

# 買い場候補版のお手本（雰囲気・形の参考。内容はそのまま使わない）
X_EXAMPLE_BUY = """ぶっちゃけ、上がる前に仕込みたいのはこの3つ👀↓↓

①ハイデイ日高(7611)
自社株買い＋増配のダブル発表✨
なのに株価の反応はまだ小さめ

②カネコ種苗(1376)
1Qが大幅増益📈
株価は横ばいなのに出来高2.4倍🔥

③きょくとう(2300)
株価は動かず出来高だけ4.4倍💡
値動きの前ぶれかも

#株クラ"""

# ニュース版のお手本（雰囲気・形の参考。内容はそのまま使わない）
X_EXAMPLE_NEWS = """はい。
今朝の決算、明暗くっきりです📰↓↓

①カネコ種苗(1376)
最終利益77.5%増で控えめに言って強い💪
株式の売却益も上乗せ

②あみやき亭(2753)
売上は10.1%増✨
ただ減損2億円で減益に⚠️

#株クラ"""

MAX_BUY_CANDIDATES = 5
BUY_UNIVERSE_LIMIT = 40  # 買い場候補を探す銘柄数の上限（株価取得の時間を抑えるため）

# AIが使えないとき（案A方式）のキーワード判定用
GOOD_WORDS = ["上方修正", "増配", "自社株買い", "自己株式の取得", "株式分割", "優待新設", "優待拡充", "復配", "最高益", "黒字転換"]
# 好材料のキーワードを含んでも、材料にしない定期報告・結果報告
ROUTINE_WORDS = ["取得状況", "取得結果", "取得終了", "取得の終了", "買付結果", "買付けの結果", "買付けに関する結果"]
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
        {
            "code": p["code"],
            "name": p["name"],
            "category": p["category"],
            "price": p.get("price"),
            "buy": bool(p.get("buy_reasons")),
            "headline": p.get("headline", ""),
            "yutai": p.get("yutai", ""),
            "kenri_months": p.get("kenri_months", []),
            "kenri_day": p.get("kenri_day", 0),
        }
        for p in picks
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
        if any(w in title for w in ROUTINE_WORDS):
            continue  # 自社株買いの進み具合・結果・終了の報告は新しい材料ではないので除外
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
    "https://kabuyoho.ifis.co.jp/index.php?id=100",
]

# 優待一覧ページの候補（取れたものだけ使う）
YUTAI_LIST_PAGES = [
    "https://finance.yahoo.co.jp/stocks/incentive/popular-ranking/all",
    "https://finance.yahoo.co.jp/stocks/incentive/popular-ranking",
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
    "required": ["market_comment", "picks"],
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
2. TDnetの適時開示も参考にする。ただし「自己株式の取得状況・取得結果・取得終了」のような、すでに発表済みの自社株買いの進み具合や結果の報告は、新しい材料として扱わない
3. 枠が余るときは「優待一覧のページ」から、{months[0]}月か{months[1]}月が権利確定月で優待が魅力的な銘柄を加える。分類は「優待」
{earnings_rule}5. 次の銘柄は直近1週間で配信済みなので選ばない: {blocked_text}
6. 合計{MIN_PICKS}〜{MAX_PICKS}件。10件以上を目標にする。根拠の弱い銘柄は入れない
7. 証券コードはページに書いてあるものだけを使う。わからない銘柄は入れない
8. market_comment には、地合いデータとニュースから今日の日本株の見通しを80字程度で書く
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
    return data["market_comment"], data["picks"], "\n".join(topic_blocks)


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
_history_cache = {}


def get_history(code):
    """1年分の日足（キャッシュ付き）"""
    if code not in _history_cache:
        _history_cache[code] = yf.Ticker(f"{code}.T").history(period="1y").dropna(subset=["Close"])
    return _history_cache[code]


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
            hist = get_history(p["code"])
            if today is not None:
                # 日中に手動で動かしても、基準は「今日より前の最後の終値」にする（夕方の振り返りが0%にならないように）
                hist = hist[hist.index.date < today]
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
    if stock.get("buy_reasons") and stock["category"] != "買い場候補":
        label += "　買い場候補👀"

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
    if stock.get("buy_reasons"):
        details.append({"type": "text", "text": "買い場候補の理由👀", "size": "xs", "color": "#16A085", "weight": "bold", "margin": "md"})
        for r in stock["buy_reasons"]:
            details.append({"type": "text", "text": f"・{r}", "size": "xs", "color": "#16A085", "wrap": True})
        if stock.get("buy_comment"):
            details.append({"type": "text", "text": stock["buy_comment"], "size": "xs", "color": "#333333", "wrap": True})
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
    for i in range(0, len(messages), 5):  # LINEは1回の送信で5メッセージまで
        res = requests.post(url, headers=headers, data=json.dumps({"to": USER_ID, "messages": messages[i : i + 5]}))
        print("送信ステータス:", res.status_code)
        if res.status_code != 200:
            print("レスポンス:", res.text)
            raise RuntimeError(f"LINE送信失敗: {res.status_code}")


def send_line_flex_message(stocks, market_bubble, today, market_closed, extra_messages=()):
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
    push_line(messages + list(extra_messages))


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
    topic_text = ""
    client = None
    try:
        client = anthropic.Anthropic()
        market_comment, raw_picks, topic_text = pick_with_claude(client, today, market_closed, disclosures, blocked_codes, market_rows)
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

    buy_cards = []
    try:
        buy_cards = find_buy_candidates(picks, disclosures, topic_text, blocked_codes, today)
    except Exception as e:
        print("買い場候補の探索に失敗:", e)

    picks = picks + buy_cards
    if client is not None:
        picks = add_yutai_info(client, picks)
    picks = add_price_info(picks, today)
    if not picks:
        raise RuntimeError("株価を取得できた銘柄が1件もありませんでした")

    # 優待の先回り（権利確定月の1〜2ヶ月前）も買い場の理由に加える
    for p in picks:
        reason = yutai_advance_reason(p, today)
        if reason:
            p["buy_reasons"] = p.get("buy_reasons", []) + [reason]
    # 買い場候補は当てはまった条件が多い順に最大5件。外れたものは印を消し、候補だけのカードは出さない
    ranked = sorted([p for p in picks if p.get("buy_reasons")], key=lambda p: len(p["buy_reasons"]), reverse=True)
    buy_list = ranked[:MAX_BUY_CANDIDATES]
    for p in ranked[MAX_BUY_CANDIDATES:]:
        p.pop("buy_reasons")
    picks = [p for p in picks if p["category"] != "買い場候補" or p.get("buy_reasons")]
    print(f"買い場候補: {[(p['code'], p['buy_reasons']) for p in buy_list]}")
    x_post_buy, x_post_news, x_post_survey = "", "", ""
    if client is not None:
        x_post_buy, x_post_news, x_post_survey = write_x_posts(client, picks, buy_list, disclosures, topic_text, today)
    for p in picks:
        p["continued"] = p["code"] in prev_codes

    market_bubble = build_market_bubble(market_rows, market_comment, today) if (market_rows or market_comment) else None
    # X下書きと画像（本文だけ送る。順番はニュース版→買い場候補版→アンケート版）
    by_code = {p["code"]: p for p in picks}
    images = {}
    weekday_str = "月火水木金土日"[today.weekday()]
    date_text = f"{today:%Y.%m.%d}（{weekday_str}）"
    for kind, post, title, subtitle, accent in (
        ("news", x_post_news, "今朝のニュース銘柄", "材料が出た注目の3銘柄", x_media.COLORS["green"]),
        ("buy", x_post_buy, "買い場候補", "上がる前にチェックしたい3銘柄", x_media.COLORS["accent"]),
    ):
        stocks = [by_code[c] for c in x_media.codes_in_post(post) if c in by_code]
        if post and stocks:
            try:
                images[kind] = x_media.render_stock_card(
                    x_media.image_path(today, kind), title, subtitle, stocks, accent, get_history, date_text
                )
            except Exception as e:
                print(f"画像の作成に失敗 ({kind}):", e)
    urls = x_media.publish_images(list(images.values()), today)

    extra = []
    for kind, post in (("news", x_post_news), ("buy", x_post_buy), ("survey", x_post_survey)):
        if post:
            extra.append({"type": "text", "text": post})
            if images.get(kind) in urls:
                extra.append(x_media.line_image_message(urls[images[kind]]))
    send_line_flex_message(picks, market_bubble, today, market_closed, extra)

    # Xに自動投稿（オンのときだけ）。失敗やスキップがあったときだけLINEで知らせる
    x_result = x_media.post_all_to_x(
        [(x_post_news, images.get("news")), (x_post_buy, images.get("buy")), (x_post_survey, None)]
    )
    if "⚠️" in x_result or "⏭" in x_result:
        send_line_text(x_result)
    save_history(history, today, picks)
    print_cost(usd_jpy)


# ---------------------------------------------------------------
# 買い場候補（ルールで候補を絞り、AIが注目理由を添える）
# ---------------------------------------------------------------
def build_universe(picks, disclosures, topic_text):
    """候補を探す銘柄：朝の選定銘柄＋好材料の開示＋ランキング等に載っていた銘柄"""
    universe = {}
    for p in picks:
        universe.setdefault(p["code"], p["name"])
    for d in disclosures:
        if d["category"] == "好材料":
            universe.setdefault(d["code"], d["name"])
    for name, code in re.findall(r"([^\n<>]{1,40}?) <https://finance\.yahoo\.co\.jp/quote/(\d{3}[0-9A-Z])\.T>", topic_text):
        universe.setdefault(code, name.strip())
    return dict(list(universe.items())[:BUY_UNIVERSE_LIMIT])


def buy_rule_reasons(hist, has_good_news):
    """買い場候補のルール。当てはまった理由のリストを返す"""
    reasons = []
    closes = hist["Close"]
    if len(closes) < 76:
        return reasons
    last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
    change = (last - prev) / prev * 100

    ma25 = float(closes.iloc[-25:].mean())
    ma75 = float(closes.iloc[-75:].mean())
    ma75_before = float(closes.iloc[-80:-5].mean())
    gap25 = (last - ma25) / ma25 * 100

    # 材料の織り込み前：適時開示で好材料が出たのに、株価はまだ大きく反応しておらず、過熱もしていない
    if has_good_news and -2 < change < 3 and gap25 < 5:
        reasons.append(f"好材料の開示が出たが株価の反応はまだ小さい（前日比{change:+.1f}%）")

    # 押し目：75日線が上向きの上昇トレンド中に、25日線から-5〜-15%まで下げている（年初来安値は除く）
    this_year = hist[hist.index.year == hist.index[-1].year]
    at_low = len(this_year) >= 2 and float(this_year["Low"].iloc[-1]) <= float(this_year["Low"].iloc[:-1].min())
    if ma75 > ma75_before and last > ma75 * 0.9 and -15 <= gap25 <= -5 and not at_low:
        reasons.append(f"上昇トレンド中の押し目（25日線から{gap25:+.1f}%）")

    # 出来高先行：株価はほぼ動いていないのに、出来高だけ急に増えている
    volumes = hist["Volume"]
    avg20 = float(volumes.iloc[-21:-1].mean())
    if avg20 > 0 and float(volumes.iloc[-1]) >= avg20 * 2 and abs(change) < 2:
        reasons.append(f"株価は横ばいで出来高が急増（20日平均の{float(volumes.iloc[-1]) / avg20:.1f}倍）")
    return reasons


def yutai_advance_reason(stock, today):
    """優待の先回り：権利確定月が1〜2ヶ月先"""
    for ahead in (1, 2):
        month = (today.month + ahead - 1) % 12 + 1
        if month in stock.get("kenri_months", []) and stock.get("yutai") not in ("なし", "不明", "", None):
            return f"優待の権利確定月（{month}月）の{ahead}ヶ月前で、先回りの時期"
    return ""


def is_after_last_close(pubdate, today):
    """開示時刻が、今日より前の最後の営業日の15:30以降か。時刻が読めなければ False（厳しい側に倒す）"""
    try:
        published = datetime.datetime.fromisoformat(pubdate.strip()[:19].replace("/", "-"))
    except Exception:
        return False
    last_close = datetime.datetime.combine(prev_business_day(today), datetime.time(15, 30))
    return published >= last_close


def find_buy_candidates(picks, disclosures, topic_text, blocked_codes, today):
    """ルールで買い場候補を探す。朝の選定銘柄は印を付け、それ以外は新しいカードにする"""
    # 「材料の織り込み前」は、AIの判断ではなく適時開示（公式の発表）で好材料が出た銘柄だけを対象にする。
    # さらに、前営業日の引け（15:30）以降に出た開示に限る（それより前の開示は、すでに株価が反応している）
    good_codes = set()
    for d in disclosures:
        if d["category"] == "好材料":
            after = is_after_last_close(d["pubdate"], today)
            print(f"好材料の開示: {d['code']} {d['title'][:30]} / 開示時刻 {d['pubdate']!r} → {'引け後（対象）' if after else '対象外'}")
            if after:
                good_codes.add(d["code"])
    pick_by_code = {p["code"]: p for p in picks}
    universe = build_universe(picks, disclosures, topic_text)
    print(f"買い場候補の探索対象: {len(universe)}銘柄")

    found = []
    for code, name in universe.items():
        if code in blocked_codes and code not in pick_by_code:
            continue
        try:
            reasons = buy_rule_reasons(get_history(code), code in good_codes)
        except Exception as e:
            print(f"買い場判定の株価取得失敗 ({code}):", e)
            continue
        if reasons:
            found.append((code, name, reasons))
    found.sort(key=lambda x: len(x[2]), reverse=True)

    new_cards = []
    for code, name, reasons in found:
        if code in pick_by_code:
            pick_by_code[code]["buy_reasons"] = reasons
        elif len(new_cards) < MAX_BUY_CANDIDATES:
            new_cards.append(
                {"code": code, "name": name, "category": "買い場候補", "headline": "", "source_url": "", "buy_reasons": reasons}
            )
    return new_cards


BUY_REASON_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "reason": {"type": "string", "description": "注目理由（60字以内）"},
                },
                "required": ["code", "reason"],
                "additionalProperties": False,
            },
        },
        "x_post_buy": {"type": "string", "description": "買い場候補版のX投稿の下書き。買い場候補がなければ空文字"},
        "x_post_news": {"type": "string", "description": "ニュース版のX投稿の下書き。買い場候補の銘柄は使わない"},
        "x_post_survey": {"type": "string", "description": "アンケート版のX投稿の下書き。指示がなければ空文字"},
    },
    "required": ["items", "x_post_buy", "x_post_news", "x_post_survey"],
    "additionalProperties": False,
}


EMOJI_CHARS = "\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF"


def format_x_post(text):
    """「。」や絵文字で文が終わったら改行する（すでに改行があれば何もしない）"""
    text = re.sub(rf"(。|[{EMOJI_CHARS}]\uFE0F?)(?=[^\s{EMOJI_CHARS}\uFE0F\u2193])", r"\1\n", text.strip())
    text = re.sub(r"↓↓(?=[^\s↓])", "↓↓\n", text)
    return re.sub(r"[ \t]+\n", "\n", text)


def stock_context(stock, disclosures, topic_text):
    lines = [l for l in topic_text.splitlines() if stock["code"] in l or (stock["name"] and stock["name"] in l)][:5]
    lines += [f"適時開示: {d['title']}" for d in disclosures if d["code"] == stock["code"]][:3]
    if stock.get("headline"):
        lines.append(f"朝の選定理由: {stock['headline']}")
    if stock.get("change") is not None:
        lines.append(f"前日の騰落: {stock['change']:+.1f}%")
    return "\n".join(lines) or "（なし）"


def write_x_posts(client, picks, buy_list, disclosures, topic_text, today):
    """買い場候補の注目理由と、X下書き2本（買い場候補版・ニュース版。銘柄は重複させない）を作る"""
    buy_codes = {c["code"] for c in buy_list}
    buy_blocks = [
        f"<stock code=\"{c['code']}\" name=\"{c['name']}\">\n"
        f"ルールで当てはまった条件: {' / '.join(c['buy_reasons'])}\n"
        f"関連情報:\n{stock_context(c, disclosures, topic_text)}\n</stock>"
        for c in buy_list
    ]
    news_blocks = [
        f"<stock code=\"{p['code']}\" name=\"{p['name']}\" category=\"{p['category']}\">\n"
        f"関連情報:\n{stock_context(p, disclosures, topic_text)}\n</stock>"
        for p in picks
        if p["code"] not in buy_codes and p["category"] != "買い場候補"
    ]
    if today.weekday() == SURVEY_DAY:
        next_month = today.month % 12 + 1
        theme = X_SURVEY_THEMES[(today.toordinal() // 7) % len(X_SURVEY_THEMES)].format(next_month=next_month)
        survey_rule = f"""### x_post_survey（アンケート版・日曜だけ）
- 今週のテーマ: {theme}
- フォロワーにリプで答えてもらう問いかけ投稿にする。銘柄の紹介はしない
- 1行目はテーマを問いかけで（最後に絵文字）。続けて「1位＋理由を教えてください」のように答え方を具体的に書く
- 「〇〇な人の声が聞きたい！」のように、答えたくなる一言で締める
- 書き方は共通ルールに従う。短く、100字程度まで

### アンケート版のお手本（形・雰囲気の参考。内容は使わない）
{X_EXAMPLE_SURVEY}
"""
    else:
        survey_rule = "### x_post_survey\n- 今日は空文字にする\n"

    prompt = f"""毎朝のLINE配信とX投稿のための文章を作ります。下の情報だけを使い、書いていない材料やニュースは作らないでください。

## 1. 買い場候補の注目理由（items）
「買い場候補」の銘柄ごとに「注目理由」を60字以内で書く。
- ルールの条件と関連情報を組み合わせて、なぜ今注目なのかを書く
- 関連情報がない銘柄は、ルールの条件だけで書く

## 2. X投稿の下書き2本
2本は役割が違う。**同じ銘柄を両方に入れてはいけない。**

### x_post_buy（買い場候補版）
- 「買い場候補」の銘柄だけを使う。それぞれ「なぜまだ上がる前と言えるのか」を伝える
- 材料が出たばかりの銘柄を優先する
- 買い場候補がなければ空文字

### x_post_news（ニュース版）
- 「ニュース銘柄」だけを使う（買い場候補の銘柄は使わない）
- 好材料・懸念材料・決算の中身など、今朝のニュースで何が起きたかを解説する
- 基本は材料が出たばかりの銘柄を優先する。前日に大きく上がった（下がった）銘柄で理由がわかるものは、3銘柄のうち1銘柄まで「なぜ動いたのか」の解説にしてよい（例: 「前日+15%🚀」の次の行に理由）

{X_POST_STYLE}

### 今日のフックの型
- 買い場候補版: {X_HOOK_TYPES[today.toordinal() % len(X_HOOK_TYPES)]}
- ニュース版: {X_HOOK_TYPES[(today.toordinal() + 3) % len(X_HOOK_TYPES)]}

### 買い場候補版のお手本（形・雰囲気の参考。内容は使わない）
{X_EXAMPLE_BUY}

### ニュース版のお手本（形・雰囲気の参考。内容は使わない）
{X_EXAMPLE_NEWS}

{survey_rule}
## 買い場候補
{chr(10).join(buy_blocks) or "（なし）"}

## ニュース銘柄
{chr(10).join(news_blocks) or "（なし）"}"""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={"format": {"type": "json_schema", "schema": BUY_REASON_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        track_usage(response)
        if response.stop_reason == "refusal":
            raise RuntimeError("拒否されました")
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
    except Exception as e:
        print("X下書き・買い場候補の理由づけに失敗:", e)
        return "", "", ""
    reasons = {item["code"].upper(): item["reason"] for item in data["items"]}
    for c in buy_list:
        c["buy_comment"] = reasons.get(c["code"], "")
    x_buy = format_x_post(data["x_post_buy"]) if data["x_post_buy"] else ""
    x_news = format_x_post(data["x_post_news"]) if data["x_post_news"] else ""
    x_survey = format_x_post(data["x_post_survey"]) if data["x_post_survey"] and today.weekday() == SURVEY_DAY else ""
    overlap = [code for code in buy_codes if code in x_news]
    if overlap:
        print(f"注意: ニュース版に買い場候補の銘柄が入っています: {overlap}")
    return x_buy, x_news, x_survey


# ---------------------------------------------------------------
# 夕方の振り返り・週間成績（AIは使わない）
# ---------------------------------------------------------------
_close_cache = {}


def recent_closes(code):
    if code not in _close_cache:
        try:
            _close_cache[code] = yf.Ticker(f"{code}.T").history(period="3mo")["Close"].dropna()
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
    mark = "📈" if r["change"] > 0.05 else "📉" if r["change"] < -0.05 else "➖"
    buy = " 買い場候補👀" if r.get("buy") and r["category"] != "買い場候補" else ""
    return f"{mark} {r['change']:+.1f}%  {r['name']}({r['code']}) [{r['category']}]{buy}"


def summary_line(rows):
    avg = sum(r["change"] for r in rows) / len(rows)
    ups = sum(1 for r in rows if r["change"] > 0.05)
    downs = sum(1 for r in rows if r["change"] < -0.05)
    flat = len(rows) - ups - downs
    return f"平均 {avg:+.1f}%（上昇 {ups} / 下落 {downs}" + (f" / 変わらず {flat}" if flat else "") + "）"


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


def change_after(entry, delivered, trading_days):
    """配信日を1日目として、N営業日目の終値までの騰落率。まだその日が来ていなければ None"""
    closes = recent_closes(entry["code"])
    if closes is None or not entry.get("price"):
        return None
    after = closes[closes.index.date >= delivered]
    if len(after) < trading_days:
        return None
    return (float(after.iloc[trading_days - 1]) - entry["price"]) / entry["price"] * 100


def buy_performance_text(history, today):
    """買い場候補の成績：1週間後（5営業日）・1ヶ月後（20営業日）"""
    lines = ["👀 買い場候補の成績（配信時の株価から）"]
    has_data = False
    for label, days in (("1週間後", 5), ("1ヶ月後", 20)):
        results = []
        for date_str, entries in history.items():
            delivered = datetime.date.fromisoformat(date_str)
            for e in entries:
                if isinstance(e, dict) and e.get("buy"):
                    c = change_after(e, delivered, days)
                    if c is not None:
                        results.append(c)
        if results:
            has_data = True
            wins = sum(1 for c in results if c > 0)
            lines.append(
                f"・{label}: 平均 {sum(results) / len(results):+.1f}%（{len(results)}件、上昇 {wins}件＝{wins / len(results) * 100:.0f}%）"
            )
        else:
            lines.append(f"・{label}: まだデータなし")
    return "\n".join(lines) if has_data or any(
        isinstance(e, dict) and e.get("buy") for entries in history.values() for e in entries
    ) else ""


def is_last_business_day_of_week(today):
    d = today + datetime.timedelta(days=1)
    while is_market_holiday(d):
        d += datetime.timedelta(days=1)
    return d.isocalendar()[1] != today.isocalendar()[1]


def next_business_day(d):
    d += datetime.timedelta(days=1)
    while is_market_holiday(d):
        d += datetime.timedelta(days=1)
    return d


def kenri_reminder_text(history, today):
    """明日が権利付き最終日の優待銘柄（直近60日に配信した銘柄から）"""
    tomorrow = next_business_day(today)
    found = {}
    for entries in history.values():
        for e in entries:
            if not isinstance(e, dict) or not e.get("kenri_months") or e.get("yutai") in ("なし", "不明", "", None):
                continue
            for y, m in ((tomorrow.year, tomorrow.month), (tomorrow.year + (tomorrow.month == 12), tomorrow.month % 12 + 1)):
                if m in e["kenri_months"] and kenri_last_day(y, m, e.get("kenri_day", 0)) == tomorrow:
                    found[e["code"]] = (e, m)
    if not found:
        return ""
    lines = [f"🎁 明日（{tomorrow:%m/%d}）が権利付き最終日"]
    for e, m in found.values():
        lines.append(f"・{e['name']}({e['code']}) {m}月権利：{e['yutai']}")
    lines.append("※明日の大引けまでに買えば、優待の権利がもらえます")
    return "\n".join(lines)


def performance_rows(history, today):
    """買い場候補の1週間後の成績を、X用の実績報告にする（AIは使わない）"""
    rows = []
    for date_str, entries in history.items():
        delivered = datetime.date.fromisoformat(date_str)
        if (today - delivered).days > 14:
            continue
        for e in entries:
            if not (isinstance(e, dict) and e.get("buy")):
                continue
            # 1週間後（5営業日目）が今週に来た銘柄だけ数える（同じ銘柄を2週続けて出さない）
            closes = recent_closes(e["code"])
            if closes is None:
                continue
            after = closes[closes.index.date >= delivered]
            if len(after) >= 5 and (today - after.index[4].date()).days < 7:
                c = change_after(e, delivered, 5)
                if c is not None:
                    rows.append({**e, "change": c})
    return rows


def x_performance_post(rows):
    if not rows:
        return ""
    avg = sum(r["change"] for r in rows) / len(rows)
    ups = sum(1 for r in rows if r["change"] > 0)
    best = max(rows, key=lambda r: r["change"])
    lines = [
        "買い場候補の1週間後の成績、公開します📊↓↓",
        "",
        f"平均{avg:+.1f}%{'📈' if avg >= 0 else '📉'}",
        f"上がったのは{len(rows)}銘柄中{ups}銘柄{'✨' if ups * 2 >= len(rows) else '💦'}",
    ]
    if best["change"] > 0:
        lines += ["", "いちばん伸びたのは", f"{best['name']}({best['code']}) {best['change']:+.1f}%🚀"]
    lines += ["", "#株クラ"]
    return "\n".join(lines)


# 夕方に読むページ（今日動いた銘柄・決算予定）
EVENING_MOVER_PAGES = [
    "https://finance.yahoo.co.jp/stocks/ranking/up",
    "https://finance.yahoo.co.jp/stocks/ranking/down",
]

EVENING_SCHEMA = {
    "type": "object",
    "properties": {
        "movers_post": {"type": "string", "description": "今日大きく動いた銘柄の「なぜ動いたか」X下書き。理由がわかる銘柄がなければ空文字"},
        "earnings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "name": {"type": "string"},
                    "note": {"type": "string", "description": "注目ポイント（30字以内）。ページに書いてあることだけ"},
                },
                "required": ["code", "name", "note"],
                "additionalProperties": False,
            },
        },
        "earnings_post": {"type": "string", "description": "明日の決算予告のX下書き。明日の予定が見つからなければ空文字"},
        "note": {"type": "string", "description": "空にした項目があれば、その理由を一言で"},
    },
    "required": ["movers_post", "earnings", "earnings_post", "note"],
    "additionalProperties": False,
}


def quarterly_growth_text(code):
    """yfinanceの四半期業績から、直近四半期の前年同期比（売上高・営業利益・純利益）を文章にする"""
    try:
        q = yf.Ticker(f"{code}.T").quarterly_income_stmt
        if q is None or q.empty or q.shape[1] < 5:
            return ""
        latest, year_ago = q.columns[0], q.columns[4]
        parts = []
        for key, label in (("Total Revenue", "売上高"), ("Operating Income", "営業利益"), ("Net Income", "純利益")):
            if key in q.index:
                now, before = q.at[key, latest], q.at[key, year_ago]
                if now == now and before == before and before:  # NaNを除外
                    if before > 0:
                        parts.append(f"{label} {(now / before - 1) * 100:+.1f}%")
                    elif now > 0:
                        parts.append(f"{label} 黒字転換")
        if not parts:
            return ""
        return f"業績データ（{latest:%Y年%m月}末までの四半期・前年同期比）: " + "、".join(parts)
    except Exception as e:
        print(f"四半期業績の取得失敗 ({code}):", e)
        return ""


def ensure_x_format(post, default_hook):
    """X下書きの最低限の形をそろえる：1行目がフックでなければ補い、最後に #株クラ を付ける"""
    if not post:
        return ""
    lines = post.strip().splitlines()
    if re.search(r"\(\d{3}[0-9A-Z]\)", lines[0]):  # 1行目からいきなり銘柄が始まっている
        lines = [default_hook, ""] + lines
    text = "\n".join(lines).rstrip()
    if "#株クラ" not in text:
        text += "\n\n#株クラ"
    return text


def earnings_tomorrow(codes, tomorrow):
    """yfinanceの決算予定日が明日の銘柄（対象は配信履歴・ランキングに出た銘柄）"""
    found = []
    with_dates = 0  # 決算予定日が1つでも入っていた銘柄数（0ならyfinanceにデータが無いと判断できる）
    for code, name in codes.items():
        try:
            cal = yf.Ticker(f"{code}.T").calendar or {}
            dates = cal.get("Earnings Date") or []
            if dates:
                with_dates += 1
            if any(getattr(d, "date", lambda: d)() == tomorrow for d in dates):
                found.append((code, name))
        except Exception as e:
            print(f"決算予定日の取得失敗 ({code}):", e)
    return found, with_dates


def evening_with_claude(client, today):
    """今日動いた銘柄の理由解説と、明日の決算予告（X下書き付き）"""
    tomorrow = next_business_day(today)
    mover_blocks = fetch_pages(EVENING_MOVER_PAGES, max_chars=8000)
    # 前営業日の引け後〜今日の開示（今日の値動きの理由は、前日夕方の開示であることが多い）
    start = prev_business_day(today)
    disclosures = fetch_tdnet([start + datetime.timedelta(days=i) for i in range((today - start).days + 1)])
    print(f"夕方ページ取得: 値動き{len(mover_blocks)}件 / 開示{len(disclosures)}件")

    # 値動きランキングに載った銘柄に、関連する開示・朝の選定理由を紐づける
    history = load_history()
    morning = {e["code"]: e for e in history.get(today.isoformat(), []) if isinstance(e, dict)}
    ranking_text = "\n".join(mover_blocks)
    movers = {}
    for name, code in re.findall(r"([^\n<>]{1,40}?) <https://finance\.yahoo\.co\.jp/quote/(\d{3}[0-9A-Z])\.T>", ranking_text):
        movers.setdefault(code, name.strip())
    mover_info = []
    for code, name in list(movers.items())[:40]:
        related = [f"適時開示: {d['title']}" for d in disclosures if d["code"] == code][:3]
        if code in morning and morning[code].get("headline"):
            related.append(f"今朝の注目理由: {morning[code]['headline']}")
        if any("決算" in d["title"] for d in disclosures if d["code"] == code):
            perf = quarterly_growth_text(code)
            if perf:
                related.append(perf)
        if related:
            lines = ranking_text.splitlines()
            idx = next((i for i, l in enumerate(lines) if f"/quote/{code}.T" in l), None)
            # ページの表は1セルずつ改行されるので、銘柄の行から数行分（株価・騰落率）をまとめて渡す
            row = " ".join(lines[idx:idx + 6]) if idx is not None else ""
            change_text = ""
            try:
                closes = yf.Ticker(f"{code}.T").history(period="5d")["Close"].dropna()
                if len(closes) >= 2 and closes.index[-1].date() == today:
                    change_text = f"今日の騰落率: {(float(closes.iloc[-1]) / float(closes.iloc[-2]) - 1) * 100:+.1f}%\n"
            except Exception as e:
                print(f"騰落率の取得失敗 ({code}):", e)
            mover_info.append(
                f"<stock code=\"{code}\" name=\"{name}\">\n{change_text}ランキングの行: {row[:200]}\n" + "\n".join(related) + "\n</stock>"
            )
    print(f"値動きランキングの銘柄: {len(movers)}件 / うち理由の手がかりあり: {len(mover_info)}件")

    # 決算予定日を調べる銘柄：直近60日に配信した銘柄＋今日のランキング銘柄
    watch = {}
    for entries in history.values():
        for e in entries:
            if isinstance(e, dict):
                watch.setdefault(e["code"], e["name"])
    for code, name in movers.items():
        watch.setdefault(code, name)
    tomorrow_earnings, with_dates = earnings_tomorrow(dict(list(watch.items())[:150]), tomorrow)
    print(f"決算予定日の確認: {min(len(watch), 150)}銘柄（うち予定日データあり {with_dates}銘柄）→ 明日決算 {len(tomorrow_earnings)}件")
    earnings_info = "\n".join(f"- {name}({code})" for code, name in tomorrow_earnings)

    prompt = f"""今日は{today:%Y年%m月%d日}、明日（次の営業日）は{tomorrow:%m月%d日}です。夕方のLINE配信とX投稿の文章を作ります。
下の情報だけを使い、書いていない材料やニュースは作らないでください。

## 1. movers_post（なぜ動いたか・X下書き）
- 「理由の手がかりがある値動き銘柄」から、今日大きく動いた銘柄を選び、「なぜ動いたのか」を解説する
- 理由は、その銘柄に紐づいた適時開示・今朝の注目理由に書いてあることだけを使う
- 「今日+15%🚀」のように騰落率を入れ、次の行に理由
- 「業績データ」がある銘柄は、「営業利益+30%」のように具体的な数字で理由を書く。ただし業績データの四半期が今回の決算と違いそうなら使わない
- 1行目は必ずフック（例: 「今日爆上げした銘柄、理由はこれ🚀↓↓」）。銘柄名から書き始めない
- 最後の行は必ず #株クラ
- 理由がわかる銘柄が1つもなければ空文字

## 2. earnings（明日の決算予告・LINE用）
- 「明日（{tomorrow:%m月%d日}）決算発表予定の銘柄」から、注目度の高いものを最大5件
- note には注目ポイントを書く。下の情報に書いていなければ「決算発表予定」とだけ書く
- 一覧が空なら空の配列

## 3. earnings_post（明日の決算予告・X下書き）
- earnings の中から3銘柄。「明日決算」であることが1行目でわかるフックにする
- earnings が空なら空文字

{X_POST_STYLE}

### 今日のフックの型
- movers_post: {X_HOOK_TYPES[(today.toordinal() + 1) % len(X_HOOK_TYPES)]}
- earnings_post: {X_HOOK_TYPES[(today.toordinal() + 4) % len(X_HOOK_TYPES)]}

### お手本（形・雰囲気の参考。内容は使わない）
{X_EXAMPLE_NEWS}

## 明日（{tomorrow:%m月%d日}）決算発表予定の銘柄
{earnings_info or "（なし）"}

## 理由の手がかりがある値動き銘柄
{chr(10).join(mover_info) or "（なし）"}"""
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"format": {"type": "json_schema", "schema": EVENING_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    track_usage(response)
    if response.stop_reason == "refusal":
        raise RuntimeError("拒否されました")
    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    print(f"夕方のAI結果: なぜ動いたか {'あり' if data['movers_post'] else 'なし'} / 決算予告 {len(data['earnings'])}件 / メモ: {data['note']}")

    earnings_text = ""
    if data["earnings"]:
        lines = [f"📊 明日（{tomorrow:%m/%d}）決算の注目銘柄"]
        lines += [f"・{e['name']}({e['code']}) {e['note']}" for e in data["earnings"]]
        earnings_text = "\n".join(lines)
    movers = format_x_post(ensure_x_format(data["movers_post"], "今日大きく動いた銘柄、理由はこれ🚀↓↓")) if data["movers_post"] else ""
    earnings_post = format_x_post(ensure_x_format(data["earnings_post"], "明日決算の注目銘柄📊↓↓")) if data["earnings_post"] else ""
    return earnings_text, movers, earnings_post


def run_evening():
    today = now_jst().date()
    if is_market_holiday(today):
        print("今日は休場日のため振り返りなし")
        return
    history = load_history()
    is_week_end = is_last_business_day_of_week(today)

    # LINEで読む情報は1つのメッセージにまとめる
    sections = []
    daily = daily_review_text(history, today)
    if daily:
        sections.append(daily)
    if is_week_end:
        weekly = weekly_review_text(history, today)
        buy_text = buy_performance_text(history, today)
        sections += [t for t in (weekly, buy_text) if t]
    reminder = kenri_reminder_text(history, today)
    if reminder:
        sections.append(reminder)

    earnings_text, movers_post, earnings_post = "", "", ""
    try:
        earnings_text, movers_post, earnings_post = evening_with_claude(anthropic.Anthropic(), today)
    except Exception as e:
        print("夕方のAI処理に失敗:", e)
    if earnings_text:
        sections.append(earnings_text)

    # 実績報告（週の最終営業日だけ）は、成績のグラフ画像を付ける
    rows = performance_rows(history, today) if is_week_end else []
    perf_post = x_performance_post(rows)
    perf_image = None
    if rows:
        try:
            perf_image = x_media.render_performance_chart(x_media.image_path(today, "performance"), rows, "買い場候補の成績", f"{today:%Y.%m.%d}")
        except Exception as e:
            print("成績グラフの作成に失敗:", e)
    urls = x_media.publish_images([perf_image] if perf_image else [], today)

    messages = []
    if sections:
        messages.append({"type": "text", "text": "\n\n".join(sections)[:4900]})
    # X下書きは本文だけ（そのままコピペできるように）。順番は なぜ動いたか→明日の決算→実績報告（週末のみ）
    for post, image in ((movers_post, None), (earnings_post, None), (perf_post, perf_image)):
        if post:
            messages.append({"type": "text", "text": post})
            if image in urls:
                messages.append(x_media.line_image_message(urls[image]))
    if not messages:
        print("送る内容がありません")
        return
    push_line(messages)
    print_cost()

    x_result = x_media.post_all_to_x([(movers_post, None), (earnings_post, None), (perf_post, perf_image)])
    if "⚠️" in x_result or "⏭" in x_result:
        send_line_text(x_result)


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
