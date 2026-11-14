"""くじら枠：優待・高配当の投稿（平日の朝に1本。曜日ごとに型を替える）

アカウントの軸（株主優待・高配当）に合わせた投稿を作る。
- 月：整理　権利確定が近い人気の優待まとめ（3銘柄）
- 火：今日の1銘柄（優待）
- 水：解説　配当・優待の見方（個別の銘柄は出さない）
- 木：今日の1銘柄（高配当）
- 金：実体験　自分の売買・配当の記録。これは自分で書くもので、夕方のLINEに書く型だけ送る（自動投稿しない）

数字（利回り・最低投資額・権利確定日など）はYahoo!ファイナンスのページから取ったものをそのまま使い、
AIには文章の言い回しだけを任せる。AIが使えないときは、決まった型の文章で出す。
"""
import datetime
import json
import re
import unicodedata

import yfinance as yf

PLAN = {0: "matome", 1: "one_yutai", 2: "kaisetsu", 3: "one_haito"}
HASHTAGS = "#株主優待 #高配当株 #株クラ #NISA #投資家さんと繋がりたい"
FEATURE_COOLDOWN_DAYS = 45  # 同じ銘柄を「今日の1銘柄」に出すのは、この日数あけてから
FEATURE_KEEP_DAYS = 120

YUTAI_MONTH_URL = "https://finance.yahoo.co.jp/stocks/incentive/popular-ranking/all/month_{month}"
HAITO_URL = "https://finance.yahoo.co.jp/stocks/ranking/dividendYield?market=tokyo1&page={page}"
HAITO_PAGES = (3, 4, 5, 6, 7)  # 東証プライムの配当利回りランキング。1〜2ページ目は記念配当などの極端な銘柄が多いので使わない
HAITO_YIELD_RANGE = (3.5, 5.5)  # 高すぎる利回りは減配や株価急落の裏返しのことが多いので、この範囲に絞る
HAITO_MIN_MARKET_CAP = 100_000_000_000  # 時価総額1,000億円以上
HAITO_PAYOUT_RANGE = (0.1, 0.7)  # 配当性向10〜70%（無理のない配当かの目安）
HAITO_MAX_TRIES = 12

# 金曜の夕方にLINEへ送る、実体験の投稿の型（自分で埋めて投稿する）
DIARY_TEMPLATE = """📝 今週の記録（自分で書く投稿）
自分の売買・配当の話はbotでは作れないので、下の型を埋めてXに投稿してください。自動投稿はしません。

今週の記録。

・買った：〇〇（理由は〇〇）
・売った：〇〇（損益〇〇）
・届いた配当・優待：〇〇
・反省：〇〇

来週は〇〇を見ていく。

#株主優待 #高配当株 #株クラ"""

