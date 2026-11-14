"""株ニュースの投稿（朝と夕方に1本ずつ）と、週まとめ（土曜の朝に1本）

株価に関わりそうな会社の発表を広く拾う（優待・配当・自社株買いに加えて、業績予想の修正・株式分割・TOB・提携など）。
相場全体の動き（日経平均・ドル円・米国株）も1行入れる。
材料は、会社の公式発表（TDnetの適時開示）と、株価指数の終値だけ。
- 朝：前の回のあと〜今朝までに出た開示から
- 夕方：今日出た開示から（引け後の15:30〜16:30に多い）
- 土曜の朝：その週（月〜金）の開示を数えた「今週のまとめ」

開示のタイトルでニュースを拾い、PDFの1ページ目をAIに読ませて
「何がどう変わったか」を一言にする。投稿文に、PDFに無い数字が混ざっていたら決まった型の文章に切り替える。
"""
import datetime
import json
import re
import unicodedata

import earnings_watch

HASHTAGS = "#株主優待 #高配当株 #日本株 #株クラ #NISA"
MAX_STOCKS = 3  # 1本の投稿で紹介する数
# 知られた会社かどうかの目安に使う、時価総額ランキング（上位300社）
CAP_RANK_URL = "https://finance.yahoo.co.jp/stocks/ranking/marketCapitalHigh?market=all&page={page}"
CAP_RANK_PAGES = 6
PDF_CHARS = 2500
KEEP_DAYS = 14  # 投稿済みの記録を残す日数

# 定期の報告など、新しいニュースではない開示
SKIP_WORDS = ["取得状況", "取得結果", "取得終了", "取得の終了", "買付結果", "買付けの結果", "買付けに関する結果", "（訂正）", "(訂正)", "訂正"]

# 種類と、投稿で優先する順番（先に書いたものほど先）。優待の新設・廃止は数が少なく関心が高いので先にする
KIND_ORDER = [
    "優待新設", "優待廃止", "優待拡充", "優待変更", "増配", "減配・無配", "記念配当", "自社株買い",
    "上方修正", "下方修正", "業績修正", "TOB・MBO", "株式分割", "提携・M&A",
]
AXIS_KINDS = set(KIND_ORDER[:8])  # アカウントの軸（優待・配当・自社株買い）。1本の投稿に、あれば必ず1社は入れる


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
    if "自己株式" in t:
        return None  # 自己株式の公開買付け・消却などは、上で拾えた分だけにする
    if "上方修正" in t:
        return "上方修正"
    if "下方修正" in t:
        return "下方修正"
    if "業績予想" in t and "修正" in t:
        return "業績修正"  # タイトルに上方・下方が無いもの。向きはPDFを読んで書く
    if "公開買付" in t or "MBO" in t:
        return "TOB・MBO"
    if "株式分割" in t:
        return "株式分割"
    if any(w in t for w in ("業務提携", "資本提携", "事業提携", "子会社化", "経営統合", "合併")):
        return "提携・M&A"
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


_cap_rank = None


def cap_rank(tools):
    """時価総額ランキングの順位（コード→順位）。上位300社だけ。取れなければ空"""
    global _cap_rank
    if _cap_rank is None:
        codes = []
        for page in range(1, CAP_RANK_PAGES + 1):
            text = tools.fetch_page_text(CAP_RANK_URL.format(page=page), max_chars=60000)
            codes += re.findall(r"/quote/(\d{3}[0-9A-Z])\.T>", text)
        _cap_rank = {code: i for i, code in enumerate(dict.fromkeys(codes))}
        print(f"時価総額ランキング: {len(_cap_rank)}社")
    return _cap_rank


