import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import HTTPException

from ingest import MAX_UPLOAD_BYTES

API = "https://api2.openreview.net"
PDF_URL = "https://openreview.net/pdf?id="
FORUM_URL = "https://openreview.net/forum?id="
HOSTS = {"openreview.net", "www.openreview.net", "api2.openreview.net"}
SESSION_FILE = Path(__file__).resolve().parent.parent / "config" / "openreview.json"
ID = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
ARXIV_IN_BIB = re.compile(
    r"(?:eprint|arxiv(?:id)?)\s*=\s*[{\"]\s*(?:arXiv:)?([0-9]{4}\.[0-9]{4,5})", re.I)
NOT_FOUND = "OpenReview 上找不到这篇论文"


def parse_url(raw) -> str:
    if not isinstance(raw, str):
        raise HTTPException(400, "无效的 OpenReview 链接")
    value = raw.strip()
    if ID.fullmatch(value):
        return value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(400, "无效的 OpenReview 链接") from exc
    if (parsed.scheme != "https" or parsed.hostname not in ("openreview.net", "www.openreview.net")
            or port not in (None, 443) or parsed.username is not None or parsed.password is not None):
        raise HTTPException(400, "只允许 https://openreview.net/forum?id=… 形式的链接")
    ids = parse_qs(parsed.query).get("id") or []
    if len(ids) != 1 or not ID.fullmatch(ids[0]):
        raise HTTPException(400, "链接里没有有效的 OpenReview id")
    return ids[0]


def canonical_url(raw) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    return FORUM_URL + parse_url(raw)


def read_session() -> dict:
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise HTTPException(500, "OpenReview 登录状态文件损坏，请重新登录") from exc
    if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
        return {}
    return data


def _write_session(data: dict) -> None:
    if SESSION_FILE.is_symlink():
        raise HTTPException(400, "OpenReview 登录状态不能是符号链接")
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=SESSION_FILE.parent, prefix=".openreview", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESSION_FILE)
    finally:
        Path(tmp).unlink(missing_ok=True)


def session_status() -> dict:
    data = read_session()
    return {"logged_in": bool(data), "user": data.get("user", ""),
            "saved_at": data.get("saved_at", "")}


def clear_session() -> dict:
    if SESSION_FILE.is_symlink():
        raise HTTPException(400, "OpenReview 登录状态不能是符号链接")
    SESSION_FILE.unlink(missing_ok=True)
    return session_status()


def _token() -> str:
    token = read_session().get("token", "")
    if not token:
        raise HTTPException(401, "请先登录 OpenReview")
    return token


def _validate_target(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(502, "OpenReview 返回了无效地址") from exc
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS
            or port not in (None, 443) or parsed.username is not None
            or parsed.password is not None or parsed.fragment):
        raise HTTPException(502, "OpenReview 返回了非白名单地址")


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_target(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _send(request, limit: int, timeout: float):
    last = ""
    for use_proxy in (True, False):
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler() if use_proxy else urllib.request.ProxyHandler({}),
            _Redirects())
        try:
            with opener.open(request, timeout=timeout) as resp:
                length = (resp.headers.get("Content-Length") or "").strip()
                if length.isdigit() and int(length) > limit:
                    raise HTTPException(413, "OpenReview 文件超过大小上限")
                data = resp.read(limit + 1)
                if len(data) > limit:
                    raise HTTPException(413, "OpenReview 文件超过大小上限")
                return resp.status, data
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(65536)
        except HTTPException:
            raise
        except (urllib.error.URLError, OSError) as exc:
            last = str(getattr(exc, "reason", exc))
    raise HTTPException(502, f"无法连接 OpenReview: {last}")


def _fail(status: int, body: bytes):
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        payload = {}
    name = payload.get("name") if isinstance(payload, dict) else ""
    if status == 401:
        raise HTTPException(401, "OpenReview 登录已失效，请重新登录")
    if status == 403 and name == "ChallengeRequiredError":
        raise HTTPException(403, "OpenReview 要求人机验证：请先登录 OpenReview 再抓取")
    if status == 403:
        raise HTTPException(403, "OpenReview 拒绝访问（403），请确认这篇论文是否公开")
    if status == 404:
        raise HTTPException(404, NOT_FOUND)
    if status == 429:
        raise HTTPException(503, "OpenReview 请求过于频繁（429），请稍后重试")
    if status >= 500:
        raise HTTPException(502, f"OpenReview 服务暂时不可用（HTTP {status}），请稍后重试")
    raise HTTPException(502, f"OpenReview 请求失败（HTTP {status}）")


