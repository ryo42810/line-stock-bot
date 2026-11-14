"""キーや通信を使わない動作確認。プルリクエストを出すと check.yml から自動で動く。
手元で動かすとき: python -m unittest discover -s tests -v
"""
import datetime
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
from openpyxl import Workbook  # noqa: E402

import app  # noqa: E402
import earnings_calendar  # noqa: E402
import kujira_posts  # noqa: E402
import x_media  # noqa: E402

JST = app.JST


def at(day, hour, minute):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=JST)


class ScheduleGateTest(unittest.TestCase):
    """定時実行のずれ対策"""

    def gate(self, mode, now, done=""):
        waited = []
        result = app.schedule_gate(mode, now, True, done, sleep=waited.append)
        return result, waited

    def test_manual_run_never_waits(self):
        self.assertEqual(app.schedule_gate("morning", at(16, 3, 0), False), (datetime.date(2026, 11, 16), True))

    def test_too_early_does_nothing(self):
        (day, _), waited = self.gate("morning", at(16, 0, 43))
        self.assertIsNone(day)
        self.assertEqual(waited, [])

    def test_early_waits_until_target(self):
        (day, allow_x), waited = self.gate("morning", at(16, 4, 43))
        self.assertEqual((day, allow_x), (datetime.date(2026, 11, 16), True))
        self.assertEqual(waited, [2 * 60 * 60])

    def test_on_time_posts(self):
        (day, allow_x), waited = self.gate("morning", at(16, 7, 30))
        self.assertEqual((day, allow_x, waited), (datetime.date(2026, 11, 16), True, []))

    def test_late_morning_skips_x(self):
        (day, allow_x), _ = self.gate("morning", at(16, 10, 50))
        self.assertEqual((day, allow_x), (datetime.date(2026, 11, 16), False))

    def test_already_done_does_nothing(self):
        (day, _), _ = self.gate("morning", at(16, 7, 0), done="2026-11-16")
        self.assertIsNone(day)

    def test_evening_after_midnight_is_previous_day(self):
        # 金曜の夕方の回が、土曜の0:42に起動したとき → 金曜の分として動く（休場日扱いにしない）
        (day, allow_x), _ = self.gate("evening", at(14, 0, 42))
        self.assertEqual((day, allow_x), (datetime.date(2026, 11, 13), False))
        self.assertFalse(app.is_market_holiday(day))

    def test_evening_early_waits(self):
        (day, allow_x), waited = self.gate("evening", at(16, 14, 55))
        self.assertEqual((day, allow_x, waited), (datetime.date(2026, 11, 16), True, [2 * 60 * 60]))


class RankingParseTest(unittest.TestCase):
    YUTAI = (
        "12月\n"
        "1 楽天グループ(株) 4755 東証PRM その他 最低投資金額 6.37万円 優待発生株数 100株 配当利回り（会社予想） --- 権利確定日 12月末日 <https://finance.yahoo.co.jp/quote/4755.T/incentive>\n"
        "3 ヒューリック(株) 3003 東証PRM 飲食料品 その他 最低投資金額 59.61万円 優待発生株数 300株 配当利回り（会社予想） 3.52% 権利確定日 12月末日 <https://finance.yahoo.co.jp/quote/3003.T/incentive>\n"
    )
    HAITO = (
        "153\n(株)サンゲツ <https://finance.yahoo.co.jp/quote/8130.T>\n8130\n東証PRM\n掲示板 <https://finance.yahoo.co.jp/quote/8130.T/forum>\n"
        "3,225\n11/13\n2027/03\n155.00\n+4.81\n%\n"
        "154\nいちごオフィスリート投資法人 <https://finance.yahoo.co.jp/quote/8975.T>\n8975\n東証\n掲示板 <https://finance.yahoo.co.jp/quote/8975.T/forum>\n"
        "90,000\n11/13\n2027/04\n4,500.00\n+5.00\n%\n"
    )

    def test_yutai_ranking(self):
        rows = kujira_posts.parse_yutai_ranking(self.YUTAI)
        self.assertEqual([r["code"] for r in rows], ["4755", "3003"])
        self.assertEqual(rows[0]["name"], "楽天グループ")
        self.assertIsNone(rows[0]["page_yield"])
        self.assertEqual((rows[1]["min_shares"], rows[1]["page_yield"], round(rows[1]["min_yen"])), (300, 3.52, 596100))

    def test_haito_ranking_skips_reit(self):
        rows = kujira_posts.parse_haito_ranking(self.HAITO)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["code"], rows[0]["name"], rows[0]["dividend"], rows[0]["page_yield"]), ("8130", "サンゲツ", 155.0, 4.81))

    def test_numbers_must_come_from_facts(self):
        facts = "配当利回り（会社予想）: 4.81%\n1株配当: 155円\n配当性向: 55%"
        self.assertTrue(kujira_posts.numbers_ok("利回り4.81%💰\n1株配当は155円", facts))
        self.assertFalse(kujira_posts.numbers_ok("利回り6.2%で過去最高", facts))

    def test_kaisetsu_has_no_stock_codes(self):
        for topic in kujira_posts.KAISETSU_TOPICS:
            self.assertFalse(x_media.codes_in_post(" ".join(topic["facts"])))
            self.assertGreaterEqual(len(topic["facts"]), 3)

    def test_plan_covers_monday_to_thursday(self):
        self.assertEqual(sorted(kujira_posts.PLAN), [0, 1, 2, 3])


