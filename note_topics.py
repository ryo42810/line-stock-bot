"""徹底解説（note）の記事用のトピック候補を、毎日17:30にLINEへ送る

その日の時事ネタを5個以上拾い、トピックと簡単なサマリを付ける（2026/11/19に朝7時・前日分から変更。当日の引け後の開示まで入れて、新鮮なネタで書くため）。
- 材料1：Claude APIのWeb検索（その日の株・経済ニュース）
- 材料2：前日17:30以降〜その日の適時開示のうち、株価に関わりそうな発表（news_posts と同じ判定）と、相場の動き
Web検索が使えないときは、材料2だけで候補を作る（それも無理なら開示の一覧だけ送る）。

記録は topics_state.json に分けて持つ（朝の配信の state.json と同時に書き換えて、保存がぶつからないように）。
"""
import datetime
import json
import re

import ai
import news_posts

STATE_FILE = "topics_state.json"
MIN_TOPICS = 5
MAX_TOPICS = 8
WEB_SEARCH_MAX_USES = 8
KEEP_DAYS = 14  # 同じネタを何日さかのぼって避けるか
SINCE_TIME = datetime.time(17, 30)  # 前の日のこの時刻より後の開示を拾う（前の回のあとに出た分）
MAX_TOKENS = 24000  # Fable は考える分（thinking）も出力の上限に数えるので、多めに取る（モデルは ai.py で決める）

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.setdefault("done", "")
    state.setdefault("recent", [])
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def recent_titles(state, today):
    cutoff = (today - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    return [r["title"] for r in state["recent"] if r["date"] >= cutoff]


def remember(state, topics, today):
    cutoff = (today - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    state["recent"] = [r for r in state["recent"] if r["date"] >= cutoff] + [
        {"date": today.isoformat(), "title": t["title"]} for t in topics
    ]


def disclosure_text(tools, tdnet_items, limit=25, since=None):
    """前の回のあと〜今日の開示から、株価に関わりそうなものを知られた会社の順に並べた材料"""
    news = news_posts.pick_news(tdnet_items, since=since)
    rank = news_posts.cap_rank(tools)
    unknown = len(rank) + 1
    news.sort(key=lambda d: (rank.get(d["code"], unknown), news_posts.KIND_ORDER.index(d["kind"])))
    lines = [f"- {d['name']}({d['code']}) ［{d['kind']}］{news_posts.norm(d['title'])[:70]}" for d in news[:limit]]
    return "\n".join(lines), news


def text_of(response):
    """Web検索つきの返事から、最後の文章だけを取り出す（考えた部分・検索結果のブロックは飛ばす）"""
    after_tool = any(b.type in ("server_tool_use", "web_search_tool_result") for b in response.content)
    return ai.text_of(response), after_tool


def parse_topics(text):
    """返事の中のJSONを読む。読めなければ空のリスト"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    topics = []
    for t in data.get("topics", []):
        title = str(t.get("title", "")).strip()
        summary = str(t.get("summary", "")).strip()
        if title and summary:
            outline = [str(o).strip()[:40] for o in (t.get("outline") or []) if str(o).strip()][:4]
            topics.append({"title": title[:40], "summary": summary[:140], "related": str(t.get("related", "")).strip()[:40], "outline": outline})
    return topics


def ask_claude(tools, client, model, news_day, market, disclosures, avoid, use_web=True):
    """トピック候補を作る。use_web=True のときはWeb検索でその日のニュースを調べる"""
    avoid_text = "\n".join(f"- {t}" for t in avoid) or "（なし）"
    source_rule = (
        f"Web検索で、{news_day:%Y年%m月%d日}（今日）の日本の株式市場・経済のニュースを調べてください。前日の夕方以降に出て、今日の相場で話題になったニュースも含めます。"
        "日経・ロイター・ブルームバーグ・NHK・株探など、報道機関や公式の発表を優先してください。"
        "検索で確かめられなかったことは書かないでください。"
        if use_web
        else "下の「材料」だけを使ってください。材料に無いことは書かないでください。"
    )
    prompt = f"""株主優待・高配当を発信している個人のnoteで、時事ネタを1本の記事にして深く解説する「徹底解説」を書いています。
その記事のトピック候補を{MIN_TOPICS}〜{MAX_TOPICS}個出してください。

## 調べ方
{source_rule}

## 選ぶ基準
- 今日（{news_day:%m/%d}）に起きた・発表された・話題になったことに限る（前日の夕方以降に出て、今日話題になったことは入れてよい）。今日が土日・祝日なら、その休みのあいだに話題になったことでよい
- 株価や個人投資家に関わりがあるもの（金利・為替・政策・税制・NISA、業界の動き、大きな会社の発表、優待・配当・株主還元の動き など）
- 1本の記事として、背景から深く書けるもの。単なる株価の上げ下げだけのものは選ばない
- 少なくとも1つは、優待・配当・株主還元に関わるものを入れる
- 幅を持たせる（同じ業界・同じ話題に偏らない）
- 最近の候補と同じ話題は避ける（続報で新しい動きがあるときだけ、続報として出してよい）

## 書き方
- title：トピック（30字以内）。記事の題になりそうな言い方で
- summary：何が起きたかと、株にどう関わるか（100字以内）。数字は確かめたものだけ
- related：関係する銘柄・業種・指標（なければ空文字）
- outline：徹底解説の記事の構成案。見出しを3つ（各25字以内）。「何が起きたか→背景・なぜ今か→個人投資家への影響（優待・配当・関連銘柄）」の流れを基本に、トピックに合わせて具体的に
- 「買い」「おすすめ」のような売買のすすめは書かない

## 出力
最後に、次の形のJSONだけを書いてください（前後に説明は書かない）。
{{"topics": [{{"title": "…", "summary": "…", "related": "…", "outline": ["…", "…", "…"]}}]}}

## 材料：相場の動き（直近の終値）
{market or "（取得できず）"}

## 材料：前日の夕方以降〜今日の適時開示（株価に関わりそうなもの・知られた会社の順）
{disclosures or "（なし）"}

## 最近の候補（避ける）
{avoid_text}"""
    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": model, "max_tokens": MAX_TOKENS, "messages": messages, "effort": "high"}
    if use_web:
        kwargs["tools"] = [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": WEB_SEARCH_MAX_USES,
                "user_location": {"type": "approximate", "country": "JP", "timezone": "Asia/Tokyo"},
            }
        ]
    text = ""
    for _ in range(3):  # 検索が長いと途中で止まる（pause_turn）ので、続きを頼む
        response = client.messages.create(**kwargs)
        tools.track_usage(response)
        searches = getattr(getattr(response.usage, "server_tool_use", None), "web_search_requests", 0) or 0
        if searches:
            print(f"ネタ候補: Web検索 {searches}回")
        if response.stop_reason == "pause_turn":
            messages = messages + [{"role": "assistant", "content": response.content}]
            kwargs["messages"] = messages
            continue
        if response.stop_reason == "refusal":
            raise RuntimeError("拒否されました")
        text, _ = text_of(response)
        break
    return parse_topics(text)


def fallback_topics(news, limit=MAX_TOPICS):
    """AIが使えないときの候補：その日の開示を並べる。同じ種類ばかりにならないように、まず種類ごとに1社ずつ選ぶ"""
    picked, kinds = [], set()
    for d in news:
        if d["kind"] not in kinds:
            picked.append(d)
            kinds.add(d["kind"])
    picked += [d for d in news if d not in picked]
    return [
        {"title": f"{d['name']}の{d['kind']}", "summary": news_posts.norm(d["title"])[:100], "related": f"{d['name']}({d['code']})", "outline": []}
        for d in picked[:limit]
    ]


NUMS = "①②③④⑤⑥⑦⑧⑨⑩"
RULE = "━━━━━━━━━━━━"


def num(i):
    return NUMS[i - 1] if i <= len(NUMS) else f"{i}."


def sentences(text):
    """要約を「。」ごとに1行にする"""
    parts = [p.strip() for p in str(text).replace("。", "。\n").split("\n")]
    return [p for p in parts if p]


def format_message(today, news_day, topics, used_web):
    """LINEで読みやすいよう、目次 → 1候補ずつ区切り線で分けて出す"""
    lines = [f"📝 徹底解説のネタ候補（{today.month}/{today.day}）", f"{news_day.month}/{news_day.day}の時事ネタから{len(topics)}本", "", "【目次】"]
    lines += [f"{num(i)} {t['title']}" for i, t in enumerate(topics, 1)]
    for i, t in enumerate(topics, 1):
        lines += ["", RULE, f"{num(i)} {t['title']}", RULE, ""]
        lines += sentences(t["summary"])
        related = [r.strip() for r in str(t.get("related", "")).split("、") if r.strip()]
        if related:
            lines += ["", "🔗 関連：" + "／".join(related)]
        if t.get("outline"):
            lines += ["", "🧭 構成案", *[f"{k}. {o}" for k, o in enumerate(t["outline"], 1)]]
    lines += ["", RULE]
    if not used_web:
        lines.append("※今回はWeb検索が使えなかったので、適時開示だけから選んでいます")
    lines.append("✍️ 書くときは、Claudeで「note：トピック」と送ってください")
    text = "\n".join(lines).strip()
    if len(text) > 4900:  # LINEの上限（5000字）を超えるときは、構成案を省いて作り直す
        return format_message(today, news_day, [{**t, "outline": []} for t in topics], used_web)[:4900]
    return text


def run(tools, today):
    """17:30の配信の本体。送った候補の数を返す"""
    yesterday = today - datetime.timedelta(days=1)
    state = load_state()
    market = news_posts.market_text(tools.fetch_market_data())
    # 前の回（前日17:30）のあとに出た開示から、今日の分まで
    since = f"{datetime.datetime.combine(yesterday, SINCE_TIME):%Y-%m-%d %H:%M:%S}"
    disclosures, news = disclosure_text(tools, tools.fetch_tdnet([yesterday, today]), since=since)
    print(f"ネタ候補の材料: 開示 {len(news)}社")
    avoid = recent_titles(state, today)

    topics, used_web = [], True
    try:
        client = ai.Client("topics", today)
        # Fable（予算を超えた月は Sonnet）でWeb検索ありで作り、だめならWeb検索なしで作り直す。
        # Fable が失敗したときの Sonnet でのやり直しは ai.Client がする
        model = client.model
        for use_web in (True, False):
            used_web = use_web
            try:
                topics = ask_claude(tools, client, model, today, market, disclosures, avoid, use_web)
            except Exception as e:
                print(f"ネタ候補づくりに失敗（{model}・Web検索{'あり' if use_web else 'なし'}）:", e)
                topics = []
            if len(topics) >= MIN_TOPICS:
                print(f"ネタ候補: Web検索{'あり' if use_web else 'なし'}で{len(topics)}個")
                break
            print(f"ネタ候補が{len(topics)}個しか出なかったので、作り直します")
    except Exception as e:
        print("Claude APIを使えませんでした:", e)
    if len(topics) < MIN_TOPICS:
        used_web = False
        topics = (topics + fallback_topics(news))[:MAX_TOPICS]
    topics = topics[:MAX_TOPICS]
    if not topics:
        tools.send_line_text(f"📝【{today:%m/%d}】徹底解説のネタ候補\n今回は材料が集められませんでした（Web検索・適時開示とも取得できず）。")
        return 0

    tools.send_line_text(format_message(today, today, topics, used_web))
    remember(state, topics, today)
    # kujira-note（徹底解説の自動下書き）が、ここから1位のネタを読む
    state["latest"] = {"date": today.isoformat(), "news_date": today.isoformat(), "used_web": used_web, "topics": topics}
    state["done"] = today.isoformat()
    save_state(state)
    tools.print_cost()
    return len(topics)
