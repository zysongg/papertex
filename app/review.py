"""审稿任务的字段校验、Markdown 骨架与模板解析；不接触文件系统。"""

import re
from datetime import date, datetime, timezone

SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DUE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

STATUSES = ("pending", "reading", "writing", "submitted", "discussion", "closed")
STATUS_LABELS = {
    "pending": "待读",
    "reading": "阅读中",
    "writing": "撰写中",
    "submitted": "已提交",
    "discussion": "讨论期",
    "closed": "已结束",
}

MAX_TITLE = 300
MAX_VENUE_LABEL = 120
MAX_SHORT = 40
MAX_NOTE = 2000
MAX_BODY = 200_000

DEFAULT_TEMPLATE_NAME = "通用"
DEFAULT_TEMPLATE = {
    "fields": ["rating", "confidence"],
    "sections": [
        "Summary and main contributions",
        "Strengths",
        "Weaknesses",
        "Questions for the authors",
        "Missing or related work",
        "Suggestions for improvement",
        "Overall assessment",
    ],
}

# Extra templates live in config/review-templates.json and are authored by the
# reviewer; nothing here enumerates venues on their behalf.
KNOWN_FIELDS = ("rating", "confidence")


class ReviewError(ValueError):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def now_text():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_segment(value, label):
    if not isinstance(value, str):
        raise ReviewError(f"{label}必须是文本")
    value = value.strip()
    if not value:
        raise ReviewError(f"{label}不能为空")
    if not SEGMENT.fullmatch(value):
        raise ReviewError(f"{label}只能包含字母、数字、点、下划线、短横线，且以字母或数字开头")
    return value


def key_of(venue, submission):
    return f"{clean_segment(venue, '会议')}/{clean_segment(submission, '投稿号')}"


def parse_key(key):
    if not isinstance(key, str) or key.count("/") != 1:
        raise ReviewError("审稿任务编号应为「会议/投稿号」")
    venue, submission = key.split("/")
    return clean_segment(venue, "会议"), clean_segment(submission, "投稿号")