# 水曜の解説のテーマ。週ごとに順番に使う。facts に書いたことだけを材料にする
KAISETSU_TOPICS = [
    {
        "title": "配当利回りだけで選ぶと危ない理由",
        "facts": [
            "配当利回り＝1株あたりの年間配当÷株価。株価が下がると、配当が同じでも利回りは上がる",
            "業績が悪くて株価が下がった銘柄も、利回りランキングの上位に並ぶ",
            "記念配当や特別配当は1回きりのことが多く、翌年は利回りが下がる",
            "利回りと一緒に、配当性向や過去の減配の有無を見ると安心",
        ],
    },
    {
        "title": "権利付き最終日と権利落ち日",
        "facts": [
            "優待や配当をもらうには、権利付き最終日の取引終了までに株を買って持っておく必要がある",
            "権利付き最終日は、権利確定日の2営業日前",
            "権利付き最終日の翌営業日が権利落ち日。この日に売っても権利はもらえる",
            "権利落ち日は、配当や優待の分だけ株価が下がりやすい",
        ],
    },
    {
        "title": "配当性向の見方",
        "facts": [
            "配当性向＝配当の総額÷純利益。利益のうち何割を配当に回しているかを表す",
            "配当性向が高すぎると、利益が減ったときに配当を維持しにくい",
            "配当性向が100%を超えているのは、利益より多く配当を出している状態",
            "会社によっては、配当性向の目標を決算資料や中期経営計画で公表している",
        ],
    },
    {
        "title": "優待の改悪・廃止に気づくポイント",
        "facts": [
            "優待の変更や廃止は、会社が適時開示で発表する",
            "発表のタイトルに「株主優待制度の変更」「廃止」と入ることが多い",
            "優待をやめて配当を増やす形に切り替える会社もある",
            "優待目当てで買った銘柄は、廃止の発表で株価が大きく動くことがある",
        ],
    },
    {
        "title": "累進配当ってなに？",
        "facts": [
            "累進配当は、配当を減らさず、維持するか増やしていく方針のこと",
            "会社が方針として公表しているだけで、将来の配当が保証されるわけではない",
            "方針は中期経営計画や決算説明資料に書かれていることが多い",
            "連続増配の年数と合わせて見ると、配当への姿勢がわかりやすい",
        ],
    },
    {
        "title": "優待利回りの考え方",
        "facts": [
            "優待利回り＝1年間にもらえる優待の金額換算÷必要な投資額",
            "配当利回りと足したものを総合利回りと呼ぶことが多い",
            "優待をもらえる最低株数は銘柄ごとに違う。100株とは限らない",
            "自分が使わない優待は、金額換算が高くても実質の価値は低い",
        ],
    },
    {
        "title": "長期保有の優遇がある優待",
        "facts": [
            "一定期間以上持ち続けた株主だけが対象の優待や、長く持つと内容が増える優待がある",
            "長期保有の判定は、株主名簿に同じ株主番号で続けて載っているかで決まることが多い",
            "いったん全部売ると株主番号が変わり、保有期間が最初からになることがある",
            "条件は会社ごとに違うので、会社の優待ページで確認する",
        ],
    },
    {
        "title": "増配と自社株買いの違い",
        "facts": [
            "増配は、1株あたりの配当を増やすこと。株主は現金で受け取る",
            "自社株買いは、会社が市場から自分の株を買うこと。出回る株数が減り、1株あたりの利益が上がりやすい",
            "どちらも株主還元。配当と自社株買いを合わせた割合を総還元性向と呼ぶ",
            "自社株買いは「取得枠の発表」と「実際に買った結果の報告」が別々に開示される",
        ],
    },
]

ROW_PATTERN = re.compile(
    r"^(?P<rank>\d+) (?P<name>.+?) (?P<code>\d{3}[0-9A-Z]) (?P<market>東証\S*|名証\S*|札証\S*|福証\S*) .*?"
    r"最低投資金額 (?P<min_yen>[\d,.]+)万円 優待発生株数 (?P<shares>[\d,]+)株 "
    r"配当利回り（会社予想） (?P<yield>\S+) 権利確定日 (?P<kenri>\S+) <https://finance\.yahoo\.co\.jp/quote/(?P=code)\.T/incentive>",
    re.M,
)
HAITO_PATTERN = re.compile(
    r"(?P<name>[^\n<>]+) <https://finance\.yahoo\.co\.jp/quote/(?P<code>\d{3}[0-9A-Z])\.T>\n(?P=code)\n(?P<market>[^\n]+)\n"
    r"(?:掲示板[^\n]*\n)?(?P<price>[\d,.]+)\n[^\n]+\n(?P<fy>\d{4}/\d{2})\n(?P<div>[\d,.]+)\n\+(?P<yield>[\d.]+)"
)


def clean_name(name):
    """「(株)サンゲツ」「ＴＯＡ(株)」→「サンゲツ」「TOA」"""
    name = unicodedata.normalize("NFKC", name)
    return re.sub(r"\(株\)|\s+", "", name).strip()


def to_float(text):
    try:
        return float(str(text).replace(",", "").replace("%", "").replace("+", ""))
    except ValueError:
        return None


