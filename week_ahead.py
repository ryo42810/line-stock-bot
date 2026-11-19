"""日曜の17:30に送る「来週の予定」（月〜金）

AIは使わず、公式の予定表と暦から作る（事実だけ）。
- 決算発表：JPX（日本取引所グループ）の決算発表予定表から、日ごとの社数と主な会社
  （ウォッチ・保有銘柄 → 時価総額ランキング上位 の順）
- 月末権利の権利付き最終日・権利落ち日（来週に入るとき）
- SQ（毎月第2金曜。3・6・9・12月はメジャーSQ）
- 来週の休場日
"""
import datetime

import earnings_calendar
import news_posts

WEEKDAYS = "月火水木金土日"
PER_DAY = 6  # 1日に名前を出す会社の数


def next_week(today):
    """today の次の月曜〜金曜"""
    monday = today + datetime.timedelta(days=(7 - today.weekday()) % 7 or 7)
    return [monday + datetime.timedelta(days=i) for i in range(5)]


def fmt(day):
    return f"{day.month}/{day.day}（{WEEKDAYS[day.weekday()]}）"


def second_friday(year, month):
    first = datetime.date(year, month, 1)
    return first + datetime.timedelta(days=(4 - first.weekday()) % 7 + 7)


def earnings_lines(tools, days, watch):
    rank = news_posts.cap_rank(tools)
    unknown = len(rank) + 1
    lines = []
    for day in days:
        rows = earnings_calendar.scheduled_on(day, watch)
        if not rows:
            continue
        rows.sort(key=lambda r: (r["code"] not in watch, rank.get(r["code"], unknown)))
        names = "、".join(f"{'★' if r['code'] in watch else ''}{r['name']}" for r in rows[:PER_DAY])
        more = f" ほか{len(rows) - PER_DAY}社" if len(rows) > PER_DAY else ""
        lines.append(f"・{fmt(day)} {len(rows)}社：{names}{more}")
    return lines


def schedule_lines(tools, days):
    items = []  # (日付, 文)
    for day in days:
        if tools.is_market_holiday(day):
            items.append((day, f"・{fmt(day)} 休場"))
    months = {(d.year, d.month) for d in days}
    for year, month in sorted(months):
        last = tools.kenri_last_day(year, month)
        if last in days:
            ochi = tools.next_business_day(last)
            items.append((last, f"・{fmt(last)} {month}月末権利の権利付き最終日（翌営業日 {fmt(ochi)} が権利落ち日）"))
        sq = second_friday(year, month)
        if sq in days and not tools.is_market_holiday(sq):
            items.append((sq, f"・{fmt(sq)} {'メジャーSQ' if month in (3, 6, 9, 12) else 'SQ'}（先物・オプションの清算日）"))
    return [text for _, text in sorted(items, key=lambda x: x[0])]


def build(tools, today, watch=()):
    """送る文章。予定が1つもなければ空"""
    days = next_week(today)
    watch = set(watch)
    earnings = earnings_lines(tools, days, watch)
    schedule = schedule_lines(tools, days)
    if not earnings and not schedule:
        return ""
    lines = [f"🗓 来週の予定（{fmt(days[0])}〜{fmt(days[-1])}）"]
    if schedule:
        lines += ["", "【日程】"] + schedule
    if earnings:
        lines += ["", "【決算発表】" + ("　★はウォッチ・保有銘柄" if any("★" in l for l in earnings) else "")] + earnings
    lines += ["", "出典：日本取引所グループ（決算発表予定）。日程は変わることがあるので、各社の発表で確認してください"]
    return "\n".join(lines)[:4900]


def run(tools, today):
    watch = set()
    try:
        watch = set(tools.earnings_watch.load_watchlist())  # 銘柄コード
    except Exception as e:
        print("ウォッチ銘柄（スプシ）は読めませんでした:", e)
    text = build(tools, today, watch)
    if text:
        tools.send_line_text(text)
        print("来週の予定を送りました")
    else:
        print("来週の予定はありませんでした")
