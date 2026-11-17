"""Xのリプ案（半自動）：自分あてのリプと、絡む候補の投稿を拾って、Opusで返信案を作り、LINEに送る

- 1日3回（平日の10:00・13:00・18:00）。土日・祝日は動かさない
- 自分あてのリプ（メンション）：全部拾って返信案を作る（最大 MENTION_MAX 件）
- 絡む候補：優待・高配当まわりの青バッジの人の投稿を検索して、1回 PER_RUN 人に絞る
  - 同じ人には1日1回まで（その日に絡んだ人は state に記録）
  - フォロワーの人：それ以外 ＝ 3：7 くらいになるように選ぶ
- Xには何も投稿しない。LINEのカードの「コピー」「返信する」から、自分で確かめて送る
  （Xのルールで、自分あてでない投稿への自動返信は禁止。APIでも弾かれる）
- 使いすぎ防止：X APIの読み取りに使った額を月ごとに数え、X_BUDGET_USD（初期値40ドル）を超えそうなら、その月は止めてLINEで知らせる
- AI（Opus）の分は ai.py の使いすぎ防止にそのまま乗る（budget/replies.json に記録）
"""
import datetime
import json
import os
import re
import subprocess
import time
import urllib.parse
from zoneinfo import ZoneInfo

import ai
import x_media

JST = ZoneInfo("Asia/Tokyo")
STATE_FILE = "replies_state.json"
API = "https://api.x.com/2"

SLOTS = [datetime.time(10, 0), datetime.time(13, 0), datetime.time(18, 0)]
LAST_DEADLINE = datetime.time(22, 0)  # 18:00の回がこれより遅れたら、その日は見送る
EARLY_LIMIT_MIN = 240  # 次の回までこれより早く起動したら、何もしないで終わる（あとの予約に任せる）

PER_RUN = 5  # 1回に絡む人数（1日15人）
FOLLOWER_SHARE = 0.3  # フォロワーの人の割合
SEARCH_MAX = 20  # 1回に検索で読む投稿の数（多いほど選べるが、読み取り料金が増える）
MENTION_MAX = 10  # 1回に返信案を作る自分あてのリプの数
SEARCH_HOURS = 6  # 何時間前までの投稿から探すか
VOICE_REFRESH_DAYS = 7  # 自分の過去の投稿（口調のお手本）を読み直す間隔
REPLY_MAX_CHARS = 90  # 返信案の長さの目安（返信リンクに文章を入れられるように短めにする）

KEYWORDS = ["株主優待", "優待", "高配当", "配当金", "増配", "新NISA"]
SKIP_WORDS = ["プレゼント企画", "フォロー&RT", "フォロー＆RT", "フォローとRT", "抽選で", "LINE登録", "無料で配布"]
VERIFIED_TYPES = ("blue", "business", "government")

# X APIの料金（USD、1件あたり。docs.x.com の料金表 2026/11 時点）。
# 自分のデータ（$0.001）になるかはアプリの持ち主しだいなので、安全側にふつうの料金で数える
PRICE_POST = 0.005
PRICE_USER = 0.010
X_BUDGET_USD = 40.0


# ---------------------------------------------------------------
# 記録（replies_state.json）
# ---------------------------------------------------------------
def load_state(text=None):
    try:
        if text is None:
            with open(STATE_FILE, encoding="utf-8") as f:
                text = f.read()
        state = json.loads(text)
    except Exception:
        state = {}
    state.setdefault("day", "")
    state.setdefault("slots_done", [])
    state.setdefault("engaged", [])
    state.setdefault("followers_today", 0)
    state.setdefault("others_today", 0)
    state.setdefault("x_budget", {"month": "", "usd": 0.0, "notified": False})
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def latest_slots_done(day):
    """GitHub上の最新の記録から、その日に済んだ回を読む（待っているあいだに別の回が動いていないか確かめる）"""
    try:
        subprocess.run(["git", "fetch", "-q", "origin", "main"], check=True, timeout=60)
        text = subprocess.run(["git", "show", f"origin/main:{STATE_FILE}"], check=True, capture_output=True, text=True, timeout=30).stdout
        data = load_state(text)
    except Exception as e:
        print("最新の記録を確かめられませんでした（手元の記録で続けます）:", e)
        data = load_state()
    return data["slots_done"] if data["day"] == day.isoformat() else []


