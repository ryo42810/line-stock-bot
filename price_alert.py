"""値下がりアラート：Diego式の銘柄と成長株が下がったときに、LINEで知らせる（price_alert.yml・`python app.py alert`）

2026/11/25に追加。「値下がりしたタイミングで買いたいが、気づく仕組みがない」ための通知。AIは使わない（コスト0）。
- 対象：スプシ「気になるテーマ株_一覧表」の「Diego式」タブ（判定が「×」の銘柄は除く）と「成長株スクリーニング」タブ
- 回数：平日の昼休み（11:52ごろ。前場の終値）と、引け後（15:55ごろ。終値）の2回。知らせるものが無い回は送らない
- 知らせる条件（数字は Variables で変えられる）
  1. 急落：前日比 -8%以上（1銘柄につき1日1回）
  2. 52週高値からの下げ：-10% / -15% / -20% の段階ごとに1回だけ
  3. 配当利回り（Diego式だけ）：3.5% / 4.0% を超えたとき、段階ごとに1回だけ
  4. 相場全体の下げ：日経平均が -3%以上の日に、対象銘柄の下げ幅ランキングを1通（1日1回）
- 「段階ごとに1回だけ」のやり直し：その段階より 3ポイント（利回りは0.2ポイント）戻ったら、次に同じ段階まで下がったときにもう一度知らせる
- 初めて動かした回は、いまの位置（すでに -10%以下の銘柄など）を1通にまとめて送り、そこから先の変化だけを知らせる
- 急落した銘柄には、日経平均の動きと、その日の適時開示のタイトルを添える（相場全体の下げか、その銘柄だけの下げかを見分けるため）

数字のあつかい
- 株価は yfinance（Yahoo!ファイナンス。約20分遅れ）。配当で調整していない値を使う（証券アプリの前日比・52週高値と合わせるため）
- 52週高値は、過去1年の日足の高値（場中の高値）のいちばん高い値
- 配当利回りは、スプシのDiego式タブにある1株配当 ÷ いまの株価
- 売買のすすめは書かない。事実（株価・下げ幅・利回り・開示のタイトル）だけを並べる
"""
import datetime
import io
import json
import os
import re
import time
import traceback
import unicodedata

import requests

STATE_FILE = "alert_state.json"
FALLBACK_FILE = "alert_watchlist.json"  # スプシを読めないときに使う銘柄一覧（あれば）
SHEET_ID = (os.environ.get("WATCHLIST_SHEET_ID") or "").strip()
DIEGO_SHEET = (os.environ.get("ALERT_DIEGO_SHEET") or "Diego式").strip()
GROWTH_SHEET = (os.environ.get("ALERT_GROWTH_SHEET") or "成長株スクリーニング").strip()
DIEGO, GROWTH = "Diego", "成長"
DIEGO_EXCLUDE_PREFIX = "×"  # Diego式タブの判定がこれで始まる銘柄（条件外）は対象にしない


def _numbers(name, default):
    """Variables の「10,15,20」のような指定を数字の並びにする。空や読めない値なら初期値"""
    try:
        values = sorted(float(x) for x in (os.environ.get(name) or "").replace("、", ",").split(",") if x.strip())
        return tuple(values) or default
    except ValueError:
        return default


DROP_PCT = _numbers("ALERT_DROP_PCT", (8.0,))[0]  # 急落：前日比がこの％以上の下げ
DD_LEVELS = _numbers("ALERT_DD_LEVELS", (10.0, 15.0, 20.0))  # 52週高値からの下げの段階（％）
YIELD_LEVELS = _numbers("ALERT_YIELD_LEVELS", (3.5, 4.0))  # 配当利回りの段階（％）
MARKET_PCT = _numbers("ALERT_MARKET_PCT", (3.0,))[0]  # 相場全体の下げ：日経平均がこの％以上の下げ
DD_RESET = 3.0  # 段階よりこれだけ戻ったら、その段階の「通知済み」を外す（ポイント）
YIELD_RESET = 0.2
MARKET_WEAK_PCT = 1.5  # 日経平均がこれ以上下げていたら「相場全体も下げ」と書く
RANKING_COUNT = 10