def _json_get(url: str, token: str, limit: int = 2 * 1024 * 1024, timeout: float = 30):
    _validate_target(url)
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    status, body = _send(request, limit, timeout)
    if status != 200:
        _fail(status, body)
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(502, "OpenReview 返回的元数据不是合法 JSON") from exc


def login(username, password) -> dict:
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        raise HTTPException(400, "请填写 OpenReview 账号和密码")
    if len(username) > 200 or len(password) > 200:
        raise HTTPException(400, "OpenReview 账号或密码过长")
    request = urllib.request.Request(
        API + "/login", method="POST",
        data=json.dumps({"id": username, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    status, body = _send(request, 65536, 30)
    if status == 400:
        raise HTTPException(401, "OpenReview 账号或密码不正确")
    if status == 403:
        raise HTTPException(403, "OpenReview 登录被人机验证拦截，请稍后重试")
    if status != 200:
        _fail(status, body)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(502, "OpenReview 登录响应格式异常") from exc
    token = payload.get("token") if isinstance(payload, dict) else ""
    if not isinstance(token, str) or not token:
        raise HTTPException(502, "OpenReview 登录响应缺少 token")
    user = ""
    if isinstance(payload.get("user"), dict):
        user = str(payload["user"].get("id") or "")
    _write_session({"token": token, "user": user,
                    "saved_at": datetime.now(timezone.utc).isoformat()})
    return session_status()


def save_token(token) -> dict:
    token = str(token or "").strip()
    if not token or len(token) > 4096 or not re.fullmatch(r"[A-Za-z0-9._-]+", token):
        raise HTTPException(400, "无效的 OpenReview token")
    _json_get(API + "/notes?limit=1", token, 65536)
    _write_session({"token": token, "user": "",
                    "saved_at": datetime.now(timezone.utc).isoformat()})
    return session_status()


def _value(content: dict, name: str):
    value = content.get(name)
    return value.get("value") if isinstance(value, dict) else value


def _text(value) -> str:
    return " ".join(str(value or "").split())


def lookup(forum_id: str) -> dict:
    token = _token()
    payload = _json_get(f"{API}/notes?id={quote(forum_id, safe='')}", token)
    notes = payload.get("notes") if isinstance(payload, dict) else None
    notes = [n for n in notes or [] if isinstance(n, dict)]
    if not notes:
        raise HTTPException(404, NOT_FOUND)
    note = next((n for n in notes if n.get("id") == forum_id), None)
    if note is None:
        raise HTTPException(404, NOT_FOUND)
    content = note.get("content") if isinstance(note.get("content"), dict) else {}

    title = _text(_value(content, "title"))
    if not title:
        raise HTTPException(404, f"OpenReview 上没有可用的标题: {forum_id}")
    authors = _value(content, "authors")
    authors = [_text(a) for a in authors if _text(a)] if isinstance(authors, list) else []
    venue = _text(_value(content, "venue") or _value(content, "venueid"))
    abstract = _text(_value(content, "abstract"))
    bibtex = str(_value(content, "_bibtex") or "")
    arxiv = _text(_value(content, "arxiv"))
    if not arxiv:
        hit = ARXIV_IN_BIB.search(bibtex)
        arxiv = hit.group(1) if hit else ""

    year = ""
    stamp = note.get("pdate") or note.get("cdate") or note.get("odate")
    if isinstance(stamp, (int, float)) and stamp > 0:
        year = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).year
    return {
        "openreview_id": forum_id,
        "openreview_url": FORUM_URL + forum_id,
        "title": title,
        "authors": authors,
        "abstract": abstract[:2000],
        "venue": venue,
        "year": year,
        "arxiv_id": arxiv,
        "bibtex": bibtex[:20000],
    }


def pdf_bytes(forum_id: str) -> bytes:
    token = _token()
    url = PDF_URL + quote(forum_id, safe="")
    _validate_target(url)
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/pdf"})
    status, body = _send(request, MAX_UPLOAD_BYTES, 180)
    if status != 200:
        _fail(status, body)
    if not body.startswith(b"%PDF-"):
        raise HTTPException(502, "OpenReview 返回的不是 PDF")
    return body