def new_day(state, day):
    """日付が変わったら、その日の記録（済んだ回・絡んだ人）をリセットする"""
    if state["day"] != day.isoformat():
        state.update(day=day.isoformat(), slots_done=[], engaged=[], followers_today=0, others_today=0)
    return state


def slot_name(t):
    return f"{t:%H:%M}"


def is_off_day(day):
    try:
        import jpholiday

        holiday = jpholiday.is_holiday(day)
    except Exception:
        holiday = False
    return day.weekday() >= 5 or holiday


def plan(now, done):
    """この回でどの枠を動かすか。戻り値は (枠の時刻 or None, 待つ秒数)。
    遅れて起動して、過ぎた枠が2つ以上あるときは、いちばん新しい枠だけ動かす（古い枠は飛ばす）"""
    if is_off_day(now.date()):
        print(f"{now:%m/%d} は土日・祝日なので、リプ案は作りません")
        return None, 0
    t = now.time()
    passed = [s for s in SLOTS if s <= t and slot_name(s) not in done]
    if passed and t <= LAST_DEADLINE:
        return passed[-1], 0
    upcoming = [s for s in SLOTS if s > t and slot_name(s) not in done]
    if not upcoming:
        print("今日のリプ案の回はもう残っていません")
        return None, 0
    target = datetime.datetime.combine(now.date(), upcoming[0], tzinfo=JST)
    wait = (target - now).total_seconds()
    if wait > EARLY_LIMIT_MIN * 60:
        print(f"次の回（{slot_name(upcoming[0])}）まで{wait / 60:.0f}分あるので、この回は何もしません（あとの予約に任せます）")
        return None, 0
    return upcoming[0], wait


# ---------------------------------------------------------------
# X API（読み取りだけ）と、使った額
# ---------------------------------------------------------------
def month_of(day):
    return f"{day:%Y-%m}"


def x_spent(state, day):
    b = state["x_budget"]
    return b["usd"] if b.get("month") == month_of(day) else 0.0


def add_x_cost(state, day, usd):
    b = state["x_budget"]
    if b.get("month") != month_of(day):
        b.update(month=month_of(day), usd=0.0, notified=False)
    b["usd"] = round(b["usd"] + usd, 4)


def max_cost_per_run(refresh_voice):
    """1回でかかりうる、いちばん多い額（これを足して上限を超えるなら、その回は動かさない）"""
    cost = SEARCH_MAX * (PRICE_POST + PRICE_USER) + MENTION_MAX * (2 * PRICE_POST + PRICE_USER)
    if refresh_voice:
        cost += 50 * PRICE_POST + PRICE_USER
    return cost


def cost_of_response(body):
    """返ってきた投稿・ユーザーの数から、読み取り料金を数える"""
    data = body.get("data") or []
    if isinstance(data, dict):
        data = [data]
    inc = body.get("includes") or {}
    posts = len(data) + len(inc.get("tweets") or [])
    users = len(inc.get("users") or [])
    return posts * PRICE_POST + users * PRICE_USER


class XReader:
    def __init__(self, session=None):
        if session is None:
            from requests_oauthlib import OAuth1Session

            k = x_media.X_KEYS
            session = OAuth1Session(
                k["X_API_KEY"], client_secret=k["X_API_SECRET"],
                resource_owner_key=k["X_ACCESS_TOKEN"], resource_owner_secret=k["X_ACCESS_TOKEN_SECRET"],
            )
        self.session = session
        self.cost = 0.0

    def get(self, path, params=None):
        res = self.session.get(f"{API}{path}", params=params or {}, timeout=60)
        if res.status_code >= 300:
            raise XApiError(res.status_code, res.text[:300])
        body = res.json()
        self.cost += cost_of_response(body) if path != "/users/me" else PRICE_USER
        return body


class XApiError(RuntimeError):
    def __init__(self, status, text):
        super().__init__(f"X API {status}: {text}")
        self.status = status


USER_FIELDS = "username,name,verified,verified_type,connection_status,public_metrics"
TWEET_FIELDS = "created_at,author_id,conversation_id,public_metrics,referenced_tweets"


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def users_by_id(body):
    return {u["id"]: u for u in (body.get("includes") or {}).get("users") or []}


def is_verified(user):
    return bool(user.get("verified")) or user.get("verified_type") in VERIFIED_TYPES


def is_follower(user):
    return "followed_by" in (user.get("connection_status") or [])


