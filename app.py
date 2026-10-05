import datetime
import json
import os
import re
from zoneinfo import ZoneInfo

import anthropic
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# GitHubのSecretsから取得し、先頭・末尾の余計な空白や改行を除去（strip）
LINE_ACCESS_TOKEN = (os.environ.get("LINE_ACCESS_TOKEN") or "").strip()
USER_ID = (os.environ.get("USER_ID") or "").strip()

MODEL = "claude-opus-5-5"
JST = ZoneInfo("Asia/Tokyo")
MIN_PICKS = 5
MAX_PICKS = 15
BUBBLES_PER_CAROUSEL = 12  # LINEのカルーセルは1つにつき最大12枚

CATEGORY_STYLE = {
    "好材料": {"color": "#1DB446", "label": "📈 好材料"},
    "懸念材料": {"color": "#E0352B", "label": "📉 懸念材料"},
    "優待": {"color": "#F5A623", "label": "🎁 今月の優待"},
}

# AIが使えないとき（案A方式）のキーワード判定用
GOOD_WORDS = ["上方修正", "増配", "自社株買い", "自己株式の取得", "株式分割", "優待新設", "優待拡充", "復配", "最高益", "黒字転換"]
BAD_WORDS = ["下方修正", "減配", "無配", "赤字", "優待廃止", "特別損失", "減損", "業務停止", "不適切", "上場廃止"]


def now_jst():
    return datetime.datetime.now(JST)


# ---------------------------------------------------------------
# 1. 適時開示（TDnet）の取得
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
        if any(w in title for w in GOOD_WORDS):
            picked.append({**item, "category": "好材料"})
        elif any(w in title for w in BAD_WORDS):
            picked.append({**item, "category": "懸念材料"})
    return picked


# ---------------------------------------------------------------
# 2. みんかぶ・株探のページ取得（スクレイピング）
# ---------------------------------------------------------------
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.8",
}

# 話題の銘柄を探すときに読むページ
TOPIC_PAGES = [
    "https://minkabu.jp/",
    "https://minkabu.jp/news",
    "https://kabutan.jp/news/marketnews/",
]


def fetch_page_text(url, max_chars=15000, start_word=None):
    """ページを取ってきて、リンク付きのテキストにする。失敗したら空文字"""
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
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if text:
            a.replace_with(f"{text} <{urljoin(url, a['href'])}>")
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    text = "\n".join(line for line in lines if line)

    if start_word and start_word in text:
        text = text[max(0, text.index(start_word) - 300):]
    return text[:max_chars]


# ---------------------------------------------------------------
# 3. Claudeで調査（ニュース・話題の銘柄）→ 銘柄の選定
# ---------------------------------------------------------------
def research_with_claude(client, today, is_weekend, disclosures):
    weekday_str = "月火水木金土日"[today.weekday()]
    disclosure_text = "\n".join(
        f"- [{d['category']}] {d['code']} {d['name']}: {d['title']} ({d['pubdate']})"
        for d in disclosures[:80]
    ) or "（取得できず）"

    page_blocks = []
    for url in TOPIC_PAGES:
        text = fetch_page_text(url)
        if text:
            page_blocks.append(f"<page url=\"{url}\">\n{text}\n</page>")
    print(f"話題ページ取得: {len(page_blocks)}/{len(TOPIC_PAGES)}件")
    pages_text = "\n\n".join(page_blocks) or "（取得できず。Web検索で探してください）"

    market_note = (
        "今日は土日で市場が休み。株価は動かないので、金曜〜今日までに出たニュースと、今月が権利確定月の優待株を中心に選ぶ。"
        if is_weekend
        else "今日は平日。前日〜今朝までに出たニュース・開示で、今日の株価に影響しそうな銘柄を優先する。"
    )

    prompt = f"""今日は{today:%Y年%m月%d日}（{weekday_str}曜）です。毎朝自動でLINEに送る「今日の注目株」リストの銘柄を選んでください。
これは自動処理で、人が途中で確認・指示することはありません。質問や「配信前に必要な作業」は書かず、手元の情報で最善のリストを完成させてください。

{market_note}

## 選び方
1. まず下の「みんかぶ・株探のページ」で話題・トピックになっている銘柄を拾う（ニュース見出し、ランキング、注目銘柄など）
   - 好材料（上方修正、増配、自社株買い、大型受注、提携など）
   - 懸念材料（下方修正、減配、不祥事、優待廃止など）
   - 見出しだけで中身がわからないものは、記事をweb_fetchで開いて確認してよい
2. TDnetの適時開示も参考にする。ただし「自己株式の取得状況」のような定期報告は材料にしない
3. ニュース銘柄で枠が埋まらないときは、{today.month}月が権利確定月で優待が魅力的な銘柄を加える（みんかぶの優待情報をWeb検索で探す）
4. 合計{MIN_PICKS}〜{MAX_PICKS}件。10件以上を目標にする。根拠の弱い銘柄は入れない
5. 優待内容と権利確定月は後の処理でみんかぶから取得するので、ここでは調べなくてよい

## みんかぶ・株探のページ（今朝取得したもの）
{pages_text}

## 参考：TDnetの適時開示（キーワードで抽出したもの）
{disclosure_text}

## 出力
銘柄ごとに、証券コード（4桁）、銘柄名、分類（好材料／懸念材料／優待）、選んだ理由の要約（40字程度）、根拠となった記事のURLを書いてください。
確認できなかった情報は推測で埋めないでください。"""

    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 10, "user_location": {"type": "approximate", "country": "JP", "timezone": "Asia/Tokyo"}},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 15},
    ]
    messages = [{"role": "user", "content": prompt}]

    response = None
    for _ in range(5):  # pause_turn の再開は最大5回まで
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high"},
            tools=tools,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": response.content}]

    if response is None or response.stop_reason == "refusal":
        raise RuntimeError("Claudeの調査が拒否されました")

    notes = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not notes:
        raise RuntimeError("Claudeの調査結果が空でした")
    return notes


