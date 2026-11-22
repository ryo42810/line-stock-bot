"""共感ポスト（kyokan_posts.py）の動作確認。キーや通信は使わない"""
import datetime
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import kyokan_posts as k  # noqa: E402
import x_queue  # noqa: E402

JST = app.JST


def at(day, hour, minute):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=JST)


class FakeClient:
    """AIの代わりに、決まったJSONを返す"""

    def __init__(self, batches):
        self.batches, self.calls = list(batches), 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        data = self.batches.pop(0) if self.batches else []
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(data, ensure_ascii=False))])


POSTS = [
    {"type": "flip", "theme": "利回り", "post": "高配当株って利回りが高いほど\n正義みたいな空気あるけど\n\n利回りの高さと\n安心して持てるかって\n全く別の話だよね"},
    {"type": "aruaru", "theme": "株クラの空気", "post": "株クラ民、もう喧嘩する元気も残ってない説"},
    {"type": "join", "theme": "優待の使い方", "post": "優待が届いたら\n\n①すぐ使う派\n②取っておく派\n\nどっち？"},
    {"type": "question", "theme": "権利落ち", "post": "ちょっと気になるんですが\n\n権利落ちの日って\nみんな株価見ないようにしてるんですか？"},
    {"type": "honne", "theme": "カタログ", "post": "正直に言うと\n\nカタログが届く日のほうが\n配当の入金日よりワクワクする"},
    {"type": "flip", "theme": "長期保有", "post": "株って長く持つほど\n正義みたいな空気あるけど\n\n長く持つことと\nほったらかしにすることって\n全く別の話だよね"},
    {"type": "join", "theme": "株主総会", "post": "株主総会のお知らせが届いたとき\n\n中身まで読む人ー？"},
    {"type": "kazu", "theme": "手取り", "post": "配当金って、額面より手取りで見たほうがいい\n\n年10万円の配当でも\n税金で約2割引かれて\n手元に残るのは約8万円"},
    {"type": "aruaru", "theme": "権利日", "post": "権利付き最終日の夜だけ\n\nやたら早く寝る株クラ民"},
    {"type": "honne", "theme": "含み損", "post": "ぶっちゃけ\n\n含み損の銘柄ほど\n優待が届くとうれしい"},
]


def slots(day):
    """その日の共感ポストの時刻（日ごとに数分ずれる）"""
    return [x_queue.slot_time(datetime.date(2026, 11, day), base) for base, _ in k.SLOTS]


class KyokanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (k.STOCK_FILE, x_queue.QUEUE_FILE)
        k.STOCK_FILE = os.path.join(self.tmp.name, "kyokan_stock.json")
        x_queue.QUEUE_FILE = os.path.join(self.tmp.name, "x_queue.json")
        self.lines = []

    def tearDown(self):
        k.STOCK_FILE, x_queue.QUEUE_FILE = self.old
        self.tmp.cleanup()

    def test_rejects_numbers_hashtags_and_duplicates(self):
        today = datetime.date(2026, 11, 22)
        items = []
        bad = [
            {"type": "flip", "theme": "a", "post": "NTTは100株で買える"},
            {"type": "honne", "theme": "b", "post": "正直に言うと #株クラ"},
            {"type": "nope", "theme": "c", "post": "型がない投稿"},
            {"type": "honne", "theme": "d", "post": "正直に言うと\n\n利回り4%あれば十分だと思ってる"},  # 数字は kazu の型だけ
            {"type": "join", "theme": "e", "post": "優待が届いたら何に使う？\n\nリプで教えて"},  # 反応のお願いは書かない
        ]
        self.assertEqual(k.add(items, bad, "bot", today), [])
        k.add(items, POSTS[:1], "bot", today)
        # 言い回しを少し変えただけのものは、同じ内容としてはじく
        again = dict(POSTS[0], post=POSTS[0]["post"].replace("安心して持てるか", "安心して持ち続けられるか"))
        self.assertEqual(k.add(items, [again], "bot", today), [])
        self.assertEqual(len(items), 1)

    def test_numbers_are_allowed_only_in_kazu_type(self):
        today = datetime.date(2026, 11, 22)
        items = []
        added = k.add(items, [p for p in POSTS if p["type"] == "kazu"], "bot", today)
        self.assertEqual(len(added), 1)
        # 例えの数字でも、銘柄コードが入っていたらはじく
        self.assertEqual(k.add(items, [{"type": "kazu", "theme": "x", "post": "ある会社(9432)は100株で2万円"}], "bot", today), [])

    def test_refill_and_book_three_slots_without_same_type_in_a_row(self):
        client = FakeClient([POSTS])
        k.prepare(at(22, 6, 0), lambda: client, self.lines.append)
        stock = k.load()
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(stock), len(POSTS))
        self.assertIn(f"共感ポストのストックを{len(POSTS)}本作り足しました", self.lines[0])
        queue = x_queue.load()
        self.assertEqual([q["time"] for q in queue], slots(22))
        # どの投稿も最後に #株 #株クラ が付く
        self.assertTrue(all(q["post"].endswith("\n\n#株 #株クラ") for q in queue))
        types = {k.with_tags(i["post"]): i["type"] for i in stock}
        self.assertNotEqual(types[queue[0]["post"]], types[queue[1]["post"]])
        self.assertNotEqual(types[queue[1]["post"]], types[queue[2]["post"]])
        # 同じ日にもう一度起動しても、二重には並べない（ストックは十分なので作り足さない）
        k.prepare(at(22, 7, 0), lambda: client, self.lines.append)
        self.assertEqual(len(x_queue.load()), 3)
        self.assertEqual(client.calls, 1)

    def test_skipped_post_goes_back_to_stock(self):
        client = FakeClient([POSTS])
        k.prepare(at(22, 6, 0), lambda: client, self.lines.append)
        queue = x_queue.load()
        queue[0]["status"], queue[1]["status"] = "posted", "skipped"
        x_queue.save(queue, datetime.date(2026, 11, 22))
        k.prepare(at(23, 6, 0), lambda: client, self.lines.append)
        stock = {k.with_tags(i["post"]): i for i in k.load()}
        self.assertEqual(stock[queue[0]["post"]]["status"], "posted")
        self.assertNotEqual(stock[queue[1]["post"]]["status"], "skipped")
        self.assertIn(stock[queue[1]["post"]]["status"], ("pending", "queued"))

    def test_late_start_skips_slots_already_past(self):
        client = FakeClient([POSTS])
        k.prepare(at(22, 10, 0), lambda: client, self.lines.append)
        self.assertEqual([q["time"] for q in x_queue.load()], slots(22)[1:])

    def test_chat_posts_go_first(self):
        today = datetime.date(2026, 11, 22)
        items = []
        k.add(items, POSTS[:3], "bot", today)
        k.add(items, POSTS[3:4], "chat", today)
        self.assertEqual(k.pick(items, "")["source"], "chat")

    def test_queue_runs_prepare_and_posts_at_its_time(self):
        client = FakeClient([POSTS])
        now = [at(22, 7, 0)]
        posted = []

        def sleep(sec):
            now[0] += datetime.timedelta(seconds=sec)

        def post(posts):
            posted.append((f"{now[0]:%H:%M}", posts[0][0]))
            return ""

        prepare = lambda n: k.prepare(n, lambda: client, self.lines.append)  # noqa: E731
        x_queue.run(lambda: now[0], post, self.lines.append, sleep=sleep, weekday_only=False, prepare=prepare)
        self.assertEqual(posted[0][0], slots(22)[0])


if __name__ == "__main__":
    unittest.main()