class EarningsCalendarTest(unittest.TestCase):
    def test_parse_workbook(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["９月に四半期末又は期末を迎えた決算発表予定会社の一覧"])
        ws.append(["決算発表予定日", "コード", "会社名", "Issue Name", "決算期末", "業種名", "Industry", "種別", "Fiscal", "市場区分", "Market"])
        ws.append([datetime.datetime(2026, 11, 16), 2158, "ＦＲＯＮＴＥＯ", "FRONTEO", None, "サービス業", "", "第２四半期", "", "グロース", ""])
        ws.append([datetime.datetime(2026, 11, 16), "130A", "あい　ホールディングス", "", None, "", "", "本決算", "", "プライム", ""])
        buf = io.BytesIO()
        wb.save(buf)
        rows = earnings_calendar.parse_workbook(buf.getvalue())
        self.assertEqual([(r["code"], r["name"], r["market"]) for r in rows], [("2158", "FRONTEO", "グロース"), ("130A", "あいホールディングス", "プライム")])
        earnings_calendar._cache = rows
        try:
            found = earnings_calendar.scheduled_on(datetime.date(2026, 11, 16), ["2158"])
            self.assertEqual(found[0]["code"], "2158")
            self.assertIn("★FRONTEO(2158)", earnings_calendar.as_text(found, ["2158"]))
            self.assertEqual(earnings_calendar.scheduled_on(datetime.date(2026, 11, 17)), [])
        finally:
            earnings_calendar._cache = None


class ImageTest(unittest.TestCase):
    """画像が落ちずに作れるか（見た目は人が確認する）"""

    @staticmethod
    def history(code):
        idx = pd.date_range("2025-11-17", periods=250, freq="B")
        return pd.DataFrame({"Close": [1000 + i * 2 + (i % 7) * 15 for i in range(250)]}, index=idx)

    def test_render_all(self):
        stock = {"code": "8130", "name": "サンゲツ", "category": "高配当", "price": 3225.0, "change": -0.4,
                 "yield_fmt": "配当4.8%", "headline": "1株配当155円（会社予想）・配当性向55%", "signals": ["25日線から-3.2%"]}
        with tempfile.TemporaryDirectory() as d:
            for theme in (True, False):
                x_media.MIDNIGHT = theme
                card = x_media.render_stock_card(os.path.join(d, "card.png"), "今朝のニュース銘柄", "材料が出た注目の3銘柄", [stock, stock], x_media.COLORS["green"], self.history, "2026.11.16（月）")
                perf = x_media.render_performance_chart(os.path.join(d, "perf.png"), [{"name": "サンゲツ", "code": "8130", "change": 2.5}, {"name": "東名", "code": "4439", "change": -1.0}], "買い場候補の成績", "2026.11.13")
                self.assertGreater(os.path.getsize(card), 10000)
                self.assertGreater(os.path.getsize(perf), 10000)
            x_media.MIDNIGHT = True
            single = x_media.render_single_card(
                os.path.join(d, "single.png"), "今日の1銘柄", "高配当株", stock,
                [("株価（前営業日終値）", "3,225", "円"), ("配当利回り（会社予想）", "4.81", "%"), ("配当性向", "", "%")],
                [("利回りの水準", "4.81%", "過去5年の平均は4.5%（いまは0.3ポイント高い）"), ("株主優待", "", "自社商品の詰め合わせ" * 6), ("PBR", "", ""),
                 ("気になる点", "", "配当性向は55%でやや高め")], self.history, "2026.11.19（木）", "配当を見るときのチェック表",
            )
            self.assertGreater(os.path.getsize(single), 10000)


class PostFormatTest(unittest.TestCase):
    def test_hashtags_replace_ai_tags(self):
        text = app.add_hashtags("今日の1銘柄はサンゲツ(8130)💰\n#適当なタグ", kujira_posts.HASHTAGS)
        self.assertTrue(text.endswith(kujira_posts.HASHTAGS))
        self.assertNotIn("#適当なタグ", text)

    def test_diary_template_is_not_auto_posted_text(self):
        self.assertIn("自動投稿はしません", kujira_posts.DIARY_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
