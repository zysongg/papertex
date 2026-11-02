import base64
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

KNOWLEDGE_DIR = "_knowledge"
KINDS = ("note", "link")
KIND_LABELS = {"note": "笔记", "link": "链接"}
ID = re.compile(r"^[0-9a-f]{32}$")
URL = re.compile(r"^https?://[^\s<>\"'`]+$")
MAX_TITLE = 200
MAX_URL = 2000
MAX_SOURCE = 200
MAX_TAGS = 20
MAX_TAG = 40
MAX_BODY = 100_000
MAX_META_BYTES = 64 * 1024


class KnowledgeError(ValueError):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def new_id():
    return uuid4().hex


def is_id(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def key_for(item_id):
    return f"{KNOWLEDGE_DIR}/{item_id}"


def now_text():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value, label, limit, required=False, multiline=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise KnowledgeError(f"{label}必须是文本")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.strip("\n") if multiline else value.strip()
    if required and not value:
        raise KnowledgeError(f"{label}不能为空")
    if len(value) > limit:
        raise KnowledgeError(f"{label}不能超过 {limit} 个字符")
    allowed = "\n\t" if multiline else ""
    if any((ord(ch) < 32 and ch not in allowed) or ord(ch) == 127 for ch in value):
        raise KnowledgeError(f"{label}不能包含控制字符")
    return value


def _url(value):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise KnowledgeError("链接必须是文本")
    value = value.strip()
    if not value:
        return ""
    if len(value) > MAX_URL or not URL.fullmatch(value):
        raise KnowledgeError("链接必须是 http(s) 地址，最多 2000 字符")
    return value


def _tags(value):
    if value is None:
        return []
    if isinstance(value, str):
        raw = re.split(r"[,，]", value)
    elif isinstance(value, list):
        raw = value
    else:
        raise KnowledgeError("标签必须是数组或逗号分隔的文本")
    out = []
    for entry in raw:
        if not isinstance(entry, str):
            raise KnowledgeError("标签必须是文本")
        name = entry.strip()
        if not name:
            continue
        if len(name) > MAX_TAG:
            raise KnowledgeError(f"单个标签不能超过 {MAX_TAG} 个字符")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
            raise KnowledgeError("标签不能包含控制字符")
        if name not in out:
            out.append(name)
    if len(out) > MAX_TAGS:
        raise KnowledgeError(f"标签不能超过 {MAX_TAGS} 个")
    return out


def normalize(payload):
    if not isinstance(payload, dict):
        raise KnowledgeError("请求内容必须是对象")
    kind = payload.get("kind")
    if kind not in KINDS:
        raise KnowledgeError("类型只能是 note 或 link")
    title = _text(payload.get("title"), "标题", MAX_TITLE, required=True)
    url = _url(payload.get("url"))
    if kind == "link" and not url:
        raise KnowledgeError("链接资料必须填写 http(s) 链接")
    return {
        "kind": kind,
        "title": title,
        "url": url,
        "source": _text(payload.get("source"), "来源", MAX_SOURCE),
        "tags": _tags(payload.get("tags")),
        "body": _text(payload.get("body"), "正文", MAX_BODY, multiline=True),
    }


def build_meta(fields, existing=None):
    now = now_text()
    created = existing.get("created_at") if isinstance(existing, dict) else None
    if not isinstance(created, str) or not created:
        created = now
    return {
        "kind": fields["kind"],
        "title": fields["title"],
        "url": fields["url"],
        "source": fields["source"],
        "tags": list(fields["tags"]),
        "has_body": bool(fields["body"].strip()),
        "created_at": created,
        "updated_at": now,
    }


def parse_meta(data, item_id):
    if not is_id(item_id) or not isinstance(data, dict):
        return None
    try:
        fields = normalize({
            "kind": data.get("kind"),
            "title": data.get("title"),
            "url": data.get("url", ""),
            "source": data.get("source", ""),
            "tags": data.get("tags", []),
            "body": "",
        })
    except KnowledgeError:
        return None
    item = {
        "id": item_id,
        "key": key_for(item_id),
        **{name: value for name, value in fields.items() if name != "body"},
    }
    item["has_body"] = bool(data.get("has_body"))
    item["created_at"] = data.get("created_at") if isinstance(data.get("created_at"), str) else ""
    item["updated_at"] = data.get("updated_at") if isinstance(data.get("updated_at"), str) else ""
    return item


def parse_listing(lines):
    items, seen = [], set()
    for line in lines:
        if "\t" not in line:
            continue
        item_id, encoded = line.split("\t", 1)
        if not is_id(item_id) or item_id in seen:
            continue
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            continue
        if len(raw) > MAX_META_BYTES:
            continue
        try:
            meta = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        item = parse_meta(meta, item_id)
        if item is None:
            continue
        seen.add(item_id)
        items.append(item)
    items.sort(key=lambda item: (item["updated_at"], item["title"].lower()), reverse=True)
    return items
