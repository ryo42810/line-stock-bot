"""朝の市況（market_board.py：おはよう投稿）の動作確認。キーや通信は使わない（サイトの撮影は偽物に差し替える）"""
import datetime
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import market_board as mb  # noqa: E402
import x_queue  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
TODAY = datetime.date(2026, 11, 24)

# サイトのタイルの文字（2026/11/24に実際に読んだもの）
TEXTS = {
    "111": "日本225\n11/24\n▲3.91%\n58,644.63\n+2,207.98\n09\n10",
    "112": "TOPIX\n11/24\n▲2.47%\n3,764.88\n+90.66",
    "121": "グロース250\n11/24\n▲2.60%\n654.84\n+16.62",
    "751": "DEX日本225 フルタイム\n19:08\n58,036.00\n指数比\n-608.63\n▼1.04%\n21",
    "511": "ドル円\n19:08\n▲0.33%\n159.517\n+0.518",
    "151": "日本国債10年利回り\n19:07\n▼0.44%\n2.707\n-0.012",
    "211": "NYダウ\n11/23\n▲3.01%\n49,595.31\n+1,447.98",
    "213": "S&P500\n11/23\n▲4.73%\n7,237.17\n+326.78",
    "212": "NASDAQ\n11/23\n▲6.51%\n24,089.56\n+1,471.82",
    "611": "SOX PHL半導体\n11/23\n▲14.03%\n9,588.05\n+1,179.47",
    "621": "VIX恐怖指数\n18:53\n▲3.54%\n27.56\n+0.94",
    "811": "米国債10年利回り\n19:06\n▼0.16%\n4.900\n-0.008",
}


def at(hour, minute, day=24):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=JST)


