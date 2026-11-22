"""共感ポスト：読んだ人が「わかる」「自分はこう思う」と言いたくなる短い投稿を、毎日 7:21ごろ・12:34ごろ・18:39ごろ にXへ出す
（時刻は日ごとに数分ずらす。x_queue.slot_time）

- 投稿は kyokan_stock.json にストックしておき、x_queue.py の回（1時間おき）が、その日の3本を予約に並べる
  （GitHub Actionsの起動は数時間遅れることがあるので、その場で作らずに前もって作っておく）
- ストックが少なくなったら、AI（ai.py の Opus）がまとめて作り足し、LINEに一覧を送る
- 型は6種類（TYPES）。続けて同じ型が出ないように並べる
- 6つ目の「仮の数字で見方を変える」の型（kazu）だけ、仮の数字（「仮に」「〜とする」などの、実在の銘柄を指さない数字）を使ってよい
  - この型は、チャットで作って検算したもの（source="chat"）だけをストックに入れる。botの自動作り足し（refill）では作らない
  - 18:39ごろの枠は、ストックにこの型があれば先に出す（2026/11/22の投稿で、この型の反応が良かったため）
- 過去に作った投稿（出したもの・ストック中のもの）と似ている投稿は、作り足すときにはじく
- 投稿の最後には、かならず HASHTAGS（#株 #株クラ）を付ける。ストックには本文だけを持つ
- チャットのスキル「投稿文作成」で作った投稿も、このファイルに source="chat" で入れれば同じように出る

ストックの1件： {"id", "type", "theme", "post", "source", "created", "status", "date", "time"}
status： pending（出番待ち）→ queued（その日の予約に並べた）→ posted（出した）
"""
import difflib
import json
import re
import unicodedata

import ai
import quick_posts
import x_queue

STOCK_FILE = "kyokan_stock.json"
SLOTS = (("07:21", "09:30"), ("12:34", "14:30"), ("18:39", "21:30"))  # （出す時刻のもと, これより遅れたら見送る）。実際の時刻は日ごとに数分ずらす
MIN_STOCK = 9  # これより少なくなったら作り足す（3日分）
BATCH = 15  # 1回に作り足す本数
NUMBER_TYPES = ("kazu",)  # 仮の数字を使ってよい型
CHAT_ONLY_TYPES = ("kazu",)  # チャットで作って検算したもの（source="chat"）だけ入れる型。botの作り足しでは作らない
PREFER_AT = {"18:39": "kazu"}  # この枠（もとの時刻）は、ストックにこの型があれば先に出す
ASK_WORDS = ("教えて", "リプで", "リプして", "いいねして", "フォローして", "リポストして", "保存して")  # 反応を頼む言い方（Xの収益化のルールで禁止）
SIMILAR = 0.6  # これ以上似ていたら同じ内容とみなす（0〜1）
HASHTAGS = "#株 #株クラ"  # すべての投稿の最後に付ける（AIには書かせず、予約に並べるときにプログラムで付ける）
MAX_CHARS = 130  # 本文の長さの上限（全角で数える。ハッシュタグを付けても140字に収まるように）
NG_WORDS = ("絶対上がる", "必ず上がる", "今すぐ買え", "買うべき", "儲かる", "爆益確定")

