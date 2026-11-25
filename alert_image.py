"""値下がりアラートの表の画像（price_alert.py から使う）

LINEで文字だけを並べると読みにくいので、1枚の表にまとめる（2026/11/25。3案を見比べて「表の画像」に決定）。
- 列：銘柄（コード）／区分（Diego・成長・両方）／株価／前日比／52週高値から／買値まで／配当利回り
- 「買値まで」は、スプシの「買いたい価格（52週安値×110%）」までの残り。0%以下が買い時（緑）、5%以内がもうすぐ（黄）、それより上は待ち（灰）
- 知らせる条件に当たった項目は、黄色の地で目立たせる
- 自分用の通知なので、Xの画像（ミッドナイト×金）とは別の、白地の見やすさ優先の表にしている
"""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Circle, FancyBboxPatch, Rectangle  # noqa: E402

import x_media  # noqa: E402  日本語フォントの設定と、文字幅の見積もり（text_px・fit_size）を使う

W = 1080
PAD = 30
HEAD_H = 150  # 題名と日経平均
COLS_H = 46  # 列の見出し
SECTION_H = 48
ROW_H = 68
FOOT_H = 96
MAX_ROWS = 28  # 1枚に入れる銘柄の行数（これを超えたら、2枚目に分ける）

C = {
    "bg": "#FFFFFF", "ink": "#1A2530", "sub": "#6B7785", "line": "#E3E8EE", "band": "#F5F8FB",
    "section": "#0B1F3A", "section_text": "#FFFFFF",
    "up": "#D32F2F", "down": "#1565C0",  # 上げは赤・下げは青（日本の株価表示の色）
    "hot": "#FFE08A",  # 条件に当たった項目の地
    "buy": "#2E7D32", "soon": "#E6A100", "wait": "#B5BEC8",
    "diego": ("#FFF1CC", "#7A5200"), "growth": ("#DCEBFB", "#0C447C"), "both": ("#E4F3E0", "#1E5B21"),
}
# 列：（見出し, 右端のx）。銘柄と区分は左寄せ
X_NAME, X_GROUP = PAD, 348
NUM_COLS = (("price", "株価", 548), ("change", "前日比", 676), ("dd", "高値から", 804), ("gap", "買値まで", 942), ("yield", "利回り", W - PAD))
NAME_W = X_GROUP - X_NAME - 12
NUM_SIZE = 19
GAP_DOT_X = 824  # 「買値まで」の丸（買い時・もうすぐ・待ち）を置く位置。数字の長さに関係なく、たてにそろえる
BUY_SOON_PCT = 5.0  # 買いたい価格まであと何％以内なら「もうすぐ」


def signed(value, digits=1):
    return f"{value:+.{digits}f}%".replace("-", "−")


def price_text(price):
    return f"{price:,.0f}" if price >= 1000 else f"{price:,.1f}".replace(".0", "")


def gap_color(gap):
    if gap is None:
        return C["wait"]
    return C["buy"] if gap <= 0 else C["soon"] if gap <= BUY_SOON_PCT else C["wait"]


def cells(row):
    """1行分の（列のキー, 文字, 文字の色）"""
    out = {"price": (price_text(row["price"]), C["ink"])}
    change = row.get("change")
    out["change"] = (signed(change), C["down"] if change < 0 else C["up"] if change > 0 else C["ink"]) if change is not None else ("—", C["sub"])
    out["dd"] = (signed(-row["dd"]), C["ink"]) if row.get("dd") is not None else ("—", C["sub"])
    out["gap"] = (signed(row["gap"]), C["ink"]) if row.get("gap") is not None else ("—", C["sub"])
    out["yield"] = (f"{row['yield']:.2f}%", C["ink"]) if row.get("yield") else ("—", C["sub"])
    return out


def pages(sections, max_rows=MAX_ROWS):
    """[(見出し, 行のリスト), ...] を、1枚に max_rows 行までに分ける。分かれた見出しには「（続き）」を付ける"""
    out, current, count = [], [], 0
    for title, rows in sections:
        rows = list(rows)
        first = True
        while rows:
            room = max_rows - count
            if room <= 0:
                out.append(current)
                current, count, room = [], 0, max_rows
            take, rows = rows[:room], rows[room:]
            current.append((title if first else f"{title}（続き）", take))
            count += len(take)
            first = False
    if current:
        out.append(current)
    return out


def _width(ax, text_obj):
    """描いた文字の実際の横幅（px。画像は1px＝座標1）"""
    return text_obj.get_window_extent(renderer=ax.figure.canvas.get_renderer()).width


def _name(ax, x, y, name, code, limit):
    """銘柄名とコードを、limit の幅に収めて描く（長いときは小さくし、それでも入らなければ切る）"""
    size = 21
    while True:
        t = ax.text(x, y, name, fontsize=size, color=C["ink"], weight="bold", va="center", zorder=4)
        c = ax.text(x + _width(ax, t) + 8, y + 2, code, fontsize=13, color=C["sub"], va="center", zorder=4)
        if _width(ax, t) + 8 + _width(ax, c) <= limit or len(name) <= 3:
            return
        t.remove()
        c.remove()
        if size > 15:
            size -= 1
        else:
            name = name.rstrip("…")[:-1] + "…"


