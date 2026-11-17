"""LINEに送った内容を、Notionにも転記する（1日＝1ページ。1通ずつトグルで並べる）。

使うもの（GitHubのSecrets）:
  NOTION_TOKEN   … Notionの連携（インテグレーション）のトークン
  NOTION_PAGE_ID … 転記先のページのURLかID。このページの中に「LINE株botログ」のDBを自動で作る
どちらかが無いときは、何もしない（LINEの送信はいつも通り）。
Notionへの転記に失敗しても、botは止めない（ログに出すだけ）。

Notionでの見え方:
  DB「LINE株botログ」… 1日＝1行（名前「11/17(火)の配信」・日付・件数）
  その日のページ … 1通＝1つのトグル。見出しは「06:45 朝｜1行目」
                   中身は1行＝1つの箇条書き。X下書きはコピペしやすいよう普通の文章のまま。画像はその下に付ける
"""
import datetime
import os
import re

import requests

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
DB_TITLE = "LINE株botログ"
PROPERTIES = {"日付": {"date": {}}, "件数": {"number": {}}}
LABELS = {"朝の配信": "朝", "夕方の配信": "夕方", "ネタ候補": "ネタ候補", "来週の予定": "来週の予定", "リプ案": "リプ案", "通知": "⚠️ 通知"}
JST = datetime.timezone(datetime.timedelta(hours=9))
TEXT_LIMIT = 2000  # Notionの1つのテキストの上限
CHILD_LIMIT = 100  # 1つのブロックの下に一度に入れられる数
TITLE_LIMIT = 80  # トグルの見出しに使う1行目の長さ
BULLET_MARKS = ("・", "- ", "• ", "● ")

_kind = "朝の配信"
_db_id = None


def set_kind(kind):
    """これから送る内容の種類（朝の配信・夕方の配信・ネタ候補・来週の予定）を決める"""
    global _kind
    _kind = kind


def page_id_from(value):
    """ページのURLでもIDでも受け取れるようにする（最後の32桁の英数字を取り出す）"""
    found = re.findall(r"[0-9a-fA-F]{32}", (value or "").replace("-", ""))
    return found[-1] if found else ""


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _rich(text):
    return [{"type": "text", "text": {"content": text[:TEXT_LIMIT]}}]


def flex_texts(node):
    """Flexメッセージ（カード）の中の文字だけを、上から順に取り出す"""
    out = []
    if isinstance(node, dict):
        if node.get("type") == "text" and node.get("text"):
            out.append(node["text"])
        for key in ("header", "hero", "body", "footer"):
            if key in node:
                out += flex_texts(node[key])
        contents = node.get("contents")
        if isinstance(contents, (list, dict)):
            out += flex_texts(contents)
    elif isinstance(node, list):
        for child in node:
            out += flex_texts(child)
    return out


