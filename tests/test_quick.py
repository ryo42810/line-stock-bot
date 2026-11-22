"""AIを使わない投稿（quick_posts.py：「〇万円あったら」・大型株の値動き）の動作確認。キーや通信は使わない"""
import datetime
import os
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd  # noqa: E402

import app  # noqa: E402
import quick_posts as q  # noqa: E402
import x_queue  # noqa: E402

JST = app.JST
TODAY = datetime.date(2026, 11, 24)  # 火曜


def at(hour, minute, day=24):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=JST)


class FakeTools:
    """株価と配当を、決まった数字で返す。prices: {コード: (前日の終値, 今日の終値)} / divs: {コード: (予想, 実績)}"""

    def __init__(self, prices=None, divs=None, held=(), last_day=TODAY):
        self.prices, self.divs, self.held, self.last_day = prices or {}, divs or {}, set(held), last_day
        self.yf = SimpleNamespace(Ticker=lambda symbol: SimpleNamespace(info=self._info(symbol[:-2])))

    def _info(self, code):
        rate, trail = self.divs.get(code, (4.0, 4.0))
        return {"dividendRate": rate, "trailingAnnualDividendRate": trail}

    def get_history(self, code):
        prev, last = self.prices.get(code, (100.0, 100.0))
        index = pd.DatetimeIndex([self.last_day - datetime.timedelta(days=1), self.last_day], tz=JST)
        return pd.DataFrame({"Close": [prev, last]}, index=index)

    def is_market_holiday(self, day):
        return day.weekday() >= 5

    def load_held(self):
        return self.held


class QuickPostsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (q.STATE_FILE, x_queue.QUEUE_FILE)
        q.STATE_FILE = os.path.join(self.tmp.name, "quick_state.json")
        x_queue.QUEUE_FILE = os.path.join(self.tmp.name, "x_queue.json")

    def tearDown(self):
        q.STATE_FILE, x_queue.QUEUE_FILE = self.old
        self.tmp.cleanup()

    def test_numbers_are_calculated_from_price_and_dividend(self):
        f = {"code": "9432", "name": "NTT", "price": 167.7, "div": 5.4, "yield": 5.4 / 167.7 * 100, "change": 0.0}
        post = q.single_post(f, datetime.date(2026, 11, 22), 0)
        budget = int(re.search(r"(\d+)万円", post).group(1)) * 10_000
        shares = int(budget // 16_770) * 100
        self.assertIn("NTT", post.splitlines()[0])  # 1行目に会社名と数字
        self.assertIn(f"{shares:,}株", post.splitlines()[0])
        self.assertIn(q.yen(shares * 5.4), post)
        self.assertIn("年間配当を1株5.4円として", post)  # 計算の前提を書く
        self.assertIn("約3.2%", post)
        self.assertNotIn("#", post)  # 新しい型はハッシュタグなし
        for ng in ("教えて", "買うべき", "おすすめ", "買いました", "保有して"):
            self.assertNotIn(ng, post)

    def test_everyday_label_uses_the_same_table_as_kujira(self):
        self.assertEqual(q.everyday_label(12_000), "2人でちょっといいディナー1回分")
        self.assertEqual(q.everyday_label(40_000), "温泉宿の1泊分")
        self.assertEqual(q.everyday_label(500), "")

    def test_bad_dividend_data_is_not_used(self):
        # 予想と実績が大きく違う・利回りが高すぎる会社は、配当の数字を使わない
        tools = FakeTools(prices={"9432": (100, 100), "9434": (100, 100)}, divs={"9432": (10.0, 3.0), "9434": (20.0, 20.0)})
        self.assertIsNone(q.facts(tools, "9432", "NTT", TODAY)["div"])
        self.assertIsNone(q.facts(tools, "9434", "ソフトバンク", TODAY)["div"])
        self.assertEqual(q.facts(tools, "8306", "三菱UFJ", TODAY)["div"], 4.0)
        # 株価が古い（1週間より前）ときは、その会社ごと使わない
        self.assertIsNone(q.facts(FakeTools(last_day=TODAY - datetime.timedelta(days=10)), "8306", "三菱UFJ", TODAY))

    def test_two_posts_a_day_use_different_companies(self):
        tools = FakeTools()
        first, second = q.build_yosan(tools, TODAY, 0), q.build_yosan(tools, TODAY, 1)
        self.assertTrue(first and second)
        names = [name for _, name in q.WIDE]
        used = lambda post: {n for n in names if n in post}  # noqa: E731
        self.assertFalse(used(first) & used(second))
        # 3日続けて見ても、同じ文面にならない
        posts = {q.build_yosan(tools, TODAY + datetime.timedelta(days=i), s) for i in range(3) for s in (0, 1)}
        self.assertEqual(len(posts), 6)

    def test_held_stock_gets_a_note(self):
        tools = FakeTools()
        code = q.dividend_stocks(tools, TODAY, 1)[0]["code"]
        self.assertTrue(q.build_yosan(tools, TODAY, 0, held={code}).endswith(q.HELD_NOTE))

    def test_ugoki_picks_the_biggest_mover_and_skips_quiet_days(self):
        tools = FakeTools(prices={"2914": (5857, 5265), "9432": (166, 167.7)}, divs={"2914": (204.0, 230.0)})
        post, ready = q.build_ugoki(tools, TODAY)
        self.assertTrue(ready)
        self.assertTrue(post.startswith("JTが今日-10.1%、終値5,265円📉"))
        self.assertIn("100株だと約52.6万円", post)
        self.assertIn("利回りは約3.9%", post)
        # ±2%以上の会社が無い日は出さない
        self.assertEqual(q.build_ugoki(FakeTools(prices={"9432": (166, 167.7)}), TODAY), ("", True))
        # 今日の終値がまだ入っていないときは、あとでやり直す
        self.assertEqual(q.build_ugoki(FakeTools(last_day=TODAY - datetime.timedelta(days=1)), TODAY), ("", False))

    def test_prepare_books_each_post_once_at_shifted_times(self):
        tools = FakeTools(prices={"2914": (5857, 5265)})
        q.prepare(tools, at(6, 30))
        queue = x_queue.load()
        self.assertEqual([i["time"] for i in queue], [x_queue.slot_time(TODAY, base) for base, _ in q.YOSAN_SLOTS])
        q.prepare(tools, at(9, 0))  # もう一度呼んでも二重には並べない。値動きは引け後まで作らない
        self.assertEqual(len(x_queue.load()), 2)
        q.prepare(tools, at(16, 25))  # 引け後の回で、値動きを1本並べる
        queue = x_queue.load()
        self.assertEqual(len(queue), 3)
        self.assertTrue(queue[-1]["post"].startswith("JTが今日"))
        q.prepare(tools, at(17, 25))
        self.assertEqual(len(x_queue.load()), 3)

    def test_prepare_skips_ugoki_on_weekends_and_late_slots(self):
        tools = FakeTools(prices={"2914": (5857, 5265)}, last_day=datetime.date(2026, 11, 20))
        q.prepare(tools, at(16, 25, day=22))  # 日曜：朝の分は時刻を過ぎたので見送り、夜の分だけ。値動きは出さない
        queue = x_queue.load()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["time"], x_queue.slot_time(datetime.date(2026, 11, 22), q.YOSAN_SLOTS[1][0]))


if __name__ == "__main__":
    unittest.main()