def fake_capture(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()
    return TEXTS


class FakeClient:
    """AIの代わり。決まった文章を返す"""

    def __init__(self, post):
        self.post = post
        self.messages = self

    def create(self, **kwargs):
        block = SimpleNamespace(type="text", text=json.dumps({"post": self.post}, ensure_ascii=False))
        return SimpleNamespace(content=[block], stop_reason="end_turn")


class MarketBoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_queue, self.old_cwd = x_queue.QUEUE_FILE, os.getcwd()
        x_queue.QUEUE_FILE = os.path.join(self.tmp.name, "x_queue.json")
        os.chdir(self.tmp.name)  # 画像を images/ に書くので、作業場所を一時フォルダにする

    def tearDown(self):
        os.chdir(self.old_cwd)
        x_queue.QUEUE_FILE = self.old_queue
        self.tmp.cleanup()

    def test_parse_tile_reads_date_value_and_change(self):
        self.assertEqual(mb.parse_tile(TEXTS["111"]), {"when": "11/24", "value": "58,644.63", "pct": 3.91})
        self.assertEqual(mb.parse_tile(TEXTS["751"]), {"when": "19:08", "value": "58,036.00", "pct": -1.04})
        self.assertEqual(mb.parse_tile(TEXTS["811"])["pct"], -0.16)
        self.assertIsNone(mb.parse_tile("NYダウ\n11/23"))
        self.assertEqual(len(mb.read_values(TEXTS)), 12)

    def test_prepare_books_one_empty_slot_before_the_morning_news(self):
        mb.prepare(at(1, 20))
        mb.prepare(at(3, 20))  # もう一度呼んでも二重には並べない
        queue = x_queue.load()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["kind"], mb.KIND)
        self.assertEqual(queue[0]["post"], "")
        self.assertLess(queue[0]["time"], "06:46")  # 朝の株ニュース（6:46ごろ〜）より前
        self.assertNotEqual(int(queue[0]["time"][-1]) % 5, 0)  # きりのいい分は避ける
        mb.prepare(at(8, 0, day=25))  # 見送る時刻を過ぎてからは並べない
        self.assertEqual(len(x_queue.load()), 1)

    def test_build_uses_ai_post_when_numbers_match(self):
        ai_post = "おはようございます🐳\nSOX指数が+14.03%の大幅高。\n派手な日ほど、高配当株は落ち着いて見ていきたいですね。"
        post, path = mb.build(at(6, 14), lambda day: FakeClient(ai_post), fake_capture)
        self.assertEqual(post, f"{ai_post}\n#朝活")
        self.assertTrue(path.endswith("2026-11-24-board.png"))

    def test_build_falls_back_when_ai_adds_unknown_numbers(self):
        ai_post = "おはようございます🐳\n日経平均は60,000円を目指す展開ですね。"
        post, _ = mb.build(at(6, 14), lambda day: FakeClient(ai_post), fake_capture)
        self.assertTrue(post.startswith("おはようございます🐳\n\n日経平均 +3.91%"))
        self.assertIn("NYダウ +3.01%／NASDAQ +6.51%", post)
        self.assertIn("ドル円 159.52円", post)
        self.assertIn(post.splitlines()[-2], mb.CLOSINGS["us_up"])  # 米国株が大きく上がった朝の一言
        self.assertTrue(post.endswith("\n#朝活"))

    def test_build_falls_back_when_ai_does_not_end_with_desune(self):
        ai_post = "おはようございます🐳\nSOX指数が+14.03%の大幅高。\n今日も落ち着いていきたい。"
        post, _ = mb.build(at(6, 14), lambda day: FakeClient(ai_post), fake_capture)
        self.assertTrue(post.startswith("おはようございます🐳\n\n日経平均 +3.91%"))

    def test_every_closing_ends_with_desune(self):
        for key, lines in mb.CLOSINGS.items():
            for line in lines:
                self.assertTrue(line.endswith("ですね。"), (key, line))

    def test_closing_comment_follows_the_market_and_weekday(self):
        rows = mb.read_values(TEXTS)
        calm = [(label, dict(v, pct=0.1)) for label, v in rows]
        down = [(label, dict(v, pct=-2.5 if label == "NASDAQ" else 0.1)) for label, v in rows]
        tue, sat, mon = datetime.date(2026, 11, 24), datetime.date(2026, 11, 28), datetime.date(2026, 11, 30)
        self.assertIn(mb.closing_comment(rows, tue), mb.CLOSINGS["us_up"])
        self.assertIn(mb.closing_comment(down, tue), mb.CLOSINGS["us_down"])
        self.assertIn(mb.closing_comment(calm, tue), mb.CLOSINGS["calm"])
        self.assertIn(mb.closing_comment(calm, mon), mb.CLOSINGS["mon"])
        self.assertIn(mb.closing_comment(rows, sat), mb.CLOSINGS["sat"])
        # 同じ場面でも、日によって言い回しが替わる
        self.assertNotEqual(mb.closing_comment(calm, tue), mb.closing_comment(calm, tue + datetime.timedelta(days=1)))

    def test_queue_builds_the_slot_at_post_time_and_posts_it_first(self):
        mb.prepare(at(5, 0))
        x_queue.enqueue(TODAY, [("07:21", "共感ポスト", None)], "09:30")
        clock = {"now": at(5, 0)}
        posted, lines = [], []

        def sleep(sec):
            clock["now"] += datetime.timedelta(seconds=sec)

        def post(posts):
            posted.append((clock["now"].strftime("%H:%M"), posts[0][0], posts[0][1]))
            return "Xへの自動投稿\n✅ 投稿"

        builders = {mb.KIND: lambda now: mb.build(now, None, fake_capture)}
        x_queue.run(lambda: clock["now"], post, lines.append, sleep=sleep, builders=builders)
        self.assertEqual([p[1].splitlines()[0] for p in posted], ["おはようございます🐳", "共感ポスト"])
        self.assertEqual(posted[0][0], mb.slot(TODAY))
        self.assertTrue(posted[0][2].endswith("board.png"))
        self.assertEqual(lines, [])

    def test_queue_skips_and_notifies_when_capture_fails(self):
        mb.prepare(at(5, 0))
        clock = {"now": at(6, 0)}
        lines = []

        def broken(path):
            raise RuntimeError("サイトを開けませんでした")

        def sleep(sec):
            clock["now"] += datetime.timedelta(seconds=sec)

        builders = {mb.KIND: lambda now: mb.build(now, None, broken)}
        x_queue.run(lambda: clock["now"], lambda posts: self.fail("投稿しない"), lines.append, sleep=sleep, builders=builders)
        self.assertEqual(x_queue.load()[0]["status"], "skipped")
        self.assertTrue(lines and "朝の市況" in lines[0])


if __name__ == "__main__":
    unittest.main()
