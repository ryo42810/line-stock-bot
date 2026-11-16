"""LINEに送った内容を、Notionのデータベースにも転記する（1通＝1行）。

使うもの（GitHubのSecrets）:
  NOTION_TOKEN   … Notionの連携（インテグレーション）のトークン
  NOTION_PAGE_ID … 転記先のページのURLかID。このページの中に「LINE株botログ」のDBを自動で作る
どちらかが無いときは、何もしない（LINEの送信はいつも通り）。
Notionへの転記に失敗しても、botは止めない（ログに出すだけ）。
"""
import datetime
import os
import re

import requests

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
DB_TITLE = "LINE株botログ"
KINDS = {
    "朝の配信": "blue",
    "夕方の配信": "orange",
    "ネタ候補": "purple",
    "来週の予定": "green",
    "通知": "red",
}
JST = datetime.timezone(datetime.timedelta(hours=9))
TEXT_LIMIT = 2000  # Notionの1つのテキストの上限
BLOCK_LIMIT = 100  # 1回で追加できるブロックの上限

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


def flex_texts(node):
    """Flexメッセージ（カード）の中の文字だけを、上から順に取り出す"""
    out = []
    if isinstance(node, dict):
        if node.get("type") == "text" and node.get("text"):
            out.append(node["text"])
        for key in ("header", "hero", "body", "footer"):
            if key in node:
                out += flex_texts(node[key])
        for child in node.get("contents", []) if isinstance(node.get("contents"), list) else [node.get("contents")]:
            out += flex_texts(child)
    elif isinstance(node, list):
        for child in node:
            out += flex_texts(child)
    return out


def to_entries(messages, kind):
    """LINEのメッセージを、Notionの行（タイトル・種類・本文・画像）にまとめる。画像は直前の行に付ける"""
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
                entries.append({"title": "画像", "kind": kind, "text": "", "images": [url]})
            continue
        if mtype == "text":
            text = m.get("text", "")
        elif mtype == "flex":
            lines = [m.get("altText", "")] + flex_texts(m.get("contents"))
            text = "\n".join(line for line in lines if line)
        else:
            continue
        if not text.strip():
            continue
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        row_kind = "通知" if first.startswith(("⚠️", "⏭")) else kind
        entries.append({"title": first[:100], "kind": row_kind, "text": text, "images": []})
    return entries


def body_blocks(entry):
    """本文を段落ブロックに分ける（空行で区切り、長い段落は2000字ずつに切る）"""
    blocks = []
    for para in re.split(r"\n\s*\n", entry["text"]):
        para = para.strip("\n")
        for i in range(0, len(para), TEXT_LIMIT):
            chunk = para[i : i + TEXT_LIMIT]
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}})
    for url in entry["images"]:
        blocks.append({"object": "block", "type": "image", "image": {"type": "external", "external": {"url": url}}})
    return blocks[:BLOCK_LIMIT]


def find_or_create_db(token, page_id):
    """ページの中の「LINE株botログ」DBを探し、無ければ作る"""
    global _db_id
    if _db_id:
        return _db_id
    headers = _headers(token)
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        res = requests.get(f"{API}/blocks/{page_id}/children", headers=headers, params=params, timeout=15)
        res.raise_for_status()
        data = res.json()
        for block in data.get("results", []):
            if block.get("type") == "child_database" and block["child_database"].get("title") == DB_TITLE:
                _db_id = block["id"]
                return _db_id
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    payload = {
        "parent": {"type": "page_id", "page_id": page_id},
        "title": [{"type": "text", "text": {"content": DB_TITLE}}],
        "properties": {
            "名前": {"title": {}},
            "日時": {"date": {}},
            "種類": {"select": {"options": [{"name": k, "color": c} for k, c in KINDS.items()]}},
        },
    }
    res = requests.post(f"{API}/databases", headers=headers, json=payload, timeout=15)
    res.raise_for_status()
    _db_id = res.json()["id"]
    print(f"Notionに「{DB_TITLE}」のDBを作りました")
    return _db_id


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
        db_id = find_or_create_db(token, page_id)
        when = (now or datetime.datetime.now(JST)).isoformat(timespec="seconds")
        for entry in entries:
            payload = {
                "parent": {"database_id": db_id},
                "properties": {
                    "名前": {"title": [{"type": "text", "text": {"content": entry["title"] or "(無題)"}}]},
                    "日時": {"date": {"start": when}},
                    "種類": {"select": {"name": entry["kind"]}},
                },
                "children": body_blocks(entry),
            }
            res = requests.post(f"{API}/pages", headers=_headers(token), json=payload, timeout=15)
            if res.status_code != 200:
                print("Notionへの転記に失敗:", res.status_code, res.text[:300])
                return
        print(f"Notionに{len(entries)}件転記しました")
    except Exception as e:
        print("Notionへの転記に失敗:", type(e).__name__, str(e)[:300])
