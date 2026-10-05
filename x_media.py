"""X投稿用の画像づくりと、Xへの自動投稿"""
import datetime
import os
import re
import subprocess
import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

IMAGE_DIR = "images"
IMAGE_KEEP_DAYS = 14
REPO = os.environ.get("GITHUB_REPOSITORY", "ryo42810/line-stock-bot")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

# Xの自動投稿（リポジトリの Variables で X_AUTO_POST=on のときだけ投稿する）
X_AUTO_POST = (os.environ.get("X_AUTO_POST") or "").strip().lower() == "on"
X_KEYS = {k: (os.environ.get(k) or "").strip() for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")}
X_POST_INTERVAL_MIN = 15  # 同時に何本も出すと伸びにくいので、投稿の間隔をあける
X_NG_WORDS = ["絶対上がる", "必ず上がる", "絶対に上がる", "今すぐ買え", "買うべき", "儲かる"]

# トンマナ（アカウントの画像と統一：白背景・ダークネイビー・スカイブルー・ゴールド・フォレストグリーン）
COLORS = {
    "bg": "#FFFFFF",
    "ink": "#1A2530",      # メイン文字・枠線
    "sub": "#6B7785",      # 補足文字
    "line": "#DCE3EA",     # 薄い罫線・カード枠
    "panel": "#F5F9FC",    # カードの地色
    "accent": "#00A8E8",   # チャート・メインアクセント
    "gold": "#E6A100",     # 還元・利回り
    "green": "#2E7D32",    # 成長・上昇
    "red": "#D64545",      # 下落
}
CATEGORY_COLORS = {
    "好材料": COLORS["green"],
    "懸念材料": COLORS["red"],
    "決算": COLORS["accent"],
    "優待": COLORS["gold"],
    "買い場候補": COLORS["accent"],
}
WHALE_ICON = os.path.join("assets", "whale.png")  # 右上のクジラアイコン（置いてあれば使う）
W, H = 1080, 1350  # Xで見やすい4:5


def _setup_font():
    """日本語フォント（GitHub Actionsでは fonts-noto-cjk を入れておく）"""
    for name in ["Noto Sans CJK JP", "Noto Sans JP", "IPAexGothic", "WenQuanYi Zen Hei"]:
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False  # マイナス記号の文字化け対策
            return
    print("日本語フォントが見つからないため、画像の文字が化ける可能性があります")


_setup_font()


def codes_in_post(text):
    """投稿文に出てくる証券コード（出てきた順）"""
    return list(dict.fromkeys(re.findall(r"\((\d{3}[0-9A-Z])\)", text or "")))


def _change_color(change):
    if change is None:
        return COLORS["sub"]
    return COLORS["green"] if change >= 0 else COLORS["red"]


# ---------------------------------------------------------------
# 文字の幅と折り返し（日本語の禁則つき）
# ---------------------------------------------------------------
def _char_w(c):
    return 1.0 if ord(c) > 0x2E80 else 0.6


def text_px(text, size):
    """文字列のおおよその横幅（px）。size はポイント、dpi=100"""
    return sum(_char_w(c) for c in text) * size * 100 / 72


NO_LINE_START = set("、。，．）」』】％%ー・！？!?）)")


def wrap_ja(text, width_px, size, max_lines):
    """数字や英字のかたまりは途中で切らず、句読点は行頭に来ないように折り返す"""
    tokens = re.findall(r"[0-9A-Za-z.,%+\-−/:]+|.", (text or "").replace("\n", ""))
    lines, cur = [], ""
    for t in tokens:
        if text_px(cur + t, size) <= width_px or not cur:
            cur += t
            continue
        if t[0] in NO_LINE_START:  # 句読点は前の行にぶら下げる
            cur += t
            continue
        lines.append(cur)
        cur = t
    if cur:
        lines.append(cur)
    # 最後の行が1〜2文字だけになるときは、前の行に少しはみ出して入れる（「期」「落」だけの行を作らない）
    if len(lines) >= 2 and len(lines[-1]) <= 2 and text_px(lines[-2] + lines[-1], size) <= width_px * 1.12:
        last = lines.pop()
        lines[-1] += last
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and text_px(lines[-1] + "…", size) > width_px:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def fit_size(text, width_px, size, min_size):
    """幅に収まるまで文字を小さくする"""
    while size > min_size and text_px(text, size) > width_px:
        size -= 1
    return size


# ---------------------------------------------------------------
# 描画の部品（左上を原点にしたピクセル座標で描く）
# ---------------------------------------------------------------
def _canvas():
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor=COLORS["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.set_axis_off()
    return fig, ax


def _box(ax, x, y, w, h, face, edge=None, r=18, lw=1.5, z=1):
    ax.add_patch(
        FancyBboxPatch(
            (x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
            facecolor=face, edgecolor=edge or face, linewidth=lw, zorder=z,
        )
    )


def _pill(ax, x, y, text, size, fg, bg, pad_x=14, h=None, align="left", bold=True):
    """角丸のラベル。align=right なら x が右端。幅を返す"""
    w = text_px(text, size) + pad_x * 2
    h = h or size * 100 / 72 + 16
    left = x - w if align == "right" else x
    _box(ax, left, y, w, h, bg, r=h / 2, z=3)
    ax.text(left + w / 2, y + h / 2, text, fontsize=size, color=fg, ha="center", va="center",
            weight="bold" if bold else "normal", zorder=4)
    return w


def _header(fig, ax, title, subtitle, date_text, accent):
    """上部：小ラベル・大見出し・日付・クジラアイコン"""
    _box(ax, 0, 0, W, 10, accent, r=0)  # 一番上の細い帯
    ax.text(60, 92, title, fontsize=50, color=COLORS["ink"], weight="bold", va="center")
    ax.add_patch(plt.Rectangle((60, 135), 120, 8, color=accent, zorder=2))
    ax.text(60, 180, subtitle, fontsize=22, color=COLORS["sub"], va="center")
    right = W - 60
    if os.path.exists(WHALE_ICON):
        try:
            icon = plt.imread(WHALE_ICON)
            fig.add_axes([(W - 60 - 110) / W, 1 - (40 + 110) / H, 110 / W, 110 / H]).imshow(icon)
            fig.axes[-1].set_axis_off()
            right = W - 60 - 125
        except Exception as e:
            print("クジラアイコンの読み込みに失敗:", e)
    _pill(ax, right, 160, date_text, 16, COLORS["ink"], COLORS["panel"], align="right", bold=False)


def _sparkline(fig, closes, x, y, w, h):
    ax = fig.add_axes([x / W, 1 - (y + h) / H, w / W, h / H])
    vals = closes.values
    ax.plot(range(len(vals)), vals, color=COLORS["accent"], linewidth=2.6, solid_capstyle="round")
    ax.fill_between(range(len(vals)), vals, float(vals.min()), color=COLORS["accent"], alpha=0.10)
    ax.scatter([len(vals) - 1], [vals[-1]], s=40, color=COLORS["accent"], zorder=3)
    ax.set_xlim(0, len(vals) - 1)
    ax.set_axis_off()


# ---------------------------------------------------------------
# 銘柄紹介カード
# ---------------------------------------------------------------
def render_stock_card(path, title, subtitle, stocks, accent, get_history, date_text=""):
    """3銘柄までの紹介画像（銘柄名・カテゴリ・前日比・理由・指標・3ヶ月チャート）"""
    fig, ax = _canvas()
    _header(fig, ax, title, subtitle, date_text, accent)

    top, gap, panel_h = 230, 26, 340
    for i, s in enumerate(stocks[:3]):
        y = top + i * (panel_h + gap)
        x, w = 50, W - 100
        color = CATEGORY_COLORS.get(s.get("category"), accent)
        _box(ax, x, y, w, panel_h, COLORS["bg"], COLORS["line"], r=22, lw=2)
        ax.add_patch(plt.Rectangle((x, y + 24), 8, panel_h - 48, color=color, zorder=2))

        # 1行目：番号・銘柄名・コード／右に前日比
        ax.add_patch(plt.Circle((x + 52, y + 50), 22, color=color, zorder=3))
        ax.text(x + 52, y + 50, str(i + 1), fontsize=20, color="white", ha="center", va="center", weight="bold", zorder=4)
        change = s.get("change")
        change_text = f"前日比 {change:+.1f}%" if change is not None else ""
        change_w = text_px(change_text, 18) + 28 if change_text else 0
        name_w = w - 100 - change_w - 40
        name = s.get("name", "")
        name_size = fit_size(name, name_w - text_px(f"（{s['code']}）", 18), 30, 20)
        ax.text(x + 90, y + 50, name, fontsize=name_size, color=COLORS["ink"], weight="bold", va="center")
        ax.text(x + 90 + text_px(name, name_size) + 6, y + 52, f"（{s['code']}）", fontsize=18, color=COLORS["sub"], va="center")
        if change_text:
            bg = "#E8F5E9" if change >= 0 else "#FDECEC"
            _pill(ax, x + w - 24, y + 30, change_text, 18, _change_color(change), bg, align="right")

        # 2行目：カテゴリのラベル
        cat = s.get("category", "")
        if cat:
            _pill(ax, x + 90, y + 88, cat, 14, "white", color)

        # 理由（左側）とチャート（右側）
        reason = s.get("buy_comment") or s.get("headline") or ""
        text_w = 520
        for j, line in enumerate(wrap_ja(reason, text_w, 21, 3)):
            ax.text(x + 40, y + 160 + j * 40, line, fontsize=21, color=COLORS["ink"], va="center")

        try:
            closes = get_history(s["code"])["Close"].iloc[-60:]
            _sparkline(fig, closes, x + w - 360, y + 120, 330, 150)
            ax.text(x + w - 30, y + 288, "3ヶ月チャート", fontsize=13, color=COLORS["sub"], ha="right", va="center")
        except Exception as e:
            print(f"チャート描画失敗 ({s['code']}):", e)

        # 下の段：指標のチップ（株価・利回り・テクニカル）
        chips = []
        if s.get("price"):
            chips.append((f"株価 {s['price']:,.0f}円", COLORS["ink"], COLORS["panel"]))
        if s.get("yield_fmt") and not any(w in s["yield_fmt"] for w in ("取得できず", "なし", "不明")):
            chips.append((s["yield_fmt"].replace("＝", " = "), COLORS["gold"], "#FFF6E0"))
        for sig in s.get("signals", [])[:1]:
            sig = re.sub(r"^[^\w（]+", "", sig).split("（")[0]
            chips.append((sig, COLORS["green"], "#E8F5E9"))
        cx = x + 40
        for text, fg, bg in chips:
            size = 15
            if cx + text_px(text, size) + 28 > x + w - 380:
                break
            cx += _pill(ax, cx, y + panel_h - 62, text, size, fg, bg, bold=False) + 10

    fig.savefig(path, facecolor=COLORS["bg"])
    plt.close(fig)
    return path


# ---------------------------------------------------------------
# 成績グラフ
# ---------------------------------------------------------------
def render_performance_chart(path, rows, title, date_text=""):
    """買い場候補の1週間後の成績（横棒グラフ）"""
    rows = sorted(rows, key=lambda r: r["change"])[-10:]
    avg = sum(r["change"] for r in rows) / len(rows)
    ups = sum(1 for r in rows if r["change"] > 0)

    fig, ax = _canvas()
    _header(fig, ax, title, "配信時の株価から5営業日後までの騰落率", date_text, COLORS["accent"])

    # サマリー（平均・上昇した数）
    for k, (label, value, color) in enumerate(
        [("平均", f"{avg:+.1f}%", _change_color(avg)), ("上昇した銘柄", f"{ups} / {len(rows)}", COLORS["ink"])]
    ):
        bx = 50 + k * 500
        _box(ax, bx, 230, 480, 130, COLORS["panel"], COLORS["line"], r=20)
        ax.text(bx + 30, 268, label, fontsize=18, color=COLORS["sub"], va="center")
        ax.text(bx + 30, 320, value, fontsize=40, color=color, weight="bold", va="center")

    # 横棒グラフ
    chart = fig.add_axes([0.36, 0.06, 0.58, 0.62])
    labels = [f"{r['name'][:10]}（{r['code']}）" for r in rows]
    values = [r["change"] for r in rows]
    chart.barh(range(len(rows)), values, color=[_change_color(v) for v in values], height=0.55)
    chart.axvline(0, color=COLORS["ink"], linewidth=1.2)
    chart.set_yticks(range(len(rows)))
    chart.set_yticklabels(labels, fontsize=17, color=COLORS["ink"])
    chart.tick_params(axis="y", length=0)
    chart.tick_params(axis="x", labelsize=13, colors=COLORS["sub"])
    for side in ("top", "right", "left"):
        chart.spines[side].set_visible(False)
    chart.spines["bottom"].set_color(COLORS["line"])
    span = max(abs(v) for v in values) * 1.45 or 1
    chart.set_xlim(-span if min(values) < 0 else 0, span)
    for yv, v in enumerate(values):
        chart.text(v, yv, f" {v:+.1f}% ", va="center", ha="left" if v >= 0 else "right", fontsize=16,
                   color=_change_color(v), weight="bold")

    fig.savefig(path, facecolor=COLORS["bg"])
    plt.close(fig)
    return path


def image_path(today, kind):
    os.makedirs(IMAGE_DIR, exist_ok=True)
    return os.path.join(IMAGE_DIR, f"{today:%Y-%m-%d}-{kind}.png")


def publish_images(paths, today):
    """画像をリポジトリにpushして、LINEで使える公開URLを返す（リポジトリが公開である前提）"""
    if not paths:
        return {}
    cutoff = f"{today - datetime.timedelta(days=IMAGE_KEEP_DAYS):%Y-%m-%d}"
    for name in os.listdir(IMAGE_DIR):
        if name[:10] < cutoff:
            os.remove(os.path.join(IMAGE_DIR, name))
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "-A", IMAGE_DIR], check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-q", "-m", f"X用の画像を追加（{today:%m/%d}）", "--", IMAGE_DIR], check=True)
            subprocess.run(["git", "pull", "-q", "--rebase", "--autostash", "origin", BRANCH], check=True)
            subprocess.run(["git", "push", "-q", "origin", f"HEAD:{BRANCH}"], check=True)
    except Exception as e:
        print("画像のpushに失敗:", e)
        return {}
    return {p: f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{p}" for p in paths}


def line_image_message(url):
    return {"type": "image", "originalContentUrl": url, "previewImageUrl": url}


# ---------------------------------------------------------------
# Xへの自動投稿
# ---------------------------------------------------------------
def x_enabled():
    return X_AUTO_POST and all(X_KEYS.values())


def check_post(text):
    """投稿してよい文章か。ダメなら理由を返す"""
    if not text.strip():
        return "空の投稿"
    if "http" in text:
        return "URLが入っている（URL付きの投稿は料金が約13倍）"
    hit = [w for w in X_NG_WORDS if w in text]
    if hit:
        return f"NGワード {hit}"
    return ""


def post_to_x(text, image=None):
    """Xに投稿して投稿IDを返す。失敗したら例外"""
    from requests_oauthlib import OAuth1Session

    session = OAuth1Session(
        X_KEYS["X_API_KEY"],
        client_secret=X_KEYS["X_API_SECRET"],
        resource_owner_key=X_KEYS["X_ACCESS_TOKEN"],
        resource_owner_secret=X_KEYS["X_ACCESS_TOKEN_SECRET"],
    )
    payload = {"text": text}
    if image:
        with open(image, "rb") as f:
            res = session.post(
                "https://api.x.com/2/media/upload",
                files={"media": f},
                data={"media_category": "tweet_image"},
                timeout=60,
            )
        if res.status_code >= 300:
            raise RuntimeError(f"画像アップロード失敗 {res.status_code}: {res.text[:300]}")
        body = res.json()
        media_id = (body.get("data") or {}).get("id") or body.get("media_id_string")
        if not media_id:
            raise RuntimeError(f"画像IDが取れません: {res.text[:300]}")
        payload["media"] = {"media_ids": [str(media_id)]}
    res = session.post("https://api.x.com/2/tweets", json=payload, timeout=60)
    if res.status_code >= 300:
        raise RuntimeError(f"投稿失敗 {res.status_code}: {res.text[:300]}")
    return res.json()["data"]["id"]


def post_all_to_x(posts):
    """[(文章, 画像パス or None), ...] を間隔をあけて順に投稿。結果の文言を返す"""
    if not x_enabled():
        print("Xの自動投稿はオフ（X_AUTO_POST=on と4つのキーが必要）")
        return ""
    results = []
    for i, (text, image) in enumerate(p for p in posts if p[0]):
        problem = check_post(text)
        if problem:
            print(f"X投稿をスキップ: {problem}")
            results.append(f"⏭ スキップ（{problem}）")
            continue
        if i > 0:
            time.sleep(X_POST_INTERVAL_MIN * 60)
        try:
            post_id = post_to_x(text, image)
            print("Xに投稿しました:", post_id)
            results.append(f"✅ 投稿 https://x.com/i/web/status/{post_id}")
        except Exception as e:
            print("X投稿に失敗:", e)
            results.append(f"⚠️ 失敗（{str(e)[:80]}）")
    return "Xへの自動投稿\n" + "\n".join(results) if results else ""