# 伸びている株クラの投稿（2026/11 にXで調査）を参考にした5つの型
TYPES = {
    "flip": {
        "name": "空気をひっくり返す",
        "how": "株クラにある「〇〇ほど正義」「〇〇なら安心」みたいな空気を1文目で言い、2文目で「〇〇と〇〇って全く別の話だよね」と静かに言い切る。",
        "example": "高配当株って利回りが高いほど\n正義みたいな空気あるけど\n\n利回りの高さと\n安心して持ち続けられるかって\n全く別の話だよね",
    },
    "aruaru": {
        "name": "株クラあるある・一言",
        "how": "株をやっている人ならクスッとする、短いあるあるや自虐の一言。「〇〇で草」「〇〇のコツ：やらない」のような、オチが1行で分かるもの。",
        "example": "株クラ民がギスギスしてる理由 第2位\n\n3ヶ月もお金が増えない",
    },
    "join": {
        "name": "参加したくなる問いかけ",
        "how": "「あてはまる人ー？」「〇〇派と〇〇派、どっち？」のように、ひと言で答えられる問いかけで終わる。答えやすい二択か、手を挙げるだけでいい形にする。",
        "example": "優待が届いたとき\n\n①すぐ使う派\n②もったいなくて取っておく派\n\nどっち？",
    },
    "question": {
        "name": "素朴な疑問",
        "how": "「ちょっと気になるんですが」から入り、投資の制度やみんなの習慣について、詳しい人が答えたくなる素朴な疑問を投げる。事実の説明はせず、疑問のまま終える。",
        "example": "ちょっと気になるんですが\n\n権利確定日の翌日に株価が下がるの\nみんな分かってて持ち続けてるんですか？",
    },
    "honne": {
        "name": "本音",
        "how": "「正直に言うと」「ぶっちゃけ」から入り、多くの人が思っているけど言いにくい本音を1つだけ言う。強い主張ではなく、共感されるくらいの温度にする。",
        "example": "正直に言うと\n\n優待のカタログが届く日のほうが\n配当金が入る日よりワクワクする",
    },
    "kazu": {
        "name": "仮の数字で見方を変える",
        "how": "「仮に」「〜とする」などの仮の数字を1つ使って、ふだんの見方をひっくり返す。計算は暗算で確かめられるものだけにし、必ず検算する。銘柄名・社名・銘柄コード・実際の株価は書かない。",
        "example": "仮に、年10万円の配当が出るとする\n\n税金で約2割引かれて\n手元に残るのは約8万円\n\n配当は額面より\n手取りで見たほうがいいよね",
    },
}
BOT_TYPES = [key for key in TYPES if key not in CHAT_ONLY_TYPES]  # botが自分で作り足す型
ORDER = list(TYPES)

PROMPT = """あなたはXで「くじら@20代|株主優待🐳」という、優待・高配当株のアカウントを運用している20代の個人投資家です。
読んだ人が「わかる」「自分はこう思う」とリプや引用をしたくなる、短い投稿を{n}本作ってください。

# 5つの型（各型を{per}本前後ずつ）
{types}

# 書き方
- テーマは、優待・高配当・配当金・NISA・株主総会・権利確定日・株クラの空気など、優待・高配当の投資家に身近なもの
- 1本は全角{max_chars}文字以内。短い行で改行し、意味のかたまりごとに空行を入れる
- タメ口のやわらかい話し言葉（関西弁は使わない）。「〜だよね」「〜ってない？」など
- 絵文字は使わないか、最後に1つまで。ハッシュタグ・URL・@は入れない（ハッシュタグはあとでプログラムが付ける）
- 「じゃ、見ていこう🐳」は使わない（別の投稿の合図なので）。くじらの比喩も使わない

# やってはいけないこと
- 実在の会社の株価・利回り・配当額・日付・銘柄名・社名を書かない（間違いを書かないため）
- 数字（〇円・〇%・〇株・〇倍）を使わない
- 「教えてください」「リプで」「いいねして」「フォローして」「リポストして」「保存して」のように、反応を頼む言い方（問いかけはよいが、行動を頼まない。Xの収益化のルールで禁止されているため）
- 「〇〇株を買った」「優待が届いた」など、自分の保有・売買・受け取りの体験談として書かない（気持ちや考えはOK）
- 株価の断定、売買のすすめ、特定の人やグループを見下す言い方、炎上をねらった言い方
- 下の「これまでの投稿」と同じ内容・同じ切り口・同じオチ。言い回しを変えただけのものもダメ
- この依頼の中で、同じテーマを2本以上使うこと

# これまでの投稿（かぶらないように）
{past}

# 出力
JSONの配列だけを出してください。説明やコードブロックは不要です。
[{{"type": "{type_keys}のどれか", "theme": "テーマを10文字以内で", "post": "投稿文"}}, ...]
"""