def parse_yutai_ranking(text):
    """優待人気ランキング（月別）のページから、銘柄ごとの数字を取り出す"""
    rows = []
    for m in ROW_PATTERN.finditer(text):
        rows.append(
            {
                "code": m["code"],
                "name": clean_name(m["name"]),
                "rank": int(m["rank"]),
                "min_yen": to_float(m["min_yen"]) * 10000,
                "min_shares": int(m["shares"].replace(",", "")),
                "page_yield": to_float(m["yield"]),  # 会社予想の配当利回り。「---」なら None
                "kenri_text": m["kenri"],
            }
        )
    return rows


def parse_haito_ranking(text):
    """配当利回りランキングのページから、銘柄ごとの数字を取り出す"""
    rows = []
    for m in HAITO_PATTERN.finditer(text):
        if "投資法人" in m["name"]:
            continue  # REITは株と配当の仕組みが違うので外す
        rows.append(
            {
                "code": m["code"],
                "name": clean_name(m["name"]),
                "page_price": to_float(m["price"]),
                "fiscal": m["fy"],
                "dividend": to_float(m["div"]),
                "page_yield": to_float(m["yield"]),
            }
        )
    return rows


def upcoming_months(tools, today, count):
    """権利付き最終日がまだ来ていない、近い順の権利確定月"""
    months = []
    y, m = today.year, today.month
    while len(months) < count:
        if tools.kenri_last_day(y, m) >= today:
            months.append((y, m))
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    return months


def recently_featured(state, today):
    cutoff = (today - datetime.timedelta(days=FEATURE_COOLDOWN_DAYS)).isoformat()
    return {code for code, day in state.get("featured", {}).items() if day >= cutoff}


def remember_featured(state, codes, today):
    keep = (today - datetime.timedelta(days=FEATURE_KEEP_DAYS)).isoformat()
    featured = {c: d for c, d in state.get("featured", {}).items() if d >= keep}
    featured.update({c: today.isoformat() for c in codes})
    state["featured"] = featured


def as_pick(row, category):
    return {
        "yutai": "不明", "kenri_month": "不明", "category": category, "headline": "", "source_url": "",
        **row,
        "page_min_shares": row.get("min_shares"),
    }


def enrich(tools, client, picks, today):
    """優待内容（優待ページ）と株価・テクニカルを付ける。ランキングの数字はページのものを優先する"""
    picks = tools.add_yutai_info(client, picks) if client is not None else picks
    picks = tools.add_price_info(picks, today)
    for p in picks:
        if p.get("page_min_shares"):
            p["min_shares"] = p["page_min_shares"]  # 優待をもらえる株数は、ランキングのページの数字を使う
        if p.get("page_yield") is not None:
            # 配当利回りは、ランキングに載っている会社予想ベースの数字にそろえる
            value, price = p.get("yutai_value_yen", 0), p.get("price")
            if value and price:
                y = value / (price * p.get("min_shares", 100)) * 100
                p["yield_fmt"] = f"配当{p['page_yield']:.1f}%＋優待{y:.1f}%＝{p['page_yield'] + y:.1f}%"
            else:
                p["yield_fmt"] = f"配当{p['page_yield']:.1f}%"
    return picks


def has_yutai(p):
    return p.get("yutai") not in ("なし", "不明", "", None)


def yutai_facts(tools, p, year, month, today):
    last = tools.kenri_last_day(year, month, p.get("kenri_day", 0))
    lines = [
        f"銘柄: {p['name']}({p['code']})",
        f"優待: {p['yutai']}",
        f"優待をもらえる最低株数: {p.get('min_shares', 100)}株（必要な金額の目安 約{p['min_yen'] / 10000:.1f}万円）" if p.get("min_yen") else "",
        f"利回り: {p['yield_fmt']}" if "不明" not in p.get("yield_fmt", "不明") and "取得" not in p.get("yield_fmt", "") else "",
        f"{month}月権利の権利付き最終日: {last:%m/%d}（あと{tools.business_days_between(today, last)}営業日）" if last >= today else "",
    ]
    return "\n".join(l for l in lines if l)