def search_query(username, verified_only=True):
    words = " OR ".join(KEYWORDS)
    q = f"({words}) lang:ja -is:retweet -is:reply -is:quote"
    if username:
        q += f" -from:{username}"
    return q + (" is:verified" if verified_only else "")


def fetch_candidates(x, me, now):
    """絡む候補の投稿（青バッジの人だけ）。is:verified が使えないプランなら、使わずに検索して後で絞る"""
    params = {
        "max_results": SEARCH_MAX, "sort_order": "relevancy",
        "start_time": iso(now - datetime.timedelta(hours=SEARCH_HOURS)),
        "expansions": "author_id", "tweet.fields": TWEET_FIELDS, "user.fields": USER_FIELDS,
    }
    try:
        body = x.get("/tweets/search/recent", dict(params, query=search_query(me.get("username"))))
    except XApiError as e:
        if e.status != 400:
            raise
        print("is:verified が使えなかったので、外して検索します:", e)
        body = x.get("/tweets/search/recent", dict(params, query=search_query(me.get("username"), verified_only=False)))
    users = users_by_id(body)
    out = []
    for t in body.get("data") or []:
        u = users.get(t.get("author_id"), {})
        if u.get("id") == me.get("id") or not is_verified(u):
            continue
        if any(w in t.get("text", "") for w in SKIP_WORDS):
            continue
        out.append({"id": t["id"], "text": t.get("text", ""), "user": u, "metrics": t.get("public_metrics") or {}})
    return out


def fetch_mentions(x, me, state, now):
    params = {
        "max_results": max(MENTION_MAX, 5), "expansions": "author_id,referenced_tweets.id",
        "tweet.fields": TWEET_FIELDS, "user.fields": USER_FIELDS,
    }
    if state.get("mention_since_id"):
        params["since_id"] = state["mention_since_id"]
    else:
        params["start_time"] = iso(now - datetime.timedelta(hours=24))
    body = x.get(f"/users/{me['id']}/mentions", params)
    data = body.get("data") or []
    if data:
        state["mention_since_id"] = max((t["id"] for t in data), key=int)
    users = users_by_id(body)
    parents = {t["id"]: t.get("text", "") for t in (body.get("includes") or {}).get("tweets") or []}
    out = []
    for t in data[:MENTION_MAX]:
        if t.get("author_id") == me["id"]:
            continue
        parent_id = next((r["id"] for r in t.get("referenced_tweets") or [] if r.get("type") == "replied_to"), "")
        out.append({"id": t["id"], "text": t.get("text", ""), "user": users.get(t.get("author_id"), {}),
                    "parent": parents.get(parent_id, "")})
    return out


def fetch_voice(x, me):
    """自分の最近の投稿・リプ（口調のお手本）。リプを優先して最大20件"""
    body = x.get(f"/users/{me['id']}/tweets", {"max_results": 50, "exclude": "retweets", "tweet.fields": "referenced_tweets"})
    replies, posts = [], []
    for t in body.get("data") or []:
        is_reply = any(r.get("type") == "replied_to" for r in t.get("referenced_tweets") or [])
        (replies if is_reply else posts).append(re.sub(r"https?://\S+", "", t.get("text", "")).strip())
    return {"replies": replies[:15], "posts": posts[: max(5, 20 - len(replies[:15]))]}


# ---------------------------------------------------------------
# 絡む人の選び方
# ---------------------------------------------------------------
def score(c):
    m = c.get("metrics") or {}
    return (m.get("like_count") or 0) + 3 * (m.get("reply_count") or 0) + 2 * (m.get("retweet_count") or 0)


def pick_targets(candidates, engaged, followers_today, others_today, per_run=PER_RUN, share=FOLLOWER_SHARE):
    """同じ人は1日1回まで。フォロワー：それ以外 ＝ share：(1-share) に近づくように per_run 人を選ぶ"""
    best = {}
    for c in sorted(candidates, key=score, reverse=True):
        uid = c["user"].get("id")
        if uid and uid not in engaged and uid not in best:
            best[uid] = c
    followers = [c for c in best.values() if is_follower(c["user"])]
    others = [c for c in best.values() if not is_follower(c["user"])]
    want_f = round(share * (followers_today + others_today + per_run)) - followers_today
    want_f = min(max(want_f, 0), per_run)
    picked = followers[:want_f] + others[: per_run - want_f]
    if len(picked) < per_run:  # 片方が足りなければ、もう片方で埋める
        rest = [c for c in followers[want_f:] + others[per_run - want_f:] if c not in picked]
        picked += sorted(rest, key=score, reverse=True)[: per_run - len(picked)]
    return picked