# (目標の時刻, これより遅れたら見送る時刻, 見送る時刻は次の日か)
SLOTS = {
    "midday": (datetime.time(11, 52), datetime.time(14, 30), False),  # 前場の引け（11:30）のデータが入るころ
    "close": (datetime.time(15, 55), datetime.time(8, 30), True),  # 引け（15:30）のデータが入るころ。次の日の寄り付き前まで有効
}
EARLY_LIMIT_MIN = 300  # 目標よりこれ以上早く起動した回は、何もしないで終わる（あとの回に任せる）
MAX_RUN_MIN = 320  # 1回の起動で動ける長さ（GitHub Actionsは1回6時間まで）
READY_RATIO = 0.5  # 対象のうち、これ以上の銘柄でその日の株価が取れたら「データが入った」とみなす
READY_RETRIES = 3
READY_WAIT_SEC = 600
LINE_MAX_CHARS = 4500
WEEKDAYS = "月火水木金土日"


class WatchlistError(Exception):
    """対象の銘柄一覧を読めなかった"""


# ---------------------------------------------------------------
# 1. 対象の銘柄（スプシのDiego式タブ・成長株スクリーニングタブ）
# ---------------------------------------------------------------
def clean_code(value):
    """証券コードの書き方をそろえる（4783.0 → "4783"、"208a" → "208A"）。コードでなければ空"""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().upper()
    text = re.sub(r"\.0$", "", text)
    return text if re.fullmatch(r"\d{3}[\dA-Z]", text) else ""


def short_name(name):
    """LINEで読みやすいように、全角の英数字を半角にして、長い社名を縮める"""
    text = unicodedata.normalize("NFKC", str(name or "")).strip()
    text = re.sub(r"\s+", " ", text).replace("ホールディングス", "HD").replace("フィナンシャルグループ", "FG")
    return text if len(text) <= 16 else text[:15] + "…"


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN は None


def _table(ws):
    """シートの見出し行（「コード」がある行）を探し、(見出し, その下の行たち) を返す"""
    rows = ws.iter_rows(values_only=True)
    for _ in range(6):
        row = next(rows, None)
        if row is None:
            break
        header = [str(c).strip() if c is not None else "" for c in row]
        if "コード" in header:
            return header, rows
    raise WatchlistError(f"「{ws.title}」タブに「コード」の列が見つかりません")


def _col(header, *names, prefix=""):
    for i, label in enumerate(header):
        if label in names or (prefix and label.startswith(prefix)):
            return i
    return None


def parse_workbook(wb):
    """スプシ（Excel形式）から、{コード: {name, groups, div, verdict}} を作る。タブが無ければ WatchlistError"""
    stocks = {}
    for sheet, group in ((DIEGO_SHEET, DIEGO), (GROWTH_SHEET, GROWTH)):
        if sheet not in wb.sheetnames:
            raise WatchlistError(f"スプシに「{sheet}」タブがありません（タブ名を変えたときは Variables の ALERT_DIEGO_SHEET / ALERT_GROWTH_SHEET で指定）")
        header, rows = _table(wb[sheet])
        i_code, i_name = _col(header, "コード"), _col(header, "銘柄名", "企業名")
        i_verdict = _col(header, "判定") if group == DIEGO else None
        i_div = _col(header, prefix="1株配当") if group == DIEGO else None
        count = 0
        for row in rows:
            code = clean_code(row[i_code]) if len(row) > i_code else ""
            if not code:
                continue
            verdict = str(row[i_verdict] or "").strip() if i_verdict is not None and len(row) > i_verdict else ""
            if group == DIEGO and verdict.startswith(DIEGO_EXCLUDE_PREFIX):
                continue
            stock = stocks.setdefault(code, {"name": "", "groups": [], "div": None, "verdict": ""})
            name = row[i_name] if i_name is not None and len(row) > i_name else ""
            stock["name"] = stock["name"] or short_name(name) or code
            if group not in stock["groups"]:
                stock["groups"].append(group)
            if group == DIEGO:
                stock["verdict"] = verdict
                div = _number(row[i_div]) if i_div is not None and len(row) > i_div else None
                stock["div"] = div if div and div > 0 else None
            count += 1
        print(f"値下がりアラートの対象（{sheet}）: {count}件")
    return stocks