def load():
    try:
        with open(STOCK_FILE, encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        items = []
    return items if isinstance(items, list) else []


def save(items):
    with open(STOCK_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
        f.write("\n")


def normalize(text):
    """似ているかを比べるために、記号・空白・絵文字を落とした文字列にする"""
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in text if unicodedata.category(ch)[0] in ("L", "N"))


def width(text):
    """全角を1、半角を0.5として数えた長さ"""
    return sum(1 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 0.5 for ch in text)


def similar(a, b):
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_duplicate(post, theme, items):
    """これまでの投稿と同じ内容か（文面が似ている、またはテーマが同じで文面もある程度似ている）"""
    for item in items:
        score = similar(post, item.get("post", ""))
        if score >= SIMILAR:
            return True
        if theme and normalize(theme) == normalize(item.get("theme", "")) and score >= SIMILAR / 2:
            return True
    return False


def problem_of(post, kind=""):
    """出してはいけない投稿なら理由を返す。kind が NUMBER_TYPES のときだけ、例えの数字を通す"""
    if not post.strip():
        return "空"
    if width(post) > MAX_CHARS:
        return "長すぎる"
    if re.search(r"https?://|[#＃@＠]", post):
        return "URL・ハッシュタグ・@が入っている"
    if kind not in NUMBER_TYPES and re.search(r"[0-9０-９]+\s*(円|%|％|株|倍)", post):
        return "具体的な数字が入っている"
    asked = [w for w in ASK_WORDS if w in post]
    if asked:
        return f"反応を頼む言い方が入っている {asked}"
    names = [name for _, name in quick_posts.WIDE if name in unicodedata.normalize("NFKC", post)]
    if names:
        return f"社名が入っている {names}"
    if re.search(r"[（(][0-9０-９]{4}[）)]", post):
        return "銘柄コードが入っている"
    hit = [w for w in NG_WORDS if w in post]
    if hit:
        return f"NGワード {hit}"
    return ""


def add(items, candidates, source, today):
    """candidates: [{"type", "theme", "post"}, ...] のうち、出してよくて、かぶらないものだけをストックに足す。足したものを返す"""
    added = []
    for c in candidates:
        post = (c.get("post") or "").strip()
        kind = c.get("type") if c.get("type") in TYPES else ""
        theme = (c.get("theme") or "").strip()
        problem = problem_of(post, kind) or ("型が分からない" if not kind else "")
        if not problem and kind in CHAT_ONLY_TYPES and source != "chat":
            problem = "この型はチャットで作って検算したものだけ"
        if problem:
            print(f"共感ポストをはじきました（{problem}）: {post[:30]}")
            continue
        if is_duplicate(post, theme, items + added):
            print(f"共感ポストをはじきました（過去の投稿とかぶる）: {post[:30]}")
            continue
        added.append({
            "id": f"{today:%Y%m%d}-{source}-{len(items) + len(added) + 1:03d}",
            "type": kind, "theme": theme, "post": post, "source": source,
            "created": today.isoformat(), "status": "pending", "date": "", "time": "",
        })
    items.extend(added)
    return added


def with_tags(post):
    """本文の最後に、決まったハッシュタグを付ける"""
    return f"{post.rstrip()}\n\n{HASHTAGS}"


def parse(text):
    match = re.search(r"\[.*\]", text or "", re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except Exception:
        return []
    return [d for d in data if isinstance(d, dict)]


def generate(client, items, n=BATCH):
    """AIにn本作らせる（まだストックには足さない）"""
    past = [i for i in items if i.get("post")][-120:]
    lines = "\n".join(f"- [{TYPES.get(i.get('type'), {}).get('name', '')}] " + i["post"].replace("\n", " ") for i in past) or "（まだありません）"
    types = "\n\n".join(f"## {key}：{TYPES[key]['name']}\n{TYPES[key]['how']}\n例：\n{TYPES[key]['example']}" for key in BOT_TYPES)
    prompt = PROMPT.format(n=n, per=max(1, n // len(BOT_TYPES)), types=types, max_chars=MAX_CHARS, past=lines, type_keys="/".join(BOT_TYPES))
    response = client.messages.create(effort="high", max_tokens=6000, messages=[{"role": "user", "content": prompt}])
    return parse(ai.text_of(response))


def refill(items, today, client_fn, notify):
    """ストックが少なければ作り足す。足した本数を返す"""
    pending = [i for i in items if i.get("status") in ("pending", "queued")]  # まだ出していないもの
    if len(pending) >= MIN_STOCK:
        return 0
    client = client_fn()
    added = []
    for _ in range(2):  # はじかれて足りなければ、もう1回だけ作る
        added += add(items, generate(client, items), "bot", today)
        if len(pending) + len(added) >= MIN_STOCK:
            break
    if added:
        body = "\n\n―――\n\n".join(f"【{TYPES[a['type']]['name']}】\n{a['post']}" for a in added)
        notify(f"🗣 共感ポストのストックを{len(added)}本作り足しました（毎日7:21ごろ・12:34ごろ・18:39ごろに順番に出ます）\n\n{body}")
    return len(added)


def pick(items, last_type, prefer=""):
    """出番待ちから1本選ぶ。チャットで作ったものを先に、前と同じ型は避ける。prefer の型があれば、それをいちばん先にする"""
    pending = [i for i in items if i.get("status") == "pending"]
    if not pending:
        return None
    pending.sort(key=lambda i: (i.get("source") != "chat", i.get("created", ""), i.get("id", "")))
    if prefer:
        first = next((i for i in pending if i.get("type") == prefer), None)
        if first:
            return first
    else:
        # ほかの枠で先に出すと決めている型は、その枠のために取っておく（ほかに出すものが無いときだけ使う）
        others = [i for i in pending if i.get("type") not in PREFER_AT.values()]
        pending = others or pending
    for item in pending:
        if item.get("type") != last_type:
            return item
    return pending[0]


def reconcile(items, queue, today):
    """予約の結果をストックに写す。出せたら posted、見送り・行方不明なら出番待ちに戻す"""
    status = {(q.get("date"), q.get("time")): q.get("status") for q in queue}
    for item in items:
        if item.get("status") != "queued":
            continue
        result = status.get((item.get("date"), item.get("time")))
        if result == "posted":
            item["status"] = "posted"
        elif result == "skipped" or (result is None and item.get("date", "") < today.isoformat()):
            item.update(status="pending", date="", time="")


def last_type(items):
    done = [i for i in items if i.get("status") in ("posted", "queued") and i.get("date")]
    return max(done, key=lambda i: (i["date"], i["time"]))["type"] if done else ""


def prepare(now, client_fn, notify):
    """x_queue.py の回の最初に呼ぶ。ストックを整えて、今日の3本を予約に並べる"""
    today = now.date()
    x_queue.sync()
    items = load()
    queue = x_queue.load()
    reconcile(items, queue, today)
    try:
        refill(items, today, client_fn, notify)
    except Exception as e:  # 作り足せなくても、ストックがあれば出す
        print("共感ポストを作り足せませんでした:", type(e).__name__, str(e)[:200])
    booked = {(q.get("date"), q.get("time")) for q in queue}
    entries = []
    for base, until in SLOTS:
        hhmm = x_queue.slot_time(today, base)
        if (today.isoformat(), hhmm) in booked or now > x_queue._at(today, until, now.tzinfo):
            continue
        item = pick(items, last_type(items), PREFER_AT.get(base, ""))
        if not item:
            print("共感ポストのストックがありません")
            break
        item.update(status="queued", date=today.isoformat(), time=hhmm)
        entries.append((hhmm, until, with_tags(item["post"])))
    save(items)
    for hhmm, until, post in entries:
        x_queue.enqueue(today, [(hhmm, post, None)], until)
    x_queue.push("共感ポストのストックを更新", files=(STOCK_FILE, "budget"))
