"""朝の市況（おはよう投稿）：毎朝いちばん最初に、主要12指数の一覧画像を付けて投稿する

2026/11/24に追加。「世界の株価」（https://nikkei225jp.com/）の「リアルタイム主要株価指数」を
3列表示にして、下の12枚だけを並べた状態で撮影し、おはようのあいさつと一緒に出す。
- 出す時刻：毎日 6:12ごろ（日ごとに数分ずらす。x_queue.slot_time）。朝6:50ごろの株ニュースより前
  （米国市場は日本時間の6:00に閉まる〔夏時間は5:00〕ので、前日の終値がそろってから撮る）
- 7:40を過ぎても出せなかった日は見送る（おはようの時間を過ぎるので）
- x_queue.py の回が、待ちから戻るたびに prepare を呼び、今日の分の「枠」を予約に並べる。
  画像と文章は、出す時刻になってから build で作る（撮るのが早すぎると、前日の終値が入っていないため）
- 文章はAI（Opus）が数字を見て書く。材料に無い数字が混ざったら、決まった型の文章に切り替える

サイトの規約（2026/11/24確認）：運営者情報のページに、画像の転載・SNS投稿・自動取得についての記載は無い。
著作権表示（Stockbrain Ltd.）はある。運営会社には別途確認する予定。止めるときは ENABLED を False にする。
"""
import importlib
import json
import os
import re
import subprocess
import sys
import unicodedata

import x_queue

ENABLED = True  # 止めるときは False（予約の枠を並べなくなる）
KIND = "board"  # x_queue.json の予約で、出す時刻に作る投稿の印
BASE, UNTIL = "06:12", "07:40"  # （出す時刻のもと, これより遅れたら見送る）
URL = "https://nikkei225jp.com/"
HASHTAGS = "#朝活"  # 朝の市況だけは「#朝活」の1つ（2026/11/24に決定。ほかの投稿は #株 #株クラ）
PLAYWRIGHT_VERSION = "1.63.0"
IMAGE_NAME = "board"
MAX_CHARS = 120  # ハッシュタグを除いた本文の長さの上限（Xは日本語140字まで）

# 並べる12枚（3列×4段）。コードはサイトの「詳細ポップアップ」のリンク（/_ssi/if/?c=コード）の番号
TILES = [
    ("111", "日経平均"), ("112", "TOPIX"), ("121", "グロース250"),
    ("751", "日経の夜間（DEX）"), ("511", "ドル円"), ("151", "日本10年債利回り"),
    ("211", "NYダウ"), ("213", "S&P500"), ("212", "NASDAQ"),
    ("611", "SOX指数"), ("621", "VIX"), ("811", "米国10年債利回り"),
]

