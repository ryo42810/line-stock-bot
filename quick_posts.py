"""AIを使わない投稿：「〇万円あったら」（毎日2本）と、大型株の値動き（平日の引け後に1本）

2026/11/22に追加。伸びている株アカウントで読まれていた「予算から考える投稿」と
「持っている人が多い会社の値動き」を、株価と配当のデータだけで作る（文章の枠は決まった型。AIは使わない）。
- 「〇万円あったら」：毎日 8:26ごろ・21:13ごろ（時刻は日ごとに数分ずらす。x_queue.slot_time）
- 大型株の値動き　：平日の引け後 16時すぎ。今日いちばん動いた会社が ±2% 以上のときだけ
- x_queue.py の回（1時間おき）が、待ちから戻るたびに prepare を呼び、その日の分を予約に並べる

数字のあつかい
- 株価は yfinance の終値。年間配当は yfinance の「予想（dividendRate）」と「実績（trailingAnnualDividendRate）」の
  両方が取れて、差が35%以内で、利回りが0.8〜6.5%に収まる会社だけ使う（どちらかがおかしいデータをはじくため）
- 投稿には「年間配当を1株〇円として」と、計算の前提を書く。売買のすすめ・株価の予想・保有や売買の体験談は書かない
- スプシの状況が「購入済」の銘柄を出すときは、最後に「※保有中の銘柄です」と書く
- ハッシュタグは付けない（伸びているアカウントが付けていないので、新しい型はタグなしで試す。2026/12/6に比べる）
"""
import datetime
import json
import math
import random

import kujira_posts
import x_queue

STATE_FILE = "quick_state.json"
YOSAN_SLOTS = (("08:26", "10:30"), ("21:13", "23:30"))  # （出す時刻のもと, これより遅れたら見送る）
UGOKI_BASE, UGOKI_UNTIL = "15:58", "18:30"  # 15:30の引けから、終値のデータが入るまで少し待つ
UGOKI_MIN_CHANGE = 2.0  # 今日の騰落率がこれ以上（±）の会社があるときだけ出す
UGOKI_RETRY_MIN = 20  # 今日の終値がまだ取れないとき、次に試すまでの間隔
HASHTAGS = ""  # 付けるなら "#株 #株クラ"
HELD_NOTE = "※保有中の銘柄です"
BUDGETS = (50_000, 100_000, 300_000, 500_000, 1_000_000)
TARGETS = (10_000, 30_000, 60_000)  # 「配当で年〇円ほしいなら」の金額（kujira_posts.EVERYDAY_BANDS の区切りに合わせる）
YIELD_RANGE = (0.8, 6.5)
DIV_GAP_MAX = 0.35
BUDGET_USE_MIN = 0.6  # 予算のうち、これ以上を使える株数になる予算だけ使う

# 持っている人が多い会社（個人株主が多い大型株）。値動きはこの中から選ぶ
WIDE = [
    ("9432", "NTT"), ("9434", "ソフトバンク"), ("9433", "KDDI"), ("8306", "三菱UFJ"), ("8316", "三井住友FG"),
    ("8411", "みずほFG"), ("2914", "JT"), ("8591", "オリックス"), ("8593", "三菱HCキャピタル"), ("8766", "東京海上HD"),
    ("8058", "三菱商事"), ("8031", "三井物産"), ("8001", "伊藤忠商事"), ("8053", "住友商事"), ("7203", "トヨタ自動車"),
    ("7267", "ホンダ"), ("4502", "武田薬品"), ("4503", "アステラス製薬"), ("1605", "INPEX"), ("5020", "ENEOS"),
    ("6178", "日本郵政"), ("7182", "ゆうちょ銀行"), ("8729", "ソニーFG"), ("1928", "積水ハウス"), ("8750", "第一生命HD"),
    ("8725", "MS&AD"), ("5108", "ブリヂストン"), ("7751", "キヤノン"), ("4452", "花王"), ("2503", "キリンHD"),
    ("9101", "日本郵船"), ("5401", "日本製鉄"), ("2702", "日本マクドナルドHD"), ("8267", "イオン"), ("4755", "楽天グループ"),
    ("7974", "任天堂"), ("6758", "ソニーグループ"), ("9984", "ソフトバンクグループ"),
]