# ---------------------------------------------------------------
# 型ごとの材料づくり
# ---------------------------------------------------------------
def build_matome(tools, client, today, exclude):
    (year, month), = upcoming_months(tools, today, 1)
    text = tools.fetch_page_text(YUTAI_MONTH_URL.format(month=month), max_chars=60000)
    rows = [r for r in parse_yutai_ranking(text) if r["code"] not in exclude][:8]
    picks = [p for p in enrich(tools, client, [as_pick(r, "優待") for r in rows], today) if has_yutai(p)][:3]
    if len(picks) < 2:
        return {}
    for p in picks:
        p["headline"] = p["yutai"]
    last = tools.kenri_last_day(year, month)
    facts = f"テーマ: {month}月に権利確定の人気優待（優待ページの閲覧数ランキングの上位）\n月末権利の権利付き最終日: {last:%m/%d}\n\n"
    facts += "\n\n".join(yutai_facts(tools, p, year, month, today) for p in picks)
    blocks = [f"{month}月権利の人気優待、まとめた🎁↓↓"]
    for i, p in enumerate(picks):
        block = [f"{'①②③'[i]}{p['name']}({p['code']})", p["yutai"][:40]]
        if p.get("min_yen"):
            block.append(f"約{p['min_yen'] / 10000:.1f}万円から")
        blocks.append("\n".join(block))
    blocks.append(f"権利付き最終日は{last:%m/%d}⏰")
    return {
        "kind": "matome", "stocks": picks, "facts": facts, "fallback": "\n\n".join(blocks),
        "title": f"{month}月の人気優待", "subtitle": f"権利付き最終日は {last:%m/%d}",
        "rule": "3銘柄を①②③で並べる。それぞれ優待の中身と、必要な金額の目安を書く。最後に権利付き最終日を1行で書く",
    }


def build_one_yutai(tools, client, today, exclude):
    """今日の1銘柄（優待）：1〜2ヶ月先に権利確定する人気の優待から、まだ紹介していない銘柄"""
    months = upcoming_months(tools, today, 3)[1:]
    for year, month in months:
        text = tools.fetch_page_text(YUTAI_MONTH_URL.format(month=month), max_chars=60000)
        rows = [r for r in parse_yutai_ranking(text) if r["code"] not in exclude][:5]
        picks = [p for p in enrich(tools, client, [as_pick(r, "優待") for r in rows], today) if has_yutai(p)]
        if not picks:
            continue
        p = picks[0]
        cautions = []
        if p.get("min_shares", 100) > 100:
            cautions.append(f"優待は{p['min_shares']}株から。100株ではもらえない")
        if p.get("page_yield") is None:
            cautions.append("配当は会社予想が出ていない")
        gap = next((s for s in p.get("signals", []) if "25日線" in s and "（" in s), "")
        if gap:
            cautions.append(f"株価は{gap}")
        p["headline"] = p["yutai"]
        facts = yutai_facts(tools, p, year, month, today)
        facts += "\n気になる点: " + (" / ".join(cautions) if cautions else "優待は会社の判断で変更・廃止されることがある")
        lines = [f"今日の1銘柄は{p['name']}({p['code']})🎁", "", f"優待：{p['yutai'][:50]}", f"権利確定：{month}月"]
        if p.get("min_yen"):
            lines.append(f"必要な金額：約{p['min_yen'] / 10000:.1f}万円（{p.get('min_shares', 100)}株）")
        if cautions:
            lines += ["", f"気になる点は、{cautions[0]}⚠️"]
        total = p["yield_fmt"].split("＝")[-1].replace("配当", "").replace("%", "") if "%" in p.get("yield_fmt", "") else ""
        stats = [
            ("株価（前営業日終値）", f"{p['price']:,.0f}", "円"),
            ("利回り（配当＋優待）" if "＝" in p.get("yield_fmt", "") else "配当利回り", total, "%"),
            ("必要な金額の目安", f"{p['min_yen'] / 10000:.1f}" if p.get("min_yen") else "", "万円"),
            ("権利確定", f"{month}月", ""),
        ]
        last = tools.kenri_last_day(year, month, p.get("kenri_day", 0))
        table = [
            ("優待の中身", "", p["yutai"]),
            ("必要な金額", f"約{p['min_yen'] / 10000:.1f}万円" if p.get("min_yen") else "", f"{p.get('min_shares', 100)}株から優待がもらえる"),
            ("権利確定", f"{month}月", f"権利付き最終日は{last:%m/%d}（あと{tools.business_days_between(today, last)}営業日）"),
            ("配当利回り", f"{p['page_yield']:.2f}%", "会社予想の配当から計算") if p.get("page_yield") is not None else ("配当利回り", "", "会社予想が出ていない"),
            # 配当の行と同じ話を繰り返さないように、気になる点は配当以外のものを優先する
            ("気になる点", "", next((c for c in cautions if "配当は会社予想" not in c), "優待は会社の判断で変更・廃止されることがある")),
        ]
        return {
            "kind": "one_yutai", "stocks": [p], "facts": facts, "fallback": "\n".join(lines), "stats": stats, "table": table, "table_title": "優待を見るときのチェック表",
            "title": "今日の1銘柄", "subtitle": f"{month}月権利の株主優待",
            "rule": "1銘柄だけを紹介する。優待の中身→必要な金額→権利確定の時期→気になる点、の順。気になる点は必ず1行入れる",
        }
    return {}