def load_watchlist():
    """スプシから対象の銘柄を読む。読めなければ、リポジトリの alert_watchlist.json（あれば）を使う"""
    error = "WATCHLIST_SHEET_ID が未設定です"
    if SHEET_ID:
        try:
            import openpyxl

            res = requests.get(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=xlsx", timeout=60)
            res.raise_for_status()
            if not res.content.startswith(b"PK"):
                raise WatchlistError("スプシがExcel形式で返ってきません（共有が「リンクを知っている全員・閲覧者」になっていない）")
            stocks = parse_workbook(openpyxl.load_workbook(io.BytesIO(res.content), read_only=True, data_only=True))
            if stocks:
                return stocks
            error = "スプシに対象の銘柄がありません"
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:160]}"
            if "401" in error or "403" in error:
                error = "スプシの共有が「リンクを知っている全員・閲覧者」になっていません（401/403）"
    print("スプシから対象の銘柄を読めませんでした:", error)
    try:
        with open(FALLBACK_FILE, encoding="utf-8") as f:
            rows = json.load(f)
        stocks = {}
        for row in rows:
            code = clean_code(row.get("code"))
            if code:
                div = _number(row.get("div"))
                stocks[code] = {"name": short_name(row.get("name")) or code, "groups": list(row.get("groups") or [GROWTH]), "div": div, "verdict": ""}
        if stocks:
            print(f"{FALLBACK_FILE} の銘柄一覧を使います: {len(stocks)}件")
            return stocks
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"{FALLBACK_FILE} も読めませんでした:", e)
    raise WatchlistError(error)


# ---------------------------------------------------------------
# 2. 株価（その日の値・前日比・52週高値）
# ---------------------------------------------------------------
def fetch_bars(tools, symbol):
    """過去1年の日足（配当で調整しない値）。失敗したら1回だけやり直す"""
    for attempt in range(2):
        try:
            hist = tools.yf.Ticker(symbol).history(period="1y", auto_adjust=False)
            return hist.dropna(subset=["Close"])
        except Exception as e:
            if attempt:
                print(f"株価の取得に失敗 ({symbol}):", type(e).__name__, str(e)[:120])
            else:
                time.sleep(2)
    return None


def quote(hist, day=None):
    """日足から {day, price, change, high, dd} を作る。day を渡したときは、その日の値が入っているときだけ返す
    change: 前日比（％）/ high: 52週高値 / dd: 52週高値からの下げ（％。下がっているほど大きい正の数）"""
    if hist is None or len(hist) < 2:
        return None
    last_day = hist.index[-1].date()
    if day is not None and last_day != day:
        return None
    price, prev = float(hist["Close"].iloc[-1]), float(hist["Close"].iloc[-2])
    if price <= 0 or prev <= 0:
        return None
    highs = hist["High"] if "High" in hist else hist["Close"]
    high = max(float(highs.max()), float(hist["Close"].max()))
    return {"day": last_day, "price": price, "change": (price - prev) / prev * 100, "high": high, "dd": (high - price) / high * 100}


def fetch_quotes(tools, stocks, day=None):
    quotes = {}
    for code in stocks:
        q = quote(fetch_bars(tools, f"{code}.T"), day)
        if q:
            quotes[code] = q
    return quotes


# ---------------------------------------------------------------
# 3. 段階の判定（同じ段階は1回だけ。戻ったらやり直す）
# ---------------------------------------------------------------
def stage(value, levels):
    """value が届いている、いちばん深い段階。届いていなければ 0"""
    reached = [level for level in levels if value >= level]
    return max(reached) if reached else 0


def settle(recorded, value, levels, reset):
    """前に記録した段階から、value が reset 以上戻っていたら、その段階の記録を外す"""
    kept = [level for level in levels if level <= recorded and value >= level - reset]
    return max(kept) if kept else 0


def step(recorded, value, levels, reset):
    """(新しい記録, 知らせる段階 or None) を返す"""
    recorded = settle(recorded or 0, value, levels, reset)
    now = stage(value, levels)
    if now > recorded:
        return now, now
    return recorded, None


# ---------------------------------------------------------------
# 4. 記録
# ---------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    for key in ("done", "dd", "yield", "drop"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


# ---------------------------------------------------------------
# 5. 文章
# ---------------------------------------------------------------
def price_text(price):
    return f"{price:,.0f}円" if price >= 1000 else f"{price:,.1f}円".replace(".0円", "円")


def pct(value):
    return f"{value:+.1f}%".replace("+", "＋").replace("-", "−")


def level_text(level):
    return f"{level:g}"


def label(code, stock):
    return f"{stock['name']}({code})［{'・'.join(stock['groups'])}］"


def yield_of(stock, q):
    return stock["div"] / q["price"] * 100 if stock.get("div") and DIEGO in stock["groups"] else None