PICKS_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "4桁の証券コード（例: 7203, 130A）"},
                    "name": {"type": "string"},
                    "category": {"type": "string", "enum": ["好材料", "懸念材料", "優待"]},
                    "headline": {"type": "string", "description": "選んだ理由の要約（40字程度）"},
                    "source_url": {"type": "string", "description": "根拠記事のURL。なければ空文字"},
                },
                "required": ["code", "name", "category", "headline", "source_url"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["picks"],
    "additionalProperties": False,
}


def structure_picks(client, notes):
    """調査メモを決まった形のJSONに変換"""
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": PICKS_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": f"次の調査メモに出てくる銘柄を、重要度の高い順に最大{MAX_PICKS}件、指定の形式で抜き出してください。メモにない情報は足さないでください。\n\n{notes}",
            }
        ],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claudeの整形が拒否されました")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["picks"]


def clean_picks(picks):
    """コードの形式チェック・重複除去・件数上限"""
    seen = set()
    result = []
    for p in picks:
        code = p.get("code", "").strip().upper()
        if not re.fullmatch(r"\d{3}[0-9A-Z]", code) or code in seen:
            continue
        seen.add(code)
        result.append({"yutai": "不明", "kenri_month": "不明", **p, "code": code})
    return result[:MAX_PICKS]


def fallback_picks(disclosures):
    """AIが使えないとき：TDnetのキーワード判定だけで選ぶ（案A方式）"""
    seen = set()
    result = []
    for d in disclosures:
        if d["code"] in seen:
            continue
        seen.add(d["code"])
        result.append(
            {
                "code": d["code"],
                "name": d["name"],
                "category": d["category"],
                "headline": d["title"][:60],
                "yutai": "不明",
                "kenri_month": "不明",
                "source_url": d["url"],
            }
        )
    return result[:MAX_PICKS]


# ---------------------------------------------------------------
# 4. 優待内容・権利確定月（みんかぶの優待ページから）
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
                },
                "required": ["code", "yutai", "kenri_month"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def add_yutai_info(client, picks):
    """各銘柄のみんかぶ優待ページを取ってきて、AIで優待内容と権利確定月を抜き出す"""
    page_blocks = []
    for p in picks:
        url = f"https://minkabu.jp/stock/{p['code']}/yutai"
        text = fetch_page_text(url, max_chars=10000, start_word="優待")
        if text:
            page_blocks.append(f"<page code=\"{p['code']}\" name=\"{p['name']}\" url=\"{url}\">\n{text}\n</page>")
    print(f"みんかぶ優待ページ取得: {len(page_blocks)}/{len(picks)}件")
    if not page_blocks:
        return picks

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": YUTAI_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": "次はみんかぶの株主優待ページです。銘柄ごとに、現在の優待内容と権利確定月を抜き出してください。"
                    "ページに書いていないことは推測せず「不明」にしてください。\n\n" + "\n\n".join(page_blocks),
                }
            ],
        )
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
    return picks


