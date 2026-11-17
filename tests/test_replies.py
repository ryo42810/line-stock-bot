import datetime
import json
import os
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import x_replies as xr  # noqa: E402

JST = xr.JST


def at(h, m=0, day=datetime.date(2026, 11, 18)):  # 11/18は水曜
    return datetime.datetime.combine(day, datetime.time(h, m), tzinfo=JST)


def cand(pid, uid, follower=False, likes=0, verified=True):
    return {"id": pid, "text": f"優待の話 {pid}", "metrics": {"like_count": likes},
            "user": {"id": uid, "username": f"u{uid}", "verified": verified,
                     "connection_status": ["followed_by"] if follower else []}}


class PlanTest(unittest.TestCase):
    def test_early_run_waits_for_next_slot(self):
        slot, wait = xr.plan(at(8, 3), [])
        self.assertEqual(slot, datetime.time(10, 0))
        self.assertAlmostEqual(wait, 117 * 60)

    def test_too_early_does_nothing(self):
        self.assertEqual(xr.plan(at(5, 0), [])[0], None)

    def test_late_run_skips_to_latest_slot(self):
        self.assertEqual(xr.plan(at(14, 10), []), (datetime.time(13, 0), 0))
        self.assertEqual(xr.plan(at(14, 10), ["10:00", "13:00"])[0], datetime.time(18, 0))

    def test_after_deadline_and_weekend(self):
        self.assertEqual(xr.plan(at(22, 30), ["10:00", "13:00"])[0], None)
        self.assertEqual(xr.plan(at(10, 5, datetime.date(2026, 11, 21)), [])[0], None)  # 土曜
        self.assertEqual(xr.plan(at(10, 5, datetime.date(2026, 11, 23)), [])[0], None)  # 勤労感謝の日


class PickTest(unittest.TestCase):
    def test_one_post_per_person_and_ratio(self):
        cands = [cand(str(i), f"f{i}", follower=True, likes=i) for i in range(5)]
        cands += [cand(str(10 + i), f"o{i}", likes=i) for i in range(8)]
        cands.append(cand("99", "o7", likes=100))  # 同じ人の2つ目の投稿
        picked = xr.pick_targets(cands, set(), 0, 0)
        self.assertEqual(len(picked), 5)
        self.assertEqual(len({c["user"]["id"] for c in picked}), 5)
        self.assertEqual(sum(xr.is_follower(c["user"]) for c in picked), 2)  # 5人の3割 ≒ 2人
        self.assertIn("99", [c["id"] for c in picked])  # 同じ人なら反応が多い方

    def test_skip_people_already_engaged_today(self):
        picked = xr.pick_targets([cand("1", "a"), cand("2", "b")], {"a"}, 0, 1)
        self.assertEqual([c["user"]["id"] for c in picked], ["b"])

    def test_fill_from_other_group(self):
        picked = xr.pick_targets([cand(str(i), f"o{i}") for i in range(6)], set(), 0, 0)
        self.assertEqual(len(picked), 5)


class BudgetTest(unittest.TestCase):
    def test_cost_of_response(self):
        body = {"data": [{"id": "1"}, {"id": "2"}], "includes": {"users": [{"id": "a"}], "tweets": [{"id": "3"}]}}
        self.assertAlmostEqual(xr.cost_of_response(body), 3 * xr.PRICE_POST + xr.PRICE_USER)

    def test_stop_when_over_budget(self):
        sent = []

        class Tools:
            def send_line_text(self, text):
                sent.append(text)

            def push_line(self, messages):
                raise AssertionError("上限を超えたら送らない")

        state = xr.load_state("{}")
        xr.add_x_cost(state, datetime.date(2026, 11, 18), 39.9)
        self.assertFalse(xr.run_slot(Tools(), at(10), "10:00", state))
        self.assertFalse(xr.run_slot(Tools(), at(13), "13:00", state))
        self.assertEqual(len(sent), 1)  # 知らせるのは月1回
        xr.add_x_cost(state, datetime.date(2026, 12, 1), 0)
        self.assertEqual(xr.x_spent(state, datetime.date(2026, 12, 1)), 0)  # 月が変わればリセット