def choose(news, rank):
    """投稿に使う3社。
    1社目：優待・配当・自社株買いのニュース（あれば）。種類の優先順のあと、知られた会社を先に
    残り　：すべての種類から、知られた会社（時価総額ランキングの上位）を先に。同じ種類は1社まで"""
    unknown = len(rank) + 1
    for d in news:
        d["rank"] = rank.get(d["code"], unknown)
    picked = []
    axis = sorted((d for d in news if d["kind"] in AXIS_KINDS), key=lambda d: (KIND_ORDER.index(d["kind"]), d["rank"]))
    if axis:
        picked.append(axis[0])
    rest = sorted((d for d in news if d not in picked), key=lambda d: (d["rank"], KIND_ORDER.index(d["kind"])))
    kinds = {d["kind"] for d in picked}
    for d in rest:
        if len(picked) == MAX_STOCKS:
            break
        if d["kind"] in kinds:
            continue
        picked.append(d)
        kinds.add(d["kind"])
    for d in rest:  # 種類が足りなくて3社に届かないときは、同じ種類でも入れる
        if len(picked) == MAX_STOCKS:
            break
        if d not in picked:
            picked.append(d)
    return picked


def market_text(rows):
    """相場全体の動き（AIに渡す材料と、決まった型の文章の両方に使う）"""
    return "\n".join(f"- {r['name']}: {r['value']}（前日比 {r['change']:+.2f}%）" for r in rows)


def market_line(rows):
    """投稿に入れる1行（日経平均だけ）"""
    nikkei = next((r for r in rows if r["symbol"] == "^N225"), None)
    return f"日経平均は{nikkei['value']}円（{nikkei['change']:+.2f}%）" if nikkei else ""


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


def fallback_post(picked, when, market_rows=()):
    blocks = [f"{when}の株ニュース📰↓↓"]
    if market_line(market_rows):
        blocks.append(market_line(market_rows))
    for i, d in enumerate(picked):
        blocks.append(f"{'①②③'[i]}{d['name']}({d['code']})\n{d['kind']}を発表")
    return "\n\n".join(blocks)