# ---------------------------------------------------------------
# 返信案（Opus）
# ---------------------------------------------------------------
REPLY_STYLE = f"""## 返信案の書き方
- アカウント：「くじら｜株主優待・高配当」（20代、優待・高配当の個人投資家）
- 口調：友だちに話しかけるようなラフな話し言葉。「いいなぁ！！」「〜ですよねぇ」「〜気になってました！」「〜すごい！」くらいの軽さ。関西弁は使わない。くじら・海のたとえは使わない。かたい言い回し（「安心感があります」「魅力的ですね」「参考になります」「具体的になりますね」など）は使わない
- 1件につき1案。1〜2文、20〜50文字くらい（長くても{REPLY_MAX_CHARS}文字まで）。ハッシュタグ・URLは入れない。絵文字は1〜2個（✨😊😭🙌 など）。「！」「！！」も使ってよい
- 相手の投稿の中身は1点だけ拾って、感想として書く。相手の数字や内容を言い直す・まとめる・解説するのはしない（要約っぽく、AIっぽく見えるため）
- 自分の気持ちは書いてよい（「買おうか迷ってるんですよねぇ」「ずっと気になってて」「欲しくなりました」など）。ただし「迷ってる」系は、今回の返信案全体で3件に1件までにする（どれにも付くと嘘っぽくなる）
- 事実の体験談は作らない（「自分も持ってます」「優待届きました」「〇株買いました」「使いました」など、保有・売買・受け取りの話は書かない）
- 自分あてのリプには、お礼＋相手の言ったことへのひとこと＋相手が答えやすい軽い問いかけで締める（「〇〇さんは何株くらい持ってるんですか？」「どの優待がお気に入りですか？」など）。会話を続けてもらうのがねらい。質問されたら、わかる範囲で短く答えてから問いかけを返す（わからないことは「調べてみます！」でよい）
- 他の人の投稿には、共感・気持ちが基本。そのうち3件に1件くらいは、相手が返したくなる軽い問いかけで締める（「何株目標ですか？」「次に狙ってる優待あります？」など）。売り込み・自分のnoteやアカウントの宣伝はしない
- 問いかけは1つだけ、はい／いいえや一言で答えられる軽いものにする。相手を試すような質問・答えにくい質問（資産額・年収など）はしない
- 株価の断定・売買のすすめ（「買い」「絶対上がる」など）はしない
- スパム・宣伝・関係ない内容・攻撃的な投稿には、返信案を作らず skip にする

## 返信のお手本（この軽さに合わせる。文はそのまま使わない）
- 優待が届いた投稿に →「優待いいなぁ！！その株買おうか迷ってるんですよねぇ😊」
- 株数ごとの配当を並べた投稿に →「200株すごい！コツコツ集めてるの憧れます✨」
- 銘柄の豆知識の投稿に →「セノビックがロートなの初めて知りました！！」
- 利回りの話の投稿に →「優待と合わせて3%あったら最高ですよねぇ😊」
- 自分あてのリプに →「ありがとうございます！そこ自分も気になってました😭〇〇さんはもう持ってるんですか？」
- 株数を話す投稿に（問いかけで締める例）→「200株すごい！✨最終的に何株目標ですか？」
"""


def build_prompt(mentions, targets, voice):
    items = []
    for m in mentions:
        items.append({"id": m["id"], "kind": "自分あてのリプ", "from": "@" + m["user"].get("username", ""),
                      "my_post": m.get("parent", "")[:200], "text": m["text"][:400]})
    for t in targets:
        items.append({"id": t["id"], "kind": "絡む候補の投稿", "from": "@" + t["user"].get("username", ""),
                      "text": t["text"][:400]})
    samples = "\n".join(f"- {s}" for s in (voice.get("replies") or [])[:15] + (voice.get("posts") or [])[:5] if s)
    return f"""Xの返信案を作ってください。送るのは本人が確かめてからです。

{REPLY_STYLE}
## 本人の最近の投稿・リプ（口調のお手本。中身はまねしない）
{samples or "（まだ少ないので、上の口調のルールに合わせる）"}

## 返信先（JSON）
{json.dumps(items, ensure_ascii=False, indent=1)}

## 出力
JSONの配列だけを返す。説明や```は付けない。
[{{"id": "返信先のid", "reply": "返信案（skipなら空）", "skip": false}}]
"""