def is_x_draft(text):
    """最後の行がハッシュタグならX下書き（ハッシュタグはプログラムで最後に付けている）"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and lines[-1].startswith("#")


def to_entries(messages, kind):
    """LINEのメッセージを、トグル1つ分（見出し・種類・中身の行・画像）にまとめる。画像は直前のトグルに付ける"""
    entries = []
    for m in messages:
        mtype = m.get("type")
        if mtype == "image":
            url = m.get("originalContentUrl")
            if not url:
                continue
            if entries:
                entries[-1]["images"].append(url)
            else:
                entries.append({"title": "画像", "kind": kind, "lines": [], "x_draft": False, "images": [url]})
            continue
        if mtype == "text":
            text = m.get("text", "")
            lines = [line for line in text.splitlines() if line.strip()]
            title = lines[0].strip() if lines else ""
            body = lines[1:] if len(title) <= TITLE_LIMIT else lines  # 見出しで切れるときは本文にも全文を残す
            x_draft = is_x_draft(text)
        elif mtype == "flex":
            title = m.get("altText", "").strip()
            body = [line for line in flex_texts(m.get("contents")) if line.strip()]
            x_draft = False
        else:
            continue
        if not title and not body:
            continue
        row_kind = "通知" if title.startswith(("⚠️", "⏭")) else kind
        if x_draft:
            title = f"X下書き：{title}"
            body = [line for line in m.get("text", "").splitlines()]  # X下書きは空行も含めてそのまま
        entries.append({"title": title, "kind": row_kind, "lines": body, "x_draft": x_draft, "images": []})
    return entries


def toggle_heading(entry, now):
    label = LABELS.get(entry["kind"], entry["kind"])
    if entry["kind"] == "通知":
        return f"{label}｜{entry['title']}"[:TEXT_LIMIT]
    title = entry["title"] if len(entry["title"]) <= TITLE_LIMIT else entry["title"][:TITLE_LIMIT] + "…"
    return f"{now:%H:%M} {label}｜{title}"


def toggle_children(entry):
    """トグルの中身。ふだんは1行＝1つの箇条書き、X下書きは普通の文章のまま"""
    blocks = []
    if entry["x_draft"]:
        text = "\n".join(entry["lines"]).strip("\n")
        for i in range(0, len(text), TEXT_LIMIT):
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich(text[i : i + TEXT_LIMIT])}})
    else:
        for line in entry["lines"]:
            line = line.strip()
            for mark in BULLET_MARKS:
                if line.startswith(mark):
                    line = line[len(mark) :].strip()
                    break
            for i in range(0, len(line), TEXT_LIMIT):
                blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich(line[i : i + TEXT_LIMIT])}})
    for url in entry["images"]:
        blocks.append({"object": "block", "type": "image", "image": {"type": "external", "external": {"url": url}}})
    return blocks[:CHILD_LIMIT]


def toggle_block(entry, now):
    block = {"object": "block", "type": "toggle", "toggle": {"rich_text": _rich(toggle_heading(entry, now))}}
    children = toggle_children(entry)
    if children:
        block["toggle"]["children"] = children
    return block


def day_title(day):
    return f"{day:%m/%d}({'月火水木金土日'[day.weekday()]})の配信"


def find_or_create_db(token, page_id):
    """ページの中の「LINE株botログ」DBを探し、無ければ作る。足りない列があれば足す"""
    global _db_id
    if _db_id:
        return _db_id
    headers = _headers(token)
    cursor = None
    found = None
    while not found:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        res = requests.get(f"{API}/blocks/{page_id}/children", headers=headers, params=params, timeout=15)
        res.raise_for_status()
        data = res.json()
        found = next((b["id"] for b in data.get("results", []) if b.get("type") == "child_database" and b["child_database"].get("title") == DB_TITLE), None)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    if found:
        res = requests.get(f"{API}/databases/{found}", headers=headers, timeout=15)
        res.raise_for_status()
        missing = {k: v for k, v in PROPERTIES.items() if k not in res.json().get("properties", {})}
        if missing:
            requests.patch(f"{API}/databases/{found}", headers=headers, json={"properties": missing}, timeout=15).raise_for_status()
        _db_id = found
        return _db_id

    payload = {
        "parent": {"type": "page_id", "page_id": page_id},
        "title": [{"type": "text", "text": {"content": DB_TITLE}}],
        "properties": {"名前": {"title": {}}, **PROPERTIES},
    }
    res = requests.post(f"{API}/databases", headers=headers, json=payload, timeout=15)
    res.raise_for_status()
    _db_id = res.json()["id"]
    print(f"Notionに「{DB_TITLE}」のDBを作りました")
    return _db_id


def title_property(db_props):
    return next((name for name, p in db_props.items() if p.get("type") == "title"), "名前")


def find_or_create_day_page(token, db_id, day):
    """その日のページを探し、無ければ作る。(ページID, 今の件数) を返す"""
    headers = _headers(token)
    res = requests.post(f"{API}/databases/{db_id}/query", headers=headers, json={"filter": {"property": "日付", "date": {"equals": day.isoformat()}}, "page_size": 1}, timeout=15)
    res.raise_for_status()
    results = res.json().get("results", [])
    if results:
        page = results[0]
        return page["id"], page["properties"].get("件数", {}).get("number") or 0
    db = requests.get(f"{API}/databases/{db_id}", headers=headers, timeout=15)
    db.raise_for_status()
    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            title_property(db.json().get("properties", {})): {"title": _rich(day_title(day))},
            "日付": {"date": {"start": day.isoformat()}},
            "件数": {"number": 0},
        },
    }
    res = requests.post(f"{API}/pages", headers=headers, json=payload, timeout=15)
    res.raise_for_status()
    return res.json()["id"], 0


def log_messages(messages, now=None):
    """LINEに送ったメッセージをNotionに転記する。失敗してもエラーにしない"""
    token = (os.environ.get("NOTION_TOKEN") or "").strip()
    page_id = page_id_from(os.environ.get("NOTION_PAGE_ID"))
    if not token or not page_id:
        return
    try:
        entries = to_entries(messages, _kind)
        if not entries:
            return
        now = now or datetime.datetime.now(JST)
        db_id = find_or_create_db(token, page_id)
        day_page, count = find_or_create_day_page(token, db_id, now.date())
        headers = _headers(token)
        toggles = [toggle_block(e, now) for e in entries]
        for i in range(0, len(toggles), 10):  # 1回の追加は10通ずつ（中身の数の上限にかからないように）
            res = requests.patch(f"{API}/blocks/{day_page}/children", headers=headers, json={"children": toggles[i : i + 10]}, timeout=30)
            if res.status_code != 200:
                print("Notionへの転記に失敗:", res.status_code, res.text[:300])
                return
        requests.patch(f"{API}/pages/{day_page}", headers=headers, json={"properties": {"件数": {"number": count + len(entries)}}}, timeout=15)
        print(f"Notionに{len(entries)}件転記しました")
    except Exception as e:
        print("Notionへの転記に失敗:", type(e).__name__, str(e)[:300])