def detail(stock, q):
    """高値からの下げと利回りを1行に"""
    parts = [f"高値から {pct(-q['dd'])}"]
    y = yield_of(stock, q)
    if y:
        parts.append(f"利回り {y:.2f}%")
    return "｜".join(parts)


def market_note(nikkei):
    if not nikkei:
        return "日経平均は取得できず"
    if nikkei["change"] <= -MARKET_WEAK_PCT:
        return f"相場全体も下げ（日経平均 {pct(nikkei['change'])}）"
    return f"この銘柄だけの下げ（日経平均 {pct(nikkei['change'])}）。悪い材料が出ていないか確認"


def evaluate(stocks, quotes, state, day, nikkei, disclosures=None):
    """条件に当たったものを文章にして返す。state の記録もここで書きかえる。戻り値は行のリスト（無ければ空）"""
    disclosures = disclosures or {}
    iso = day.isoformat()
    first = not state.get("seeded")
    drops, dd_hits, yield_hits = [], [], []
    for code, stock in stocks.items():
        q = quotes.get(code)
        if not q:
            continue
        # 52週高値からの下げ
        new_code = code not in state["dd"]
        state["dd"][code], hit = step(state["dd"].get(code), q["dd"], DD_LEVELS, DD_RESET)
        if hit and not (first or new_code):  # 初回と、あとから足した銘柄は、いまの位置を記録するだけ
            dd_hits.append((hit, q["dd"], f"・−{level_text(hit)}%に到達：{label(code, stock)} {price_text(q['price'])}（高値 {price_text(q['high'])} から {pct(-q['dd'])}）"))
        # 配当利回り（Diego式だけ）
        y = yield_of(stock, q)
        if y:
            new_code = code not in state["yield"]
            state["yield"][code], hit = step(state["yield"].get(code), y, YIELD_LEVELS, YIELD_RESET)
            if hit and not (first or new_code):
                yield_hits.append((hit, y, f"・{level_text(hit)}%超え：{label(code, stock)} 利回り {y:.2f}%（株価 {price_text(q['price'])}・1株配当 {stock['div']:g}円）"))
        # 急落
        if q["change"] <= -DROP_PCT and state["drop"].get(code) != iso:
            state["drop"][code] = iso
            lines = [f"・{label(code, stock)} {price_text(q['price'])} {pct(q['change'])}", f"　{detail(stock, q)}", f"　{market_note(nikkei)}"]
            titles = disclosures.get(code) or []
            if titles:
                lines.append("　今日の開示：" + "／".join(f"「{t[:40]}」" for t in titles[:2]))
            drops.append((q["change"], lines))

    out = []
    if drops:
        out += ["", f"【急落：前日比 −{level_text(DROP_PCT)}%以上】"]
        for _, lines in sorted(drops, key=lambda d: d[0]):
            out += lines
    if dd_hits:
        out += ["", "【52週高値からの下げ】"] + [text for _, _, text in sorted(dd_hits, key=lambda h: (-h[0], -h[1]))]
    if yield_hits:
        out += ["", "【配当利回り（Diego式）】"] + [text for _, _, text in sorted(yield_hits, key=lambda h: (-h[0], -h[1]))]
    if nikkei and nikkei["change"] <= -MARKET_PCT and state.get("market") != iso:
        state["market"] = iso
        ranked = sorted(((quotes[c]["change"], c) for c in stocks if c in quotes), key=lambda r: r[0])[:RANKING_COUNT]
        out += ["", f"【相場全体の下げ：日経平均 {pct(nikkei['change'])}】", f"対象{len(quotes)}銘柄の下げ幅ランキング"]
        for i, (change, code) in enumerate(ranked, 1):
            out.append(f"{i}. {label(code, stocks[code])} {price_text(quotes[code]['price'])} {pct(change)}｜{detail(stocks[code], quotes[code])}")
    return out