def _text(value, label, limit, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ReviewError(f"{label}必须是文本")
    value = value.strip()
    if any(ord(ch) < 32 or ch == "\x7f" for ch in value):
        raise ReviewError(f"{label}不能包含控制字符")
    if required and not value:
        raise ReviewError(f"{label}不能为空")
    if len(value) > limit:
        raise ReviewError(f"{label}不能超过 {limit} 个字符")
    return value


def clean_due(value):
    due = _text(value, "截止日期", 10)
    if not due:
        return ""
    if not DUE.fullmatch(due):
        raise ReviewError("截止日期格式应为 YYYY-MM-DD")
    try:
        date.fromisoformat(due)
    except ValueError as exc:
        raise ReviewError(f"截止日期不合法：{exc}") from exc
    return due


def clean_status(value):
    status = _text(value, "状态", 20)
    if not status:
        return STATUSES[0]
    if status not in STATUSES:
        raise ReviewError("状态只能是：" + "、".join(STATUS_LABELS[s] for s in STATUSES))
    return status


def new_task(payload, existing=None):
    """校验创建/更新输入；existing 提供未提交字段的旧值。"""
    if not isinstance(payload, dict):
        raise ReviewError("请求体必须是 JSON 对象")
    old = existing if isinstance(existing, dict) else {}
    venue = clean_segment(payload.get("venue") or old.get("venue") or "", "会议")
    submission = clean_segment(payload.get("submission") or old.get("submission") or "", "投稿号")
    task = {
        "venue": venue,
        "submission": submission,
        "title": _text(payload.get("title", old.get("title")), "稿件标题", MAX_TITLE),
        "venue_label": _text(payload.get("venue_label", old.get("venue_label")), "会议全称",
                             MAX_VENUE_LABEL),
        "round": _text(payload.get("round", old.get("round")), "轮次", MAX_SHORT),
        "due": clean_due(payload.get("due", old.get("due"))),
        "status": clean_status(payload.get("status", old.get("status"))),
        "rating": _text(payload.get("rating", old.get("rating")), "评分", MAX_SHORT),
        "confidence": _text(payload.get("confidence", old.get("confidence")), "置信度", MAX_SHORT),
        "note": _text(payload.get("note", old.get("note")), "备注", MAX_NOTE),
        "purged_at": _text(payload.get("purged_at", old.get("purged_at")), "清除时间", 40),
    }
    task["key"] = f"{venue}/{submission}"
    return task


def parse_task(raw, venue, submission):
    """读取时保持宽容：非法字段回落默认值，绝不让列表接口 500。"""
    data = raw if isinstance(raw, dict) else {}
    text = data.get
    def_str = lambda value, limit: (  # noqa: E731
        value if isinstance(value, str) and len(value.strip()) <= limit else ""
    )
    due = def_str(text("due"), 10)
    if due and not DUE.fullmatch(due):
        due = ""
    status = text("status")
    if not isinstance(status, str) or status not in STATUSES:
        status = STATUSES[0]
    return {
        "venue": venue,
        "submission": submission,
        "key": f"{venue}/{submission}",
        "title": def_str(text("title"), MAX_TITLE).strip(),
        "venue_label": def_str(text("venue_label"), MAX_VENUE_LABEL).strip(),
        "round": def_str(text("round"), MAX_SHORT).strip(),
        "due": due,
        "status": status,
        "rating": def_str(text("rating"), MAX_SHORT).strip(),
        "confidence": def_str(text("confidence"), MAX_SHORT).strip(),
        "note": def_str(text("note"), MAX_NOTE).strip(),
        "purged_at": def_str(text("purged_at"), 40).strip(),
        "created_at": def_str(text("created_at"), 40).strip(),
        "updated_at": def_str(text("updated_at"), 40).strip(),
    }


def deadline_of(due, today=None):
    """返回剩余天数与是否逾期；没有截止日时两者为 None。"""
    if not due or not DUE.fullmatch(due):
        return None, False
    try:
        target = date.fromisoformat(due)
    except ValueError:
        return None, False
    current = today or datetime.now(timezone.utc).date()
    return (target - current).days, target < current


def templates_from(raw):
    """解析 config/review-templates.json；始终保证「通用」存在。"""
    templates = {DEFAULT_TEMPLATE_NAME: dict(DEFAULT_TEMPLATE)}
    if not isinstance(raw, dict):
        return templates
    for name, body in raw.items():
        label = _text(name, "模板名", MAX_SHORT, required=True)
        if not isinstance(body, dict) or label == DEFAULT_TEMPLATE_NAME:
            continue
        sections = body.get("sections")
        if not isinstance(sections, list):
            continue
        cleaned = []
        for section in sections[:40]:
            text = _text(section, "小节标题", 120, required=True)
            if text:
                cleaned.append(text)
        if not cleaned:
            continue
        fields = body.get("fields")
        allowed = [f for f in (fields if isinstance(fields, list) else [])
                   if isinstance(f, str) and f in KNOWN_FIELDS]
        templates[label] = {"sections": cleaned, "fields": sorted(set(allowed)) or
                            list(DEFAULT_TEMPLATE["fields"])}
    return templates


def skeleton(template):
    """生成带小节标题的初稿；正文由审稿人自己填写。"""
    sections = template.get("sections") or DEFAULT_TEMPLATE["sections"]
    lines = ["<!-- papertex 审稿笔记：本文件是唯一事实源 -->", ""]
    for name in sections:
        lines += [f"## {name}", ""]
    return "\n".join(lines)


def render_export(task, body):
    """拼成可直接粘贴进投稿系统的纯文本。"""
    head = [f"# {task['venue']} / {task['submission']}"]
    if task.get("title"):
        head.append(f"稿件：{task['title']}")
    if task.get("round"):
        head.append(f"轮次：{task['round']}")
    if task.get("due"):
        head.append(f"截止：{task['due']}")
    scores = []
    if task.get("rating"):
        scores.append(f"评分：{task['rating']}")
    if task.get("confidence"):
        scores.append(f"置信度：{task['confidence']}")
    if scores:
        head.append(" / ".join(scores))
    parts = ["\n".join(head), (body or "").strip()]
    return "\n\n".join(part for part in parts if part) + "\n"