class FakeX:
    def __init__(self):
        self.cost = 0.0
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        self.cost += 0.01
        if path == "/users/me":
            return {"data": {"id": "me", "username": "Whale2608"}}
        if path.endswith("/tweets"):
            return {"data": [{"id": "m1", "text": "優待いいよね", "referenced_tweets": [{"type": "replied_to", "id": "z"}]}]}
        if path.endswith("/mentions"):
            return {"data": [{"id": "500", "text": "@Whale2608 その銘柄気になってました", "author_id": "x1",
                              "referenced_tweets": [{"type": "replied_to", "id": "400"}]}],
                    "includes": {"users": [{"id": "x1", "username": "fan"}], "tweets": [{"id": "400", "text": "今日の1銘柄"}]}}
        return {"data": [{"id": "700", "text": "高配当株のまとめ", "author_id": "v1", "public_metrics": {"like_count": 5}}],
                "includes": {"users": [{"id": "v1", "username": "big", "verified": True}]}}


class FakeClient:
    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        text = json.dumps([{"id": "500", "reply": "ありがとうございます！権利月が近いので気になりますよね", "skip": False},
                           {"id": "700", "reply": "まとめ見やすいです。利回り順だと3番目が気になりました", "skip": False}],
                          ensure_ascii=False)

        class Block:
            type = "text"

        b = Block()
        b.text = text

        class Res:
            content = [b]

        return Res()


class RunTest(unittest.TestCase):
    def test_run_slot_sends_cards_and_records(self):
        pushed = []

        class Tools:
            def push_line(self, messages):
                pushed.extend(messages)

        state = xr.load_state("{}")
        x = FakeX()
        self.assertTrue(xr.run_slot(Tools(), at(10), "10:00", state, x=x, client=FakeClient()))
        self.assertEqual(state["engaged"], ["v1"])
        self.assertEqual(state["others_today"], 1)
        self.assertEqual(state["mention_since_id"], "500")
        self.assertGreater(xr.x_spent(state, at(10).date()), 0)
        flex = [m for m in pushed if m["type"] == "flex"][0]
        cards = flex["contents"]["contents"]
        self.assertEqual(len(cards), 2)
        actions = [b["action"] for b in cards[0]["footer"]["contents"]]
        self.assertEqual(actions[0]["type"], "clipboard")
        self.assertIn("in_reply_to=500", actions[1]["uri"])
        query = "is:verified" in [p for path, p in x.calls if path == "/tweets/search/recent"][0]["query"]
        self.assertTrue(query)


class MiscTest(unittest.TestCase):
    def test_reply_url(self):
        url = xr.reply_url("123", "こんにちは")
        self.assertTrue(url.startswith("https://x.com/intent/tweet?in_reply_to=123&text="))
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["text"][0], "こんにちは")
        self.assertEqual(xr.reply_url("123", "あ" * 300), "https://x.com/intent/tweet?in_reply_to=123")

    def test_parse_and_check(self):
        rows = xr.parse_replies('説明\n[{"id": "1", "reply": "いいですね"}, {"id": "2", "reply": "x", "skip": true}]')
        self.assertEqual(rows, {"1": "いいですね", "2": ""})
        self.assertTrue(xr.check_reply("見て https://example.com"))
        self.assertTrue(xr.check_reply("絶対上がる"))

    def test_new_day_resets(self):
        state = xr.load_state(json.dumps({"day": "2026-11-17", "engaged": ["a"], "slots_done": ["10:00"], "followers_today": 2}))
        xr.new_day(state, datetime.date(2026, 11, 18))
        self.assertEqual((state["engaged"], state["slots_done"], state["followers_today"]), ([], [], 0))

    def test_main_marks_skipped_slots(self):
        pushed = []

        class Tools:
            def push_line(self, messages):
                pushed.extend(messages)

        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                orig_keys, orig_run, orig_latest = dict(xr.x_media.X_KEYS), xr.run_slot, xr.latest_slots_done
                xr.x_media.X_KEYS.update({k: "x" for k in xr.x_media.X_KEYS})
                xr.run_slot = lambda tools, now, slot, state, **kw: True
                xr.latest_slots_done = lambda day: []
                xr.main(Tools(), at(14, 30), scheduled=True)
                state = xr.load_state()
                self.assertEqual(state["slots_done"], ["10:00", "13:00"])
            finally:
                xr.x_media.X_KEYS.clear()
                xr.x_media.X_KEYS.update(orig_keys)
                xr.run_slot, xr.latest_slots_done = orig_run, orig_latest
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
