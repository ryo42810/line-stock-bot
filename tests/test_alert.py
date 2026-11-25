"""値下がりアラート（price_alert.py）の動作確認。キーや通信は使わない"""
import datetime
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import openpyxl  # noqa: E402
import pandas as pd  # noqa: E402

import app  # noqa: E402
import price_alert as pa  # noqa: E402

JST = app.JST
TODAY = datetime.date(2026, 11, 25)  # 水曜


def at(hour, minute, day=25):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=JST)


def bars(prev, last, high, last_day=TODAY):
    """日足2本（前日・当日）。high は52週高値として前日の高値に入れる"""
    index = pd.DatetimeIndex([last_day - datetime.timedelta(days=1), last_day], tz=JST)
    return pd.DataFrame({"Close": [prev, last], "High": [high, last]}, index=index)


STOCKS = {
    "5288": {"name": "アジアパイルHD", "groups": [pa.DIEGO], "div": 70.0, "verdict": "◎ 合格"},
    "3922": {"name": "PRTIMES", "groups": [pa.GROWTH], "div": None, "verdict": ""},
}


class FakeTools:
    """株価を決まった数字で返す。prices: {コード or "^N225": (前日の終値, 今日の終値, 52週高値)}"""

    def __init__(self, prices, last_day=TODAY, tdnet=()):
        self.prices, self.last_day, self.tdnet, self.sent = prices, last_day, list(tdnet), []
        self.yf = SimpleNamespace(Ticker=lambda symbol: SimpleNamespace(history=lambda **kw: self._bars(symbol)))

    def _bars(self, symbol):
        key = symbol[:-2] if symbol.endswith(".T") else symbol
        if key not in self.prices:
            return pd.DataFrame({"Close": [], "High": []}, index=pd.DatetimeIndex([], tz=JST))
        return bars(*self.prices[key], last_day=self.last_day)

    def is_market_holiday(self, day):
        return day.weekday() >= 5

    def fetch_tdnet(self, days):
        return self.tdnet

    def push_line(self, messages):
        self.sent += [m["text"] for m in messages]

    def send_line_text(self, text):
        self.sent.append(text)


def fresh_state(**extra):
    state = {"done": {}, "dd": {}, "yield": {}, "drop": {}, "seeded": "2026-11-24"}
    state.update(extra)
    return state


class StageTest(unittest.TestCase):
    def test_each_stage_is_reported_once(self):
        recorded, hit = pa.step(0, 10.5, pa.DD_LEVELS, pa.DD_RESET)
        self.assertEqual((recorded, hit), (10, 10))
        self.assertEqual(pa.step(recorded, 12.0, pa.DD_LEVELS, pa.DD_RESET), (10, None))  # 同じ段階は知らせない
        self.assertEqual(pa.step(recorded, 15.2, pa.DD_LEVELS, pa.DD_RESET), (15, 15))  # 次の段階は知らせる

    def test_jumping_two_stages_reports_the_deepest_only(self):
        self.assertEqual(pa.step(0, 21.0, pa.DD_LEVELS, pa.DD_RESET), (20, 20))

    def test_stage_is_reported_again_after_recovering(self):
        self.assertEqual(pa.step(20, 18.0, pa.DD_LEVELS, pa.DD_RESET), (20, None))  # 少し戻っただけでは外さない
        self.assertEqual(pa.step(20, 16.0, pa.DD_LEVELS, pa.DD_RESET), (15, None))  # 3ポイント以上戻ったら -20% の記録を外す
        self.assertEqual(pa.step(15, 20.1, pa.DD_LEVELS, pa.DD_RESET), (20, 20))  # もう一度 -20% まで下がったら知らせる
        self.assertEqual(pa.step(10, 5.0, pa.DD_LEVELS, pa.DD_RESET), (0, None))