SINGLE_HOOKS = (
    "{budget}あったら、{name}が{shares}株買える計算。",
    "手元に{budget}あるとして。{name}なら{shares}株。",
    "{budget}の置き場所を配当で考えてみる。{name}なら{shares}株。",
)
SINGLE_TAILS = (
    "この数字、高いと見る？ふつうと見る？🤔",
    "同じ{budget}、みんなならどこに置くんだろう🤔",
    "配当だけで見るか、値動きも見るか。迷うところ🤔",
)
UP_TAILS = ("上がった日は、利回りが下がる日でもある🤔", "持ってる人はうれしい日、狙ってた人はくやしい日。")
DOWN_TAILS = ("ここで拾うか、もう一段待つか。悩ましい水準🤔", "下がった理由を確かめてから、が先かな🤔")


# ---------------------------------------------------------------
# 数字の書き方
# ---------------------------------------------------------------
def yen(value):
    """金額の書き方。1万円以上は「約12.3万円」、それより小さければ「約8,400円」"""
    if value >= 10_000:
        text = f"{value / 10_000:.1f}".rstrip("0").rstrip(".")
        return f"約{text}万円"
    return f"約{round(value, -1 if value >= 1000 else 0):,.0f}円"


def budget_text(value):
    return f"{value // 10_000}万円"


def price_text(price):
    return f"{price:,.0f}円" if price >= 1000 else f"{price:,.1f}円".replace(".0円", "円")


def div_text(div):
    return f"{div:,.1f}".rstrip("0").rstrip(".") + "円"


def everyday_label(annual):
    """年間配当の金額を、身近なものに置きかえた言葉（kujira_posts の置きかえ表）。無ければ空"""
    for floor, label in kujira_posts.EVERYDAY_BANDS:
        if annual >= floor:
            return label
    return ""


# ---------------------------------------------------------------
# データ
# ---------------------------------------------------------------
def facts(tools, code, name, today, need_today=False):
    """株価と配当。使えないデータなら None。need_today: 今日の終値が入っているときだけ返す（値動き用）"""
    try:
        hist = tools.get_history(code)
        closes = hist["Close"].dropna()
        if len(closes) < 2:
            return None
        last_day = closes.index[-1].date()
        if need_today and last_day != today:
            return None
        if (today - last_day).days > 6:
            return None
        price, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
        if price <= 0 or prev <= 0:
            return None
        data = {"code": code, "name": name, "price": price, "change": (price - prev) / prev * 100, "div": None, "yield": None}
        info = tools.yf.Ticker(f"{code}.T").info or {}
        rate, trail = info.get("dividendRate"), info.get("trailingAnnualDividendRate")
        if rate and trail and rate > 0 and trail > 0 and abs(rate - trail) / rate <= DIV_GAP_MAX:
            y = rate / price * 100
            if YIELD_RANGE[0] <= y <= YIELD_RANGE[1]:
                data["div"], data["yield"] = float(rate), y
        return data
    except Exception as e:
        print(f"株価・配当を取れませんでした ({code}):", type(e).__name__, str(e)[:120])
        return None


PER_DAY = 4  # 1日に使う会社の数（朝2社・夜2社）
_div_cache = {}


def dividend_stocks(tools, today, count, skip=0):
    """配当のデータが使える会社を、その日の順番で count 社返す（skip 社は飛ばす）。
    順番は期間ごとに並べかえ、1日に PER_DAY 社ずつ進む（同じ会社が続けて出ないように）"""
    if today not in _div_cache:
        rows = [f for f in (facts(tools, code, name, today) for code, name in WIDE) if f and f["div"]]
        _div_cache.clear()
        _div_cache[today] = rows
    rows = _div_cache[today][:]
    if not rows:
        return []
    cycle, pos = divmod(today.toordinal(), len(WIDE))
    random.Random(cycle).shuffle(rows)
    start = (pos * PER_DAY + skip) % len(rows)
    rows = rows[start:] + rows[:start]
    return rows[:count]