def position_lines(stocks, quotes):
    """初回に送る「いまの位置」。すでに段階に届いている銘柄を並べる"""
    lines = ["", "■ いまの位置（ここから深くなったら知らせます）"]
    bounds = sorted(DD_LEVELS, reverse=True)
    for i, level in enumerate(bounds):
        upper = bounds[i - 1] if i else None
        rows = sorted(
            ((quotes[c]["dd"], c) for c in stocks if c in quotes and quotes[c]["dd"] >= level and (upper is None or quotes[c]["dd"] < upper)),
            reverse=True,
        )
        title = f"52週高値から −{level_text(level)}%以下" if upper is None else f"52週高値から −{level_text(level)}〜−{level_text(upper)}%"
        lines += ["", f"【{title}】{len(rows)}銘柄"] + [f"・{label(c, stocks[c])} {pct(-dd)}" for dd, c in rows]
    lines.append("")
    yields = {c: yield_of(stocks[c], quotes[c]) for c in stocks if c in quotes}
    yields = {c: y for c, y in yields.items() if y}
    y_bounds = sorted(YIELD_LEVELS, reverse=True)
    for i, level in enumerate(y_bounds):
        upper = y_bounds[i - 1] if i else None
        rows = sorted(((y, c) for c, y in yields.items() if y >= level and (upper is None or y < upper)), reverse=True)
        title = f"配当利回り {level_text(level)}%以上" if upper is None else f"配当利回り {level_text(level)}〜{level_text(upper)}%"
        names = "、".join(f"{stocks[c]['name']} {y:.1f}%" for y, c in rows)
        lines.append(f"【{title}（Diego式）】{len(rows)}銘柄" + (f"：{names}" if names else ""))
    return lines


def header_line(slot, day, now, nikkei):
    when = {"midday": f"昼（{now:%H:%M}時点）", "close": "引け後（終値）"}.get(slot, f"手動（{now:%H:%M}時点）")
    head = f"📉 値下がりアラート {day.month}/{day.day}（{WEEKDAYS[day.weekday()]}）{when}"
    if nikkei:
        head += f"\n日経平均 {nikkei['price']:,.0f}（{pct(nikkei['change'])}）"
    return head


def chunks(lines, limit=LINE_MAX_CHARS):
    """LINEの1通の上限に収まるように、行の区切りで分ける"""
    parts, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > limit:
            parts.append(current.rstrip("\n"))
            current = ""
        current += line + "\n"
    if current.strip():
        parts.append(current.rstrip("\n"))
    return parts


# ---------------------------------------------------------------
# 6. 1回分の処理
# ---------------------------------------------------------------
def disclosures_for(tools, codes, day):
    """その日の適時開示のタイトル（急落した銘柄の分だけ使う）。取れなくても続ける"""
    found = {}
    try:
        for item in tools.fetch_tdnet([day]):
            if item["code"] in codes:
                found.setdefault(item["code"], []).append(item["title"])
    except Exception as e:
        print("適時開示は取れませんでした:", e)
    return found


def process(tools, state, slot, day, now, sleep=time.sleep):
    """その回の通知を送る。その日の株価がまだ入っていなければ False（あとの回でやり直す）
    day が None（手動実行）のときは、取れた中でいちばん新しい日の値で動かす"""
    stocks = load_watchlist()
    quotes = {}
    for attempt in range(READY_RETRIES):
        quotes = fetch_quotes(tools, stocks, day)
        if len(quotes) >= max(1, len(stocks) * READY_RATIO) or day is None:
            break
        print(f"{day:%m/%d} の株価がまだ入っていません（{len(quotes)}/{len(stocks)}銘柄）")
        if attempt < READY_RETRIES - 1:
            sleep(READY_WAIT_SEC)
    else:
        return False
    if not quotes:
        return False
    if day is None:  # 手動実行：いちばん多くの銘柄がそろっている日を、その回の日付にする
        days = [q["day"] for q in quotes.values()]
        day = max(set(days), key=days.count)
        quotes = {c: q for c, q in quotes.items() if q["day"] == day}
    nikkei = quote(fetch_bars(tools, "^N225"), day)
    falling = {c for c, q in quotes.items() if q["change"] <= -DROP_PCT and state["drop"].get(c) != day.isoformat()}
    first = not state.get("seeded")
    body = evaluate(stocks, quotes, state, day, nikkei, disclosures_for(tools, falling, day) if falling else None)
    lines = []
    if first:
        counts = {g: sum(1 for s in stocks.values() if g in s["groups"]) for g in (DIEGO, GROWTH)}
        lines += [
            f"🔰 値下がりアラートを始めました（対象 {len(stocks)}銘柄：Diego式 {counts[DIEGO]}／成長株 {counts[GROWTH]}）",
            f"知らせる条件：前日比 −{level_text(DROP_PCT)}%以上／52週高値から −{'・−'.join(level_text(x) for x in DD_LEVELS)}%"
            f"／配当利回り {'・'.join(level_text(x) for x in YIELD_LEVELS)}%超え（Diego式）／日経平均 −{level_text(MARKET_PCT)}%以上の日",
        ] + position_lines(stocks, quotes)
        state["seeded"] = day.isoformat()
    if body or first:
        missing = len(stocks) - len(quotes)
        lines = [header_line(slot, day, now, nikkei)] + ([""] + lines if lines else []) + body
        if missing:
            lines += ["", f"※株価が取れなかった銘柄：{missing}"]
        lines += ["", "株価はYahoo!ファイナンス（約20分遅れ）。買う前に、決算と開示を確かめてください"]
        tools.push_line([{"type": "text", "text": part} for part in chunks(lines)])
        print(f"値下がりアラートを送りました（{slot} {day:%m/%d}）")
    else:
        print(f"値下がりアラート：知らせるものはありませんでした（{slot} {day:%m/%d}・{len(quotes)}銘柄）")
    return True