def write_news(tools, client, model, picked, when, today, market_rows=()):
    """PDFの1ページ目を読んで、各社の一言（point）と投稿文を作る。失敗したらタイトルだけで作る"""
    for d in picked:
        d["point"] = f"{d['kind']}を発表"
        try:
            d["pdf"] = earnings_watch.extract_pdf_text(d["url"], pages=1, max_chars=PDF_CHARS)
        except Exception as e:
            print(f"開示PDFを読めませんでした ({d['code']}):", e)
            d["pdf"] = ""
    if client is None:
        return fallback_post(picked, when, market_rows)
    blocks = "\n\n".join(
        f"<disclosure code=\"{d['code']}\" name=\"{d['name']}\" kind=\"{d['kind']}\">\nタイトル: {d['title']}\n{d['pdf'] or '（資料は読めず。タイトルだけで書く）'}\n</disclosure>"
        for d in picked
    )
    market = market_text(market_rows)
    prompt = f"""株主優待・高配当を発信しているXアカウントの投稿を1本書きます。{when}の株ニュース（会社の公式発表と、相場全体の動き）の紹介です。
下の資料に書いてある事実と数字だけを使ってください。資料に無い数字・理由・評価は書かないでください。

## 1. items（各社の一言）
- 会社ごとに、何がどう変わったかを40字以内で書く（例: 年間配当を60円から70円に増額／100株で3,000円分の優待を新設／通期の営業利益予想を100億円から120億円に上方修正）
- 業績予想の修正は、上方修正か下方修正かを資料から読み取って書く
- 資料から読み取れないときは「{picked[0]['kind']}を発表」のように、種類だけを書く

## 2. post（X投稿）
- 1行目は「{when}の株ニュース」だとわかるフックにする
- 「相場の動き」があれば、日経平均の動きを1行で入れる（数字は相場の動きに書いてあるとおりに）
- 資料の会社を①②③で並べ、それぞれ何が変わったかを数字つきで書く
- 「買い」「おすすめ」のような売買のすすめは書かない。株価がどう動くかの予想も書かない。減配・下方修正・優待廃止も、事実として淡々と書く

{tools.X_POST_STYLE}

### 今日のフックの型
- {tools.X_HOOK_TYPES[(today.toordinal() + 5) % len(tools.X_HOOK_TYPES)]}

## 相場の動き
{market or "（取得できず。相場の行は書かない）"}

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
        return fallback_post(picked, when, market_rows)
    source = blocks + "\n" + market
    points = {i["code"].upper(): i["point"].strip() for i in data["items"]}
    for d in picked:
        point = points.get(d["code"], "")
        if point and numbers_ok(point, source):
            d["point"] = point
    post = data["post"].strip()
    codes = set(re.findall(r"\((\d{3}[0-9A-Z])\)", post))
    if not post or codes - {d["code"] for d in picked} or not numbers_ok(post, source):
        return fallback_post(picked, when, market_rows)
    return post


def prune(state, today):
    news = state.setdefault("news", {})
    cutoff = (today - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    news["posted"] = {k: v for k, v in news.get("posted", {}).items() if v >= cutoff}
    news["week"] = [w for w in news.get("week", []) if w["date"] >= cutoff]
    return news


def build(tools, client, model, tdnet_items, today, state, when, held=(), market_rows=()):
    """株ニュースの投稿を作る。戻り値は {post, stocks, title, subtitle}。ニュースが無ければ {}
    when: 「昨日」「今日」など、投稿の中での言い方 / market_rows: 相場全体の動き（app.fetch_market_data の結果）"""
    news_state = prune(state, today)
    news = pick_news(tdnet_items, posted=news_state["posted"])
    print(f"株ニュースの開示: {len(news)}社")
    if not news:
        return {}
    picked = choose(news, cap_rank(tools))
    print(f"株ニュースに使う会社: {[(d['code'], d['kind'], d['rank']) for d in picked]}")
    post = tools.format_x_post(write_news(tools, client, model, picked, when, today, market_rows))
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
        "title": "株ニュース", "subtitle": f"{when}の会社発表から",
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
    """今週の株ニュースを数えたまとめ。AIは使わない（数えるだけなので、決まった型で十分）"""
    news = pick_news(tdnet_items)
    if not news:
        return {}
    by_kind = {}
    for d in news:
        by_kind.setdefault(d["kind"], []).append(d)
    # 今週の投稿で紹介した会社を先に並べる（読者が見覚えのある名前を出す）
    featured = {w["code"] for w in prune(state, today)["week"]}
    rank = cap_rank(tools)
    for rows in by_kind.values():
        rows.sort(key=lambda d: (d["code"] not in featured, rank.get(d["code"], len(rank) + 1)))

    def group(*kinds):
        rows = [d for k in kinds for d in by_kind.get(k, [])]
        return sorted(rows, key=lambda d: (d["code"] not in featured, rank.get(d["code"], len(rank) + 1)))

    groups = [
        ("優待の新設・再開", group("優待新設")),
        ("優待の拡充・変更", group("優待拡充", "優待変更")),
        ("優待の廃止", group("優待廃止")),
        ("増配・記念配当", group("増配", "記念配当")),
        ("減配・無配", group("減配・無配")),
        ("自社株買い", group("自社株買い")),
        ("業績予想の修正", group("上方修正", "下方修正", "業績修正")),
        ("TOB・MBO", group("TOB・MBO")),
        ("株式分割", group("株式分割")),
    ]
    groups = [(label, rows) for label, rows in groups if rows]
    days = week_days(today)
    span = f"{days[0]:%m/%d}〜{days[-1]:%m/%d}"
    # 画像は6項目まで。件数の少ない「優待の廃止」「減配・無配」は、あるときだけ前の項目にまとめる
    merged = [
        ("優待の新設・再開", group("優待新設")),
        ("優待の拡充・変更・廃止", group("優待拡充", "優待変更", "優待廃止")),
        ("増配・記念配当", group("増配", "記念配当")),
        ("自社株買い", group("自社株買い")),
        ("業績予想の修正", group("上方修正", "下方修正", "業績修正")),
        ("TOB・MBO／株式分割", group("TOB・MBO", "株式分割")),
    ]
    points = [f"{label}：{len(rows)}社（{names_text(rows)}）" for label, rows in merged if rows][:6]
    lines = [f"今週（{span}）の株ニュース、まとめた📝↓↓", ""]
    lines += [f"・{label} {len(rows)}社" for label, rows in groups]
    top = groups[0]
    lines += ["", f"{top[0]}は{names_text(top[1])}👀"]
    return {
        "post": tools.add_hashtags(tools.format_x_post("\n".join(lines)), HASHTAGS),
        "points": points, "title": "今週の株ニュース", "subtitle": f"{span}の会社発表を集計",
    }