# ---------------------------------------------------------------
# 「〇万円あったら」
# ---------------------------------------------------------------
def shares_for(budget, price):
    return int(budget // (price * 100)) * 100


def fits(budget, price):
    """その予算の6割以上を使える株数が買えるか（100万円で100株＝50万円分、のような半端な例を出さないため）"""
    return shares_for(budget, price) * price >= budget * BUDGET_USE_MIN


def single_post(f, today, slot):
    seed = today.toordinal() + slot
    budgets = [b for b in BUDGETS if fits(b, f["price"])]
    if not budgets:
        return ""
    budget = budgets[seed % min(len(budgets), 3)]  # 小さい予算から3つまでを回す（大きすぎる予算は身近でないため）
    shares = shares_for(budget, f["price"])
    annual = shares * f["div"]
    ctx = {"budget": budget_text(budget), "name": f["name"], "shares": f"{shares:,}"}
    lines = [
        SINGLE_HOOKS[seed % len(SINGLE_HOOKS)].format(**ctx),
        "",
        f"株価{price_text(f['price'])}、年間配当を1株{div_text(f['div'])}として",
        f"年の配当は{yen(annual)}💰",
    ]
    label = everyday_label(annual)
    if label:
        lines.append(f"{label}くらい。")
    lines += ["", f"利回りにすると約{f['yield']:.1f}%。", SINGLE_TAILS[(seed // 3) % len(SINGLE_TAILS)].format(**ctx)]
    return "\n".join(lines)


def compare_post(a, b, today):
    budgets = [x for x in BUDGETS if fits(x, a["price"]) and fits(x, b["price"])]
    if not budgets or a["code"] == b["code"]:
        return ""
    budget = budgets[0]
    na, nb = shares_for(budget, a["price"]), shares_for(budget, b["price"])
    if na == nb:
        return ""
    return "\n".join([
        f"同じ{budget_text(budget)}でも、{a['name']}なら{na:,}株、{b['name']}なら{nb:,}株。",
        "",
        f"・{a['name']}（株価{price_text(a['price'])}）",
        f"　年の配当 {yen(na * a['div'])}",
        f"・{b['name']}（株価{price_text(b['price'])}）",
        f"　年の配当 {yen(nb * b['div'])}",
        "",
        f"年間配当は1株{div_text(a['div'])}・{div_text(b['div'])}として計算。",
        "株数が多いほうが配当も多い、とは限らないのがおもしろい🐳",
    ])


def target_post(f, today, slot):
    target = TARGETS[(today.toordinal() + slot) % len(TARGETS)]
    shares = math.ceil(target / f["div"] / 100) * 100
    cost = shares * f["price"]
    if cost > 5_000_000:
        return ""
    label = everyday_label(target)
    lines = [
        f"配当で年{budget_text(target)}ほしいなら、{f['name']}は{shares:,}株で{yen(cost)}。",
        "",
        f"株価{price_text(f['price'])}、年間配当を1株{div_text(f['div'])}として計算。",
    ]
    if label:
        lines.append(f"「{label}」くらいを、毎年配当でまかなう形。")
    lines += ["", "この金額、思ったより多い？少ない？🤔"]
    return "\n".join(lines)


def with_note(post, codes, held):
    if not post:
        return ""
    if any(c in held for c in codes):
        post += f"\n\n{HELD_NOTE}"
    return f"{post}\n\n{HASHTAGS}" if HASHTAGS else post


def build_yosan(tools, today, slot, held=()):
    """「〇万円あったら」の投稿を1本。slot: 0＝朝、1＝夜（型と会社が重ならないようにする）。作れなければ空"""
    kind = ("single", "compare", "target")[(today.toordinal() + slot * 2) % 3]
    stocks = dividend_stocks(tools, today, 2, skip=slot * 2)
    if kind != "compare" and len(stocks) == 2 and not any(fits(b, stocks[0]["price"]) for b in BUDGETS):
        stocks = stocks[::-1]  # 1社目がどの予算にも合わない値がさ株なら、2社目を使う
    if not stocks:
        return ""
    post, codes = "", []
    if kind == "compare" and len(stocks) == 2:
        post, codes = compare_post(stocks[0], stocks[1], today), [s["code"] for s in stocks]
    elif kind == "target":
        post, codes = target_post(stocks[0], today, slot), [stocks[0]["code"]]
    if not post:
        post, codes = single_post(stocks[0], today, slot), [stocks[0]["code"]]
    return with_note(post, codes, held)


# ---------------------------------------------------------------
# 大型株の値動き
# ---------------------------------------------------------------
def build_ugoki(tools, today, held=()):
    """今日いちばん動いた大型株の短い投稿。戻り値は (文章, 今日の終値が取れたか)"""
    rows = [f for f in (facts(tools, code, name, today, need_today=True) for code, name in WIDE) if f]
    if len(rows) < len(WIDE) // 2:
        return "", False  # 今日の終値がまだそろっていない
    top = max(rows, key=lambda f: abs(f["change"]))
    if abs(top["change"]) < UGOKI_MIN_CHANGE:
        return "", True
    up = top["change"] > 0
    tails = UP_TAILS if up else DOWN_TAILS
    lines = [
        f"{top['name']}が今日{top['change']:+.1f}%、終値{price_text(top['price'])}{'📈' if up else '📉'}",
        "",
        f"100株だと{yen(top['price'] * 100)}。",
    ]
    if top["div"]:
        lines.append(f"年間配当を1株{div_text(top['div'])}として計算すると、利回りは約{top['yield']:.1f}%。")
    lines += ["", tails[today.toordinal() % len(tails)]]
    return with_note("\n".join(lines), [top["code"]], held), True


# ---------------------------------------------------------------
# 予約に並べる
# ---------------------------------------------------------------
def load_state(today):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    if state.get("date") != today.isoformat():
        state = {"date": today.isoformat(), "done": [], "ugoki_tried": ""}
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.write("\n")


def prepare(tools, now):
    """x_queue.py の回が、待ちから戻るたびに呼ぶ。今日の分でまだ並べていないものを予約に並べる（済んでいれば何もしない）"""
    today = now.date()
    state = load_state(today)
    done = state["done"]
    todo = [f"yosan{i}" for i in range(len(YOSAN_SLOTS)) if f"yosan{i}" not in done]
    ugoki_open = (
        "ugoki" not in done and today.weekday() < 5 and not tools.is_market_holiday(today)
        and x_queue._at(today, UGOKI_BASE, now.tzinfo) <= now <= x_queue._at(today, UGOKI_UNTIL, now.tzinfo)
    )
    if ugoki_open and state.get("ugoki_tried"):
        last = datetime.datetime.fromisoformat(state["ugoki_tried"])
        ugoki_open = (now - last).total_seconds() >= UGOKI_RETRY_MIN * 60
    if not todo and not ugoki_open:
        return
    try:
        held = tools.load_held()
    except Exception:
        held = set()
    entries = []
    for i, (base, until) in enumerate(YOSAN_SLOTS):
        key = f"yosan{i}"
        if key in done:
            continue
        done.append(key)
        if now > x_queue._at(today, until, now.tzinfo):
            continue
        post = build_yosan(tools, today, i, held)
        if post:
            entries.append((x_queue.slot_time(today, base), until, post))
        else:
            print(f"「〇万円あったら」を作れませんでした（{base}の分）")
    if ugoki_open:
        post, ready = build_ugoki(tools, today, held)
        state["ugoki_tried"] = now.isoformat()
        if ready:
            done.append("ugoki")
        if post:
            entries.append((x_queue.slot_time(today, UGOKI_BASE), UGOKI_UNTIL, post))
        elif ready:
            print(f"大型株の値動き: ±{UGOKI_MIN_CHANGE}%以上の会社が無いので、今日は出しません")
    save_state(state)
    for hhmm, until, post in entries:
        x_queue.enqueue(today, [(hhmm, post, None)], until)
    x_queue.push("「〇万円あったら」・大型株の値動きの記録を更新", files=(STATE_FILE,))