# ---------------------------------------------------------------
# 7. 起動のしかた（1時間おきに予約。起動できた回が、次の時刻まで待つ）
# ---------------------------------------------------------------
def next_slot(now, done, is_holiday):
    """まだ済んでいない回のうち、いちばん早いもの。戻り値は (回, 日付, 目標の時刻, 見送る時刻) か None"""
    today = now.date()
    yesterday = today - datetime.timedelta(days=1)
    for slot, day in (("close", yesterday), ("midday", today), ("close", today)):
        target_time, until_time, next_day = SLOTS[slot]
        target = datetime.datetime.combine(day, target_time, tzinfo=now.tzinfo)
        until = datetime.datetime.combine(day + datetime.timedelta(days=1 if next_day else 0), until_time, tzinfo=now.tzinfo)
        if is_holiday(day) or done.get(slot, "") >= day.isoformat() or now > until:
            continue
        return slot, day, target, until
    return None


def notify_once(tools, state, key, day, text):
    """同じ知らせは1日1回だけ送る（1時間おきの回が、同じ内容を何度も送らないように）"""
    if state.get(key) == day.isoformat():
        return
    state[key] = day.isoformat()
    try:
        tools.send_line_text(text)
    except Exception as e:
        print("LINEへの知らせに失敗:", e)


def run(tools, now_fn, scheduled, sleep=time.sleep):
    started = now_fn()
    state = load_state()
    if not scheduled:  # 手動実行は、待たずにいまの値で動かす
        try:
            process(tools, state, "manual", None, started, sleep)
        finally:
            save_state(state)
        return
    while True:
        now = now_fn()
        found = next_slot(now, state["done"], tools.is_market_holiday)
        if not found:
            print("値下がりアラート：この回に受け持つ分はありません")
            return
        slot, day, target, _ = found
        wait_sec = (target - now).total_seconds()
        if wait_sec > 0:
            if wait_sec > EARLY_LIMIT_MIN * 60 or (target - started).total_seconds() > MAX_RUN_MIN * 60:
                print(f"次の回（{target:%H:%M}）まで{wait_sec / 60:.0f}分あるので、この回はここで終わります（あとの回に任せます）")
                return
            print(f"{target:%H:%M}まで{wait_sec / 60:.0f}分待ちます")
            sleep(wait_sec)
            continue
        try:
            ok = process(tools, state, slot, day, now, sleep)
        except WatchlistError as e:
            print("対象の銘柄を読めませんでした:", e)
            notify_once(tools, state, "warned", now.date(), f"⚠️ 値下がりアラート：対象の銘柄を読めませんでした。\n{e}\n\nスプシ「気になるテーマ株_一覧表」の共有と、タブ名（{DIEGO_SHEET}／{GROWTH_SHEET}）を確認してください。")
            ok = True  # 読めるようになるまで、この回の分は済みにする（1時間おきに同じ知らせを送らない）
        except Exception as e:
            traceback.print_exc()
            notify_once(tools, state, "errored", now.date(), f"⚠️ 値下がりアラートでエラーが起きました\n{type(e).__name__}: {str(e)[:300]}\n\nGitHub Actionsのログを確認してください。")
            save_state(state)
            return
        if not ok:
            print("その日の株価がまだ入っていないので、あとの回でやり直します")
            save_state(state)
            return
        state["done"][slot] = day.isoformat()
        save_state(state)