def parse_replies(text):
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return {}
    try:
        rows = json.loads(m.group(0))
    except Exception:
        return {}
    out = {}
    for r in rows:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        reply = (r.get("reply") or "").strip()
        out[str(r["id"])] = "" if r.get("skip") else reply
    return out


def check_reply(text):
    if "http" in text:
        return "URLが入っている"
    hit = [w for w in x_media.X_NG_WORDS if w in text]
    return f"NGワード {hit}" if hit else ""


def write_replies(client, mentions, targets, voice):
    if not mentions and not targets:
        return {}
    response = client.messages.create(
        max_tokens=4000, messages=[{"role": "user", "content": build_prompt(mentions, targets, voice)}]
    )
    replies = parse_replies(ai.text_of(response))
    for key, text in list(replies.items()):
        problem = check_reply(text)
        if problem:
            print(f"返信案を外しました（{problem}）:", text)
            replies[key] = ""
    return replies


# ---------------------------------------------------------------
# LINEのカード
# ---------------------------------------------------------------
INTENT_URL = "https://x.com/intent/tweet"
URI_LIMIT = 1000  # LINEのボタンのURLの上限


def reply_url(post_id, text=""):
    """返信画面を開くリンク。文章が長くてURLに入らないときは、返信先だけ開く（文章は「コピー」で貼る）"""
    base = f"{INTENT_URL}?in_reply_to={post_id}"
    if text:
        url = f"{base}&text={urllib.parse.quote(text)}"
        if len(url) <= URI_LIMIT:
            return url
    return base


def post_url(user, post_id):
    return f"https://x.com/{user.get('username') or 'i'}/status/{post_id}"


def _t(text, **kw):
    return {"type": "text", "text": text or " ", "wrap": True, **kw}


def bubble(item, kind, reply):
    u = item["user"]
    if kind == "mention":
        label = f"💬 リプ返し｜@{u.get('username', '')}"
        tag = "フォロワー" if is_follower(u) else ""
    else:
        m = item.get("metrics") or {}
        label = f"🤝 絡み｜@{u.get('username', '')}"
        tag = ("フォロワー" if is_follower(u) else "未フォロー") + f"・♡{m.get('like_count', 0)}"
    body = [_t(label, weight="bold", size="sm"), _t(f"{u.get('name', '')}　{tag}".strip(), size="xxs", color="#8A8A8A")]
    if item.get("parent"):
        body.append(_t(f"自分の投稿：{item['parent'][:60]}", size="xxs", color="#8A8A8A", maxLines=2))
    body += [
        _t(item["text"][:200], size="xs", color="#555555", maxLines=5, margin="md"),
        {"type": "separator", "margin": "md"},
        _t(reply or "（返信案なし。開いて自分で書く）", size="sm", weight="bold" if reply else "regular", margin="md"),
    ]
    buttons = []
    if reply:
        buttons.append({"type": "button", "style": "secondary", "height": "sm",
                        "action": {"type": "clipboard", "label": "コピー", "clipboardText": reply}})
    buttons.append({"type": "button", "style": "primary", "height": "sm", "color": "#1A2530",
                    "action": {"type": "uri", "label": "返信する", "uri": reply_url(item["id"], reply)}})
    buttons.append({"type": "button", "style": "link", "height": "sm",
                    "action": {"type": "uri", "label": "元の投稿", "uri": post_url(u, item["id"])}})
    return {
        "type": "bubble", "size": "kilo",
        "body": {"type": "box", "layout": "vertical", "spacing": "xs", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "spacing": "xs", "contents": buttons},
    }


def build_messages(slot, mentions, targets, replies, spent_usd, budget_usd):
    head = (
        f"💬 リプ案 {slot}（自分あて{len(mentions)}件・絡み{len(targets)}人）\n"
        "「返信する」で返信画面が開きます。文章を確かめてから送ってください。\n"
        f"X APIの今月の読み取り：約${spent_usd:.2f} / 上限${budget_usd:.0f}"
    )
    bubbles = [bubble(m, "mention", replies.get(m["id"], "")) for m in mentions]
    bubbles += [bubble(t, "target", replies.get(t["id"], "")) for t in targets]
    messages = [{"type": "text", "text": head}]
    for i in range(0, len(bubbles), 12):  # カルーセルは12枚まで
        messages.append({"type": "flex", "altText": f"💬 リプ案 {slot}",
                         "contents": {"type": "carousel", "contents": bubbles[i : i + 12]}})
    return messages


