"""Xの予約投稿：朝・夕方の回が作った投稿を、決まった時刻に出す（x_queue.yml・`python app.py xqueue`）

朝・夕方の回は、文章と画像を作ってLINEに送り、Xに出す時刻を付けて x_queue.json に並べる。
この回はそれを時刻どおりにXへ出すだけ（AIは使わない）。
- 昼 11:50ごろ：くじら枠（火・木：今日の1銘柄／月：整理／水：解説）
- 夕方 18:05ごろ・19:10ごろ（週の最終営業日は 20:05ごろも）：夕方の2本目以降
- 毎日 7:21ごろ・12:34ごろ・18:39ごろ：共感ポスト（kyokan_posts.py。ストックからこの回が並べる）
- 毎日 8:26ごろ・21:13ごろ：「〇万円あったら」、平日 16時すぎ：大型株の値動き（quick_posts.py。AIは使わない）

時刻はきりのいい分（:00・:05・:10…）だと予約っぽく見えるので、半端な分にしたうえで、日ごとに数分ずらす（slot_time）。

GitHub Actionsの定時実行は数時間遅れることが多いので、この回は1時間おきに予約してあり、
起動できた回が次の時刻まで待つ（待てるのは5時間まで）。出すものが無ければ何もしないで終わる。
"""
import datetime
import hashlib
import json
import os
import subprocess
import time

QUEUE_FILE = "x_queue.json"
KEEP_DAYS = 7
MIN_GAP_MIN = 30  # 遅れて2本が重なったときも、これだけ間隔をあける
MAX_RUN_MIN = 320  # 1回の起動で動ける長さ（GitHub Actionsは1回6時間まで）
# まだ投稿が並んでいなくても、この時刻に向けて待つ（朝・夕方の回が、待っているあいだに並べる）
ANCHORS = {"midday": datetime.time(11, 47), "close": datetime.time(15, 58), "evening": datetime.time(18, 4)}
JITTER_MIN = 8  # 日ごとにずらす幅（0〜この分だけ後ろへ）
MIDDAY_BASE, MIDDAY_UNTIL = "11:47", "13:30"
EVENING_BASES, EVENING_UNTIL = ("18:04", "19:08", "20:03"), "22:00"


def slot_time(day, base):
    """その日に出す時刻（"HH:MM"）。base から0〜JITTER_MIN分だけ後ろへずらす（日付と枠で決まるので、同じ日なら何度呼んでも同じ）。
    毎日同じ分・きりのいい分に出ると予約っぽく見えるので、5分きざみの分（:00・:05・:10…）は必ず避ける"""
    h, m = map(int, base.split(":"))
    shift = int(hashlib.sha256(f"{day.isoformat()}|{base}".encode()).hexdigest(), 16) % (JITTER_MIN + 1)
    total = h * 60 + m + shift
    if total % 5 == 0:
        total += 1
    return f"{total // 60:02d}:{total % 60:02d}"


def midday_time(day):
    return slot_time(day, MIDDAY_BASE)


def evening_times(day):
    return tuple(slot_time(day, b) for b in EVENING_BASES)


def load():
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        items = []
    return items if isinstance(items, list) else []


