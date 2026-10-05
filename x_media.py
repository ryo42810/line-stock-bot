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
    "red": "#D32F2F",      # 下落
}
CATEGORY_COLORS = {
    "好材料": COLORS["green"],
    "懸念材料": COLORS["red"],
    "決算": COLORS["accent"],
    "優待": COLORS["gold"],
    "買い場候補": COLORS["accent"],
}
# 配色テーマ（環境変数 IMAGE_THEME で切り替え）
THEMES = {
    "navy": {"head": "#1A2530", "title": "white", "sub": "#C9D6E2", "line": "#00A8E8", "pill": "#00A8E8", "foot": "#1A2530", "foot_text": "#C9D6E2", "bars": "category", "bar": "#00A8E8", "chart": "#00A8E8"},
    "white": {"head": "#FFFFFF", "title": "#1A2530", "sub": "#6B7785", "line": "#00A8E8", "pill": "#1A2530", "foot": "#F2F5F8", "foot_text": "#6B7785", "bars": "single", "bar": "#00A8E8", "chart": "#00A8E8"},
    "blue": {"head": "#0B4F9C", "title": "white", "sub": "#CFE3F5", "line": "#E6A100", "pill": "#E6A100", "foot": "#0B4F9C", "foot_text": "#CFE3F5", "bars": "single", "bar": "#1E73D8", "chart": "#1E73D8"},
    "green": {"head": "#0F3B3A", "title": "white", "sub": "#CFE5DF", "line": "#E6A100", "pill": "#2E7D32", "foot": "#0F3B3A", "foot_text": "#CFE5DF", "bars": "single", "bar": "#2E7D32", "chart": "#00A8E8"},
    "gold": {"head": "#1A2530", "title": "white", "sub": "#C9D6E2", "line": "#E6A100", "pill": "#E6A100", "foot": "#1A2530", "foot_text": "#C9D6E2", "bars": "single", "bar": "#1A2530", "chart": "#00A8E8"},
    # ゴールドのバー（ネイビー文字）：明るく目立つ
    # ネイビー×ゴールドのトーン違い（構成はgoldと同じ。色の濃さ・鮮やかさを変える）
    "gold_vivid": {"head": "#0B1F3A", "title": "white", "sub": "#D5DEEA", "line": "#FFB300", "pill": "#FFB300", "foot": "#0B1F3A", "foot_text": "#D5DEEA", "bars": "single", "bar": "#0B1F3A", "chart": "#0096E0", "gold": "#F0A000", "gold_bg": "#FFEBB8", "tint": 0.84, "border_w": 3},
    "gold_deep": {"head": "#06121F", "title": "white", "sub": "#C7D3E0", "line": "#F2A900", "pill": "#F2A900", "foot": "#06121F", "foot_text": "#F2C75C", "bars": "single", "bar": "#06121F", "chart": "#0091D5", "gold": "#D99500", "gold_bg": "#FFE9AD", "tint": 0.86, "page": "#EEF2F6", "border_w": 0},
    "gold_contrast": {"head": "#0A2342", "title": "white", "sub": "#DCE6F2", "line": "#FFC107", "pill": "#FFC107", "pill_text": "#0A2342", "foot": "#FFC107", "foot_text": "#0A2342", "bars": "single", "bar": "#0A2342", "chart": "#00A3F0", "gold": "#E89B00", "gold_bg": "#FFF0C2", "tint": 0.82, "text": "#000000", "border_w": 3},
    "gold_solid": {"head": "#0B1F3A", "title": "white", "sub": "#D5DEEA", "line": "#FFB300", "pill": "#FFB300", "pill_text": "#0B1F3A", "foot": "#0B1F3A", "foot_text": "#FFB300", "bars": "single", "bar": "#0B1F3A", "chart": "#0096E0", "gold": "#FFB300", "solid_boxes": True, "border_w": 3},
    "gold_bar": {"head": "#1A2530", "title": "white", "sub": "#C9D6E2", "line": "#E6A100", "pill": "#E6A100", "foot": "#1A2530", "foot_text": "#C9D6E2", "bars": "single", "bar": "#E6A100", "bar_text": "#1A2530", "circle": "#1A2530", "circle_text": "#E6A100", "chart": "#1A2530"},
    # 白ヘッダー：見出しはネイビー文字＋ゴールドの下線、バーはネイビー
    "gold_light": {"head": "#FFFFFF", "title": "#1A2530", "sub": "#6B7785", "line": "#E6A100", "pill": "#1A2530", "foot": "#1A2530", "foot_text": "#E6C46A", "bars": "single", "bar": "#1A2530", "chart": "#00A8E8", "border": "#E6A100"},
    # プレミアム：ゴールドの見出し文字・ゴールドの枠・ゴールドのチャート
    "gold_premium": {"head": "#1A2530", "title": "#E6B422", "sub": "#E8DDBF", "line": "#E6A100", "pill": "#E6A100", "foot": "#1A2530", "foot_text": "#E6C46A", "bars": "single", "bar": "#1A2530", "bar_text": "#F3D27A", "circle": "#E6A100", "circle_text": "#1A2530", "chart": "#C98C00", "border": "#E6A100", "num": "#1A2530"},
}
THEME = THEMES.get((os.environ.get("IMAGE_THEME") or "navy").strip(), THEMES["navy"])
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
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor=THEME.get("page", COLORS["bg"]))
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
    """上部：ネイビーの帯に白抜きの大見出し・日付・クジラアイコン"""
    _box(ax, 0, 0, W, 200, THEME["head"], r=0)
    ax.add_patch(plt.Rectangle((0, 200), W, 10, color=THEME["line"], zorder=2))  # 帯の下のアクセントライン
    ax.text(50, 82, title, fontsize=52, color=THEME["title"], weight="bold", va="center")
    ax.text(52, 152, subtitle, fontsize=22, color=THEME["sub"], weight="bold", va="center")
    right = W - 40
    if os.path.exists(WHALE_ICON):
        try:
            icon = plt.imread(WHALE_ICON)
            iax = fig.add_axes([(W - 40 - 150) / W, 1 - (25 + 150) / H, 150 / W, 150 / H])
            iax.imshow(icon)
            iax.set_axis_off()
            right = W - 40 - 160
        except Exception as e:
            print("クジラアイコンの読み込みに失敗:", e)
    if date_text:
        _pill(ax, right, 136, date_text, 16, THEME.get("pill_text", "white"), THEME["pill"], align="right")


