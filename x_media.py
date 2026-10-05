"""X投稿用の画像づくりと、Xへの自動投稿"""
import datetime
import os
import re
import subprocess
import textwrap
import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

IMAGE_DIR = "images"
IMAGE_KEEP_DAYS = 14
REPO = os.environ.get("GITHUB_REPOSITORY", "ryo42810/line-stock-bot")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

# Xの自動投稿（リポジトリの Variables で X_AUTO_POST=on のときだけ投稿する）
X_AUTO_POST = (os.environ.get("X_AUTO_POST") or "").strip().lower() == "on"
X_KEYS = {k: (os.environ.get(k) or "").strip() for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")}
X_POST_INTERVAL_MIN = 15  # 同時に何本も出すと伸びにくいので、投稿の間隔をあける
X_NG_WORDS = ["絶対上がる", "必ず上がる", "絶対に上がる", "今すぐ買え", "買うべき", "儲かる"]

COLORS = {
    "bg": "#FFFFFF",
    "ink": "#1F2328",
    "sub": "#6E7781",
    "up": "#1DB446",
    "down": "#E0352B",
    "line": "#D0D7DE",
}


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
    return COLORS["up"] if change >= 0 else COLORS["down"]


def render_stock_card(path, title, subtitle, stocks, accent, get_history):
    """3銘柄までの紹介カード（銘柄名・前日比・理由・3ヶ月チャート）"""
    fig = plt.figure(figsize=(10.8, 13.5), dpi=100, facecolor=COLORS["bg"])
    fig.add_artist(plt.Rectangle((0, 0.88), 1, 0.12, transform=fig.transFigure, color=accent))
    fig.text(0.06, 0.94, title, fontsize=40, color="white", weight="bold", va="center")
    fig.text(0.06, 0.905, subtitle, fontsize=20, color="white", va="center")

    slot = 0.80 / 3
    for i, s in enumerate(stocks[:3]):
        top = 0.86 - slot * i
        fig.text(0.06, top - 0.03, f"{'①②③'[i]} {s['name']}（{s['code']}）", fontsize=28, color=COLORS["ink"], weight="bold", va="top")
        change = s.get("change")
        if change is not None:
            fig.text(0.94, top - 0.03, f"前日比 {change:+.1f}%", fontsize=24, color=_change_color(change), weight="bold", va="top", ha="right")
        reason = s.get("buy_comment") or s.get("headline") or ""
        fig.text(0.06, top - 0.09, "\n".join(textwrap.wrap(reason, 20)[:3]), fontsize=21, color=COLORS["ink"], va="top", linespacing=1.5)

        try:
            closes = get_history(s["code"])["Close"].iloc[-60:]
            ax = fig.add_axes([0.62, top - slot + 0.04, 0.32, slot - 0.12])
            color = _change_color(float(closes.iloc[-1]) - float(closes.iloc[0]))
            ax.plot(range(len(closes)), closes.values, color=color, linewidth=3)
            ax.fill_between(range(len(closes)), closes.values, float(closes.min()), color=color, alpha=0.08)
            ax.set_axis_off()
            fig.text(0.94, top - slot + 0.03, "3ヶ月チャート", fontsize=14, color=COLORS["sub"], ha="right")
        except Exception as e:
            print(f"チャート描画失敗 ({s['code']}):", e)

        if i < min(len(stocks), 3) - 1:
            fig.add_artist(plt.Line2D([0.06, 0.94], [top - slot + 0.01] * 2, transform=fig.transFigure, color=COLORS["line"], linewidth=1.5))

    fig.text(0.94, 0.03, "#株クラ", fontsize=18, color=COLORS["sub"], ha="right")
    fig.savefig(path, facecolor=COLORS["bg"])
    plt.close(fig)
    return path


def render_performance_chart(path, rows, title):
    """買い場候補の1週間後の成績（横棒グラフ）"""
    rows = sorted(rows, key=lambda r: r["change"])[-12:]
    fig, ax = plt.subplots(figsize=(10.8, 13.5), dpi=100, facecolor=COLORS["bg"])
    fig.subplots_adjust(left=0.38, right=0.9, top=0.84, bottom=0.08)
    fig.text(0.06, 0.93, title, fontsize=36, color=COLORS["ink"], weight="bold", va="center")
    avg = sum(r["change"] for r in rows) / len(rows)
    fig.text(0.06, 0.885, f"平均 {avg:+.1f}%（{len(rows)}銘柄）", fontsize=24, color=_change_color(avg), weight="bold", va="center")

    labels = [f"{r['name'][:10]}（{r['code']}）" for r in rows]
    values = [r["change"] for r in rows]
    ax.barh(labels, values, color=[_change_color(v) for v in values], height=0.6)
    ax.axvline(0, color=COLORS["ink"], linewidth=1)
    for y, v in enumerate(values):
        ax.text(v, y, f" {v:+.1f}% " if v >= 0 else f" {v:+.1f}% ", va="center", ha="left" if v >= 0 else "right", fontsize=18, color=COLORS["ink"])
    ax.tick_params(axis="y", labelsize=18, length=0)
    ax.tick_params(axis="x", labelsize=14, colors=COLORS["sub"])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    span = max(abs(v) for v in values) * 1.4 or 1
    ax.set_xlim(-span if min(values) < 0 else 0, span)
    fig.text(0.94, 0.03, "#株クラ", fontsize=18, color=COLORS["sub"], ha="right")
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