def build_one_haito(tools, client, today, exclude):
    """今日の1銘柄（高配当）：東証プライムの利回り3.5〜5.5%から、規模と配当性向で絞る"""
    rows = []
    for page in HAITO_PAGES:
        rows += parse_haito_ranking(tools.fetch_page_text(HAITO_URL.format(page=page), max_chars=60000))
    low, high = HAITO_YIELD_RANGE
    rows = [r for r in rows if r["page_yield"] and low <= r["page_yield"] <= high and r["code"] not in exclude]
    print(f"高配当の候補: {len(rows)}銘柄")
    if not rows:
        return {}
    start = today.toordinal() % len(rows)  # 毎回ランキングの同じ位置から選ばないように、日付で開始位置をずらす
    for r in (rows[start:] + rows[:start])[:HAITO_MAX_TRIES]:
        try:
            info = yf.Ticker(f"{r['code']}.T").info
        except Exception as e:
            print(f"高配当の候補の情報取得失敗 ({r['code']}):", e)
            continue
        cap, payout = info.get("marketCap") or 0, info.get("payoutRatio")
        if cap < HAITO_MIN_MARKET_CAP or payout is None or not HAITO_PAYOUT_RANGE[0] <= payout <= HAITO_PAYOUT_RANGE[1]:
            continue
        picks = enrich(tools, client, [as_pick(r, "高配当")], today)
        if not picks:
            continue
        p = picks[0]
        avg5, pbr = info.get("fiveYearAvgDividendYield"), info.get("priceToBook")
        p.update({"payout": payout * 100, "avg5_yield": avg5, "pbr": pbr})
        cautions = []
        if avg5 and p["page_yield"] > avg5 + 0.3:
            cautions.append(f"利回りは過去5年平均の{avg5:.1f}%より高い。株価が下がって利回りが上がった面もある")
        if payout > 0.55:
            cautions.append(f"配当性向は{payout * 100:.0f}%でやや高め")
        gap = next((s for s in p.get("signals", []) if "25日線" in s and "（" in s), "")
        if gap:
            cautions.append(f"株価は{gap}")
        if not cautions:
            cautions.append("配当は会社予想で、業績しだいで変わる")
        p["headline"] = f"1株配当{r['dividend']:g}円（会社予想）・配当性向{payout * 100:.0f}%"
        facts_lines = [
            f"銘柄: {p['name']}({p['code']})",
            f"配当利回り（会社予想）: {p['page_yield']:.2f}%",
            f"1株配当（会社予想・{r['fiscal']}期）: {r['dividend']:g}円",
            f"配当性向: {payout * 100:.0f}%",
            f"過去5年の平均利回り: {avg5:.1f}%" if avg5 else "",
            f"PBR: {pbr:.2f}倍" if pbr else "",
            f"100株の金額の目安: 約{p['price'] * 100 / 10000:.1f}万円",
            f"優待: {p['yutai']}" if has_yutai(p) else "優待: なし",
            "気になる点: " + " / ".join(cautions),
        ]
        lines = [
            f"今日の1銘柄は{p['name']}({p['code']})💰", "",
            f"配当利回り{p['page_yield']:.2f}%（会社予想）", f"1株配当は{r['dividend']:g}円", f"配当性向{payout * 100:.0f}%",
            "", f"気になる点は、{cautions[0]}⚠️",
        ]
        stats = [
            ("株価（前営業日終値）", f"{p['price']:,.0f}", "円"),
            ("配当利回り（会社予想）", f"{p['page_yield']:.2f}", "%"),
            ("1株配当（会社予想）", f"{r['dividend']:g}", "円"),
            ("配当性向", f"{payout * 100:.0f}", "%"),
        ]
        per = info.get("trailingPE")
        if avg5:
            diff = p["page_yield"] - avg5
            yield_note = f"過去5年の平均は{avg5:.1f}%（いまは{abs(diff):.1f}ポイント{'高い' if diff > 0 else '低い'}）"
        else:
            yield_note = "会社予想の配当から計算"
        valuation = " ／ ".join(v for v in (f"{pbr:.2f}倍" if pbr else "", f"{per:.1f}倍" if per else "") if v)
        table = [
            ("利回りの水準", f"{p['page_yield']:.2f}%", yield_note),
            ("配当性向", f"{payout * 100:.0f}%", f"利益の約{payout * 10:.0f}割を配当に回している"),
            ("PBR ／ PER" if pbr and per else "PBR" if pbr else "PER", valuation, "純資産・利益に対する株価の水準"),
            ("株主優待", "", p["yutai"]) if has_yutai(p) else ("会社の規模", f"{cap / 1e8:,.0f}億円", "時価総額（東証プライム）"),
            ("気になる点", "", cautions[0]),
        ]
        return {
            "kind": "one_haito", "stocks": [p], "facts": "\n".join(l for l in facts_lines if l), "fallback": "\n".join(lines), "stats": stats, "table": table, "table_title": "配当を見るときのチェック表",
            "title": "今日の1銘柄", "subtitle": "高配当株",
            "rule": "1銘柄だけを紹介する。配当利回り→1株配当→配当性向→気になる点、の順。気になる点は必ず1行入れる。優待があれば一言足す",
        }
    return {}


