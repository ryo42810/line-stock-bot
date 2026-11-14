"""優待・配当ニュースの投稿（朝と夕方に1本ずつ）と、週まとめ（土曜の朝に1本）

材料は、会社の公式発表（TDnetの適時開示）だけ。
- 朝：前の回のあと〜今朝までに出た開示から
- 夕方：今日出た開示から（引け後の15:30〜16:30に多い）
- 土曜の朝：その週（月〜金）の開示を数えた「今週のまとめ」

開示のタイトルで優待・配当・自社株買いのニュースを拾い、PDFの1ページ目をAIに読ませて
「何がどう変わったか」を一言にする。投稿文に、PDFに無い数字が混ざっていたら決まった型の文章に切り替える。
"""
import datetime
import json
import re
import unicodedata

import yfinance as yf

import earnings_watch

HASHTAGS = "#株主優待 #高配当株 #株クラ #NISA #投資家さんと繋がりたい"
MAX_STOCKS = 3  # 1本の投稿で紹介する数
MAX_CANDIDATES = 24  # 時価総額を調べる数の上限（時間を抑えるため）
PDF_CHARS = 2500
KEEP_DAYS = 14  # 投稿済みの記録を残す日数

# 定期の報告など、新しいニュースではない開示
SKIP_WORDS = ["取得状況", "取得結果", "取得終了", "取得の終了", "買付結果", "買付けの結果", "買付けに関する結果", "（訂正）", "(訂正)", "訂正"]

# 種類と、投稿で優先する順番（数字が小さいほど先）。優待の新設・廃止は数が少なく関心が高いので先にする
KIND_ORDER = ["優待新設", "優待廃止", "優待拡充", "優待変更", "増配", "減配・無配", "記念配当", "自社株買い"]


def norm(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def classify(title):
    """開示のタイトルから種類を決める。優待・配当・自社株買いのニュースでなければ None"""
    t = norm(title)
    if any(w in t for w in SKIP_WORDS):
        return None
    if "株主優待" in t:
        if "廃止" in t:
            return "優待廃止"
        if any(w in t for w in ("新設", "導入", "再開", "開始")):
            return "優待新設"
        if any(w in t for w in ("拡充", "増額")):
            return "優待拡充"
        if "変更" in t:
            return "優待変更"
        # 「取扱いについて」「実施の見送り」などは中身がまちまちなので、優待のニュースとしては拾わない（下の配当の判定には進む）
    if any(w in t for w in ("減配", "無配")):
        return "減配・無配"
    if any(w in t for w in ("増配", "復配")):
        return "増配"
    if "記念配当" in t or "特別配当" in t:
        return "記念配当"
    if "自己株式" in t and "取得" in t and any(w in t for w in ("決定", "取得及び", "取得枠", "取得について", "取得に関する")):
        return "自社株買い"
    return None


def doc_key(item):
    return f"{item['code']}|{item['pubdate']}|{norm(item['title'])[:40]}"


def pick_news(tdnet_items, posted=(), since=None):
    """優待・配当ニュースの開示を、優先する順に並べて返す（同じ会社は1件にまとめる）"""
    best = {}
    for item in tdnet_items:
        kind = classify(item["title"])
        if not kind or doc_key(item) in posted:
            continue
        if since and item["pubdate"] < since:
            continue
        cur = best.get(item["code"])
        if cur is None or KIND_ORDER.index(kind) < KIND_ORDER.index(cur["kind"]):
            best[item["code"]] = {**item, "name": norm(item["name"]), "kind": kind}
    return sorted(best.values(), key=lambda d: (KIND_ORDER.index(d["kind"]), d["pubdate"]))


def market_cap(code):
    try:
        return yf.Ticker(f"{code}.T").fast_info["marketCap"] or 0
    except Exception:
        return 0


def choose(news, held=()):
    """投稿に使う3社。種類の優先順のあと、同じ種類の中では知られた会社（時価総額が大きい順）にする。
    1本の投稿が同じ種類ばかりにならないように、種類ごとに2社までにする"""
    pool = news[:MAX_CANDIDATES]
    for d in pool:
        d["cap"] = market_cap(d["code"])
    pool.sort(key=lambda d: (KIND_ORDER.index(d["kind"]), -d["cap"]))
    picked, per_kind = [], {}
    for d in pool:
        if per_kind.get(d["kind"], 0) >= 2:
            continue
        picked.append(d)
        per_kind[d["kind"]] = per_kind.get(d["kind"], 0) + 1
        if len(picked) == MAX_STOCKS:
            break
    return picked


NEWS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "point": {"type": "string", "description": "何がどう変わったかを40字以内で。資料の数字を使う（例: 年間配当を60円から70円に増額）"},
                },
                "required": ["code", "point"],
                "additionalProperties": False,
            },
        },
        "post": {"type": "string", "description": "X投稿の本文。ハッシュタグは付けない"},
    },
    "required": ["items", "post"],
    "additionalProperties": False,
}