# ---------------------------------------------------------------
# 1回分
# ---------------------------------------------------------------
def x_budget():
    try:
        return float(os.environ.get("X_BUDGET_USD") or X_BUDGET_USD)
    except ValueError:
        return X_BUDGET_USD


def run_slot(tools, now, slot, state, x=None, client=None):
    """1回分を動かす。LINEに送ったら True"""
    day = now.date()
    budget = x_budget()
    refresh_voice = state.get("voice_day", "") < (day - datetime.timedelta(days=VOICE_REFRESH_DAYS)).isoformat()
    spent = x_spent(state, day)
    if spent + max_cost_per_run(refresh_voice) > budget:
        print(f"X APIの今月の使用額 ${spent:.2f} が上限 ${budget:.0f} に近いので、止めます")
        if not (state["x_budget"].get("month") == month_of(day) and state["x_budget"].get("notified")):
            tools.send_line_text(f"⏸ リプ案は今月の上限（X API ${budget:.0f}）に近づいたので、今月は止めます。\n使った額：約${spent:.2f}\n上限を変えるときは、Variables の X_BUDGET_USD を変えてください。")
            add_x_cost(state, day, 0)
            state["x_budget"]["notified"] = True
        return False

    x = x or XReader()
    try:
        if not state.get("me"):
            state["me"] = x.get("/users/me")["data"]
        me = state["me"]
        if refresh_voice:
            try:
                state["voice"] = fetch_voice(x, me)
                state["voice_day"] = day.isoformat()
            except XApiError as e:
                print("自分の過去の投稿を読めませんでした（口調のルールだけで作ります）:", e)
        mentions = fetch_mentions(x, me, state, now)
        candidates = fetch_candidates(x, me, now)
    finally:
        add_x_cost(state, day, x.cost)
        print(f"X APIの読み取り：今回 約${x.cost:.3f} / 今月 約${x_spent(state, day):.2f}")

    targets = pick_targets(candidates, set(state["engaged"]), state["followers_today"], state["others_today"])
    print(f"自分あてのリプ {len(mentions)}件、絡む候補 {len(candidates)}件 → {len(targets)}人")
    replies = {}
    try:
        replies = write_replies(client or ai.Client("replies", day), mentions, targets, state.get("voice") or {})
    except Exception as e:
        print("返信案を作れませんでした（投稿だけ送ります）:", e)

    if not mentions and not targets:
        print("今回は送るものがありませんでした")
        return False
    tools.push_line(build_messages(slot, mentions, targets, replies, x_spent(state, day), budget))
    for t in targets:
        state["engaged"].append(t["user"]["id"])
        state["followers_today" if is_follower(t["user"]) else "others_today"] += 1
    return True


def main(tools, now, scheduled, sleep=time.sleep):
    if not x_media.X_KEYS or not all(x_media.X_KEYS.values()):
        print("XのAPIキーが無いので、リプ案は作りません")
        return
    state = new_day(load_state(), now.date())
    if scheduled:
        slot, wait = plan(now, state["slots_done"])
        if slot is None:
            save_state(state)
            return
        if wait > 0:
            print(f"{slot_name(slot)} まで{wait / 60:.0f}分待ちます")
            sleep(wait)
            now = datetime.datetime.combine(now.date(), slot, tzinfo=JST)
        if slot_name(slot) in latest_slots_done(now.date()):
            print(f"{slot_name(slot)} の回は、別の回が送信済みでした。この回は何もしません")
            return
        name = slot_name(slot)
    else:
        name = f"{now:%H:%M}（手動）"
    try:
        run_slot(tools, now, name, state)
        if scheduled:
            # 動かした枠と、それより前の枠（遅れて飛ばした枠）を「済み」にする
            for s in SLOTS:
                if s <= slot and slot_name(s) not in state["slots_done"]:
                    state["slots_done"].append(slot_name(s))
    finally:
        save_state(state)  # 失敗しても、使ったX APIの額は残す