class EvaluateTest(unittest.TestCase):
    def quotes(self, prices):
        return {code: pa.quote(bars(*p), TODAY) for code, p in prices.items()}

    def test_big_drop_is_reported_with_market_note_and_disclosure(self):
        quotes = self.quotes({"5288": (1500, 1350, 1600), "3922": (2300, 2290, 2400)})
        nikkei = pa.quote(bars(50000, 49800, 52000), TODAY)
        state = fresh_state(dd={"5288": 15, "3922": 0}, **{"yield": {"5288": 4.0}})
        text = "\n".join(pa.evaluate(STOCKS, quotes, state, TODAY, nikkei, {"5288": ["業績予想の修正に関するお知らせ"]}))
        self.assertIn("【急落：前日比 −8%以上】", text)
        self.assertIn("アジアパイルHD(5288)［Diego］ 1,350円 −10.0%", text)
        self.assertIn("この銘柄だけの下げ（日経平均 −0.4%）", text)
        self.assertIn("「業績予想の修正に関するお知らせ」", text)
        self.assertNotIn("PRTIMES", text)
        # 同じ日の次の回では、同じ銘柄の急落を知らせない
        self.assertEqual(pa.evaluate(STOCKS, quotes, state, TODAY, nikkei), [])

    def test_drop_under_threshold_is_not_reported(self):
        quotes = self.quotes({"5288": (1500, 1385, 1500)})  # -7.7%
        state = fresh_state(dd={"5288": 0}, **{"yield": {"5288": 4.0}})
        self.assertEqual(pa.evaluate(STOCKS, quotes, state, TODAY, None), [])

    def test_drawdown_stage_and_yield_level(self):
        # 52週高値 2000 → 1690（-15.5%）。1株配当70円 → 利回り4.14%
        quotes = self.quotes({"5288": (1700, 1690, 2000)})
        state = fresh_state(dd={"5288": 10}, **{"yield": {"5288": 3.5}})
        text = "\n".join(pa.evaluate(STOCKS, quotes, state, TODAY, None))
        self.assertIn("・−15%に到達：アジアパイルHD(5288)［Diego］ 1,690円（高値 2,000円 から −15.5%）", text)
        self.assertIn("・4%超え：アジアパイルHD(5288)［Diego］ 利回り 4.14%（株価 1,690円・1株配当 70円）", text)
        self.assertEqual((state["dd"]["5288"], state["yield"]["5288"]), (15, 4.0))
        self.assertEqual(pa.evaluate(STOCKS, quotes, state, TODAY, None), [])  # 2回目は知らせない

    def test_growth_stock_has_no_yield_alert(self):
        stocks = {"3922": {"name": "PRTIMES", "groups": [pa.GROWTH], "div": 100.0, "verdict": ""}}
        quotes = self.quotes({"3922": (2000, 1990, 2050)})
        state = fresh_state(dd={"3922": 0})
        self.assertEqual(pa.evaluate(stocks, quotes, state, TODAY, None), [])
        self.assertEqual(state["yield"], {})

    def test_first_run_and_new_stock_only_record_position(self):
        quotes = self.quotes({"5288": (1700, 1690, 2000)})
        state = fresh_state()
        state.pop("seeded")
        self.assertEqual(pa.evaluate(STOCKS, quotes, state, TODAY, None), [])  # 初回は記録するだけ
        self.assertEqual(state["dd"]["5288"], 15)
        state = fresh_state()  # 2回目以降に足した銘柄も、記録するだけ
        self.assertEqual(pa.evaluate(STOCKS, quotes, state, TODAY, None), [])
        self.assertEqual(state["dd"]["5288"], 15)

    def test_market_wide_fall_sends_ranking_once_a_day(self):
        quotes = self.quotes({"5288": (1500, 1440, 1500), "3922": (2300, 2150, 2300)})
        nikkei = pa.quote(bars(50000, 48400, 52000), TODAY)  # -3.2%
        state = fresh_state(dd={"5288": 0, "3922": 0}, **{"yield": {"5288": 4.0}})
        lines = pa.evaluate(STOCKS, quotes, state, TODAY, nikkei)
        text = "\n".join(lines)
        self.assertIn("【相場全体の下げ：日経平均 −3.2%】", text)
        self.assertLess(text.index("1. PRTIMES(3922)［成長］"), text.index("2. アジアパイルHD(5288)［Diego］"))
        self.assertEqual(pa.evaluate(STOCKS, quotes, state, TODAY, nikkei), [])