POST_SCHEMA = {
    "type": "object",
    "properties": {"post": {"type": "string", "description": "X投稿の本文。ハッシュタグは付けない"}},
    "required": ["post"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------
# 予約の枠（出す時刻になってから中身を作る）
# ---------------------------------------------------------------
def slot(day):
    return x_queue.slot_time(day, BASE)


def prepare(now):
    """x_queue.py の回が、待ちから戻るたびに呼ぶ。今日の枠がまだ無ければ、予約に並べる（中身は空。出す時刻に build で作る）"""
    if not ENABLED:
        return
    today = now.date()
    if now > x_queue._at(today, UNTIL, now.tzinfo):
        return
    items = x_queue.load()
    if any(i.get("date") == today.isoformat() and i.get("kind") == KIND for i in items):
        return
    x_queue.sync()
    items = x_queue.load()
    if any(i.get("date") == today.isoformat() and i.get("kind") == KIND for i in items):
        return
    items.append({"date": today.isoformat(), "time": slot(today), "until": UNTIL, "post": "", "image": "", "status": "pending", "kind": KIND})
    print(f"Xの予約投稿に追加: {slot(today)}（朝の市況。画像と文章は出す時刻に作ります）")
    x_queue.save(items, today)
    x_queue.push(f"朝の市況の枠を予約に追加（{today:%m/%d}）")


# ---------------------------------------------------------------
# サイトの撮影と、数字の読み取り
# ---------------------------------------------------------------
def _playwright():
    """playwright を使えるようにする（ふだんの回では使わないので、requirements.txt には入れず、ここで入れる）"""
    try:
        return importlib.import_module("playwright.sync_api")
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", f"playwright=={PLAYWRIGHT_VERSION}"], check=True, timeout=300)
        importlib.invalidate_caches()
        return importlib.import_module("playwright.sync_api")


def _launch(p):
    """GitHub Actions のサーバーに入っている Chrome を使う。無ければ playwright の Chromium を入れて使う"""
    try:
        return p.chromium.launch(channel="chrome", args=["--no-sandbox"])
    except Exception as e:
        print("Chromeを起動できなかったので、Chromiumを入れて使います:", str(e)[:200])
        subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"], check=True, timeout=600)
        return p.chromium.launch(args=["--no-sandbox"])


_PICK_JS = """(codes) => {
  const box = document.querySelector('#ajaxTblB #Box');
  const items = Array.from(box.children);
  const byCode = {};
  for (const it of items) {
    const a = it.querySelector("a[href*='c=']");
    if (!a) continue;
    const m = a.getAttribute('href').match(/c=(\\d+)/);
    if (m && !(m[1] in byCode)) byCode[m[1]] = it;
  }
  for (const it of items) it.style.display = 'none';
  const texts = {};
  for (const c of codes) {
    const it = byCode[c];
    if (!it) continue;
    it.style.display = '';
    box.appendChild(it);
    texts[c] = it.innerText;
  }
  return texts;
}"""

_AREA_JS = """() => {
  const els = Array.from(document.querySelectorAll('#ajaxTblB #Box > *')).filter(e => e.style.display !== 'none');
  let x1 = 1e9, y1 = 1e9, x2 = 0, y2 = 0;
  for (const e of els) {
    const r = e.getBoundingClientRect();
    x1 = Math.min(x1, r.left + scrollX); y1 = Math.min(y1, r.top + scrollY);
    x2 = Math.max(x2, r.right + scrollX); y2 = Math.max(y2, r.bottom + scrollY);
  }
  return {x: x1, y: y1, width: x2 - x1, height: y2 - y1};
}"""


def capture(path):
    """サイトを開いて3列表示にし、12枚だけを並べて撮る。戻り値は {コード: タイルの文字}"""
    sync_api = _playwright()
    codes = [c for c, _ in TILES]
    with sync_api.sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 1400}, device_scale_factor=2, locale="ja-JP", timezone_id="Asia/Tokyo")
            page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_selector("#ajaxTblB #Box a[href*='c=611']", timeout=90_000)
            page.click("#ajaxTblB >> text=3列")
            page.wait_for_timeout(3000)
            texts = page.evaluate(_PICK_JS, codes)
            missing = [c for c in codes if c not in texts]
            if missing:
                raise RuntimeError(f"サイトの一覧に見つからない指数があります: {missing}")
            page.wait_for_timeout(2500)  # 並べ替えたあと、チャートの描き直しを待つ
            area = page.evaluate(_AREA_JS)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            page.screenshot(path=path, full_page=True, clip=area)
        finally:
            browser.close()
    return texts