def build_kaisetsu(today):
    topic = KAISETSU_TOPICS[(today.toordinal() // 7) % len(KAISETSU_TOPICS)]
    facts = f"テーマ: {topic['title']}\n" + "\n".join(f"- {f}" for f in topic["facts"])
    lines = [f"{topic['title']}、3行でまとめた📝↓↓", ""] + [f"・{f}" for f in topic["facts"][:3]]
    return {
        "kind": "kaisetsu", "stocks": [], "facts": facts, "fallback": "\n".join(lines),
        "rule": "個別の銘柄名は出さない。初心者がつまずく点を、材料に書いてあることだけで3〜4行にまとめる。①②③は使わず、短い文を改行で並べる",
    }


# ---------------------------------------------------------------
# 文章づくり
# ---------------------------------------------------------------
POST_SCHEMA = {
    "type": "object",
    "properties": {"post": {"type": "string", "description": "X投稿の本文。ハッシュタグは付けない"}},
    "required": ["post"],
    "additionalProperties": False,
}


def numbers_ok(post, facts):
    """AIの文章に、材料に無い数字が混ざっていないか（混ざっていたら決まった型の文章に切り替える）"""
    allowed = set(re.findall(r"\d+(?:\.\d+)?", unicodedata.normalize("NFKC", facts).replace(",", "")))
    used = re.findall(r"\d+(?:\.\d+)?", unicodedata.normalize("NFKC", post).replace(",", ""))
    extra = [n for n in used if n not in allowed and n not in ("1", "2", "3", "4")]
    if extra:
        print(f"くじら枠: 材料に無い数字が入っていたため、決まった型の文章を使います: {extra}")
    return not extra


def write_post(tools, client, model, item, today):
    """材料（facts）をもとに、アカウントの口調で投稿文を書く。失敗したら決まった型の文章を返す"""
    if client is None:
        return item["fallback"]
    prompt = f"""株主優待・高配当を発信しているXアカウントの投稿を1本書きます。
下の「材料」に書いてある事実と数字だけを使ってください。材料に無い数字・ニュース・評価は書かないでください。

## この投稿の型
- {item['rule']}
- 「買い」「おすすめ」のような売買のすすめは書かない。事実と、見るときのポイントを書く

{tools.X_POST_STYLE}

### この投稿だけの決まり
- 上の「銘柄は3つ」の決まりより、「この投稿の型」を優先する
- 1行目のフックの型: {tools.X_HOOK_TYPES[(today.toordinal() + 2) % len(tools.X_HOOK_TYPES)]}

## 材料
{item['facts']}"""
    try:
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            output_config={"format": {"type": "json_schema", "schema": POST_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        tools.track_usage(response)
        if response.stop_reason == "refusal":
            raise RuntimeError("拒否されました")
        post = json.loads(next(b.text for b in response.content if b.type == "text"))["post"].strip()
    except Exception as e:
        print("くじら枠の文章づくりに失敗。決まった型の文章を使います:", e)
        return item["fallback"]
    codes = set(re.findall(r"\((\d{3}[0-9A-Z])\)", post))
    if not post or codes - {s["code"] for s in item["stocks"]} or not numbers_ok(post, item["facts"]):
        return item["fallback"]
    return post


def build(tools, client, model, today, state, exclude=(), held=()):
    """今日のくじら枠の投稿を作る。戻り値は {kind, post, stocks, title, subtitle}。作らない日・作れなかった日は {}
    exclude: 今朝ほかの投稿で使う銘柄（重ねない） / held: 自分が保有している銘柄（投稿に「保有中」と書く）"""
    kind = PLAN.get(today.weekday())
    if not kind:
        return {}
    skip = set(exclude) | (recently_featured(state, today) if kind.startswith("one_") else set())
    try:
        if kind == "matome":
            item = build_matome(tools, client, today, set(exclude))
        elif kind == "one_yutai":
            item = build_one_yutai(tools, client, today, skip)
        elif kind == "one_haito":
            item = build_one_haito(tools, client, today, skip)
        else:
            item = build_kaisetsu(today)
    except Exception as e:
        print(f"くじら枠（{kind}）の材料づくりに失敗:", e)
        return {}
    if not item:
        print(f"くじら枠（{kind}）: 条件に合う銘柄が見つからなかったので、今日は出しません")
        return {}
    post = tools.format_x_post(write_post(tools, client, model, item, today))
    # 保有している銘柄を紹介するときは、必ず「保有中」と書く（ポジショントークと受け取られないように）
    if any(s["code"] in held for s in item["stocks"]):
        post = post.rstrip() + "\n\n※この中に保有中の銘柄があります" if len(item["stocks"]) > 1 else post.rstrip() + "\n\n※保有中の銘柄です"
    item["post"] = tools.add_hashtags(post, HASHTAGS)
    if kind.startswith("one_"):
        remember_featured(state, [s["code"] for s in item["stocks"]], today)
    print(f"くじら枠（{kind}）: {[s['code'] for s in item['stocks']] or item['facts'].splitlines()[0]}")
    return item