class WatchlistTest(unittest.TestCase):
    def workbook(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Diego式"
        ws.append([None, "Diego式スクリーニング（…）", "条件：…"])
        ws.append(["めも", "判定", "買い時", "銘柄名", "配当利回り(みんかぶ)", "株価", "コード", "1株配当(11/16の利回り×株価から逆算)"])
        ws.append([None, "◎ 合格", "🟢 買い時", "ＮＣＤ", 0.0608, 2304.0, 4783.0, 140.0])
        ws.append([None, "○ 合格（BPS未確認）", "🟢 買い時", "構造計画研究所ホールディングス", 0.0424, 2595.0, "208A", 110.0])
        ws.append(["即買い", "△ BPSで不合格", "⚪ 待ち", "佐藤商事", 0.0428, 2454.0, 8065.0, 105.0])
        ws.append([None, "× 利回り3%未満", "🟢 買い時", "日宣", 0.028, 1179.0, 6543.0, 33.0])
        ws = wb.create_sheet("成長株スクリーニング")
        ws.append(["コード", "銘柄名", "業種"])
        ws.append(["【抽出条件】2026-11-14時点", None, None])
        ws.append([3922.0, "ＰＲＴＩＭＥＳ", "商業サービス"])
        ws.append(["285A", "キオクシアホールディングス", "半導体"])
        ws.append([8065.0, "佐藤商事", "卸売"])
        return wb

    def test_reads_both_tabs(self):
        stocks = pa.parse_workbook(self.workbook())
        self.assertEqual(set(stocks), {"4783", "208A", "8065", "3922", "285A"})  # 判定が × の日宣は対象外
        self.assertEqual(stocks["4783"], {"name": "NCD", "groups": [pa.DIEGO], "div": 140.0, "verdict": "◎ 合格"})
        self.assertEqual(stocks["208A"]["name"], "構造計画研究所HD")
        self.assertEqual(stocks["8065"]["groups"], [pa.DIEGO, pa.GROWTH])
        self.assertEqual(stocks["285A"]["groups"], [pa.GROWTH])
        self.assertIsNone(stocks["3922"]["div"])

    def test_missing_tab_is_an_error(self):
        wb = self.workbook()
        del wb["成長株スクリーニング"]
        with self.assertRaises(pa.WatchlistError):
            pa.parse_workbook(wb)

    def test_clean_code(self):
        self.assertEqual([pa.clean_code(v) for v in (4783.0, "208a", " 1301 ", "【抽出条件】", None, 12345)], ["4783", "208A", "1301", "", "", ""])


class SlotTest(unittest.TestCase):
    def holiday(self, day):
        return day.weekday() >= 5

    def test_slots_through_the_day(self):
        self.assertEqual(pa.next_slot(at(9, 0), {"close": "2026-11-24"}, self.holiday)[:2], ("midday", TODAY))
        self.assertEqual(pa.next_slot(at(12, 0), {"midday": "2026-11-25", "close": "2026-11-24"}, self.holiday)[:2], ("close", TODAY))
        self.assertEqual(pa.next_slot(at(14, 31), {"close": "2026-11-24"}, self.holiday)[:2], ("close", TODAY))  # 昼の分は14:30で見送る
        self.assertIsNone(pa.next_slot(at(18, 0), {"midday": "2026-11-25", "close": "2026-11-25"}, self.holiday))

    def test_yesterdays_close_is_kept_until_morning(self):
        self.assertEqual(pa.next_slot(at(7, 0), {"close": "2026-11-21"}, self.holiday)[:2], ("close", datetime.date(2026, 11, 24)))
        self.assertEqual(pa.next_slot(at(8, 31), {"close": "2026-11-21"}, self.holiday)[:2], ("midday", TODAY))

    def test_weekend_has_nothing(self):
        saturday = at(12, 0, day=28)
        self.assertIsNone(pa.next_slot(saturday, {"midday": "2026-11-27", "close": "2026-11-27"}, self.holiday))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (pa.STATE_FILE, pa.load_watchlist)
        pa.STATE_FILE = os.path.join(self.tmp.name, "alert_state.json")
        pa.load_watchlist = lambda: STOCKS

    def tearDown(self):
        pa.STATE_FILE, pa.load_watchlist = self.old
        self.tmp.cleanup()

    def clock(self, start):
        """sleep で進む時計。(いまの時刻を返す関数, sleep, 待った秒数の記録) を返す"""
        box, slept = {"now": start}, []

        def sleep(sec):
            slept.append(sec)
            box["now"] += datetime.timedelta(seconds=sec)

        return (lambda: box["now"]), sleep, slept

    def test_first_run_sends_position_then_only_changes(self):
        tools = FakeTools({"5288": (1700, 1690, 2000), "3922": (2300, 2290, 2400), "^N225": (50000, 49900, 52000)})
        now_fn, sleep, slept = self.clock(at(11, 0))
        pa.run(tools, now_fn, True, sleep=sleep)
        self.assertEqual(slept, [52 * 60, 243 * 60])  # 11:52 まで待って昼の分、15:55 まで待って引け後の分
        self.assertEqual(len(tools.sent), 1)
        self.assertIn("🔰 値下がりアラートを始めました（対象 2銘柄：Diego式 1／成長株 1）", tools.sent[0])
        self.assertIn("【52週高値から −15〜−20%】1銘柄\n・アジアパイルHD(5288)［Diego］ −15.5%", tools.sent[0])
        self.assertIn("【配当利回り 4%以上（Diego式）】1銘柄：アジアパイルHD 4.1%", tools.sent[0])
        # 引け後の分は、変化が無いので送っていない
        self.assertEqual(pa.load_state()["done"], {"midday": "2026-11-25", "close": "2026-11-25"})
        # 同じ日のあとの回は、何もしない
        tools.sent.clear()
        now_fn, sleep, slept = self.clock(at(17, 37))
        pa.run(tools, now_fn, True, sleep=sleep)
        self.assertEqual((tools.sent, slept), ([], []))

    def test_waits_for_todays_prices(self):
        tools = FakeTools({"5288": (1700, 1690, 2000), "3922": (2300, 2290, 2400)}, last_day=TODAY - datetime.timedelta(days=1))
        now_fn, sleep, slept = self.clock(at(12, 0))
        pa.run(tools, now_fn, True, sleep=sleep)
        self.assertEqual(slept, [pa.READY_WAIT_SEC] * (pa.READY_RETRIES - 1))
        self.assertEqual(tools.sent, [])
        self.assertEqual(pa.load_state()["done"], {})  # 済みにしない（あとの回でやり直す）

    def test_early_run_exits_without_waiting(self):
        tools = FakeTools({})
        with open(pa.STATE_FILE, "w", encoding="utf-8") as f:
            f.write('{"done": {"close": "2026-11-24"}}')  # 前の日の引け後の分は済み
        now_fn, sleep, slept = self.clock(at(1, 37))
        pa.run(tools, now_fn, True, sleep=sleep)
        self.assertEqual((slept, tools.sent), ([], []))

    def test_unreadable_sheet_is_reported_once_a_day(self):
        def broken():
            raise pa.WatchlistError("スプシの共有が「リンクを知っている全員・閲覧者」になっていません（401/403）")

        pa.load_watchlist = broken
        tools = FakeTools({})
        now_fn, sleep, _ = self.clock(at(12, 0))
        pa.run(tools, now_fn, True, sleep=sleep)  # 昼の分 → そのまま引け後の分まで待って動く
        self.assertEqual(pa.load_state()["done"], {"midday": "2026-11-25", "close": "2026-11-25"})
        self.assertEqual(len(tools.sent), 1)
        self.assertIn("対象の銘柄を読めませんでした", tools.sent[0])

    def test_manual_run_uses_latest_prices(self):
        tools = FakeTools({"5288": (1500, 1350, 1600), "3922": (2300, 2290, 2400), "^N225": (50000, 49000, 52000)}, tdnet=[{"code": "5288", "title": "業績予想の修正に関するお知らせ"}])
        with open(pa.STATE_FILE, "w", encoding="utf-8") as f:
            f.write('{"seeded": "2026-11-24", "dd": {"5288": 15, "3922": 0}, "yield": {"5288": 5.0}}')
        pa.run(tools, self.clock(at(10, 15))[0], False)
        self.assertEqual(len(tools.sent), 1)
        self.assertIn("📉 値下がりアラート 11/25（水）手動（10:15時点）\n日経平均 49,000（−2.0%）", tools.sent[0])
        self.assertIn("相場全体も下げ（日経平均 −2.0%）", tools.sent[0])
        self.assertIn("今日の開示：「業績予想の修正に関するお知らせ」", tools.sent[0])
        self.assertEqual(pa.load_state()["done"], {})  # 手動実行は、定時の回の「済み」にしない

    def test_long_message_is_split(self):
        parts = pa.chunks(["あ" * 100] * 100, limit=1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual(sum(p.count("あ") for p in parts), 10000)


if __name__ == "__main__":
    unittest.main()