def _chip(ax, x, y, text, colors):
    bg, fg = colors
    w, h = x_media.text_px(text, 14) + 22, 30
    ax.add_patch(FancyBboxPatch((x, y - h / 2), w, h, boxstyle="round,pad=0,rounding_size=8", facecolor=bg, edgecolor="none", zorder=3))
    ax.text(x + w / 2, y, text, fontsize=14, color=fg, weight="bold", ha="center", va="center", zorder=4)


def render(path, title, subtitle, sections, page_text=""):
    """表の画像を1枚作る。sections: [(見出し, [行, ...]), ...]
    行：{name, code, groups, price, change, dd, gap, yield, hot}（hot は、黄色にする列のキーの集まり）"""
    n_rows = sum(len(rows) for _, rows in sections)
    height = HEAD_H + COLS_H + SECTION_H * len(sections) + ROW_H * n_rows + FOOT_H
    fig = plt.figure(figsize=(W / 100, height / 100), dpi=100, facecolor=C["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(height, 0)
    ax.set_axis_off()

    # 題名
    ax.add_patch(Rectangle((0, 0), W, 10, facecolor=C["section"], edgecolor="none", zorder=2))
    ax.text(PAD, 62, title, fontsize=x_media.fit_size(title, W - PAD * 2 - 90, 32, 22), color=C["ink"], weight="bold", va="center", zorder=4)
    ax.text(PAD, 114, subtitle, fontsize=x_media.fit_size(subtitle, W - PAD * 2, 19, 13), color=C["sub"], va="center", zorder=4)
    if page_text:
        ax.text(W - PAD, 62, page_text, fontsize=18, color=C["sub"], ha="right", va="center", zorder=4)

    # 列の見出し
    y = HEAD_H
    ax.add_patch(Rectangle((0, y), W, COLS_H, facecolor=C["band"], edgecolor="none", zorder=1))
    mid = y + COLS_H / 2
    ax.text(X_NAME, mid, "銘柄", fontsize=15, color=C["sub"], weight="bold", va="center", zorder=4)
    ax.text(X_GROUP, mid, "区分", fontsize=15, color=C["sub"], weight="bold", va="center", zorder=4)
    for _, label, right in NUM_COLS:
        ax.text(right, mid, label, fontsize=15, color=C["sub"], weight="bold", ha="right", va="center", zorder=4)
    y += COLS_H

    for section, rows in sections:
        ax.add_patch(Rectangle((0, y), W, SECTION_H, facecolor=C["section"], edgecolor="none", zorder=1))
        ax.text(PAD, y + SECTION_H / 2, section, fontsize=x_media.fit_size(section, W - PAD * 2 - 110, 18, 13), color=C["section_text"], weight="bold", va="center", zorder=4)
        ax.text(W - PAD, y + SECTION_H / 2, f"{len(rows)}銘柄", fontsize=15, color=C["section_text"], ha="right", va="center", zorder=4)
        y += SECTION_H
        for i, row in enumerate(rows):
            mid = y + ROW_H / 2
            if i:
                ax.plot([PAD, W - PAD], [y, y], color=C["line"], linewidth=1, zorder=2)
            _name(ax, X_NAME, mid, row["name"], row["code"], NAME_W)
            groups = row.get("groups") or []
            both = len(groups) > 1
            _chip(ax, X_GROUP, mid, "両方" if both else groups[0] if groups else "—", C["both"] if both else C["diego"] if groups and groups[0] == "Diego" else C["growth"])
            # 数字（条件に当たった項目は黄色の地）
            hot = row.get("hot") or ()
            for key, _, right in NUM_COLS:
                text, color = cells(row)[key]
                t = ax.text(right, mid, text, fontsize=NUM_SIZE, color=color, weight="bold" if key in hot or key == "change" else "normal", ha="right", va="center", zorder=4)
                if key in hot:
                    w = _width(ax, t) + 20
                    ax.add_patch(FancyBboxPatch((right - w + 10, mid - 19), w, 38, boxstyle="round,pad=0,rounding_size=8", facecolor=C["hot"], edgecolor="none", zorder=2))
                if key == "gap" and row.get("gap") is not None:
                    ax.add_patch(Circle((GAP_DOT_X, mid), 7, facecolor=gap_color(row["gap"]), edgecolor="none", zorder=4))
            y += ROW_H

    # 注記
    ax.plot([PAD, W - PAD], [y + 14, y + 14], color=C["line"], linewidth=1, zorder=2)
    ax.add_patch(FancyBboxPatch((PAD, y + 26), 26, 22, boxstyle="round,pad=0,rounding_size=5", facecolor=C["hot"], edgecolor="none", zorder=3))
    ax.text(PAD + 34, y + 37, "＝知らせる条件に当たった項目", fontsize=14, color=C["sub"], va="center", zorder=4)
    x = PAD + 34 + x_media.text_px("＝知らせる条件に当たった項目", 14) + 30
    for label, color in (("買い時", C["buy"]), ("もうすぐ(5%以内)", C["soon"]), ("待ち", C["wait"])):
        ax.add_patch(Circle((x + 7, y + 37), 7, facecolor=color, edgecolor="none", zorder=4))
        ax.text(x + 20, y + 37, label, fontsize=14, color=C["sub"], va="center", zorder=4)
        x += 20 + x_media.text_px(label, 14) + 22
    ax.text(PAD, y + 70, "買値まで＝買いたい価格（52週安値×110%）までの残り。株価はYahoo!ファイナンス（約20分遅れ）", fontsize=13, color=C["sub"], va="center", zorder=4)

    fig.savefig(path, facecolor=C["bg"])
    plt.close(fig)
    return path