def numbers_ok(text, source):
    """文章に、資料に無い数字が混ざっていないか"""
    allowed = set(re.findall(r"\d+(?:\.\d+)?", unicodedata.normalize("NFKC", source).replace(",", "")))
    used = re.findall(r"\d+(?:\.\d+)?", unicodedata.normalize("NFKC", text).replace(",", ""))
    extra = [n for n in used if n not in allowed and n not in ("1", "2", "3")]
    if extra:
        print(f"ニュース枠: 資料に無い数字が入っていたため、決まった型の文章を使います: {extra}")
    return not extra


def fallback_post(picked, when):
    blocks = [f"{when}の優待・配当ニュース📰↓↓"]
    for i, d in enumerate(picked):
        blocks.append(f"{'①②③'[i]}{d['name']}({d['code']})\n{d['kind']}を発表")
    return "\n\n".join(blocks)


def write_news(tools, client, model, picked, when, today):
    """PDFの1ページ目を読んで、各社の一言（point）と投稿文を作る。失敗したらタイトルだけで作る"""
    for d in picked:
        d["point"] = f"{d['kind']}を発表"
        try:
            d["pdf"] = earnings_watch.extract_pdf_text(d["url"], pages=1, max_chars=PDF_CHARS)
        except Exception as e:
            print(f"開示PDFを読めませんでした ({d['code']}):", e)
            d["pdf"] = ""
    if client is None:
        return fallback_post(picked, when)
    blocks = "\n\n".join(
        f"<disclosure code=\"{d['code']}\" name=\"{d['name']}\" kind=\"{d['kind']}\">\nタイトル: {d['title']}\n{d['pdf'] or '（資料は読めず。タイトルだけで書く）'}\n</disclosure>"
        for d in picked
    )
    prompt = f"""株主優待・高配当を発信しているXアカウントの投稿を1本書きます。{when}に出た会社の公式発表（適時開示）の紹介です。
下の資料に書いてある事実と数字だけを使ってください。資料に無い数字・理由・評価は書かないでください。

## 1. items（各社の一言）
- 会社ごとに、何がどう変わったかを40字以内で書く（例: 年間配当を60円から70円に増額／100株で3,000円分の優待を新設）
- 資料から読み取れないときは「{picked[0]['kind']}を発表」のように、種類だけを書く

## 2. post（X投稿）
- 1行目は「{when}の優待・配当ニュース」だとわかるフックにする
- 資料の会社を①②③で並べ、それぞれ何が変わったかを数字つきで書く
- 「買い」「おすすめ」のような売買のすすめは書かない。減配や優待廃止も、事実として淡々と書く

{tools.X_POST_STYLE}

### 今日のフックの型
- {tools.X_HOOK_TYPES[(today.toordinal() + 5) % len(tools.X_HOOK_TYPES)]}

## 資料
{blocks}"""
    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            output_config={"format": {"type": "json_schema", "schema": NEWS_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        tools.track_usage(response)
        if response.stop_reason == "refusal":
            raise RuntimeError("拒否されました")
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
    except Exception as e:
        print("ニュース枠の文章づくりに失敗。決まった型の文章を使います:", e)
        return fallback_post(picked, when)
    source = blocks
    points = {i["code"].upper(): i["point"].strip() for i in data["items"]}
    for d in picked:
        point = points.get(d["code"], "")
        if point and numbers_ok(point, source):
            d["point"] = point
    post = data["post"].strip()
    codes = set(re.findall(r"\((\d{3}[0-9A-Z])\)", post))
    if not post or codes - {d["code"] for d in picked} or not numbers_ok(post, source):
        return fallback_post(picked, when)
    return post


def prune(state, today):
    news = state.setdefault("news", {})
    cutoff = (today - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    news["posted"] = {k: v for k, v in news.get("posted", {}).items() if v >= cutoff}
    news["week"] = [w for w in news.get("week", []) if w["date"] >= cutoff]
    return news


def build(tools, client, model, tdnet_items, today, state, when, held=()):
    """優待・配当ニュースの投稿を作る。戻り値は {post, stocks, title, subtitle}。ニュースが無ければ {}
    when: 「昨日」「今日」など、投稿の中での言い方"""
    news_state = prune(state, today)
    news = pick_news(tdnet_items, posted=news_state["posted"])
    print(f"優待・配当ニュースの開示: {len(news)}社（{[(d['code'], d['kind']) for d in news[:8]]}）")
    if not news:
        return {}
    picked = choose(news, held)
    post = tools.format_x_post(write_news(tools, client, model, picked, when, today))
    if any(d["code"] in held for d in picked):
        post = post.rstrip() + "\n\n※この中に保有中の銘柄があります"
    stocks = tools.add_price_info(
        [{"code": d["code"], "name": d["name"], "category": d["kind"], "headline": d["point"], "source_url": d["url"]} for d in picked], today
    )
    for d in picked:
        news_state["posted"][doc_key(d)] = today.isoformat()
        news_state["week"].append({"date": today.isoformat(), "code": d["code"], "name": d["name"], "kind": d["kind"], "point": d["point"]})
    return {
        "post": tools.add_hashtags(post, HASHTAGS), "stocks": stocks,
        "title": "優待・配当ニュース", "subtitle": f"{when}の会社発表から",
    }


# ---------------------------------------------------------------
# 週まとめ（土曜の朝）
# ---------------------------------------------------------------
def week_days(today):
    """today（土曜）の直前の月〜金"""
    monday = today - datetime.timedelta(days=today.weekday())
    return [monday + datetime.timedelta(days=i) for i in range(5)]


def names_text(rows, limit=3):
    names = list(dict.fromkeys(r["name"] for r in rows))
    return "、".join(names[:limit]) + ("など" if len(names) > limit else "")


def build_weekly(tools, tdnet_items, today, state):
    """今週の優待・配当ニュースを数えたまとめ。AIは使わない（数えるだけなので、決まった型で十分）"""
    news = pick_news(tdnet_items)
    if not news:
        return {}
    by_kind = {}
    for d in news:
        by_kind.setdefault(d["kind"], []).append(d)
    # 今週の投稿で紹介した会社を先に並べる（読者が見覚えのある名前を出す）
    featured = {w["code"] for w in prune(state, today)["week"]}
    for rows in by_kind.values():
        rows.sort(key=lambda d: d["code"] not in featured)

    def group(*kinds):
        return [d for k in kinds for d in by_kind.get(k, [])]

    groups = [
        ("優待の新設・再開", group("優待新設")),
        ("優待の拡充・変更", group("優待拡充", "優待変更")),
        ("優待の廃止", group("優待廃止")),
        ("増配・記念配当", group("増配", "記念配当")),
        ("減配・無配", group("減配・無配")),
        ("自社株買い", group("自社株買い")),
    ]
    groups = [(label, rows) for label, rows in groups if rows]
    days = week_days(today)
    span = f"{days[0]:%m/%d}〜{days[-1]:%m/%d}"
    points = [f"{label}：{len(rows)}社（{names_text(rows)}）" for label, rows in groups][:4]
    lines = [f"今週（{span}）の優待・配当ニュース、まとめた📝↓↓", ""]
    lines += [f"・{label} {len(rows)}社" for label, rows in groups]
    top = groups[0]
    lines += ["", f"{top[0]}は{names_text(top[1])}👀"]
    return {
        "post": tools.add_hashtags(tools.format_x_post("\n".join(lines)), HASHTAGS),
        "points": points, "title": "今週の優待・配当ニュース", "subtitle": f"{span}の会社発表を集計",
    }