# ---------------------------------------------------------------
# 5. 株価の取得
# ---------------------------------------------------------------
def add_price_info(picks):
    for p in picks:
        p["price_fmt"] = "取得できず"
        p["yield_fmt"] = "取得できず"
        try:
            ticker = yf.Ticker(f"{p['code']}.T")
            price = ticker.fast_info.last_price
            if not price:
                continue
            min_cost = price * 100
            p["price_fmt"] = f"約{min_cost / 10000:.1f}万円 ({price:,.0f}円)"

            # 配当利回りは「年間配当÷株価」で自前計算（yfinanceのdividendYieldは版によって単位が違うため）
            rate = ticker.info.get("dividendRate") or ticker.info.get("trailingAnnualDividendRate")
            p["yield_fmt"] = f"{rate / price * 100:.1f}%" if rate else "なし／不明"
        except Exception as e:
            print(f"株価取得失敗 ({p['code']}):", e)
    return picks


# ---------------------------------------------------------------
# 6. LINE送信
# ---------------------------------------------------------------
def build_bubble(stock, is_weekend):
    style = CATEGORY_STYLE.get(stock["category"], CATEGORY_STYLE["優待"])
    price_label = "前営業日終値ベース" if is_weekend else "最低買付額"

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

    return {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": style["color"],
            "paddingAll": "md",
            "contents": [
                {"type": "text", "text": style["label"], "color": "#FFFFFF", "weight": "bold", "size": "sm"}
            ],
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
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "lg",
                    "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": f"{price_label}: {stock['price_fmt']}", "size": "xs", "color": "#555555", "wrap": True},
                        {"type": "text", "text": f"配当利回り: {stock['yield_fmt']}", "size": "xs", "color": "#555555"},
                        {"type": "text", "text": f"優待: {stock['yutai'] or '不明'}", "size": "xs", "color": "#666666", "wrap": True},
                        {"type": "text", "text": f"権利確定月: {stock['kenri_month'] or '不明'}", "size": "xs", "color": "#666666"},
                    ],
                },
            ],
        },
        "footer": {"type": "box", "layout": "vertical", "contents": footer_buttons},
    }


def send_line_flex_message(stocks, today, is_weekend):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
    }
    weekday_str = "月火水木金土日"[today.weekday()]

    bubbles = [build_bubble(s, is_weekend) for s in stocks]
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

    payload = {"to": USER_ID, "messages": messages}
    res = requests.post(url, headers=headers, data=json.dumps(payload))
    print("送信ステータス:", res.status_code)
    if res.status_code != 200:
        print("レスポンス:", res.text)


def send_line_text(text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
    }
    payload = {"to": USER_ID, "messages": [{"type": "text", "text": text}]}
    res = requests.post(url, headers=headers, data=json.dumps(payload))
    print("送信ステータス:", res.status_code)


# ---------------------------------------------------------------
# メイン
# ---------------------------------------------------------------
def main():
    today = now_jst()
    is_weekend = today.weekday() >= 5

    # 土日・月曜は金曜からのニュースも拾う
    if is_weekend:
        days_back = today.weekday() - 4
    elif today.weekday() == 0:
        days_back = 3
    else:
        days_back = 1
    days = [today - datetime.timedelta(days=i) for i in range(days_back + 1)]
    disclosures = pick_material_disclosures(fetch_tdnet(days))
    print(f"材料になりそうな開示: {len(disclosures)}件")

    picks = []
    client = None
    try:
        client = anthropic.Anthropic()
        notes = research_with_claude(client, today, is_weekend, disclosures)
        print("調査メモ:\n", notes)
        picks = clean_picks(structure_picks(client, notes))
    except Exception as e:
        print("AIでの選定に失敗。キーワード判定に切り替えます:", e)

    if len(picks) < MIN_PICKS:
        # 足りない分をキーワード判定の結果で補う
        known = {p["code"] for p in picks}
        picks += [p for p in fallback_picks(disclosures) if p["code"] not in known]
        picks = picks[:MAX_PICKS]

    if not picks:
        send_line_text("今日の注目株は取得できませんでした。GitHub Actionsのログを確認してください。")
        return

    if client is not None:
        picks = add_yutai_info(client, picks)
    send_line_flex_message(add_price_info(picks), today, is_weekend)


if __name__ == "__main__":
    main()
