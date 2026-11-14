"""Claude APIの呼び出しをまとめる（モデルの選び方と、使いすぎ防止）

- ふだんは Opus 5.5 を使う（銘柄選び・X投稿・夕方の解説・ネタ候補・優待の読み取り・決算要約、すべて）
  Fable 5.2 は kujira-note（noteの週次・月次・徹底解説）に回す（2026/11/15 本人の指定）
- その月（クレジットの更新日から1ヶ月）に、このbotで使った金額が BUDGET_USD を超えたら、Sonnet 5.5 に切り替える
- 上のモデルが失敗したとき（使えない・混んでいるなど）も、その回だけ Sonnet 5.5 でやり直す
- Haiku は使わない

使った金額は budget/朝・夕方・ネタ候補 ごとのファイルに記録して、足し合わせて判断する
（3つのワークフローが同時に保存しても、ぶつからないように分けている）。
kujira-note（noteの週次・月次）の分はここでは数えられないので、その分を残した額を BUDGET_USD にしている。
"""
import datetime
import json
import os
import time
from pathlib import Path

import anthropic

PRIMARY_MODEL = (os.environ.get("AI_MODEL") or "claude-opus-5-5").strip()
SAVER_MODEL = "claude-sonnet-5-5"
# Maxプランの月100ドルのAPIクレジットのうち、このbotで使ってよい額（残りの50ドルは kujira-note の分）
BUDGET_USD = float(os.environ.get("BUDGET_USD") or 50)
CREDIT_DAY = int(os.environ.get("CREDIT_DAY") or 9)  # クレジットが入る日（毎月9日）
BUDGET_DIR = Path("budget")
DEFAULT_EFFORT = "medium"  # 考える量。銘柄選びとネタ候補は high にしている（呼び出し側で指定）
THINKING_MIN_TOKENS = 16000  # Opus・Fable は考える分（thinking）も出力の上限に数えるので、多めに取る
THINKING_MODELS = ("claude-opus", "claude-fable")

# 料金の目安（USD／100万トークン：入力, 出力）。表に無いモデルは、いちばん高いものとして数える
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-fable-5-2": (8.00, 40.00),
}
WEB_SEARCH_PRICE = 0.01  # Web検索 1回あたり


def cost_of(response):
    """1回の返事の料金（USD）"""
    usage = response.usage
    model = str(getattr(response, "model", "") or PRIMARY_MODEL)
    price_in, price_out = next((p for name, p in PRICES.items() if model.startswith(name)), max(PRICES.values()))
    searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
    return ((usage.input_tokens or 0) * price_in + (usage.output_tokens or 0) * price_out) / 1_000_000 + searches * WEB_SEARCH_PRICE


def cycle_start(day):
    """クレジットの期間の始まり（day を含む期間）"""
    if day.day >= CREDIT_DAY:
        return day.replace(day=CREDIT_DAY)
    first = day.replace(day=1) - datetime.timedelta(days=1)
    return first.replace(day=min(CREDIT_DAY, 28))


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def spent(day):
    """今の期間に、このbotで使った金額（朝・夕方・ネタ候補の合計）"""
    cycle = cycle_start(day).isoformat()
    return sum(d.get("usd", 0.0) for d in (_read(p) for p in BUDGET_DIR.glob("*.json")) if d.get("cycle") == cycle)


def record(mode, day, usd):
    BUDGET_DIR.mkdir(exist_ok=True)
    path = BUDGET_DIR / f"{mode}.json"
    data = _read(path)
    cycle = cycle_start(day).isoformat()
    if data.get("cycle") != cycle:
        data = {"cycle": cycle, "usd": 0.0}
    data["usd"] = round(data["usd"] + usd, 4)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def choose_model(day):
    used = spent(day)
    model = PRIMARY_MODEL if used < BUDGET_USD else SAVER_MODEL
    print(f"今月（{cycle_start(day):%m/%d}〜）の使用額: 約${used:.2f} / 上限 ${BUDGET_USD:.0f} → {model}")
    return model


class Client:
    """anthropic.Anthropic() の代わりに使う。client.messages.create(...) で呼べる。
    渡された model は無視して、予算に合わせたモデルで呼ぶ。effort=... で考える量を指定できる"""

    def __init__(self, mode, day, raw=None):
        self.mode, self.day = mode, day
        self.raw = raw or anthropic.Anthropic()
        self.model = choose_model(day)
        self.messages = self

    def _params(self, model, kwargs, effort):
        params = dict(kwargs, model=model)
        if model.startswith(THINKING_MODELS):
            params["max_tokens"] = max(params.get("max_tokens", 4000), THINKING_MIN_TOKENS)
            params["output_config"] = {**params.get("output_config", {}), "effort": effort}
        else:
            params["max_tokens"] = max(params.get("max_tokens", 4000), 8000)
        return params

    def create(self, effort=DEFAULT_EFFORT, **kwargs):
        model = self.model
        for attempt in range(2):
            try:
                response = self.raw.messages.create(**self._params(model, kwargs, effort))
                break
            except (anthropic.BadRequestError, anthropic.NotFoundError, anthropic.PermissionDeniedError,
                    anthropic.InternalServerError, anthropic.RateLimitError, anthropic.APIConnectionError) as e:
                # 途中まで考えた会話（pause_turn の続き）は、別のモデルには引き継げないのでやり直さない
                continuing = any(m.get("role") == "assistant" for m in kwargs.get("messages", []))
                if model == SAVER_MODEL or continuing or attempt:
                    raise
                print(f"{model} が失敗したので、{SAVER_MODEL} でやり直します: {type(e).__name__}: {str(e)[:200]}")
                model = SAVER_MODEL
                time.sleep(2)
        try:
            record(self.mode, self.day, cost_of(response))
        except Exception as e:
            print("使用額の記録に失敗:", e)
        return response


def text_of(response):
    """返事の文章（考えた部分や検索結果を除いた、最後の文章）"""
    texts = []
    for block in response.content:
        if block.type in ("server_tool_use", "web_search_tool_result"):
            texts = []
        elif block.type == "text":
            texts.append(block.text)
    return "".join(texts).strip()