def parse_tile(text):
    """タイルの文字から、日付（または時刻）・値・前日比(%)を読む。読めなければ None"""
    lines = [s.strip() for s in text.splitlines() if s.strip()]
    if len(lines) < 3:
        return None
    pct = None
    for s in lines[1:8]:
        m = re.fullmatch(r"([▲▼])\s*([\d.]+)%", s)
        if m:
            pct = float(m.group(2)) * (1 if m.group(1) == "▲" else -1)
            break
    value = next((s for s in lines[2:8] if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", s)), None)
    if pct is None or value is None:
        return None
    return {"when": lines[1], "value": value, "pct": pct}


def read_values(texts):
    """{コード: タイルの文字} → [(名前, {when, value, pct}), ...]（読めなかった指数は入れない）"""
    rows = []
    for code, label in TILES:
        v = parse_tile(texts.get(code, ""))
        if v:
            rows.append((label, v))
        else:
            print(f"朝の市況: {label} の数字を読めませんでした")
    return rows


def pct_text(pct):
    return f"{pct:+.2f}%"


def facts_text(rows):
    """AIに渡す材料（1行1指数）"""
    return "\n".join(f"- {label}（{v['when']}時点）: {v['value']}（前日比 {pct_text(v['pct'])}）" for label, v in rows)


# ---------------------------------------------------------------
# 文章
# ---------------------------------------------------------------
def numbers_ok(post, facts):
    """AIの文章に、材料に無い数字が混ざっていないか（混ざっていたら決まった型の文章に切り替える）"""
    norm = lambda s: unicodedata.normalize("NFKC", s).replace(",", "").replace("+", "")  # noqa: E731
    allowed = set(re.findall(r"\d+(?:\.\d+)?", norm(facts)))
    used = re.findall(r"\d+(?:\.\d+)?", norm(post))
    extra = [n for n in used if n not in allowed and n not in ("1", "2", "3", "10", "225", "250", "500")]
    if extra:
        print(f"朝の市況: 材料に無い数字が入っていたため、決まった型の文章を使います: {extra}")
    return not extra


def fallback_post(rows, today):
    """AIが使えないとき・数字が合わないときの、決まった型の文章"""
    by = {label: v for label, v in rows}
    lines = ["おはようございます🐳", ""]
    if "日経平均" in by:
        lines.append(f"日経平均 {pct_text(by['日経平均']['pct'])}")
    us = [f"{name} {pct_text(by[name]['pct'])}" for name in ("NYダウ", "NASDAQ") if name in by]
    if us:
        lines.append("／".join(us))
    if "ドル円" in by:
        lines.append(f"ドル円 {float(by['ドル円']['value'].replace(',', '')):.2f}円")
    lines += ["", closing_comment(rows, today)]
    return "\n".join(lines)


# 締めの一言。その朝の市場に合わせて選ぶ（数字は入れない。同じ場面でも日によって言い回しを替える。どれも「〜ですね。」で終える）
CLOSINGS = {
    "vix": ("恐怖指数が高めの朝。値動きが荒い日は、見るだけにしておくのも手ですね。", "不安が強い相場。こういう日は、配当株を持ち続ける理由を思い出したいですね。"),
    "us_up": ("米国株が強い朝。こういう日こそ、高値づかみには気をつけたいですね。", "派手に上がった朝ほど、高配当株は落ち着いて見ていきたいですね。"),
    "us_down": ("米国株が大きく下げた朝。慌てて売るより、まずは深呼吸したいですね。", "下げた朝は、配当株の利回りが上がる朝でもありますね。"),
    "chip": ("半導体が大きく動いた朝。持っている人は、寄り付き前にチェックしておきたいですね。", "半導体がにぎやかな朝。配当株とは別世界の値動きですね。"),
    "yen_weak": ("円安が進んだ朝。輸出株と外貨の配当には、うれしい流れですね。", "円安の朝。米国株の配当は、円にすると少し増えますね。"),
    "yen_strong": ("円高に振れた朝。輸出株の動きには気をつけたいですね。", "円高の朝。米国株の配当は、円にすると少し目減りしますね。"),
    "sat": ("今週もおつかれさまでした。週末は相場を忘れて、ゆっくりしたいですね。", "1週間おつかれさまでした。週末はのんびり、配当の記録でもつけたいですね。"),
    "sun": ("日曜の朝。相場はお休みなので、ゆっくり過ごしたいですね。", "のんびり日曜。来週の権利日や決算を、軽く見ておくのもいいですね。"),
    "mon": ("今週もはじまり。今週も無理せずいきたいですね。", "週のはじまり。まずは寄り付きの様子を見てからにしたいですね。"),
    "calm": ("大きな動きは少なめの朝。今日も1日、無理せずいきたいですね。", "落ち着いた朝。こういう日は、配当の入金予定でも見返したいですね。"),
}


def closing_comment(rows, today):
    """締めの一言を、その朝の市場（目立つ動き）と曜日から選ぶ"""
    by = {label: v["pct"] for label, v in rows}
    us = [by[n] for n in ("S&P500", "NASDAQ", "NYダウ") if n in by]
    us_move = max(us, key=abs) if us else 0
    vix = next((float(v["value"].replace(",", "")) for label, v in rows if label == "VIX"), 0)
    if today.weekday() == 5:
        key = "sat"
    elif today.weekday() == 6:
        key = "sun"
    elif vix >= 30:
        key = "vix"
    elif us_move >= 2:
        key = "us_up"
    elif us_move <= -2:
        key = "us_down"
    elif abs(by.get("SOX指数", 0)) >= 4:
        key = "chip"
    elif by.get("ドル円", 0) >= 0.8:
        key = "yen_weak"
    elif by.get("ドル円", 0) <= -0.8:
        key = "yen_strong"
    elif today.weekday() == 0:
        key = "mon"
    else:
        key = "calm"
    return CLOSINGS[key][today.toordinal() % len(CLOSINGS[key])]


def write_post(client, rows, today):
    """数字を見て、おはようのあいさつの投稿を書く。失敗したら決まった型の文章を返す"""
    fallback = fallback_post(rows, today)
    if client is None or not rows:
        return fallback
    facts = facts_text(rows)
    weekday = "月火水木金土日"[today.weekday()]
    prompt = f"""株主優待・高配当を発信しているXアカウント「くじら」の、毎朝いちばん最初の投稿を書きます。
主要な株価指数の一覧の画像を付けて出す「おはよう」のあいさつの投稿です。

## 今日
{today:%Y/%m/%d}（{weekday}）の朝6時すぎ。米国市場は日本時間の朝に閉まったところ（土日は休み）。

## 材料（画像に写っている数字。「時点」が日付のものはその日の終値、時刻のものはその時刻の値）
{facts}

## 書き方
- 1行目は「おはようございます🐳」
- 次に、今朝いちばん目立つ動きを1〜2行で（例：米国株が大きく上がった・下がった、為替が動いた、など）。数字は材料にあるものを1〜2個だけ
- 最後に、その朝の市場の様子に合わせた、優待・高配当の株を持つ人の目線での短い一言を1行
  （例：大きく上がった朝なら「派手な日ほど、高配当株は落ち着いて見ていきたいですね。」、下げた朝なら「下げた朝は、配当株の利回りが上がる朝でもありますね。」、
  土曜なら「1週間おつかれさまでした。週末はゆっくりしたいですね。」。毎日同じ締めにならないように。決まり文句だけで終わらせない）
- 最後の一文は、必ず「〜ですね。」の形で終える
- 口調はくだけた標準語。関西弁は使わない。絵文字は1〜2個まで
- 全体で{MAX_CHARS}字以内。1文ごとに改行する
- 材料に無い数字・ニュース・理由（「〇〇の発表で」など）は書かない。値動きの理由は書かない
- 「買い」「おすすめ」のような売買のすすめや、株価の予想は書かない
- 「いいねして」「リプで教えて」のような、反応をお願いする言い方は書かない
- 自分の売買・保有・受け取りの体験談は書かない。投資歴の浅さも書かない
- ハッシュタグは付けない"""
    try:
        response = client.messages.create(
            max_tokens=2000,
            output_config={"format": {"type": "json_schema", "schema": POST_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("拒否されました")
        post = json.loads(next(b.text for b in response.content if b.type == "text"))["post"].strip()
    except Exception as e:
        print("朝の市況: 文章を作れなかったので、決まった型の文章を使います:", type(e).__name__, str(e)[:200])
        return fallback
    post = re.sub(r"\n?#\S+", "", post).strip()
    if not post or len(post) > MAX_CHARS + 20 or not numbers_ok(post, facts):
        return fallback
    if not post.endswith("ですね。"):
        print("朝の市況: 最後の一文が「〜ですね。」で終わっていないので、決まった型の文章を使います")
        return fallback
    return post


def build(now, make_client=None, capture_fn=None):
    """出す時刻に呼ぶ。撮影して文章を作り、(本文, 画像パス) を返す。撮れなかったら例外"""
    today = now.date()
    path = os.path.join("images", f"{today:%Y-%m-%d}-{IMAGE_NAME}.png")
    texts = (capture_fn or capture)(path)
    rows = read_values(texts)
    if len(rows) < len(TILES) // 2:
        raise RuntimeError(f"数字を読めた指数が少なすぎます（{len(rows)}/{len(TILES)}）")
    client = None
    if make_client:
        try:
            client = make_client(today)
        except Exception as e:
            print("朝の市況: AIを使えないので、決まった型の文章を使います:", e)
    post = write_post(client, rows, today)
    return f"{post}\n{HASHTAGS}", path