def save(items, today):
    cutoff = (today - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump([i for i in items if i.get("date", "") >= cutoff], f, ensure_ascii=False, indent=1)


def _git(*args, timeout=120):
    subprocess.run(["git", *args], check=True, timeout=timeout)


def sync():
    """GitHub上の最新の main に合わせる（待っているあいだに朝・夕方の回が並べた投稿と画像を取り込む）"""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        _git("pull", "-q", "--rebase", "--autostash", "origin", os.environ.get("GITHUB_REF_NAME", "main"))
    except Exception as e:
        print("最新の記録を取り込めませんでした（手元の記録で続けます）:", e)


def push(message, files=(QUEUE_FILE,)):
    """x_queue.json（と files に渡したもの）をすぐGitHubに保存する（ほかの回と記録がずれないように）"""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    try:
        _git("config", "user.name", "github-actions[bot]")
        _git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        files = [f for f in dict.fromkeys((QUEUE_FILE, *files)) if os.path.exists(f)]
        _git("add", *files)
        if subprocess.run(["git", "diff", "--cached", "--quiet", "--", *files]).returncode == 0:
            return
        _git("commit", "-q", "-m", message, "--", *files)
        for i in (1, 2, 3):
            try:
                _git("pull", "-q", "--rebase", "--autostash", "origin", branch)
                _git("push", "-q", "origin", f"HEAD:{branch}")
                return
            except Exception:
                time.sleep(i * 5)
        print("予約投稿の記録を保存できませんでした（最後の保存でやり直します）")
    except Exception as e:
        print("予約投稿の記録の保存に失敗:", e)


def enqueue(today, entries, until):
    """entries: [(時刻 "HH:MM", 文章, 画像パス or None), ...] をその日の予約に並べる。同じ日・同じ時刻の予約は入れ替える"""
    entries = [e for e in entries if e[1]]
    if not entries:
        return
    sync()
    day = today.isoformat()
    times = {t for t, _, _ in entries}
    items = [i for i in load() if not (i.get("date") == day and i.get("time") in times and i.get("status") == "pending")]
    for t, post, image in entries:
        items.append({"date": day, "time": t, "until": until, "post": post, "image": image or "", "status": "pending"})
        print(f"Xの予約投稿に追加: {t}")
    save(items, today)
    push(f"Xの予約投稿を追加（{today:%m/%d}）")


def _at(day, hhmm, tz):
    h, m = map(int, hhmm.split(":"))
    return datetime.datetime.combine(day, datetime.time(h, m), tzinfo=tz)


def next_item(items, now):
    """今日の、まだ出していない予約のうち一番早いもの"""
    day = now.date().isoformat()
    pending = [i for i in items if i.get("date") == day and i.get("status") == "pending"]
    return min(pending, key=lambda i: i["time"]) if pending else None


def run(now_fn, post_fn, notify, enabled=True, early_limit_min=300, sleep=time.sleep, weekday_only=True, prepare=None, refresh=None):
    """予約を時刻どおりに出す。now_fn: いまの時刻（JST）/ post_fn: [(文章, 画像)] を投稿して結果の文言を返す / notify: LINEに知らせる
    prepare: 最初に1回呼ぶ（共感ポストの今日の分を予約に並べる。kyokan_posts.prepare）
    refresh: 待ちから戻るたびに呼ぶ（その時刻にならないと作れない投稿を並べる。quick_posts.prepare）"""
    if not enabled:
        print("Xの自動投稿はオフなので、予約投稿は出しません")
        return
    if prepare:
        try:
            prepare(now_fn())
        except Exception as e:  # 共感ポストで失敗しても、ほかの予約は出す
            print("共感ポストの準備に失敗:", type(e).__name__, str(e)[:200])
    started = now_fn()
    tz = started.tzinfo
    last_posted = None
    waited_anchors = set()
    while True:
        sync()
        if refresh:
            try:
                refresh(now_fn())
            except Exception as e:  # ここで失敗しても、ほかの予約は出す
                print("追加の投稿の準備に失敗:", type(e).__name__, str(e)[:200])
        now = now_fn()
        items = load()
        item = next_item(items, now)
        if item:
            due = _at(now.date(), item["time"], tz)
            if last_posted:
                due = max(due, last_posted + datetime.timedelta(minutes=MIN_GAP_MIN))
            key = None
        else:
            # まだ並んでいない。次の決まった時刻が近ければ、そこまで待ってから見直す
            if weekday_only and now.weekday() >= 5:
                print("今日の予約はありません")
                return
            future = sorted((t, k) for k, t in ANCHORS.items() if k not in waited_anchors and _at(now.date(), t.strftime("%H:%M"), tz) > now)
            if not future:
                print("今日の予約はもうありません")
                return
            due, key = _at(now.date(), future[0][0].strftime("%H:%M"), tz), future[0][1]
        wait_sec = (due - now).total_seconds()
        if wait_sec > early_limit_min * 60 or (due - started).total_seconds() > MAX_RUN_MIN * 60:
            print(f"次の予約（{due:%H:%M}）まで{wait_sec / 60:.0f}分あるので、この回はここで終わります（あとの回に任せます）")
            return
        if wait_sec > 0:
            print(f"{due:%H:%M}まで{wait_sec / 60:.0f}分待ちます")
            sleep(wait_sec)
        if key:
            waited_anchors.add(key)
            continue
        if wait_sec > 0:
            continue  # 待ったあとは、最新の記録を取り直してから出す（次の周では待ち時間0になる）
        now = now_fn()
        image = item.get("image") or None
        if image and not os.path.exists(image):
            print("画像が見つからないので、文章だけで投稿します:", image)
            image = None
        if now > _at(now.date(), item.get("until") or "23:59", tz):
            item["status"] = "skipped"
            print(f"{item['time']}の予約は、{now:%H:%M}まで遅れたので見送ります")
            notify(f"⏭ {item['time']}に出す予定だったXの投稿は、起動が{now:%H:%M}まで遅れたため見送りました。\n使うときは、LINEに届いている下書きをコピーして投稿してください。")
        else:
            result = post_fn([(item["post"], image)])
            item["status"] = "posted"
            last_posted = now_fn()
            if "⚠️" in result or "⏭" in result:
                notify(result)
        save(items, now.date())
        push(f"Xの予約投稿を更新（{now:%m/%d %H:%M}）")
