import datetime
import json
import os
import re
from zoneinfo import ZoneInfo

import anthropic
import requests
import yfinance as yf

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
# 2. Claudeで調査（Web検索）→ 銘柄の選定
# ---------------------------------------------------------------
def research_with_claude(client, today, is_weekend, disclosures):
    weekday_str = "月火水木金土日"[today.weekday()]
    disclosure_text = "\n".join(
        f"- [{d['category']}] {d['code']} {d['name']}: {d['title']} ({d['pubdate']})"
        for d in disclosures[:80]
    ) or "（取得できず）"

    market_note = (
        "今日は土日で市場が休み。株価は動かないので、金曜〜今日までに出たニュースと、今月が権利確定月の優待株を中心に選ぶ。"
        if is_weekend
        else "今日は平日。前日〜今朝までに出たニュース・開示で、今日の株価に影響しそうな銘柄を優先する。"
    )

    prompt = f"""今日は{today:%Y年%m月%d日}（{weekday_str}曜）です。日本株の個人投資家向けに、今朝LINEで送る注目銘柄リストを作るための調査をしてください。

{market_note}

## 選び方
1. 当日・前日にニュースや適時開示が出た東証上場銘柄を優先する
   - 好材料（上方修正、増配、自社株買い、大型受注、提携など）
   - 懸念材料（下方修正、減配、不祥事、優待廃止など）
   - 個人投資家に影響が大きいものを優先
2. ニュース銘柄だけで{MIN_PICKS}件に届かない、または枠が余るときは、{today.month}月が権利確定月で、優待内容が魅力的な株主優待銘柄で埋める
3. 合計{MIN_PICKS}〜{MAX_PICKS}件。多いほうがよいが、根拠の弱い銘柄は入れない
4. すべての銘柄について、現在の株主優待の内容と権利確定月をWebで確認する（優待がない銘柄は「なし」）
   - 優待内容は古い情報を使わず、最新の内容をWeb検索で確認すること

## 参考：TDnetの適時開示（キーワードで抽出したもの）
{disclosure_text}

## 出力
銘柄ごとに、証券コード（4桁）、銘柄名、分類（好材料／懸念材料／優待）、選んだ理由の要約（40字程度）、優待内容、権利確定月、根拠となった記事のURLをまとめてください。
確認できなかった情報は推測で埋めず「不明」と書いてください。"""

    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 15, "user_location": {"type": "approximate", "country": "JP", "timezone": "Asia/Tokyo"}},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10},
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
                    "yutai": {"type": "string", "description": "株主優待の内容。なければ「なし」、不明なら「不明」"},
                    "kenri_month": {"type": "string", "description": "権利確定月（例: 3月・9月）。不明なら「不明」"},
                    "source_url": {"type": "string", "description": "根拠記事のURL。なければ空文字"},
                },
                "required": ["code", "name", "category", "headline", "yutai", "kenri_month", "source_url"],
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
        result.append({**p, "code": code})
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
# 3. 株価の取得
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
# 4. LINE送信
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

    send_line_flex_message(add_price_info(picks), today, is_weekend)


if __name__ == "__main__":
    main()