def _footer(ax, note):
    _box(ax, 0, H - 52, W, 52, THEME["foot"], r=0)
    ax.text(W - 40, H - 26, note, fontsize=14, color=THEME["foot_text"], ha="right", va="center")


def _sparkline(fig, closes, x, y, w, h, color):
    ax = fig.add_axes([x / W, 1 - (y + h) / H, w / W, h / H])
    vals = closes.values
    ax.plot(range(len(vals)), vals, color=color, linewidth=3.2, solid_capstyle="round")
    ax.fill_between(range(len(vals)), vals, float(vals.min()), color=color, alpha=0.15)
    ax.scatter([len(vals) - 1], [vals[-1]], s=70, color=color, edgecolor="white", linewidth=2, zorder=3)
    ax.set_xlim(0, len(vals) - 1)
    ax.set_axis_off()


def _tint(hex_color, ratio):
    """色を白に寄せた薄い色（カードの地色用）"""
    c = hex_color.lstrip("#")
    r, g, b = (int(c[i : i + 2], 16) for i in (0, 2, 4))
    return "#{:02X}{:02X}{:02X}".format(*(int(v + (255 - v) * ratio) for v in (r, g, b)))


# ---------------------------------------------------------------
# 銘柄紹介カード
# ---------------------------------------------------------------
def render_stock_card(path, title, subtitle, stocks, accent, get_history, date_text=""):
    """3銘柄までの紹介画像。銘柄ごとに色付きのタイトルバー＋株価・利回り・理由・3ヶ月チャート"""
    fig, ax = _canvas()
    _header(fig, ax, title, subtitle, date_text, accent)

    top, gap, panel_h = 236, 18, 340
    x, w, bar_h = 36, W - 72, 66
    for i, s in enumerate(stocks[:3]):
        y = top + i * (panel_h + gap)
        cat_color = CATEGORY_COLORS.get(s.get("category"), accent)
        color = cat_color if THEME["bars"] == "category" else THEME["bar"]

        # カード本体と、色ベタのタイトルバー
        _box(ax, x, y, w, panel_h, "white", THEME.get("border", color), r=16, lw=THEME.get("border_w", 2.5) or 0.01)
        _box(ax, x, y, w, bar_h + 16, color, r=16, z=2)
        ax.add_patch(plt.Rectangle((x + 2, y + bar_h), w - 4, 16, color="white", zorder=2))

        # タイトルバー：番号・銘柄名（コード）／右にカテゴリと前日比
        bar_text = THEME.get("bar_text", "white")
        ax.add_patch(plt.Circle((x + 40, y + bar_h / 2), 21, color=THEME.get("circle", "white"), zorder=3))
        ax.text(x + 40, y + bar_h / 2, str(i + 1), fontsize=20, color=THEME.get("circle_text", color), ha="center", va="center", weight="bold", zorder=4)
        change = s.get("change")
        change_text = f"{change:+.1f}%" if change is not None else ""
        cat = s.get("category", "")
        right_w = (text_px(change_text, 22) + 32 if change_text else 0) + (text_px(cat, 15) + 38 if cat else 0)
        name, code_text = s.get("name", ""), f"（{s['code']}）"
        name_size = fit_size(name, w - 90 - right_w - text_px(code_text, 17) - 30, 28, 18)
        ax.text(x + 76, y + bar_h / 2, name, fontsize=name_size, color=bar_text, weight="bold", va="center", zorder=4)
        ax.text(x + 76 + text_px(name, name_size) + 4, y + bar_h / 2 + 2, code_text, fontsize=17, color=bar_text, va="center", zorder=4)
        rx = x + w - 18
        if change_text:
            rx -= _pill(ax, rx, y + 13, change_text, 22, _change_color(change), "white", align="right") + 10
        if cat:
            # バーと同じ色のラベルは見えなくなるので、そのときはネイビーにする
            pill_bg = COLORS["ink"] if THEME["bars"] == "category" or cat_color.upper() == color.upper() else cat_color
            _pill(ax, rx, y + 18, cat, 15, "white", pill_bg, align="right")

        body_y = y + bar_h + 16
        # 左：株価と利回りの数字ボックス
        bx, bw = x + 20, 230
        solid = THEME.get("solid_boxes", False)  # 数字ボックスをベタ塗りにする（くっきりトーン）
        text_color = THEME.get("text", COLORS["ink"])
        price_bg = color if solid else _tint(color, THEME.get("tint", 0.88))
        price_fg, price_label = ("white", "#C9D6E2") if solid else (text_color, COLORS["sub"])
        _box(ax, bx, body_y, bw, 108, price_bg, r=12)
        ax.text(bx + 16, body_y + 26, "株価（前営業日終値）", fontsize=13, color=price_label, weight="bold", va="center")
        if s.get("price"):
            ax.text(bx + 16, body_y + 72, f"{s['price']:,.0f}", fontsize=36, color=price_fg, weight="bold", va="center")
            ax.text(bx + 20 + text_px(f"{s['price']:,.0f}", 36), body_y + 80, "円", fontsize=18, color=price_fg, weight="bold", va="center")

        yield_fmt = s.get("yield_fmt") or ""
        show_yield = yield_fmt and not any(k in yield_fmt for k in ("取得できず", "なし", "不明"))
        gold = THEME.get("gold", COLORS["gold"])
        if show_yield:
            bg, fg, label_fg = (gold, COLORS["ink"], COLORS["ink"]) if solid else (THEME.get("gold_bg", "#FFF4D6"), gold, COLORS["sub"])
        else:
            bg, fg, label_fg = (COLORS["green"], "white", "#D7EED9") if solid else (_tint(COLORS["green"], THEME.get("tint", 0.88)), COLORS["green"], COLORS["sub"])
        _box(ax, bx, body_y + 120, bw, 108, bg, r=12)
        if show_yield:
            total = yield_fmt.split("＝")[-1]
            ax.text(bx + 16, body_y + 146, "利回り" + ("（配当＋優待）" if "＝" in yield_fmt else "（配当）"), fontsize=13, color=label_fg, weight="bold", va="center")
            ax.text(bx + 16, body_y + 192, total.replace("配当", ""), fontsize=34, color=fg, weight="bold", va="center")
        else:
            sig = (s.get("signals") or ["注目材料あり"])[0]
            sig = re.sub(r"^[^\w（]+", "", sig)
            ax.text(bx + 16, body_y + 146, "テクニカル", fontsize=13, color=label_fg, weight="bold", va="center")
            for j, line in enumerate(wrap_ja(sig, bw - 30, 17, 2)):
                ax.text(bx + 16, body_y + 180 + j * 28, line, fontsize=17, color=fg, weight="bold", va="center")

        # 中央：理由（太字）とテクニカルの一言
        tx, tw = x + 272, 360
        reason = s.get("buy_comment") or s.get("headline") or ""
        lines = wrap_ja(reason, tw, 20, 4)
        for j, line in enumerate(lines):
            ax.text(tx, body_y + 24 + j * 36, line, fontsize=20, color=THEME.get("text", COLORS["ink"]), weight="bold", va="center")
        signals = [re.sub(r"^[^\w（]+", "", g).split("（")[0] for g in s.get("signals", [])]
        if show_yield and signals:
            _pill(ax, tx, body_y + 188, f"● {signals[0]}", 15, COLORS["green"], _tint(COLORS["green"], 0.85))

        # 右：3ヶ月チャート
        try:
            closes = get_history(s["code"])["Close"].iloc[-60:]
            _sparkline(fig, closes, x + w - 330, body_y + 10, 305, 180, THEME["chart"])
            ax.text(x + w - 24, body_y + 214, "3ヶ月チャート", fontsize=13, color=COLORS["sub"], weight="bold", ha="right", va="center")
        except Exception as e:
            print(f"チャート描画失敗 ({s['code']}):", e)

    _footer(ax, "※株価は前営業日終値ベース")
    fig.savefig(path, facecolor=THEME.get("page", COLORS["bg"]))
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

    # サマリー（色ベタの見出し＋大きな数字）
    for k, (label, value, color) in enumerate(
        [("平均騰落率", f"{avg:+.1f}%", _change_color(avg)), ("上昇した銘柄", f"{ups} / {len(rows)}", COLORS["accent"])]
    ):
        bx, bw = 36 + k * 512, 496
        _box(ax, bx, 240, bw, 160, "white", color, r=16, lw=2.5)
        _box(ax, bx, 240, bw, 60, color, r=16, z=2)
        ax.add_patch(plt.Rectangle((bx + 2, 300), bw - 4, 16, color="white", zorder=2))
        ax.text(bx + 24, 268, label, fontsize=20, color="white", weight="bold", va="center", zorder=3)
        ax.text(bx + 24, 350, value, fontsize=46, color=color, weight="bold", va="center")

    # 横棒グラフのセクション見出し
    _box(ax, 36, 428, W - 72, 56, COLORS["ink"], r=12)
    ax.text(60, 456, "銘柄別の成績", fontsize=20, color="white", weight="bold", va="center")

    chart = fig.add_axes([0.37, 0.07, 0.55, 0.53])
    labels = [f"{r['name'][:10]}（{r['code']}）" for r in rows]
    values = [r["change"] for r in rows]
    chart.barh(range(len(rows)), values, color=[_change_color(v) for v in values], height=0.6)
    chart.axvline(0, color=COLORS["ink"], linewidth=1.5)
    chart.set_yticks(range(len(rows)))
    chart.set_yticklabels(labels, fontsize=17, color=COLORS["ink"], fontweight="bold")
    chart.tick_params(axis="y", length=0)
    chart.tick_params(axis="x", labelsize=13, colors=COLORS["sub"])
    for side in ("top", "right", "left"):
        chart.spines[side].set_visible(False)
    chart.spines["bottom"].set_color(COLORS["line"])
    span = max(abs(v) for v in values) * 1.5 or 1
    chart.set_xlim(-span if min(values) < 0 else 0, span)
    for yv, v in enumerate(values):
        chart.text(v, yv, f" {v:+.1f}% ", va="center", ha="left" if v >= 0 else "right", fontsize=18,
                   color=_change_color(v), weight="bold")

    _footer(ax, "※配信時の株価は前営業日終値ベース")
    fig.savefig(path, facecolor=THEME.get("page", COLORS["bg"]))
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
