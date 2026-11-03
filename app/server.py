import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from ingest import (
    MAX_UPLOAD_BYTES, arxiv_http as _arxiv_http, arxiv_id, arxiv_source_bytes,
    extract_archive as _extract_archive, find_entry as _find_entry,
    make_tgz as _make_tgz, read_limited,
)
import knowledge as kb
import ieee as ieee_xplore
import openreview as orx
import settings as config_store
from request_security import LocalRequestGuard, validate_bind_host
from operation_progress import get_progress, tracked_operation, update_progress

BASE = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE / "config" / "config.json"
CFG = config_store.load(CONFIG_FILE)
LIB_LOCAL = Path(os.path.expanduser(CFG["local_library"]))
LIB_REMOTE = CFG["remote_library"].rstrip("/")
WORKSHOP = CFG.get("remote_workshop", "").rstrip("/")
REMOTE_NAME = CFG.get("remote_name") or "远端服务器"
REMOTE_CONFIGURED = config_store.is_configured(CFG)
SSH_TARGET = config_store.ssh_target(CFG) if REMOTE_CONFIGURED else []
PROJECTS_FILE = BASE / "config" / "projects.json"
PROJECTS_LOCAL_FILE = BASE / "config" / "projects.local.json"
STARS_FILE = BASE / "config" / "stars.json"

SEG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SLUG = re.compile(r"^\d{4}-[A-Za-z0-9]+-[A-Za-z0-9][A-Za-z0-9-]*$")
YEAR = re.compile(r"^\d{4}$")
KNOWLEDGE_KEY = re.compile(r"^_knowledge/[0-9a-f]{32}$")
ARXIV_VENUE = re.compile(
    r"\b(ICML|ICLR|NeurIPS|NIPS|AAAI|IJCAI|KDD|WWW|CVPR|ICCV|ECCV|ACL|EMNLP|NAACL|"
    r"SIGIR|CIKM|ICDM|ICDE|SIGMOD|VLDB|TSP|TKDE|TNNLS|TPAMI|TCOM|TWC|IoTJ)\b"
    r"(?:\s*['’]?\s*(20\d{2}))?",
    re.I,
)

DUP_HASH_LIMIT = 200
RECOVERY_ID = re.compile(r"^[0-9a-f]{32}$")
RECOVERY_CONFIRM = "清理"

PROJECT_VIEW_TYPES = {
    "pdf": ("pdf",),
    "png": ("png",),
    "jpg": ("jpg", "jpeg"),
    "webp": ("webp",),
    "gif": ("gif",),
}
PROJECT_FILE_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}
PROJECT_EXT_VIEW = {
    ext: view for view, exts in PROJECT_VIEW_TYPES.items() for ext in exts
}
PROJECT_RECENT_PRUNE = (
    ".*", "__pycache__", "node_modules", "references",
)
PROJECT_LISTING_LIMIT = 2 * 1024 * 1024
PROJECT_MAX_ENTRIES = 2000
PROJECT_MAX_FILE_BYTES = 100 * 1024 * 1024

validate_bind_host(CFG.get("host", "127.0.0.1"))
if REMOTE_CONFIGURED:
    for configured_path in (LIB_REMOTE, WORKSHOP):
        if (not configured_path.startswith("/") or configured_path == "/"
                or any(p in (".", "..", "") for p in configured_path.split("/")[1:])
                or any(ord(c) < 32 for c in configured_path)):
            raise ValueError("远端资料库和项目根必须是非根目录的绝对路径")

app = FastAPI(title="papertex")
PDF_TOKEN = secrets.token_urlsafe(32)
app.add_middleware(LocalRequestGuard, port=int(CFG.get("port", 8817)), pdf_token=PDF_TOKEN)
_write_mutex = threading.RLock()
_write_context = threading.local()


@contextmanager
def mutation_lock():
    with _write_mutex:
        if getattr(_write_context, "active", False):
            yield
            return
        lock_path = BASE / "config" / ".write.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HTTPException(409, "另一个服务进程正在修改资料，请稍后重试")
            _write_context.active = True
            yield
        finally:
            _write_context.active = False
            os.close(fd)


def serialized_write(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        update_progress("等待其他写操作完成")
        with mutation_lock():
            update_progress("检查输入与资料状态")
            return func(*args, **kwargs)
    return wrapped


def seg(value: str) -> str:
    if not isinstance(value, str) or not SEG.fullmatch(value):
        raise HTTPException(400, f"非法名称: {value!r}")
    return value


def relative_file(value: str) -> str:
    if (not isinstance(value, str) or not value or value.startswith("/") or "\\" in value
            or any(p in ("", ".", "..") for p in value.split("/"))
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise HTTPException(400, f"非法相对路径: {value!r}")
    return value


CODE_LINK = re.compile(r"https?://[^\s<>\"'`]+")


def code_link(value, label="code") -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(400, f"{label} 必须是 http(s) 链接，最多 500 字符")
    value = value.strip()
    if len(value) > 500 or (value and not CODE_LINK.fullmatch(value)):
        raise HTTPException(400, f"{label} 必须是 http(s) 链接，最多 500 字符")
    return value


DOI_VALUE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]{2,200}", re.I)


def doi_link(value, label="DOI") -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(400, f"{label} 必须是字符串")
    value = value.strip()
    if not value:
        return ""
    if len(value) > 300:
        raise HTTPException(400, f"{label} 过长")
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                   "http://dx.doi.org/", "doi:"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.strip().rstrip(".,;，。；")
    if not DOI_VALUE.fullmatch(value):
        raise HTTPException(400, f"{label} 格式不正确，应为 10.xxxx/…")
    return value


def local_path(*parts: str) -> Path:
    root = LIB_LOCAL.resolve()
    path = root
    for part in parts:
        for component in relative_file(part).split("/"):
            path = path / component
            if path.is_symlink():
                raise HTTPException(400, "资料库内不允许符号链接")
    if not path.resolve().is_relative_to(root):
        raise HTTPException(400, "路径超出本机资料库")
    return path


def q(s: str) -> str:
    return shlex.quote(str(s))


def remote_guard(path: str) -> str:
    if not path.startswith("/"):
        raise HTTPException(400, "远端路径必须是绝对路径")
    relative_file(path[1:])
    parts = PurePosixPath(path).parts
    paths = [str(PurePosixPath(*parts[:i])) for i in range(2, len(parts) + 1)]
    return ("for p in " + " ".join(q(p) for p in paths) + "; do "
            "if [ -L \"$p\" ]; then printf '%s\\n' '拒绝访问符号链接' >&2; exit 73; fi; done; ")


def remote_exists(path: str, kind: str = "e") -> bool:
    if kind not in ("e", "f", "d"):
        raise ValueError("invalid path kind")
    p = ssh_need(remote_guard(path) + f"if test -{kind} {q(path)}; then printf yes; else printf no; fi")
    return p.stdout == "yes"


def read_remote_json(path: str):
    p = ssh_run(remote_guard(path) +
                f"if [ ! -e {q(path)} ]; then exit 44; fi; test -f {q(path)} && cat -- {q(path)}",
                binary=True)
    if p.returncode == 44:
        return None, "missing"
    if p.returncode != 0:
        raise HTTPException(502, f"读取远端文件失败，未改写原文件: {path}")
    try:
        data = json.loads(p.stdout)
    except (ValueError, TypeError):
        raise HTTPException(500, f"文件不是合法 JSON，未改写原文件: {path}")
    return data, hashlib.sha256(p.stdout).hexdigest()


def write_remote_text(path: str, text: str, expected: Optional[str] = None):
    tmp = path + "." + uuid4().hex + ".tmp"
    guard = remote_guard(path) + remote_guard(tmp)
    verify = ""
    if expected == "missing":
        verify = f"if [ -e {q(path)} ]; then exit 75; fi; "
    elif expected is not None:
        verify = (f"test -f {q(path)} || exit 75; "
                  f"actual=$(sha256sum -- {q(path)}) || exit 1; "
                  f"[ \"${{actual%% *}}\" = {q(expected)} ] || exit 75; ")
    script = (guard + "set -e; umask 077; "
              f"trap {q('rm -f -- ' + q(tmp))} EXIT; "
              f"cat > {q(tmp)}; " + verify + f"mv -f -- {q(tmp)} {q(path)}")
    p = ssh_run(script, stdin=text)
    if p.returncode == 75:
        raise HTTPException(409, "远端文件已被其他操作修改，请刷新后重试")
    if p.returncode != 0:
        raise HTTPException(502, f"写入失败，原文件或恢复副本已保留: {path}")


def write_remote_json(path: str, data, expected: Optional[str] = None):
    write_remote_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", expected)


def read_local_list(path: Path) -> list:
    if path.is_symlink():
        raise HTTPException(400, f"状态文件不能是符号链接: {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        raise HTTPException(500, f"无法读取 {path.name}，不会覆盖原文件")
    if not isinstance(data, list):
        raise HTTPException(500, f"{path.name} 必须是数组，不会覆盖原文件")
    return data


def write_local_json(path: Path, data):
    if path.is_symlink():
        raise HTTPException(400, f"状态文件不能是符号链接: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name, delete=False) as fp:
            tmp = Path(fp.name)
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def write_local_text(path: Path, text: str):
    if path.is_symlink():
        raise HTTPException(400, f"资料文件不能是符号链接: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name, delete=False) as fp:
            tmp = Path(fp.name)
            fp.write(text)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def ssh_args():
    if not SSH_TARGET:
        raise HTTPException(400, "尚未配置远端服务器，请打开右上角「设置」填写")
    return ["ssh", *config_store.ssh_options(), *SSH_TARGET]


def ssh_run(cmd: str, timeout: int = 60, stdin: Optional[str] = None, binary: bool = False):
    try:
        return subprocess.run(
            ssh_args() + [cmd],
            input=stdin.encode("utf-8") if binary and stdin is not None else stdin,
            capture_output=True, text=not binary, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "SSH 操作超时")


def ssh_need(cmd: str, timeout: int = 60, stdin: Optional[str] = None):
    p = ssh_run(cmd, timeout=timeout, stdin=stdin)
    if p.returncode != 0:
        raise HTTPException(502, f"远程命令失败: {p.stderr.strip()[:500] or cmd}")
    return p


def ssh_stream(cmd: str, target, timeout: int = 120):
    try:
        return subprocess.run(
            ssh_args() + [cmd],
            stdout=target, stderr=subprocess.PIPE, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "SSH 操作超时")


def remote_dir(topic: str, year: str, slug: str) -> str:
    for value in (topic, year, slug):
        seg(value)
    return f"{LIB_REMOTE}/{topic}/{year}/{slug}"


def local_pdf_path(topic: str, year: str, slug: str):
    exact = local_path(topic, year, slug, f"{slug}.pdf")
    if exact.is_file():
        return exact
    legacy = local_path(topic, year, slug, "paper.pdf")
    return legacy if legacy.is_file() else None


def remote_pdf_path(topic: str, year: str, slug: str):
    d = remote_dir(topic, year, slug)
    for name in (f"{slug}.pdf", "paper.pdf"):
        if remote_exists(d + '/' + name, "f"):
            return f"{d}/{name}"
    return None


def titles_file() -> Path:
    return BASE / "config" / "titles.json"


_title_cache = {"path": None, "mtime": None, "data": {}}


def title_cache() -> dict:
    """本机镜像没有 meta.json，标题靠远端扫描时写下的缓存。"""
    path = titles_file()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _title_cache["path"] == path and _title_cache["mtime"] == mtime:
        return _title_cache["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _title_cache["data"] if _title_cache["path"] == path else {}
    if not isinstance(data, dict):
        return {}
    _title_cache.update(path=path, mtime=mtime, data=data)
    return data


def save_title_cache(papers: dict):
    entries = {
        key: {"title": d.get("title") or "", "venue": d.get("venue") or ""}
        for key, d in papers.items() if d.get("title") or d.get("venue")
    }
    if entries == title_cache():
        return
    path = titles_file()
    try:
        if path.is_symlink():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".titles-", delete=False) as fp:
            tmp = Path(fp.name)
            json.dump(entries, fp, ensure_ascii=False, indent=2, sort_keys=True)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
        _title_cache.update(path=path, mtime=path.stat().st_mtime, data=entries)
    except OSError:
        pass


def scan_local() -> dict:
    papers = {}
    if not LIB_LOCAL.exists():
        return papers
    titles = title_cache()
    for topic in sorted(LIB_LOCAL.iterdir()):
        if topic.name == kb.KNOWLEDGE_DIR or topic.is_symlink() or not topic.is_dir():
            continue
        for year in sorted(topic.iterdir()):
            if year.is_symlink() or not year.is_dir():
                continue
            for paper in sorted(year.iterdir()):
                if paper.is_symlink() or not paper.is_dir():
                    continue
                key = f"{topic.name}/{year.name}/{paper.name}"
                exact = paper / f"{paper.name}.pdf"
                pdf_file = exact if exact.is_file() and not exact.is_symlink() else None
                if pdf_file is None:
                    hits = sorted(p for p in paper.glob("*.pdf") if p.is_file() and not p.is_symlink())
                    pdf_file = hits[0] if hits else None
                known = titles.get(key) or {}
                added_at = ""
                if pdf_file is not None:
                    added_at = datetime.fromtimestamp(
                        pdf_file.stat().st_mtime, timezone.utc).isoformat()
                papers[key] = {
                    "has_pdf": pdf_file is not None,
                    "pdf": pdf_file.name if pdf_file else "",
                    "has_source": False,
                    "has_meta": False,
                    "title": known.get("title", ""),
                    "entry": "",
                    "venue": known.get("venue", ""),
                    "added_at": added_at,
                }
    return papers


def scan_remote(cache_titles: bool = True) -> dict:
    update_progress("扫描远端资料库")
    papers = {}
    p = ssh_need(remote_guard(LIB_REMOTE) +
        f"if [ ! -e {q(LIB_REMOTE)} ]; then exit 0; fi; cd {q(LIB_REMOTE)} || exit 1; "
        "find . -mindepth 3 -maxdepth 4 -printf '%P\\t%y\\n'"
    )
    for line in p.stdout.splitlines():
        if "\t" not in line:
            continue
        rel, _kind = line.rsplit("\t", 1)
        parts = rel.split("/")
        if (parts[0] == kb.KNOWLEDGE_DIR or _kind == "l" or len(parts) < 3
                or (len(parts) == 3 and _kind != "d")
                or not all(SEG.fullmatch(p) for p in parts[:3])):
            continue
        key = "/".join(parts[:3])
        item = papers.setdefault(key, {
            "has_pdf": False, "pdf": "", "has_source": False, "has_meta": False,
            "title": "", "entry": "", "venue": "", "added_at": "",
        })
        if len(parts) >= 4:
            name = parts[3]
            if name.lower().endswith(".pdf"):
                item["has_pdf"] = True
                item["pdf"] = name
            elif name == "source":
                item["has_source"] = True
            elif name == "meta.json":
                item["has_meta"] = True

    if papers:
        meta_cmd = (
            remote_guard(LIB_REMOTE) + f"cd {q(LIB_REMOTE)} && find . -mindepth 4 -maxdepth 4 -type f -name meta.json "
            "-printf '__META__%P\\n' -exec cat {} \\; -printf '__END__\\n' 2>/dev/null"
        )
        p2 = ssh_need(meta_cmd)
        cur = None
        buf = []
        for line in p2.stdout.splitlines():
            if line.startswith("__META__"):
                rel = line[len("__META__"):]
                cur = rel[2:] if rel.startswith("./") else rel
                cur = cur.rsplit("/meta.json", 1)[0]
                buf = []
            elif cur is not None and line.endswith("__END__"):
                rest = line[: -len("__END__")]
                if rest:
                    buf.append(rest)
                if cur in papers:
                    try:
                        meta = json.loads("\n".join(buf))
                    except ValueError:
                        raise HTTPException(500, f"远端元数据损坏: {cur}/meta.json")
                    if not isinstance(meta, dict):
                        raise HTTPException(500, f"远端元数据必须是对象: {cur}/meta.json")
                    for field in ("entry", "pdf"):
                        if meta.get(field):
                            relative_file(meta[field])
                    if not cache_titles:
                        papers[cur]["_citation_meta"] = meta
                    papers[cur]["title"] = meta.get("title", "")
                    papers[cur]["arxiv"] = meta.get("arxiv", "")
                    papers[cur]["venue"] = meta.get("venue", "")
                    papers[cur]["entry"] = meta.get("entry", "")
                    papers[cur]["code"] = meta.get("code", "")
                    papers[cur]["openreview"] = meta.get("openreview", "")
                    papers[cur]["doi"] = meta.get("doi", "")
                    papers[cur]["added_at"] = meta.get("added_at") or meta.get("updated_at") or ""
                    if meta.get("pdf"):
                        papers[cur]["pdf"] = meta["pdf"]
                cur = None
            elif cur is not None:
                buf.append(line)
    if cache_titles:
        save_title_cache(papers)
    return papers


def remote_pdf_info() -> dict:
    """Return {key: {"name", "size"}} for every remote paper that has a pdf."""
    update_progress("读取远端 PDF 体积")
    root = LIB_REMOTE
    script = (
        remote_guard(root) +
        f"if [ ! -d {q(root)} ]; then exit 0; fi; cd {q(root)} || exit 1; "
        "find . -mindepth 4 -maxdepth 4 -type f -iname '*.pdf' | "
        "while IFS= read -r f; do printf '%s\\t%s\\n' \"$(wc -c < \"$f\")\" \"$f\"; done"
    )
    p = ssh_run(script, timeout=180)
    if p.returncode != 0:
        raise HTTPException(502, "读取远端 PDF 列表失败")
    found = {}
    for line in p.stdout.splitlines():
        if "\t" not in line:
            continue
        raw_size, rel = line.split("\t", 1)
        parts = rel.split("/")
        if len(parts) != 5 or parts[0] != ".":
            continue
        topic, year, slug, name = parts[1:]
        if not all(SEG.fullmatch(value) for value in (topic, year, slug)):
            continue
        size = raw_size.strip()
        if not size.isdigit():
            continue
        found.setdefault(f"{topic}/{year}/{slug}", []).append((name, int(size)))
    out = {}
    for key, entries in found.items():
        slug = key.rsplit("/", 1)[1]
        sizes = dict(entries)
        name = (slug + ".pdf" if slug + ".pdf" in sizes
                else "paper.pdf" if "paper.pdf" in sizes else sorted(sizes)[0])
        out[key] = {"name": name, "size": sizes[name]}
    return out


def local_pdf_info(topic: str, year: str, slug: str):
    path = local_pdf_path(topic, year, slug)
    if not path:
        return None
    try:
        return {"path": path, "name": path.name, "size": path.stat().st_size}
    except OSError:
        return None


@app.get("/api/status")
def status():
    if REMOTE_CONFIGURED:
        p = ssh_run("true", timeout=10)
        ssh_ok = p.returncode == 0
        ssh_error = p.stderr.strip()[:300]
    else:
        ssh_ok = False
        ssh_error = "尚未配置远端服务器，请打开「设置」填写"
    return {
        "ssh": ssh_ok,
        "ssh_error": ssh_error,
        "local_library": str(LIB_LOCAL),
        "local_exists": LIB_LOCAL.exists(),
        "remote_library": LIB_REMOTE,
        "remote_name": REMOTE_NAME,
        "configured": REMOTE_CONFIGURED,
        "ssh_alias": CFG.get("ssh_alias", ""),
        "ssh_target": config_store.target_label(CFG) if REMOTE_CONFIGURED else "",
    }


@app.get("/api/settings")
def get_settings():
    return config_store.public(CFG)


@app.post("/api/settings/test")
def settings_test(payload: dict):
    try:
        return config_store.test_connection(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/settings")
@serialized_write
def save_settings(payload: dict):
    try:
        cfg = config_store.normalize(payload, strict=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    merged = {**CFG, **cfg}
    try:
        config_store.write(CONFIG_FILE, merged)
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"无法保存配置：{exc}")
    return {"ok": True, "restart_required": True}


@app.get("/api/library")
def library(source: str = "local"):
    if source not in ("local", "remote"):
        raise HTTPException(400, "source 只能是 local 或 remote")
    papers = scan_remote() if source == "remote" else scan_local()
    items = []
    for key, d in papers.items():
        topic, year, slug = key.split("/", 2)
        items.append({
            "key": key, "topic": topic, "year": year, "slug": slug,
            "has_pdf": d["has_pdf"], "pdf": d.get("pdf", ""),
            "has_source": d["has_source"], "has_meta": d["has_meta"],
            "title": d.get("title", ""), "entry": d.get("entry", ""), "venue": d.get("venue", ""),
            "code": d.get("code", ""), "openreview": d.get("openreview", ""),
            "doi": d.get("doi", ""), "added_at": d.get("added_at", ""),
        })
    items.sort(key=lambda x: (x["topic"].lower(), x["year"], x["slug"]))
    return {"source": source, "count": len(items), "items": items}


_opt_cache = {"t": 0.0, "data": None}


def invalidate_options():
    _opt_cache["data"] = None
    _opt_cache["t"] = 0.0


@app.get("/api/options")
def options(force: bool = False):
    if not force and _opt_cache["data"] and time.time() - _opt_cache["t"] < 20:
        return _opt_cache["data"]
    papers = {}
    remote_papers = scan_remote() if REMOTE_CONFIGURED else {}
    for key in list(scan_local().keys()) + list(remote_papers.keys()):
        topic, year, slug = key.split("/", 2)
        papers.setdefault(topic, {}).setdefault(year, set()).add(slug)
    topics = []
    venues = set()
    for topic, years in sorted(papers.items()):
        topics.append({
            "name": topic,
            "count": sum(len(v) for v in years.values()),
            "years": sorted(years, reverse=True),
        })
        for slugs in years.values():
            for slug in slugs:
                parts = slug.split("-")
                if len(parts) >= 2:
                    venues.add(parts[1])
    data = {
        "topics": topics,
        "years": sorted({y for ty in papers.values() for y in ty}, reverse=True),
        "venues": sorted(venues),
    }
    _opt_cache.update(t=time.time(), data=data)
    return data


@app.get("/api/pdf-token")
def pdf_token():
    return {"token": PDF_TOKEN}


@app.get("/api/pdf")
def pdf(source: str, topic: str, year: str, slug: str):
    for v in (topic, year, slug):
        seg(v)
    if source == "local":
        path = local_pdf_path(topic, year, slug)
        if not path:
            raise HTTPException(404, "本机没有这篇 pdf")
        return FileResponse(path, media_type="application/pdf")

    if source != "remote":
        raise HTTPException(400, "source 只能是 local 或 remote")
    rpath = remote_pdf_path(topic, year, slug)
    if not rpath:
        raise HTTPException(404, "远端上没有这篇 pdf")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fp:
        tmp = Path(fp.name)
        try:
            result = subprocess.run(
                ssh_args() + [remote_guard(rpath) + f"head -c {MAX_UPLOAD_BYTES + 1} -- {q(rpath)}"],
                stdout=fp, stderr=subprocess.PIPE, timeout=120,
            )
            fp.flush()
            if result.returncode != 0:
                raise HTTPException(502, "远端 PDF 读取失败")
            if tmp.stat().st_size > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "PDF 超过预览大小限制")
        except subprocess.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            raise HTTPException(504, "远端 PDF 读取超时")
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    return FileResponse(tmp, media_type="application/pdf",
                        background=BackgroundTask(tmp.unlink, missing_ok=True))


async def consume_write_body(request: Request):
    async for _ in request.stream():
        pass


@app.post("/api/open", dependencies=[Depends(consume_write_body)])
def open_preview(topic: str, year: str, slug: str):
    for v in (topic, year, slug):
        seg(v)
    path = local_pdf_path(topic, year, slug)
    if not path:
        raise HTTPException(404, "本机没有这篇 pdf")
    subprocess.Popen(["open", str(path)])
    return {"ok": True}


def read_meta(rdir: str) -> dict:
    data, revision = read_remote_json(rdir + "/meta.json")
    if revision == "missing":
        return {}
    if not isinstance(data, dict):
        raise HTTPException(500, "meta.json 必须是对象，不会覆盖原文件")
    for field in ("entry", "pdf"):
        if data.get(field):
            relative_file(data[field])
    return data


def write_meta(rdir: str, meta: dict) -> None:
    write_remote_json(rdir + "/meta.json", meta)


def remote_state(rdir: str, slug: str):
    pdf = f"{rdir}/{slug}.pdf"
    legacy = f"{rdir}/paper.pdf"
    src = f"{rdir}/source"
    cmd = (
        f"if [ -f {q(pdf)} ] || [ -f {q(legacy)} ]; then echo PDF; fi; "
        f"if [ -d {q(src)} ] && [ -n \"$(ls -A {q(src)})\" ]; then echo SRC; fi"
    )
    out = ssh_need(remote_guard(pdf) + remote_guard(legacy) + remote_guard(src) + cmd).stdout
    return ("PDF" in out), ("SRC" in out)


def stage_remote(old_dir: str) -> str:
    update_progress("建立远端暂存副本（保留现有资料）")
    stage = LIB_REMOTE + ".staging/" + uuid4().hex
    script = remote_guard(old_dir) + remote_guard(stage) + "set -e; umask 077; "
    script += f"mkdir -p -- {q(stage)}; "
    script += (f"if [ -d {q(old_dir)} ]; then "
               f"links=$(find {q(old_dir)} -type l -print -quit); "
               "[ -z \"$links\" ] || exit 73; "
               f"cp -a -- {q(old_dir + '/.')} {q(stage + '/')}; fi")
    ssh_need(script, timeout=300)
    return stage


def publish_remote(stage: str, old_dir: str, new_dir: str) -> str:
    update_progress("提交远端资料并保留恢复副本")
    backup = LIB_REMOTE + ".recovery/" + uuid4().hex + "/paper"
    script = remote_guard(stage) + remote_guard(old_dir) + remote_guard(new_dir) + remote_guard(backup)
    script += "set -e; umask 077; "
    if new_dir != old_dir:
        script += f"[ ! -e {q(new_dir)} ] || exit 75; "
    rollback = (f"if [ ! -e {q(old_dir)} ] && [ -e {q(backup)} ]; then "
                f"mv -- {q(backup)} {q(old_dir)}; fi")
    script += (f"mkdir -p -- {q(backup.rsplit('/', 1)[0])} {q(new_dir.rsplit('/', 1)[0])}; "
               f"trap {q(rollback)} EXIT; "
               "trap 'exit 1' HUP INT TERM; "
               f"if [ -e {q(old_dir)} ]; then mv -- {q(old_dir)} {q(backup)}; fi; "
               f"mv -- {q(stage)} {q(new_dir)}; trap - EXIT HUP INT TERM")
    try:
        ssh_need(script, timeout=120)
    except HTTPException as exc:
        raise HTTPException(exc.status_code, f"远端提交未确认，请检查目标及恢复副本后重试。目标: {new_dir}；恢复副本: {backup}；暂存: {stage}")
    return backup


def local_recovery_path(name: str) -> Path:
    root = Path(str(LIB_LOCAL.resolve()) + ".recovery")
    if root.is_symlink():
        raise HTTPException(400, "恢复目录不能是符号链接")
    path = root / uuid4().hex / name
    path.parent.mkdir(parents=True)
    return path


def publish_local_pdf(data: bytes, topic: str, year: str, slug: str):
    target = local_path(topic, year, slug, slug + ".pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".pdf-", delete=False) as fp:
            tmp = Path(fp.name)
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        if target.exists():
            shutil.copy2(target, local_recovery_path(slug + ".pdf"))
        os.replace(tmp, target)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def copy_to_remote(local: Path, remote: str):
    update_progress("向远端传输 " + ("PDF" if local.suffix == ".pdf" else "源码包"))
    ssh_need(remote_guard(remote) + "true")
    try:
        p = subprocess.run(
            ["scp", "-q", *config_store.ssh_options(), *config_store.scp_target(CFG),
             str(local), f"{config_store.host_target(CFG)}:{remote}"],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "文件传输超时，正式资料未替换")
    if p.returncode != 0:
        raise HTTPException(502, "文件传输失败，正式资料未替换")


def prepare_source(data: bytes, filename: str, tmp: Path):
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "源码包超过上传限制")
    suffix = ".zip" if str(filename or "").lower().endswith(".zip") else ".tar"
    archive = tmp / ("upload" + suffix)
    archive.write_bytes(data)
    update_progress("安全校验并解压源码包")
    root = _extract_archive(archive, tmp / "extract")
    update_progress("识别 TeX 源码入口")
    entry = _find_entry(root)
    if not entry:
        raise HTTPException(400, "源码包里没有找到 .tex 文件")
    tgz = tmp / "source.tgz"
    update_progress("打包已校验的源码")
    _make_tgz(root, tgz)
    return tgz, "source/" + relative_file(entry)


def save_paper(topic, year, slug, pdf_data, source_data, filename, copy_local, updates):
    update_progress("读取并校验现有论文信息")
    rdir = remote_dir(topic, year, slug)
    existing = read_meta(rdir)
    if pdf_data is not None:
        if len(pdf_data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "PDF 超过上传限制")
        if not pdf_data.startswith(b"%PDF-"):
            raise HTTPException(400, "上传内容不是 PDF")
        if copy_local:
            local_path(topic, year, slug, slug + ".pdf")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        source_file, entry = None, existing.get("entry", "")
        if source_data is not None:
            source_file, entry = prepare_source(source_data, filename, tmp)
        stage = stage_remote(rdir)
        try:
            if pdf_data is not None:
                pdf_file = tmp / "paper.pdf"
                pdf_file.write_bytes(pdf_data)
                copy_to_remote(pdf_file, stage + "/" + slug + ".pdf")
            if source_file is not None:
                archive = stage + "/source.tgz"
                copy_to_remote(source_file, archive)
                update_progress("在远端暂存区展开源码")
                ssh_need(remote_guard(stage + "/source") +
                         f"set -e; rm -rf -- {q(stage + '/source')}; mkdir -- {q(stage + '/source')}; "
                         f"tar -xzf {q(archive)} -C {q(stage + '/source')}; rm -f -- {q(archive)}", timeout=300)
            has_pdf, has_source = remote_state(stage, slug)
            pdf_name = ""
            if has_pdf:
                pdf_name = slug + ".pdf" if remote_exists(stage + "/" + slug + ".pdf", "f") else "paper.pdf"
            now = datetime.now(timezone.utc).isoformat()
            meta = dict(existing)
            meta.update({k: v for k, v in updates.items() if v})
            meta.update({"slug": slug, "topic": topic, "year": int(year),
                         "entry": entry if has_source else "",
                         "pdf": pdf_name,
                         "has_pdf": has_pdf, "has_source": has_source,
                         "added_at": existing.get("added_at") or now, "updated_at": now})
            update_progress("写入论文元数据")
            write_meta(stage, meta)
            backup = publish_remote(stage, rdir, rdir)
        except HTTPException as exc:
            raise HTTPException(exc.status_code, f"{exc.detail}；如有暂存文件可在 {stage} 检查，未确认提交前请勿重复覆盖")
    warnings = []
    if copy_local and pdf_data is not None:
        try:
            update_progress("更新本机 PDF 镜像")
            publish_local_pdf(pdf_data, topic, year, slug)
        except (OSError, HTTPException):
            warnings.append(f"远端已保存，但本机 PDF 副本更新失败；请检查本机目录后重新补传。远端恢复副本: {backup}")
    updated, errors = sync_projects_for_paper(f"{topic}/{year}/{slug}", meta)
    warnings.extend(errors)
    return {"ok": True, "remote_path": rdir, "entry": meta["entry"], "meta": meta,
            "projects_updated": updated, "warnings": warnings, "recovery_path": backup}


def _arxiv_slug_word(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text or "")


def _slug_keyword(title: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]*", title or "")
    for w in words:
        if len(_arxiv_slug_word(w)) >= 3 and sum(c.isupper() for c in w) >= 2:
            return _arxiv_slug_word(w)
    for w in words:
        if len(_arxiv_slug_word(w)) >= 4 and re.search(r"[A-Z]", w[1:]):
            return _arxiv_slug_word(w)
    stop = {"with", "from", "using", "towards", "toward", "via", "into", "based"}
    for w in words[1:]:
        if len(w) >= 4 and w.lower() not in stop:
            return _arxiv_slug_word(w).capitalize()
    return "Paper"


def _venue_year(text: str, fallback_year: int) -> tuple:
    hit = ARXIV_VENUE.search(text or "")
    if not hit:
        return "", fallback_year
    venue = {"NIPS": "NeurIPS"}.get(hit.group(1).upper(), hit.group(1))
    return venue, int(hit.group(2)) if hit.group(2) else fallback_year


def arxiv_lookup(aid: str) -> dict:
    raw = None
    last = None
    for attempt, base in enumerate(("https://export.arxiv.org/api/query", "https://arxiv.org/api/query"), 1):
        message = f"获取 arXiv 元数据（来源 {attempt}/2）"
        try:
            raw = _arxiv_http(f"{base}?id_list={urllib.parse.quote(aid)}", timeout=45,
                              progress=lambda **details: update_progress(message, attempt=attempt, **details))
            break
        except HTTPException as e:
            last = e
    if raw is None:
        raise HTTPException(502, f"arXiv 元数据获取失败: {last.detail if last else ''}")
    try:
        root = ET.fromstring(raw)
    except Exception:
        raise HTTPException(502, "arXiv 返回的元数据不是合法 XML")
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    entry = root.find("a:entry", ns)
    title = " ".join((entry.findtext("a:title", "", ns) if entry is not None else "").split())
    if not title:
        raise HTTPException(404, f"arXiv 上找不到: {aid}")
    authors = [
        (a.findtext("a:name", "", ns) or "").strip()
        for a in entry.findall("a:author", ns)
    ]
    published = entry.findtext("a:published", "", ns)
    year = int(published[:4]) if published[:4].isdigit() else datetime.now().year
    comment = " ".join((entry.findtext("arxiv:comment", "", ns) or "").split())
    journal = " ".join((entry.findtext("arxiv:journal_ref", "", ns) or "").split())
    cat = entry.find("arxiv:primary_category", ns)

    venue, venue_year = "arXiv", year
    hit = ARXIV_VENUE.search(comment) or ARXIV_VENUE.search(journal) or ARXIV_VENUE.search(title)
    if hit:
        venue = {"NIPS": "NeurIPS"}.get(hit.group(1).upper(), hit.group(1))
        if hit.group(2):
            venue_year = int(hit.group(2))

    keyword = _slug_keyword(title)

    family = _arxiv_slug_word(authors[0].split()[-1]) if authors else ""
    return {
        "arxiv_id": aid,
        "title": title,
        "authors": authors,
        "author": family,
        "keyword": keyword,
        "year": venue_year,
        "venue": venue,
        "comment": comment,
        "journal_ref": journal,
        "category": cat.get("term") if cat is not None else "",
        "published": published,
        "abs": f"https://arxiv.org/abs/{aid}",
    }


@app.get("/api/operations/{operation_id}")
async def operation_status(operation_id: str):
    return get_progress(operation_id)


@app.get("/api/arxiv/preview")
@tracked_operation
def arxiv_preview(url: str, operation_id: Optional[str] = None):
    return arxiv_lookup(arxiv_id(url))


def arxiv_pdf_bytes(aid: str) -> bytes:
    errors = []
    for attempt, base in enumerate(("https://export.arxiv.org/pdf/", "https://arxiv.org/pdf/"), 1):
        message = f"下载 arXiv PDF（来源 {attempt}/2）"
        try:
            data = _arxiv_http(base + aid, timeout=180,
                               progress=lambda **details: update_progress(message, attempt=attempt, **details))
            if data[:5] == b"%PDF-":
                return data
            errors.append(f"{base} 返回的不是 PDF")
        except HTTPException as e:
            errors.append(str(e.detail))
    raise HTTPException(502, "PDF 下载失败: " + "；".join(errors))


def openreview_link(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(400, "OpenReview 链接必须是字符串")
    if len(value) > 300:
        raise HTTPException(400, "OpenReview 链接过长")
    return orx.canonical_url(value)


def openreview_lookup(url: str) -> dict:
    update_progress("抓取 OpenReview 元数据")
    info = orx.lookup(orx.parse_url(url))
    fallback = int(info["year"]) if str(info.get("year", "")).isdigit() else datetime.now().year
    venue, venue_year = _venue_year(info.get("venue", ""), fallback)
    if not venue:
        venue, venue_year = _venue_year(info.get("title", ""), fallback)
    authors = info.get("authors") or []
    return {
        "openreview_id": info["openreview_id"],
        "openreview_url": info["openreview_url"],
        "title": info["title"],
        "authors": authors,
        "author": _arxiv_slug_word(authors[0].split()[-1]) if authors else "",
        "keyword": _slug_keyword(info["title"]),
        "year": venue_year,
        "venue": venue or "OpenReview",
        "venue_raw": info.get("venue", ""),
        "arxiv_id": info.get("arxiv_id", ""),
        "bibtex": info.get("bibtex", ""),
        "abstract": info.get("abstract", ""),
    }


@app.get("/api/openreview/session")
def openreview_session():
    return orx.session_status()


@app.post("/api/openreview/login")
@serialized_write
def openreview_login(payload: dict):
    if not isinstance(payload, dict):
        raise HTTPException(400, "登录参数必须是对象")
    if payload.get("token"):
        return orx.save_token(payload.get("token"))
    return orx.login(payload.get("username"), payload.get("password"))


@app.delete("/api/openreview/login")
@serialized_write
def openreview_logout():
    return orx.clear_session()


@app.get("/api/openreview/preview")
@tracked_operation
def openreview_preview(url: str, operation_id: Optional[str] = None):
    return openreview_lookup(url)


def ieee_lookup(ref: str) -> dict:
    info = ieee_xplore.lookup(ref, progress=update_progress)
    authors = info.get("authors") or []
    return {
        **info,
        "author": _arxiv_slug_word(authors[0].split()[-1]) if authors else "",
        "keyword": _slug_keyword(info.get("title", "")),
    }


@app.get("/api/ieee/preview")
@tracked_operation
def ieee_preview(ref: str, operation_id: Optional[str] = None):
    return ieee_lookup(ref)


def norm_title(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def duplicate_report(title: str = "", arxiv: str = "", slug: str = "",
                     topic: str = "", year: str = "", openreview: str = "",
                     doi: str = "") -> dict:
    papers = scan_remote()
    key = f"{topic}/{year}/{slug}" if topic and year and slug else ""
    same_key = bool(key and key in papers)
    nt = norm_title(title)
    aid = str(arxiv or "").strip()
    orid = orx.canonical_url(openreview) if openreview else ""
    target_doi = str(doi or "").strip().lower()
    hits = []
    for other, info in papers.items():
        if other == key:
            continue
        ot, oy, os_ = other.split("/", 2)
        reasons = []
        if aid and info.get("arxiv") and info["arxiv"] == aid:
            reasons.append("arxiv")
        if orid and info.get("openreview") and info["openreview"] == orid:
            reasons.append("openreview")
        if target_doi and str(info.get("doi", "")).strip().lower() == target_doi:
            reasons.append("doi")
        if len(nt) >= 12 and norm_title(info.get("title", "")) == nt:
            reasons.append("title")
        if slug and os_ == slug:
            reasons.append("slug")
        if reasons:
            hits.append({
                "key": other, "title": info.get("title", ""),
                "arxiv": info.get("arxiv", ""), "reasons": reasons,
                "has_pdf": info.get("has_pdf", False),
                "has_source": info.get("has_source", False),
            })
    local = scan_local()
    for other in local:
        if other == key or other in papers:
            continue
        if slug and other.split("/", 2)[2] == slug:
            hits.append({
                "key": other, "title": "", "arxiv": "", "reasons": ["slug"],
                "has_pdf": local[other].get("has_pdf", False), "has_source": False,
            })
    return {"same_key": same_key, "duplicates": hits[:10]}


@app.get("/api/duplicates")
def duplicates(title: str = "", arxiv: str = "", slug: str = "",
               topic: str = "", year: str = "", openreview: str = "", doi: str = ""):
    return duplicate_report(title, arxiv, slug, topic, year, openreview, doi)


def paper_key(value) -> tuple:
    if not isinstance(value, str):
        raise HTTPException(400, "论文 key 必须是字符串")
    parts = value.strip().strip("/").split("/")
    if len(parts) != 3:
        raise HTTPException(400, f"非法论文 key: {value!r}")
    for part in parts:
        seg(part)
    return tuple(parts)


def remote_md5(paths: list) -> dict:
    if not paths:
        return {}
    for path in paths:
        relative_file(path)
    listing = " ".join(q(path) for path in paths)
    script = (remote_guard(LIB_REMOTE) + f"cd {q(LIB_REMOTE)} || exit 1; "
              "if command -v md5sum >/dev/null 2>&1; then md5sum " + listing + "; "
              "else md5 -r " + listing + "; fi")
    p = ssh_run(script, timeout=300)
    if p.returncode != 0:
        raise HTTPException(502, "远端文件校验失败，无法比对内容")
    out = {}
    for line in p.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[1].strip()] = parts[0].strip()
    return out


def remote_entry(rdir: str) -> str:
    src = rdir + "/source"
    script = (remote_guard(src) +
              f"if [ ! -d {q(src)} ]; then exit 0; fi; cd {q(src)} || exit 1; "
              "f=$(find . -maxdepth 1 -type f -name '*.tex' | sort | head -1); "
              "[ -n \"$f\" ] || f=$(find . -type f -name '*.tex' | sort | head -1); "
              "[ -n \"$f\" ] && printf '%s' \"$f\"")
    p = ssh_run(script)
    if p.returncode != 0:
        return ""
    rel = p.stdout.strip()
    if rel.startswith("./"):
        rel = rel[2:]
    if not rel:
        return ""
    try:
        relative_file(rel)
    except HTTPException:
        return ""
    return "source/" + rel


def duplicate_groups() -> list:
    update_progress("读取论文标题与 arXiv 信息")
    papers = scan_remote()
    if len(papers) < 2:
        return []
    update_progress("比对 pdf 体积与内容")
    pdfs = remote_pdf_info()
    by_size = {}
    for key, info in pdfs.items():
        by_size.setdefault(info["size"], []).append(key)
    hashed = sorted(key for keys in by_size.values() if len(keys) > 1 for key in keys)
    digests = {}
    if hashed:
        paths = {f"{key}/{pdfs[key]['name']}": key for key in hashed[:DUP_HASH_LIMIT]}
        for path, digest in remote_md5(list(paths)).items():
            key = paths.get(path)
            if key:
                digests[key] = digest

    parent = {key: key for key in papers}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    buckets = {}
    for key, info in papers.items():
        aid = str(info.get("arxiv") or "").strip().lower()
        if aid:
            buckets.setdefault(("arxiv", aid), []).append(key)
        title = norm_title(info.get("title"))
        if len(title) >= 12:
            buckets.setdefault(("title", title), []).append(key)
        if key in digests:
            buckets.setdefault(("md5", digests[key]), []).append(key)

    evidence = {key: set() for key in papers}
    for (kind, _value), keys in buckets.items():
        if len(keys) < 2:
            continue
        root = find(keys[0])
        for key in keys[1:]:
            parent[find(key)] = root
        for key in keys:
            evidence[key].add(kind)

    update_progress("整理重复分组")
    grouped = {}
    for key in papers:
        grouped.setdefault(find(key), []).append(key)
    try:
        starred = set(load_stars())
    except HTTPException:
        starred = set()
    groups = []
    for members in grouped.values():
        if len(members) < 2:
            continue
        items = []
        for key in sorted(members):
            topic, year, slug = key.split("/", 2)
            info = papers[key]
            items.append({
                "key": key, "topic": topic, "year": year, "slug": slug,
                "title": info.get("title", ""), "arxiv": info.get("arxiv", ""),
                "venue": info.get("venue", ""),
                "has_pdf": bool(info.get("has_pdf")),
                "has_source": bool(info.get("has_source")),
                "pdf_size": pdfs.get(key, {}).get("size", 0),
                "digest": (digests.get(key) or "")[:10],
                "starred": key in starred,
            })
        suggested = sorted(items, key=lambda item: (
            not item["has_source"], not item["has_pdf"], not item["starred"],
            -int(item["year"]), item["key"]))[0]
        groups.append({
            "id": items[0]["key"],
            "reasons": sorted(set().union(*(evidence[key] for key in members))),
            "items": items,
            "suggested": suggested["key"],
        })
    groups.sort(key=lambda group: (-len(group["items"]), group["items"][0]["key"]))
    return groups


@app.get("/api/duplicates/scan")
@tracked_operation
def duplicates_scan(operation_id: Optional[str] = None):
    groups = duplicate_groups()
    return {"count": len(groups), "groups": groups}


def _copy_remote_path(src: str, dest: str):
    ssh_need(remote_guard(src) + remote_guard(dest) +
             f"set -e; rm -rf -- {q(dest)}; cp -a -- {q(src)} {q(dest)}", timeout=300)


@app.post("/api/duplicates/merge")
@tracked_operation
@serialized_write
def merge_duplicates(payload: dict, operation_id: Optional[str] = None):
    keep_parts = paper_key(payload.get("keep"))
    raw_drops = payload.get("drops")
    if not isinstance(raw_drops, list) or not 1 <= len(raw_drops) <= 20:
        raise HTTPException(400, "drops 必须是 1-20 篇的数组")
    drops = [paper_key(item) for item in raw_drops]
    keep = "/".join(keep_parts)
    drop_keys = ["/".join(parts) for parts in drops]
    if len(set(drop_keys)) != len(drop_keys):
        raise HTTPException(400, "drops 含重复项")
    if keep in drop_keys:
        raise HTTPException(400, "保留的论文不能同时出现在合并列表里")

    update_progress("重新核对重复关系")
    group = None
    for candidate in duplicate_groups():
        keys = {item["key"] for item in candidate["items"]}
        if keep in keys:
            group = keys
            break
    if group is None:
        raise HTTPException(409, "这篇论文当前不在任何重复组中，请重新扫描")
    outside = [key for key in drop_keys if key not in group]
    if outside:
        raise HTTPException(409, "这些论文不在同一重复组：" + "、".join(outside))

    for parts in [keep_parts] + drops:
        path = remote_dir(*parts)
        if not remote_exists(path, "d"):
            raise HTTPException(404, f"远端上找不到 {'/'.join(parts)}")
        if remote_tree_has_symlink(path):
            raise HTTPException(400, f"{'/'.join(parts)} 目录内含符号链接，未执行合并")

    update_progress("读取待合并论文的资料")
    keep_meta = read_meta(remote_dir(*keep_parts))
    drop_metas = [(parts, read_meta(remote_dir(*parts))) for parts in drops]
    stage = stage_remote(remote_dir(*keep_parts))
    has_pdf, has_source = remote_state(stage, keep_parts[2])
    entry = keep_meta.get("entry", "") if has_source else ""
    warnings = []
    try:
        for parts, meta in drop_metas:
            ddir = remote_dir(*parts)
            drop_pdf = remote_pdf_path(*parts)
            if not has_pdf and drop_pdf:
                update_progress(f"补入 pdf：{parts[2]}")
                _copy_remote_path(drop_pdf, stage + "/" + keep_parts[2] + ".pdf")
                has_pdf = True
            _drop_pdf, drop_source = remote_state(ddir, parts[2])
            if not has_source and drop_source:
                update_progress(f"补入 tex 源码：{parts[2]}")
                _copy_remote_path(ddir + "/source", stage + "/source")
                has_source = True
                entry = meta.get("entry") or ""
                if not entry or not remote_exists(stage + "/" + entry, "f"):
                    entry = ""
            for field in ("title", "arxiv", "venue"):
                if not str(keep_meta.get(field) or "").strip() and str(meta.get(field) or "").strip():
                    keep_meta[field] = meta[field]
        if has_source and not entry:
            entry = remote_entry(stage)
        if has_source and not entry:
            has_source = False
        now = datetime.now(timezone.utc).isoformat()
        meta = dict(keep_meta)
        meta.update({
            "slug": keep_parts[2], "topic": keep_parts[0], "year": int(keep_parts[1]),
            "pdf": keep_parts[2] + ".pdf" if has_pdf else "",
            "entry": entry if has_source else "",
            "has_pdf": has_pdf, "has_source": has_source,
            "added_at": keep_meta.get("added_at") or now, "updated_at": now,
        })
        write_meta(stage, meta)
        backup = publish_remote(stage, remote_dir(*keep_parts), remote_dir(*keep_parts))
    except HTTPException as exc:
        raise HTTPException(exc.status_code, f"{exc.detail}；保留项未合并，其余论文未移动。暂存: {stage}")

    update_progress("把被合并的论文移入回收区")
    recovery = {"remote": {}, "local": {}}
    for parts in drops:
        ddir = remote_dir(*parts)
        target = f"{LIB_REMOTE}.recovery/{uuid4().hex}/{parts[2]}"
        p = ssh_run(remote_guard(ddir) + remote_guard(target) +
                    f"set -e; mkdir -p -- {q(target.rsplit('/', 1)[0])}; "
                    f"if [ -e {q(ddir)} ]; then mv -- {q(ddir)} {q(target)}; fi")
        if p.returncode != 0:
            warnings.append(f"{'/'.join(parts)} 已合并但移入回收区失败，请手工核对")
        else:
            recovery["remote"]["/".join(parts)] = target

    for parts in drops:
        try:
            local_dir = local_path(*parts)
            if not local_dir.is_dir():
                continue
            if any(path.is_symlink() for path in local_dir.rglob("*")):
                raise OSError("目录内含符号链接")
            keep_local = local_path(*keep_parts)
            if not local_pdf_path(*keep_parts):
                source = local_pdf_path(*parts)
                if source:
                    keep_local.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, keep_local / (keep_parts[2] + ".pdf"))
            backup_local = local_recovery_path(parts[2])
            local_dir.rename(backup_local)
            recovery["local"]["/".join(parts)] = str(backup_local)
        except (OSError, HTTPException) as exc:
            warnings.append(f"本机 {parts[2]} 移入回收区失败: {getattr(exc, 'detail', str(exc))}")

    try:
        stars = load_stars()
        changed = False
        if keep not in stars and any(key in stars for key in drop_keys):
            stars.append(keep)
            changed = True
        for key in drop_keys:
            if key in stars:
                stars.remove(key)
                changed = True
        if changed:
            save_stars(stars)
    except (HTTPException, OSError):
        warnings.append("资料已合并，但星标更新失败，请核对星标状态文件")

    touched, errors = remap_paper_refs(set(drop_keys), meta)
    warnings.extend(errors)
    warnings.append("被合并论文的研究记录（笔记、阅读状态、版本组）保留供恢复，未覆盖到保留论文")
    invalidate_options()
    return {"ok": True, "keep": keep, "merged": drop_keys, "meta": meta,
            "recovery": recovery, "projects_updated": touched,
            "recovery_path": backup, "warnings": warnings,
            "notice": f"已合并 {len(drop_keys)} 篇到 {keep}，被合并的论文已移入回收区"}


def _mirror_pull(topic: str, year: str, slug: str):
    key = f"{topic}/{year}/{slug}"
    if local_pdf_path(topic, year, slug):
        return {"key": key, "skipped": "本机已有 pdf"}
    rpath = remote_pdf_path(topic, year, slug)
    if not rpath:
        return {"key": key, "skipped": "远端已没有 pdf"}
    target = local_path(topic, year, slug, slug + ".pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    update_progress(f"拉取 pdf 到本机：{slug}")
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".mirror-",
                                         suffix=".pdf", delete=False) as fp:
            tmp = Path(fp.name)
            result = ssh_stream(
                remote_guard(rpath) + f"head -c {MAX_UPLOAD_BYTES + 1} -- {q(rpath)}",
                fp, timeout=300,
            )
            fp.flush()
        if result.returncode != 0:
            raise HTTPException(502, "读取远端 PDF 失败")
        size = tmp.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "PDF 超过大小限制")
        with tmp.open("rb") as fh:
            if fh.read(5) != b"%PDF-":
                raise HTTPException(502, "远端上的文件不是 PDF，未写入本机")
        if target.exists():
            raise HTTPException(409, "本机目标文件已存在")
        os.replace(tmp, target)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return {"key": key, "size": size, "pdf": target.name}


def _mirror_push(topic: str, year: str, slug: str):
    key = f"{topic}/{year}/{slug}"
    if remote_pdf_path(topic, year, slug):
        return {"key": key, "skipped": "远端已有 pdf"}
    local = local_pdf_path(topic, year, slug)
    if not local:
        return {"key": key, "skipped": "本机没有 pdf"}
    size = local.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "PDF 超过大小限制")
    rdir = remote_dir(topic, year, slug)
    update_progress(f"推送到远端：{slug}")
    ssh_need(remote_guard(rdir) + f"mkdir -p -- {q(rdir)}")
    staging = rdir + "/.upload-" + uuid4().hex + ".pdf"
    copy_to_remote(local, staging)
    final = rdir + "/" + slug + ".pdf"
    p = ssh_run(remote_guard(staging) + remote_guard(final) +
                f"set -e; [ ! -e {q(final)} ] || exit 75; mv -- {q(staging)} {q(final)}")
    if p.returncode == 75:
        ssh_run(f"rm -f -- {q(staging)}")
        raise HTTPException(409, "远端上已出现同名 pdf，未覆盖")
    if p.returncode != 0:
        ssh_run(f"rm -f -- {q(staging)}")
        raise HTTPException(502, "写入远端失败，未替换任何文件")
    update_progress(f"更新远端元数据：{slug}")
    data, revision = read_remote_json(rdir + "/meta.json")
    has_pdf, has_source = remote_state(rdir, slug)
    entry = ""
    if has_source:
        existing = data if isinstance(data, dict) else {}
        candidate = str(existing.get("entry") or "")
        if candidate:
            try:
                relative_file(candidate)
            except HTTPException:
                candidate = ""
        if candidate and remote_exists(rdir + "/" + candidate, "f"):
            entry = candidate
        else:
            entry = remote_entry(rdir)
    now = datetime.now(timezone.utc).isoformat()
    if revision == "missing":
        meta = {"slug": slug, "topic": topic, "year": int(year), "title": "",
                "venue": "", "arxiv": "", "entry": entry,
                "pdf": slug + ".pdf" if has_pdf else "",
                "has_pdf": has_pdf, "has_source": has_source,
                "added_at": now, "updated_at": now}
        write_remote_json(rdir + "/meta.json", meta, expected="missing")
    elif isinstance(data, dict):
        meta = dict(data)
        meta.update({"slug": slug, "topic": topic, "year": int(year),
                     "pdf": slug + ".pdf" if has_pdf else "",
                     "entry": entry if has_source else "",
                     "has_pdf": has_pdf, "has_source": has_source, "updated_at": now})
        write_remote_json(rdir + "/meta.json", meta, expected=revision)
    else:
        raise HTTPException(500, "远端上的 meta.json 不是对象，未覆盖原文件")
    updated, errors = sync_projects_for_paper(key, meta)
    return {"key": key, "size": size, "pdf": slug + ".pdf",
            "projects_updated": updated, "warnings": errors}


@app.get("/api/mirror/plan")
def mirror_plan():
    update_progress("扫描两侧论文")
    local = scan_local()
    remote = scan_remote()
    remote_pdfs = remote_pdf_info()
    pull, push = [], []
    for key in sorted(remote):
        info = remote_pdfs.get(key)
        if not info or local.get(key, {}).get("has_pdf"):
            continue
        topic, year, slug = key.split("/", 2)
        pull.append({"key": key, "topic": topic, "year": year, "slug": slug,
                     "pdf": info["name"], "size": info["size"]})
    for key in sorted(local):
        if not local[key].get("has_pdf"):
            continue
        if key in remote and remote[key].get("has_pdf"):
            continue
        topic, year, slug = key.split("/", 2)
        info = local_pdf_info(topic, year, slug)
        if not info:
            continue
        push.append({"key": key, "topic": topic, "year": year, "slug": slug,
                     "pdf": info["name"], "size": info["size"],
                     "remote_has_source": bool(remote.get(key, {}).get("has_source"))})
    return {"pull": pull, "push": push,
            "pull_bytes": sum(item["size"] for item in pull),
            "push_bytes": sum(item["size"] for item in push)}


@app.post("/api/mirror/apply")
@tracked_operation
@serialized_write
def mirror_apply(payload: dict, operation_id: Optional[str] = None):
    direction = payload.get("direction")
    if direction not in ("pull", "push"):
        raise HTTPException(400, "direction 只能是 pull 或 push")
    raw_keys = payload.get("keys")
    if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= 100:
        raise HTTPException(400, "keys 必须是 1-100 个论文 key 的数组")
    keys = [paper_key(item) for item in raw_keys]
    results, warnings = [], []
    for index, (topic, year, slug) in enumerate(keys, 1):
        update_progress(f"镜像修复 {index}/{len(keys)}：{slug}")
        try:
            if direction == "pull":
                results.append(_mirror_pull(topic, year, slug))
            else:
                results.append(_mirror_push(topic, year, slug))
        except HTTPException as exc:
            warnings.append(f"{topic}/{year}/{slug}: {exc.detail}")
        except OSError as exc:
            warnings.append(f"{topic}/{year}/{slug}: {exc}")
    done = [item for item in results if "size" in item]
    skipped = [item for item in results if "skipped" in item]
    invalidate_options()
    verb = "拉取到本机" if direction == "pull" else "推送到远端"
    notice = f"已{verb} {len(done)} 篇"
    if skipped:
        notice += f"，跳过 {len(skipped)} 篇"
    return {"ok": True, "direction": direction, "done": done, "skipped": skipped,
            "warnings": warnings, "notice": notice}


@app.post("/api/upload")
@tracked_operation
@serialized_write
def upload(
    topic: str = Form(...),
    year: str = Form(...),
    slug: str = Form(...),
    title: str = Form(""),
    venue: str = Form(""),
    arxiv: str = Form(""),
    arxiv_url: str = Form(""),
    openreview_url: str = Form(""),
    ieee_url: str = Form(""),
    code: str = Form(""),
    bibtex: str = Form(""),
    force: bool = Form(False),
    copy_local: bool = Form(True),
    fetch_source: bool = Form(False),
    pdf: Optional[UploadFile] = File(None),
    source_archive: Optional[UploadFile] = File(None),
    operation_id: Optional[str] = None,
):
    seg(topic)
    if not YEAR.fullmatch(year):
        raise HTTPException(400, "年份必须是 4 位数字")
    if not SLUG.fullmatch(slug) or not slug.startswith(year + "-"):
        raise HTTPException(400, "slug 必须是 YYYY-VENUE-Author-Keyword,且年份前缀与年份一致")

    code_value = code_link(code)
    or_url = openreview_link(openreview_url)
    bib_fields, bib_warnings = {}, []

    def absorb_bibtex(text):
        nonlocal title, venue, arxiv
        try:
            parsed = parse_bibtex(text)
        except HTTPException as exc:
            bib_warnings.append(f"BibTeX 未导入：{exc.detail}")
            return
        for name, value in parsed.items():
            try:
                bib_fields.update(bibliography_fields({name: value}))
            except HTTPException as exc:
                bib_warnings.append(f"BibTeX 的 {name} 已忽略：{exc.detail}")
        title = title or bib_fields.get("title", "")
        venue = venue or bib_fields.get("venue", "")
        arxiv = arxiv or bib_fields.get("arxiv", "")

    if bibtex.strip():
        absorb_bibtex(bibtex)

    aid = ""
    or_id = ""
    ieee_ref = {}
    ieee_doi = ""
    fetched_pdf = None
    fetched_src, fetched_name = b"", ""
    if arxiv_url.strip():
        aid = arxiv_id(arxiv_url)
        try:
            info = arxiv_lookup(aid)
        except HTTPException:
            info = {}
        title = title or info.get("title", "")
        venue = venue or info.get("venue", "")

    if or_url:
        or_id = orx.parse_url(or_url)
        try:
            info = openreview_lookup(or_url)
        except HTTPException:
            info = {}
        title = title or info.get("title", "")
        venue = venue or info.get("venue", "")
        arxiv = arxiv or info.get("arxiv_id", "")
        if info.get("bibtex") and not bib_fields:
            absorb_bibtex(info["bibtex"])

    if ieee_url.strip():
        ieee_ref = ieee_xplore.parse_ref(ieee_url)
        ieee_doi = ieee_ref.get("doi", "")
        try:
            info = ieee_lookup(ieee_url)
        except HTTPException:
            info = {}
        title = title or info.get("title", "")
        venue = venue or info.get("venue", "")
        ieee_doi = ieee_doi or info.get("doi", "")

    if not force:
        report = duplicate_report(title, aid or arxiv, slug, topic, year, or_url, ieee_doi)
        if report["duplicates"]:
            raise HTTPException(409, detail={
                "message": "库里可能已有相同论文",
                "duplicates": report["duplicates"],
            })

    if arxiv_url.strip():
        if not pdf:
            fetched_pdf = arxiv_pdf_bytes(aid)
        # 自带 PDF 时默认只上传 PDF，避免为补源码额外等待；纯 arXiv 导入仍自动抓源码。
        if not source_archive and (not pdf or fetch_source):
            fetched_src, fetched_name = arxiv_source_bytes(aid, progress=update_progress)
    if or_id and not pdf and fetched_pdf is None:
        update_progress("下载 OpenReview PDF")
        fetched_pdf = orx.pdf_bytes(or_id)
    if ieee_ref and not pdf and fetched_pdf is None:
        fetched_pdf = ieee_xplore.pdf_bytes(ieee_url, progress=update_progress)
    if not pdf and fetched_pdf is None and not source_archive and not fetched_src:
        raise HTTPException(400, "至少上传 paper.pdf 或源码包之一，或提供可抓取的 arXiv/OpenReview/IEEE 链接")

    pdf_data = read_limited(pdf.file) if pdf else fetched_pdf
    source_data = read_limited(source_archive.file) if source_archive else (fetched_src or None)
    result = save_paper(topic, year, slug, pdf_data, source_data,
                        source_archive.filename if source_archive else fetched_name,
                        copy_local, {"title": title, "venue": venue,
                                     "arxiv": aid or arxiv, "code": code_value,
                                     "openreview": or_url, "doi": ieee_doi})
    result.update(pdf_fetched=fetched_pdf is not None, source_fetched=bool(fetched_src))
    warnings = list(result.get("warnings") or []) + bib_warnings
    if bib_fields:
        try:
            merge_paper_bibliography(f"{topic}/{year}/{slug}", bib_fields)
        except (HTTPException, OSError) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else "本机研究记录写入失败"
            warnings.append(f"书目信息未保存：{detail}")
    result["warnings"] = warnings
    return result


@app.post("/api/supplement")
@tracked_operation
@serialized_write
def supplement(
    topic: str = Form(...),
    year: str = Form(...),
    slug: str = Form(...),
    copy_local: bool = Form(True),
    pdf: Optional[UploadFile] = File(None),
    source_archive: Optional[UploadFile] = File(None),
    operation_id: Optional[str] = None,
):
    seg(topic)
    if not YEAR.fullmatch(year):
        raise HTTPException(400, "年份必须是 4 位数字")
    if not SLUG.fullmatch(slug):
        raise HTTPException(400, "slug 格式不正确")
    if not pdf and not source_archive:
        raise HTTPException(400, "至少选择 pdf 或源码包之一")

    rdir = remote_dir(topic, year, slug)
    if not remote_exists(rdir, "d"):
        raise HTTPException(404, "论文目录不存在,请先在「上传」页新增")
    return save_paper(topic, year, slug,
                      read_limited(pdf.file) if pdf else None,
                      read_limited(source_archive.file) if source_archive else None,
                      source_archive.filename if source_archive else "", copy_local, {})


@app.get("/api/meta")
def get_meta(topic: str, year: str, slug: str):
    for v in (topic, year, slug):
        seg(v)
    return read_meta(remote_dir(topic, year, slug))


@app.post("/api/paper/edit")
@tracked_operation
@serialized_write
def edit_paper(payload: dict, operation_id: Optional[str] = None):
    old = payload.get("old") or {}
    src = payload.get("meta") or {}
    if not isinstance(src, dict) or not isinstance(old, dict):
        raise HTTPException(400, "old 和 meta 必须是对象")
    ot, oy, os_ = (str(old.get(k, "")) for k in ("topic", "year", "slug"))
    for value in (ot, oy, os_):
        seg(value)
    nt = str(src.get("topic") or ot)
    ny = str(src.get("year") or oy)
    ns = str(src.get("slug") or os_)
    seg(nt)
    if not YEAR.fullmatch(ny) or not SLUG.fullmatch(ns) or not ns.startswith(ny + "-"):
        raise HTTPException(400, "年份或 slug 格式不正确")
    old_dir, new_dir = remote_dir(ot, oy, os_), remote_dir(nt, ny, ns)
    if not remote_exists(old_dir, "d"):
        raise HTTPException(404, "论文不存在")
    if new_dir != old_dir and remote_exists(new_dir):
        raise HTTPException(409, "目标目录已存在")
    previous = read_meta(old_dir)
    old_local, new_local = local_path(ot, oy, os_), local_path(nt, ny, ns)
    if old_local != new_local and new_local.exists():
        raise HTTPException(409, "本机目标目录已存在")
    research_migration = None
    if old_dir != new_dir:
        research_migration = prepare_research_migration(
            {f"{ot}/{oy}/{os_}": f"{nt}/{ny}/{ns}"}, payload)
    entry = str(src.get("entry", "") or "").strip()
    if entry and not remote_exists(old_dir + "/" + relative_file(entry), "f"):
        raise HTTPException(400, "entry 文件不存在")
    code = code_link(src.get("code", previous.get("code", "")))
    openreview = openreview_link(src.get("openreview", previous.get("openreview", "")))
    doi = doi_link(src.get("doi", previous.get("doi", "")))
    stage = stage_remote(old_dir)
    for name in (os_ + ".pdf", "paper.pdf"):
        ssh_need(remote_guard(stage + "/" + name) +
                 f"if [ -f {q(stage + '/' + name)} ] && [ ! -e {q(stage + '/' + ns + '.pdf')} ]; "
                 f"then mv -- {q(stage + '/' + name)} {q(stage + '/' + ns + '.pdf')}; fi")
    has_pdf, has_source = remote_state(stage, ns)
    meta = dict(previous)
    meta.update(src)
    meta.update(slug=ns, topic=nt, year=int(ny), entry=entry if has_source else "",
                code=code.strip(), openreview=openreview, doi=doi,
                pdf=ns + ".pdf" if has_pdf else "", has_pdf=has_pdf, has_source=has_source,
                updated_at=datetime.now(timezone.utc).isoformat())
    write_meta(stage, meta)
    backup = publish_remote(stage, old_dir, new_dir)
    warnings = []
    if old_local.exists() and (old_local != new_local or os_ != ns):
        try:
            local_backup = local_recovery_path("paper")
            if any(p.is_symlink() for p in old_local.rglob("*")):
                raise OSError("符号链接")
            new_local.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(old_local, local_backup)
            if old_local != new_local:
                old_local.rename(new_local)
            for name in (os_ + ".pdf", "paper.pdf"):
                p = new_local / name
                if p.is_file() and not (new_local / (ns + ".pdf")).exists():
                    p.rename(new_local / (ns + ".pdf"))
        except (OSError, HTTPException):
            warnings.append("远端改名已完成，本机改名未完成；原文件或本机 recovery 副本已保留，请核对后修复")
    if research_migration is not None:
        finish_research_migration(research_migration, warnings)
    try:
        rename_star(f"{ot}/{oy}/{os_}", f"{nt}/{ny}/{ns}")
    except (HTTPException, OSError):
        warnings.append("论文已改名，但星标更新失败，请核对星标状态文件")
    updated, errors = sync_projects_for_paper(f"{ot}/{oy}/{os_}", meta)
    warnings.extend(errors)
    return {"ok": True, "key": f"{nt}/{ny}/{ns}", "meta": meta,
            "projects_updated": updated, "warnings": warnings, "recovery_path": backup}


RESEARCH_STATUSES = ("", "unread", "reading", "read", "reproduce")
VERSION_LABELS = ("", "arxiv", "conference", "journal", "other")
BIB_LIMITS = {"entry_type": 20, "title": 2000, "authors": 4000, "year": 4,
              "venue": 1000, "doi": 200, "arxiv": 100}


def research_key(key) -> str:
    if not isinstance(key, str) or len(key.split("/")) != 3:
        raise HTTPException(400, "key 必须是 主题/年份/slug")
    for part in key.split("/"):
        seg(part)
    return key


def research_text(value, limit: int, field: str) -> str:
    if (not isinstance(value, str) or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
                   for c in value if c not in "\n\t")):
        raise HTTPException(400, f"{field} 必须是长度不超过 {limit} 的字符串（不含控制字符）")
    return value


def bibliography_fields(fields) -> dict:
    if not isinstance(fields, dict) or set(fields) - set(BIB_LIMITS):
        raise HTTPException(400, "fields 必须是合法书目字段对象")
    result = {}
    for name, value in fields.items():
        value = research_text(value, BIB_LIMITS[name], name).strip()
        if name == "entry_type" and value not in ("misc", "article", "inproceedings"):
            raise HTTPException(400, "entry_type 必须是 misc/article/inproceedings")
        if name == "year" and value and not re.fullmatch(r"[1-9][0-9]{3}", value):
            raise HTTPException(400, "year 必须是四位年份或空字符串")
        if name == "doi" and value and not re.fullmatch(r"10\.[0-9]{4,9}/[A-Za-z0-9._;()/:+-]+", value):
            raise HTTPException(400, "doi 格式不正确")
        if name == "arxiv" and value and not re.fullmatch(
                r"(?:[0-9]{4}\.[0-9]{4,5}|[A-Za-z][A-Za-z.-]*/[0-9]{7})(?:v[1-9][0-9]*)?", value):
            raise HTTPException(400, "arxiv 格式不正确")
        result[name] = value
    return result


BIB_ENTRY_TYPES = {"article": "article", "inproceedings": "inproceedings",
                   "conference": "inproceedings", "misc": "misc"}
BIB_FIELD_MAP = {"title": "title", "author": "authors", "year": "year",
                 "journal": "venue", "booktitle": "venue", "howpublished": "venue",
                 "doi": "doi", "eprint": "arxiv", "arxiv": "arxiv"}
BIB_ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308",
               "~": "\u0303", "=": "\u0304", ".": "\u0307"}


def bib_clean(value: str) -> str:
    def accent(match):
        return unicodedata.normalize("NFC", match.group(2) + BIB_ACCENTS[match.group(1)])
    value = re.sub(r"\\(['`^\"~=.])\s*\{?([A-Za-z])\}?", accent, value)
    value = re.sub(r"\\([&%$#_{}])", r"\1", value)
    value = re.sub(r"\\[A-Za-z]+\s*", "", value)
    return " ".join(value.replace("{", "").replace("}", "").split())


def bib_balanced(text: str, start: int, opening: str, closing: str):
    depth, i = 1, start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == opening:
            depth += 1
        elif text[i] == closing:
            depth -= 1
            if not depth:
                return text[start + 1:i], i + 1
        i += 1
    raise HTTPException(400, "括号不匹配")


def bib_field_pairs(body: str) -> dict:
    pairs, i, size = {}, 0, len(body)
    while i < size:
        while i < size and body[i] in " \t\r\n,":
            i += 1
        match = re.match(r"[A-Za-z][\w-]*", body[i:])
        if not match:
            break
        name = match.group(0).lower()
        i += match.end()
        while i < size and body[i].isspace():
            i += 1
        if i >= size or body[i] != "=":
            raise HTTPException(400, f"字段 {name} 缺少 =")
        i += 1
        while i < size and body[i].isspace():
            i += 1
        if i >= size:
            raise HTTPException(400, f"字段 {name} 没有值")
        if body[i] == "{":
            value, i = bib_balanced(body, i, "{", "}")
        elif body[i] == '"':
            j, buf = i + 1, []
            while j < size:
                if body[j] == "\\":
                    buf.append(body[j:j + 2])
                    j += 2
                    continue
                if body[j] == '"':
                    break
                buf.append(body[j])
                j += 1
            if j >= size:
                raise HTTPException(400, f"字段 {name} 引号未闭合")
            value, i = "".join(buf), j + 1
        else:
            j = i
            while j < size and body[j] != ",":
                j += 1
            value, i = body[i:j], j
        pairs.setdefault(name, bib_clean(value))
    return pairs


def parse_bibtex(text: str) -> dict:
    if not isinstance(text, str) or len(text) > 20000:
        raise HTTPException(400, "最多 20000 字符")
    cleaned = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("%"))
    for match in re.finditer(r"@\s*([A-Za-z]+)\s*([{(])", cleaned):
        kind = match.group(1).lower()
        if kind in ("comment", "string", "preamble"):
            continue
        opening = match.group(2)
        body, _ = bib_balanced(cleaned, match.end() - 1, opening, "}" if opening == "{" else ")")
        depth, cut = 0, None
        for index, char in enumerate(body):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            elif char == "," and not depth:
                cut = index
                break
        raw = bib_field_pairs(body[cut + 1:] if cut is not None else "")
        fields = {"entry_type": BIB_ENTRY_TYPES.get(kind, "misc")}
        for source, target in BIB_FIELD_MAP.items():
            if raw.get(source) and target not in fields:
                fields[target] = raw[source]
        return {name: value for name, value in fields.items() if value}
    raise HTTPException(400, "没有找到 BibTeX 条目")


def citation_key(key: str, known=()) -> str:
    # BibTeX key is the slug; same-slug papers get topic/year suffixes.
    topic, year, slug = key.split("/")
    peers = [other for other in known if other != key and other.rsplit("/", 1)[-1] == slug]
    if not peers:
        return slug
    if not any(other.split("/", 1)[0] == topic for other in peers):
        return f"{slug}-{topic}"
    return f"{slug}-{topic}-{year}"


def read_research() -> dict:
    path = BASE / "config" / "research.json"
    if path.is_symlink():
        raise HTTPException(400, "research.json 不能是符号链接")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"items": {}, "revision": hashlib.sha256(b"").hexdigest()}
    except OSError:
        raise HTTPException(500, "research.json 读取失败，不会覆盖原文件")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"papers"} or not isinstance(data["papers"], dict):
            raise ValueError("schema")
        for key, record in data["papers"].items():
            research_key(key)
            if not isinstance(record, dict) or set(record) - {
                    "status", "note", "bibliography", "cite_key", "version_group", "version_label"}:
                raise ValueError("record")
            if record.get("status", "") not in RESEARCH_STATUSES:
                raise ValueError("status")
            research_text(record.get("note", ""), 1000, "note")
            bibliography_fields(record.get("bibliography", {}))
            if "cite_key" in record and (not isinstance(record["cite_key"], str)
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", record["cite_key"])):
                raise ValueError("cite_key")
            group = record.get("version_group", "")
            if not isinstance(group, str) or (group and not re.fullmatch(r"[0-9a-f]{32}", group)):
                raise ValueError("version_group")
            if record.get("version_label", "") not in VERSION_LABELS:
                raise ValueError("version_label")
    except (ValueError, TypeError, HTTPException):
        raise HTTPException(500, "research.json 结构损坏，不会覆盖原文件")
    return {"items": data["papers"], "revision": hashlib.sha256(raw).hexdigest()}


def research_for_write(payload: dict) -> dict:
    state = read_research()
    if payload.get("revision") != state["revision"]:
        raise HTTPException(409, "研究记录已改变或缺少 revision，请刷新后重试")
    return state


def save_research(state: dict) -> dict:
    # Also detect external changes between the initial read and atomic replacement.
    if read_research()["revision"] != state["revision"]:
        raise HTTPException(409, "研究记录已改变，请刷新后重试")
    # Old releases stored generated keys here; citation keys now derive from the slug.
    for record in state["items"].values():
        record.pop("cite_key", None)
    write_local_json(BASE / "config" / "research.json", {"papers": state["items"]})
    return read_research()


def merge_paper_bibliography(key: str, fields: dict) -> None:
    if not fields:
        return
    research_key(key)
    state = read_research()
    bibliography = state["items"].setdefault(key, {}).setdefault("bibliography", {})
    for name, value in fields.items():
        if value and not bibliography.get(name):
            bibliography[name] = value
    save_research(state)


def require_research_paper(key: str):
    research_key(key)
    if not local_path(*key.split("/")).is_dir() and not remote_exists(f"{LIB_REMOTE}/{key}", "d"):
        raise HTTPException(404, f"论文不存在: {key}")


@app.get("/api/research")
def get_research():
    return read_research()


@app.post("/api/reading")
@serialized_write
def set_reading(payload: dict):
    key = research_key(payload.get("key"))
    status = payload.get("status")
    if status not in RESEARCH_STATUSES:
        raise HTTPException(400, "status 不合法")
    note = research_text(payload.get("note"), 1000, "note")
    state = research_for_write(payload)
    require_research_paper(key)
    state["items"].setdefault(key, {}).update(status=status, note=note)
    return dict(save_research(state), ok=True)


@app.post("/api/citation")
@serialized_write
def set_citation(payload: dict):
    key = research_key(payload.get("key"))
    fields = bibliography_fields(payload.get("fields"))
    state = research_for_write(payload)
    require_research_paper(key)
    state["items"].setdefault(key, {}).setdefault("bibliography", {}).update(fields)
    return save_research(state)


def bibtex_escape(value: str) -> str:
    escapes = {"\\": r"\textbackslash{}", "{": r"\textbraceleft{}", "}": r"\textbraceright{}",
               "#": r"\#", "$": r"\$", "%": r"\%", "&": r"\&", "_": r"\_",
               "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    # Single pass: never interpret user-supplied TeX, including author braces.
    return "".join(escapes.get(c, " " if c in "\n\t" else c) for c in value)


def build_bibtex(cite_key: str, fields: dict) -> str:
    output_fields = {"title": fields["title"], "author": fields["authors"], "year": fields["year"]}
    venue_field = {"article": "journal", "inproceedings": "booktitle", "misc": "howpublished"}[fields["entry_type"]]
    output_fields[venue_field] = fields["venue"]
    output_fields["doi"] = fields["doi"]
    if fields["arxiv"]:
        output_fields.update(eprint=fields["arxiv"], archivePrefix="arXiv")
    lines = [f"@{fields['entry_type']}{{{cite_key},"]
    lines.extend(f"  {name} = {{{bibtex_escape(value)}}}," for name, value in output_fields.items() if value)
    lines.append("}")
    return "\n".join(lines) + "\n"


def citation_for_key(key: str, records: dict, remote_papers=None, scan_warning="", known=None) -> dict:
    research_key(key)
    local = local_path(*key.split("/")).is_dir()
    warnings = []
    meta = {}
    remote = False
    saved_fallback = False
    record = records.get(key, {})
    try:
        if scan_warning:
            raise HTTPException(502, scan_warning)
        if remote_papers is not None:
            remote = key in remote_papers
            item = remote_papers.get(key, {})
            if remote and isinstance(item.get("_citation_meta"), dict):
                meta = item["_citation_meta"]
            elif remote:
                rdir = f"{LIB_REMOTE}/{key}"
                if remote_exists(rdir + "/meta.json", "f"):
                    meta = read_meta(rdir)
                else:
                    warnings.append("远端没有 meta.json；元数据未核对")
        else:
            rdir = f"{LIB_REMOTE}/{key}"
            remote = remote_exists(rdir, "d")
            if remote and remote_exists(rdir + "/meta.json", "f"):
                meta = read_meta(rdir)
            else:
                warnings.append("远端没有可用元数据；元数据未核对")
    except (HTTPException, OSError) as exc:
        saved_fallback = any(record.get("bibliography", {}).get(name)
                             for name in ("title", "authors", "venue", "doi", "arxiv"))
        if not local and not remote:
            if not saved_fallback:
                raise HTTPException(502, f"无法核对论文是否存在: {key}: {getattr(exc, 'detail', str(exc))}")
            warnings.append("论文是否仍存在未核对，仅使用已保存书目")
        warnings.append(f"远端元数据未核对: {getattr(exc, 'detail', str(exc))}")
    if not local and not remote and not saved_fallback:
        raise HTTPException(404, f"论文不存在: {key}")
    overrides = record.get("bibliography", {})
    cached = title_cache().get(key, {}) if local else {}
    if not isinstance(cached, dict):
        cached = {}
    fields = {}
    for name in BIB_LIMITS:
        if name in overrides:
            value = overrides[name]
        elif name in meta:
            value = meta[name]
            if name == "year" and type(value) is int:
                value = str(value)
            if name == "authors" and isinstance(value, list) and all(isinstance(v, str) for v in value):
                value = " and ".join(value)
        elif name == "title":
            value = cached.get("title", "")
        elif name == "year":
            value = key.split("/")[1]
        else:
            value = "misc" if name == "entry_type" else ""
        try:
            fields[name] = bibliography_fields({name: value})[name]
        except HTTPException:
            warnings.append(f"{name} 格式或长度不合法，已忽略")
            fields[name] = "misc" if name == "entry_type" else ""
    for name in ("title", "authors", "year"):
        if not fields[name]:
            warnings.append(f"缺少 {name}，请手动补充")
    cite_key = citation_key(key, set(records) | set(known or ()) | {key})
    return {"key": key, "cite_key": cite_key, "cite": "\\cite{" + cite_key + "}",
            "bibtex": build_bibtex(cite_key, fields), "fields": fields, "warnings": warnings,
            "complete": all(bool(fields[name]) for name in ("title", "authors", "year"))}


@app.get("/api/citation")
def get_citation(key: str):
    return citation_for_key(key, read_research()["items"])


@app.get("/api/project/bibliography")
def project_bibliography(project: str, source: str = "remote"):
    source = project_source(source)
    project, _ = registered_project_root(project, source)
    doc, _ = read_refs_doc(project, source)
    keys = list(dict.fromkeys(ref["path"] for ref in doc["references"]))
    records = read_research()["items"]
    warnings, entries = [], []
    remote_papers, scan_warning = {}, ""
    if keys:
        try:
            remote_papers = scan_local() if source == "local" else scan_remote(cache_titles=False)
        except (HTTPException, OSError) as exc:
            scan_warning = (f"{source_label(source)}批量扫描失败，元数据未核对: "
                            f"{getattr(exc, 'detail', str(exc))}")
            warnings.append(scan_warning)
    used = set()
    for key in keys:
        try:
            citation = citation_for_key(key, records, remote_papers, scan_warning, known=keys)
        except HTTPException as exc:
            warnings.append(f"跳过 {key}: {exc.detail}")
            continue
        warnings.extend(f"{key}: {warning}" for warning in citation["warnings"])
        cite_key = citation["cite_key"]
        # Distinct papers can still collide (e.g. slug "x-TS"): keep the export compilable.
        sequence = 1
        while cite_key in used:
            sequence += 1
            cite_key = f"{citation['cite_key']}-{sequence}"
        if cite_key != citation["cite_key"]:
            warnings.append(f"{key}: 引用键 {citation['cite_key']} 重复，已导出为 {cite_key}")
            citation["cite_key"] = cite_key
            citation["bibtex"] = build_bibtex(cite_key, citation["fields"])
        used.add(cite_key)
        entries.append(citation["bibtex"])
    return {"bibtex": "\n".join(entries), "filename": "references.bib",
            "warnings": warnings, "count": len(entries)}


@app.post("/api/versions/link")
@serialized_write
def link_versions(payload: dict):
    key, other = (research_key(payload.get(name)) for name in ("key", "other_key"))
    if key == other:
        raise HTTPException(400, "不能关联同一篇论文")
    labels = [payload.get(name) for name in ("label", "other_label")]
    if any(label not in VERSION_LABELS for label in labels):
        raise HTTPException(400, "版本 label 不合法")
    state = research_for_write(payload)
    for member in (key, other):
        require_research_paper(member)
    a, b = (state["items"].setdefault(member, {}) for member in (key, other))
    ga, gb = a.get("version_group"), b.get("version_group")
    if ga and gb and ga != gb:
        raise HTTPException(409, "两篇论文属于不同版本组，不能自动合组")
    group = ga or gb or uuid4().hex
    for record, label in zip((a, b), labels):
        record.update(version_group=group, version_label=label)
    return save_research(state)


@app.post("/api/versions/unlink")
@serialized_write
def unlink_versions(payload: dict):
    key = research_key(payload.get("key"))
    state = research_for_write(payload)
    # Saved missing papers may also be unlinked; deletion does not erase records.
    record = state["items"].get(key)
    if record is None:
        require_research_paper(key)
        record = state["items"].setdefault(key, {})
    group = record.pop("version_group", "")
    record.pop("version_label", None)
    remaining = [r for r in state["items"].values() if group and r.get("version_group") == group]
    if len(remaining) == 1:
        remaining[0].pop("version_group", None)
    return save_research(state)


def prepare_research_migration(mapping: dict, payload: dict) -> dict:
    state = research_for_write(payload)
    for old, new in mapping.items():
        research_key(old)
        research_key(new)
        if old != new and new in state["items"]:
            raise HTTPException(409, f"目标研究记录已存在: {new}")
    for old, new in mapping.items():
        if old != new:
            state["items"][new] = state["items"].pop(old, {})
    return state


def finish_research_migration(state: dict, warnings: list):
    try:
        save_research(state)
    except (HTTPException, OSError) as exc:
        warnings.append(f"目录已改名，但研究记录迁移失败，原记录保留，请核对: {getattr(exc, 'detail', str(exc))}")


def valid_star_key(key) -> bool:
    if not isinstance(key, str):
        return False
    if KNOWLEDGE_KEY.fullmatch(key):
        return True
    parts = key.split("/")
    return len(parts) == 3 and all(SEG.fullmatch(p) for p in parts)


def load_stars() -> list:
    data = read_local_list(STARS_FILE)
    for key in data:
        if not valid_star_key(key):
            raise HTTPException(500, "星标文件含非法条目，不会覆盖原文件")
    return list(dict.fromkeys(data))


def save_stars(items: list) -> None:
    write_local_json(STARS_FILE, items)


@app.get("/api/stars")
def get_stars():
    items = load_stars()
    return {"items": items, "count": len(items)}


@app.post("/api/star")
@serialized_write
def set_star(payload: dict):
    key = str(payload.get("key", "")).strip().strip("/")
    if not valid_star_key(key):
        raise HTTPException(400, "key 必须是 主题/年份/slug 或 _knowledge/<id>")
    starred = bool(payload.get("starred"))
    items = load_stars()
    if starred and key not in items:
        items.append(key)
    elif not starred and key in items:
        items.remove(key)
    save_stars(items)
    return {"ok": True, "starred": starred, "count": len(items)}


def rename_star(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        return
    items = load_stars()
    if old_key in items:
        save_stars([new_key if x == old_key else x for x in items])


def drop_star(key: str) -> None:
    items = load_stars()
    if key in items:
        save_stars([x for x in items if x != key])


def knowledge_remote_root() -> str:
    return LIB_REMOTE + "/" + kb.KNOWLEDGE_DIR


def knowledge_remote_dir(item_id: str) -> str:
    if not kb.is_id(item_id):
        raise HTTPException(400, "非法资料编号")
    return knowledge_remote_root() + "/" + item_id


def knowledge_local_dir(item_id: str, create=False) -> Path:
    if not kb.is_id(item_id):
        raise HTTPException(400, "非法资料编号")
    root = LIB_LOCAL.resolve()
    path = root / kb.KNOWLEDGE_DIR / item_id
    if path.parent.is_symlink() or path.is_symlink():
        raise HTTPException(400, "资料库内不允许符号链接")
    if not path.resolve().is_relative_to(root):
        raise HTTPException(400, "路径超出本机资料库")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def scan_knowledge_remote() -> list:
    update_progress("扫描远端资料")
    root = knowledge_remote_root()
    script = (
        remote_guard(root) +
        f"if [ ! -d {q(root)} ]; then exit 0; fi; cd {q(root)} || exit 1; "
        "find . -mindepth 2 -maxdepth 2 -type f -name meta.json -exec sh -c '"
        "for f do d=$(dirname \"$f\"); printf \"%s\\t\" \"${d#./}\"; "
        "base64 < \"$f\" | tr -d \"\\n\"; printf \"\\n\"; done' sh {} +"
    )
    p = ssh_run(script, timeout=120)
    if p.returncode != 0:
        raise HTTPException(502, "读取远端资料失败")
    return kb.parse_listing(p.stdout.splitlines())


def scan_knowledge_local() -> list:
    root = LIB_LOCAL / kb.KNOWLEDGE_DIR
    if root.is_symlink() or not root.is_dir():
        return []
    items = []
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir() or not kb.is_id(entry.name):
            continue
        meta_path = entry / "meta.json"
        if meta_path.is_symlink() or not meta_path.is_file():
            continue
        try:
            raw = meta_path.read_bytes()
            if len(raw) > kb.MAX_META_BYTES:
                continue
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        item = kb.parse_meta(data, entry.name)
        if item:
            items.append(item)
    items.sort(key=lambda item: (item["updated_at"], item["title"].lower()), reverse=True)
    return items


def read_knowledge_local(item_id: str):
    try:
        folder = knowledge_local_dir(item_id)
    except HTTPException:
        return None
    meta_path = folder / "meta.json"
    if meta_path.is_symlink() or not meta_path.is_file():
        return None
    try:
        raw = meta_path.read_bytes()
        if len(raw) > kb.MAX_META_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    item = kb.parse_meta(data, item_id)
    if item is None:
        return None
    body = ""
    note = folder / "note.md"
    if note.is_file() and not note.is_symlink():
        try:
            body = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            body = ""
    item["body"] = body[:kb.MAX_BODY]
    return item


def read_knowledge_remote(item_id: str):
    folder = knowledge_remote_dir(item_id)
    meta_path = folder + "/meta.json"
    note_path = folder + "/note.md"
    script = (
        remote_guard(folder) +
        f"if [ ! -d {q(folder)} ]; then exit 44; fi; "
        f"if [ ! -f {q(meta_path)} ]; then exit 44; fi; "
        f"base64 < {q(meta_path)} | tr -d '\\n'; printf '\\n'; "
        f"if [ -f {q(note_path)} ]; then base64 < {q(note_path)} | tr -d '\\n'; fi; printf '\\n'"
    )
    p = ssh_run(script, timeout=30)
    if p.returncode == 44:
        return None
    if p.returncode != 0:
        raise HTTPException(502, "读取远端资料失败")
    lines = p.stdout.splitlines()
    if not lines:
        return None
    try:
        raw = base64.b64decode(lines[0], validate=True)
        if len(raw) > kb.MAX_META_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    item = kb.parse_meta(data, item_id)
    if item is None:
        return None
    body = ""
    if len(lines) > 1 and lines[1]:
        try:
            body = base64.b64decode(lines[1], validate=True).decode("utf-8")
        except (ValueError, TypeError, UnicodeDecodeError):
            body = ""
    item["body"] = body[:kb.MAX_BODY]
    return item


def knowledge_fields(payload) -> dict:
    try:
        return kb.normalize(payload)
    except kb.KnowledgeError as exc:
        raise HTTPException(400, exc.message) from exc


def write_knowledge_remote(item_id: str, fields: dict, existing=None,
                           expected: Optional[str] = None) -> dict:
    folder = knowledge_remote_dir(item_id)
    meta = kb.build_meta(fields, existing)
    ssh_need(remote_guard(folder) + f"set -e; mkdir -p -- {q(folder)}")
    write_remote_text(folder + "/note.md", fields["body"])
    write_remote_json(folder + "/meta.json", meta, expected=expected)
    return meta


def mirror_knowledge_local(item_id: str, fields: dict, meta: dict) -> list:
    try:
        folder = knowledge_local_dir(item_id, create=True)
        write_local_text(folder / "note.md", fields["body"])
        write_local_json(folder / "meta.json", meta)
        return []
    except (OSError, HTTPException):
        return ["资料已保存到远端，但本机镜像写入失败"]


@app.get("/api/knowledge")
def knowledge_list():
    try:
        return {"items": scan_knowledge_remote(), "remote_error": ""}
    except HTTPException as exc:
        return {"items": scan_knowledge_local(), "remote_error": str(exc.detail)}


@app.get("/api/knowledge/{item_id}")
def knowledge_detail(item_id: str):
    if not kb.is_id(item_id):
        raise HTTPException(400, "非法资料编号")
    try:
        item = read_knowledge_remote(item_id)
    except HTTPException as exc:
        item = read_knowledge_local(item_id)
        if item is None:
            raise HTTPException(502, f"远端读取失败，本机没有这篇资料: {exc.detail}") from exc
        return {"item": item, "source": "local", "remote_error": str(exc.detail)}
    if item is None:
        raise HTTPException(404, "资料不存在")
    return {"item": item, "source": "remote", "remote_error": ""}


@app.post("/api/knowledge")
@serialized_write
def create_knowledge(payload: dict):
    fields = knowledge_fields(payload)
    item_id = kb.new_id()
    update_progress("保存资料到 远端")
    meta = write_knowledge_remote(item_id, fields, expected="missing")
    warnings = mirror_knowledge_local(item_id, fields, meta)
    return {"ok": True, "item": kb.parse_meta(meta, item_id), "warnings": warnings,
            "notice": "已保存到资料库"}


@app.post("/api/knowledge/{item_id}")
@serialized_write
def update_knowledge(item_id: str, payload: dict):
    folder = knowledge_remote_dir(item_id)
    existing, revision = read_remote_json(folder + "/meta.json")
    if revision == "missing":
        raise HTTPException(404, "资料不存在")
    if not isinstance(existing, dict):
        raise HTTPException(500, "远端资料元数据损坏")
    fields = knowledge_fields(payload)
    update_progress("更新远端资料")
    meta = write_knowledge_remote(item_id, fields, existing=existing, expected=revision)
    warnings = mirror_knowledge_local(item_id, fields, meta)
    item = kb.parse_meta(meta, item_id)
    touched, project_errors = sync_projects_for_knowledge(item_id, item)
    warnings.extend(project_errors)
    return {"ok": True, "item": item, "projects_updated": touched, "warnings": warnings,
            "notice": "资料已更新"}


def ensure_knowledge_local_note(item_id: str):
    folder = knowledge_local_dir(item_id)
    note = folder / "note.md"
    local = read_knowledge_local(item_id)
    if local is not None and local["kind"] != "note":
        raise HTTPException(400, "链接资料没有 Markdown 文件，请用网页打开")
    if local is not None and note.is_file() and not note.is_symlink():
        return note, "local"
    item = read_knowledge_remote(item_id)
    if item is None:
        raise HTTPException(404, "资料不存在")
    if item["kind"] != "note":
        raise HTTPException(400, "链接资料没有 Markdown 文件，请用网页打开")
    meta = {
        "kind": item["kind"], "title": item["title"], "url": item["url"],
        "source": item["source"], "tags": list(item["tags"]),
        "has_body": bool(item["body"].strip()),
        "created_at": item["created_at"], "updated_at": item["updated_at"],
    }
    try:
        knowledge_local_dir(item_id, create=True)
        write_local_text(note, item["body"])
        write_local_json(folder / "meta.json", meta)
    except (OSError, HTTPException) as exc:
        raise HTTPException(500, "本机镜像写入失败，无法用本地软件打开") from exc
    return note, "pulled"


@app.post("/api/knowledge/{item_id}/open", dependencies=[Depends(consume_write_body)])
@serialized_write
def open_knowledge_local(item_id: str):
    if sys.platform == "darwin":
        opener = "open"
    elif sys.platform.startswith("linux"):
        opener = "xdg-open"
    else:
        raise HTTPException(400, "当前系统不支持用本地软件打开")
    note, source = ensure_knowledge_local_note(item_id)
    try:
        subprocess.Popen([opener, str(note)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except OSError as exc:
        raise HTTPException(500, "无法启动本地软件") from exc
    return {"ok": True, "path": str(note), "source": source, "notice": "已用本地软件打开"}


@app.delete("/api/knowledge/{item_id}", dependencies=[Depends(consume_write_body)])
@serialized_write
def delete_knowledge(item_id: str, confirm: str):
    folder = knowledge_remote_dir(item_id)
    existing, revision = read_remote_json(folder + "/meta.json")
    if revision == "missing":
        raise HTTPException(404, "资料不存在")
    title = existing.get("title") if isinstance(existing, dict) else ""
    if not isinstance(title, str) or not title:
        raise HTTPException(500, "远端资料元数据损坏")
    if confirm != title:
        raise HTTPException(400, "确认标题与资料标题不一致")
    backup = f"{LIB_REMOTE}.recovery/{uuid4().hex}/{item_id}"
    update_progress("把资料移入远端回收区")
    try:
        ssh_need(remote_guard(folder) + remote_guard(backup) +
                 f"set -e; if [ -e {q(folder)} ]; then mkdir -p -- {q(backup.rsplit('/', 1)[0])}; "
                 f"mv -- {q(folder)} {q(backup)}; fi")
    except HTTPException as exc:
        raise HTTPException(exc.status_code,
                            f"远端移动未确认，请核对原目录 {folder} 和恢复位置 {backup}") from exc
    warnings = []
    try:
        local = knowledge_local_dir(item_id)
        if local.exists():
            shutil.rmtree(local)
    except (OSError, HTTPException):
        warnings.append("远端已移入回收区，但本机缓存清理失败")
    try:
        drop_star(kb.key_for(item_id))
    except (OSError, HTTPException):
        warnings.append("资料已移入回收区，但星标清理失败")
    return {"ok": True, "recovery": backup, "warnings": warnings,
            "notice": "资料已移入远端回收区，可在回收区恢复或清理"}


@app.delete("/api/paper", dependencies=[Depends(consume_write_body)])
@serialized_write
def delete_paper(source: str, topic: str, year: str, slug: str, confirm: str):
    for v in (topic, year, slug):
        seg(v)
    if confirm != slug:
        raise HTTPException(400, "确认名与 slug 不一致")
    if source not in ("local", "remote", "both"):
        raise HTTPException(400, "source 只能是 local/remote/both")
    local = local_path(topic, year, slug)
    recovery, warnings = {}, []
    if source in ("remote", "both"):
        path = remote_dir(topic, year, slug)
        backup = LIB_REMOTE + ".recovery/" + uuid4().hex + "/" + slug
        try:
            ssh_need(remote_guard(path) + remote_guard(backup) +
                     f"set -e; if [ -e {q(path)} ]; then mkdir -p -- {q(backup.rsplit('/', 1)[0])}; "
                     f"mv -- {q(path)} {q(backup)}; fi")
        except HTTPException as exc:
            raise HTTPException(exc.status_code,
                                f"远端移动未确认，请核对原目录 {path} 和恢复位置 {backup}") from exc
        recovery["remote"] = backup
    if source in ("local", "both") and local.exists():
        try:
            backup = local_recovery_path(slug)
            local.rename(backup)
            recovery["local"] = str(backup)
        except (OSError, HTTPException):
            if not recovery:
                raise HTTPException(500, "本机移入回收区失败，原目录未删除")
            warnings.append("远端已移入回收区，但本机目录移动失败，请核对两侧状态")
    if source in ("remote", "both"):
        try:
            drop_star(f"{topic}/{year}/{slug}")
        except (OSError, HTTPException):
            warnings.append("资料已移入回收区，但星标清理失败")
    return {"ok": True, "recovery": recovery, "warnings": warnings,
            "notice": "已移入回收区（不会自动清理）。项目引用保留并标记缺失。恢复位置: " + json.dumps(recovery, ensure_ascii=False)}


def remote_recovery_root() -> str:
    return LIB_REMOTE + ".recovery"


def local_recovery_root() -> Path:
    return Path(str(LIB_LOCAL.resolve()) + ".recovery")


def remote_keys_for_slug(slug: str) -> list:
    try:
        return [key for key in scan_remote() if key.rsplit("/", 1)[1] == slug]
    except HTTPException:
        return []


def _recovery_kind(name: str, child_kind: str, meta) -> dict:
    if child_kind == "f":
        if name.lower().endswith(".pdf"):
            return {"kind": "pdf_backup", "label": "单个 pdf 备份"}
        return {"kind": "file_backup", "label": "文件备份"}
    if isinstance(meta, dict):
        if meta.get("kind") in kb.KINDS and RECOVERY_ID.fullmatch(name):
            title = meta.get("title")
            return {"kind": "knowledge",
                    "label": title if isinstance(title, str) and title else name,
                    "target": f"{kb.KNOWLEDGE_DIR}/{name}"}
        topic = str(meta.get("topic") or "")
        year = str(meta.get("year") or "")
        slug = str(meta.get("slug") or "")
        if SEG.fullmatch(topic) and YEAR.fullmatch(year) and SEG.fullmatch(slug):
            return {"kind": "paper", "label": meta.get("title") or slug,
                    "target": f"{topic}/{year}/{slug}"}
    return {"kind": "mixed", "label": "目录备份"}


def remote_recovery_entries() -> list:
    root = remote_recovery_root()
    script = (
        remote_guard(root) +
        f"if [ ! -d {q(root)} ]; then exit 0; fi; cd {q(root)} || exit 1; "
        "for d in */; do [ -d \"$d\" ] || continue; id=${d%/}; "
        "case \"$id\" in *[!0-9a-f]*) continue;; esac; [ ${#id} -eq 32 ] || continue; "
        "if stat -c %Y \"$d\" >/dev/null 2>&1; then m=$(stat -c %Y \"$d\"); "
        "else m=$(stat -f %m \"$d\"); fi; "
        "k=$(du -sk \"$d\" | cut -f1); "
        "printf 'E\\t%s\\t%s\\t%s\\n' \"$id\" \"$m\" \"$k\"; done; "
        "find . -mindepth 2 -maxdepth 2 | while IFS= read -r p; do "
        "[ \"$p\" = \".\" ] && continue; "
        "if [ -d \"$p\" ]; then y=d; else y=f; fi; "
        "printf 'C\\t%s\\t%s\\n' \"${p#./}\" \"$y\"; done; "
        "find . -mindepth 3 -maxdepth 3 -name meta.json | while IFS= read -r p; do "
        "printf 'M\\t%s\\n' \"${p#./}\"; cat \"$p\" 2>/dev/null; printf '\\n__END__\\n'; done"
    )
    p = ssh_run(script, timeout=180)
    if p.returncode != 0:
        raise HTTPException(502, "读取远端回收区失败")
    entries, children, metas = {}, {}, {}
    current, buf = None, []
    for line in p.stdout.splitlines():
        if line.startswith("E\t"):
            parts = line.split("\t")
            if len(parts) == 4 and RECOVERY_ID.fullmatch(parts[1]):
                created = int(parts[2]) if parts[2].isdigit() else 0
                size = int(parts[3]) * 1024 if parts[3].isdigit() else 0
                entries[parts[1]] = {"created": created, "size": size}
            current, buf = None, []
        elif line.startswith("C\t"):
            parts = line.split("\t", 2)
            if len(parts) == 3:
                path = parts[1].split("/")
                if len(path) == 2 and RECOVERY_ID.fullmatch(path[0]):
                    children.setdefault(path[0], []).append((path[1], parts[2]))
        elif line.startswith("M\t"):
            path = line[2:].split("/")
            if len(path) == 3 and RECOVERY_ID.fullmatch(path[0]):
                current, buf = (path[0], path[1]), []
        elif current is not None and line.endswith("__END__"):
            rest = line[: -len("__END__")]
            if rest:
                buf.append(rest)
            try:
                metas[current] = json.loads("\n".join(buf))
            except ValueError:
                metas[current] = None
            current, buf = None, []
        elif current is not None:
            buf.append(line)

    items = []
    for rid, info in entries.items():
        kids = [kid for kid in children.get(rid, []) if kid[0] != ".DS_Store"]
        item = {"source": "remote", "id": rid, "created": info["created"],
                "size": info["size"], "name": "", "kind": "mixed",
                "label": "目录备份", "target": "", "restorable": False}
        if len(kids) == 1:
            name, kind = kids[0]
            item["name"] = name
            described = _recovery_kind(name, kind, metas.get((rid, name)))
            item.update(described)
            if described["kind"] in ("paper", "knowledge"):
                item["restorable"] = True
            elif described["kind"] == "mixed" and kind == "d" and SEG.fullmatch(name) and name != "paper":
                item.update({"kind": "topic", "label": "主题备份",
                             "target": name, "restorable": True})
        elif len(kids) > 1:
            item["name"] = "、".join(child for child, _kind in kids)[:120]
        else:
            item["label"] = "空备份（可清理）"
        items.append(item)
    items.sort(key=lambda item: item["created"], reverse=True)
    return items


def recovery_pdf_key(folder: Path, lookup_slug) -> str:
    if not lookup_slug:
        return ""
    pdfs = sorted(p for p in folder.glob("*.pdf") if p.is_file() and not p.is_symlink())
    return lookup_slug(pdfs[0].stem) if len(pdfs) == 1 else ""


def local_recovery_entries(lookup_slug=None) -> list:
    root = local_recovery_root()
    if root.is_symlink() or not root.is_dir():
        return []
    items = []
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir() or not RECOVERY_ID.fullmatch(entry.name):
            continue
        children = [child for child in entry.iterdir()
                    if not child.is_symlink() and child.name != ".DS_Store"]
        size, created = 0, 0
        for folder, _dirs, files in os.walk(entry, followlinks=False):
            for name in files:
                try:
                    size += os.path.getsize(os.path.join(folder, name))
                except OSError:
                    pass
        try:
            created = int(entry.stat().st_mtime)
        except OSError:
            pass
        item = {"source": "local", "id": entry.name, "created": created, "size": size,
                "name": "", "kind": "mixed", "label": "目录备份",
                "target": "", "restorable": False}
        if len(children) == 1:
            child = children[0]
            item["name"] = child.name
            if child.is_dir():
                meta = None
                try:
                    meta = json.loads((child / "meta.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    meta = None
                described = _recovery_kind(child.name, "d", meta)
                item.update(described)
                if described["kind"] == "paper":
                    item["restorable"] = True
                elif described["kind"] == "mixed":
                    years = [folder for folder in child.iterdir()
                             if folder.is_dir() and not folder.is_symlink() and YEAR.fullmatch(folder.name)]
                    papers = [paper for folder in years for paper in folder.iterdir()
                              if paper.is_dir() and not paper.is_symlink() and SEG.fullmatch(paper.name)]
                    if SEG.fullmatch(child.name) and papers:
                        item.update({"kind": "topic", "label": "主题备份",
                                     "target": child.name, "restorable": True})
                    else:
                        key = ""
                        if lookup_slug:
                            key = lookup_slug(child.name) or recovery_pdf_key(child, lookup_slug)
                        if key:
                            item.update({"kind": "paper", "label": child.name,
                                         "target": key, "restorable": True})
            elif child.is_file() and child.name.lower().endswith(".pdf"):
                slug = child.name[:-4]
                item.update({"kind": "pdf_backup", "label": child.name})
                if lookup_slug:
                    key = lookup_slug(slug)
                    if key:
                        item.update({"label": slug, "target": key, "restorable": True})
        elif len(children) > 1:
            item["name"] = "、".join(child.name for child in children)[:120]
        items.append(item)
    items.sort(key=lambda item: item["created"], reverse=True)
    return items


@app.get("/api/recovery/list")
def recovery_list():
    errors = []
    try:
        remote_items = remote_recovery_entries()
    except HTTPException as exc:
        remote_items = []
        errors.append(f"远端回收区读取失败: {exc.detail}")
    cache = {}

    def lookup_slug(slug):
        if "map" not in cache:
            cache["map"] = {}
            try:
                for key in scan_remote():
                    cache["map"].setdefault(key.rsplit("/", 1)[1], []).append(key)
            except HTTPException:
                cache["map"] = {}
        hits = cache["map"].get(slug) or []
        return hits[0] if len(hits) == 1 else ""

    try:
        local_items = local_recovery_entries(lookup_slug)
    except OSError as exc:
        local_items = []
        errors.append(f"本机回收区读取失败: {exc}")
    items = remote_items + local_items
    return {"items": items, "errors": errors,
            "size": sum(item["size"] for item in items),
            "remote_size": sum(item["size"] for item in remote_items),
            "local_size": sum(item["size"] for item in local_items)}


def _restore_remote(rid: str):
    entry_dir = remote_recovery_root() + "/" + rid
    update_progress("读取回收区条目")
    script = (remote_guard(entry_dir) +
              f"if [ ! -d {q(entry_dir)} ]; then exit 44; fi; "
              f"for p in {q(entry_dir)}/*; do [ -e \"$p\" ] || continue; "
              "if [ -d \"$p\" ]; then y=d; else y=f; fi; "
              "printf '%s\\t%s\\n' \"$(basename \"$p\")\" \"$y\"; done")
    p = ssh_run(script)
    if p.returncode == 44:
        raise HTTPException(404, "回收区条目不存在")
    if p.returncode != 0:
        raise HTTPException(502, "读取回收区条目失败")
    rows = [line.split("\t") for line in p.stdout.splitlines()
            if "\t" in line and line.split("\t", 1)[0] != ".DS_Store"]
    if len(rows) != 1:
        raise HTTPException(400, "这个备份包含多个条目，无法自动恢复；可以只清理它")
    name, kind = rows[0][0], rows[0][1]
    if not SEG.fullmatch(name):
        raise HTTPException(400, "备份名称不合法，无法恢复")
    if kind != "d":
        raise HTTPException(400, "这个备份是单个文件，无法自动恢复；可以清理它")
    child = entry_dir + "/" + name
    meta, revision = read_remote_json(child + "/meta.json")
    topic = year = slug = ""
    if revision != "missing" and isinstance(meta, dict):
        topic = str(meta.get("topic") or "")
        year = str(meta.get("year") or "")
        slug = str(meta.get("slug") or "")
    if (revision != "missing" and isinstance(meta, dict)
            and meta.get("kind") in kb.KINDS and RECOVERY_ID.fullmatch(name)):
        target = f"{LIB_REMOTE}/{kb.KNOWLEDGE_DIR}/{name}"
    elif SEG.fullmatch(topic) and YEAR.fullmatch(year) and SEG.fullmatch(slug):
        target = f"{LIB_REMOTE}/{topic}/{year}/{slug}"
    else:
        if name == "paper" or not SEG.fullmatch(name):
            raise HTTPException(400, "无法判断这个备份原来的位置，只能清理，不能恢复")
        probe = ssh_run(remote_guard(child) +
                        f"find {q(child)} -mindepth 2 -maxdepth 2 -type d | head -1")
        if probe.returncode != 0 or not probe.stdout.strip():
            raise HTTPException(400, "无法判断这个备份原来的位置，只能清理，不能恢复")
        target = f"{LIB_REMOTE}/{name}"
    if remote_exists(target):
        raise HTTPException(409, f"恢复目标已存在: {target}")
    update_progress("恢复备份到资料库")
    p = ssh_run(remote_guard(child) + remote_guard(target) +
                f"set -e; [ ! -e {q(target)} ] || exit 75; "
                f"mkdir -p -- {q(target.rsplit('/', 1)[0])}; mv -- {q(child)} {q(target)}")
    if p.returncode == 75:
        raise HTTPException(409, "恢复目标已存在")
    if p.returncode != 0:
        raise HTTPException(502, "恢复失败，备份仍在回收区")
    ssh_run(f"rmdir {q(entry_dir)} 2>/dev/null || true")
    invalidate_options()
    return {"ok": True, "source": "remote", "id": rid, "target": target,
            "notice": f"已恢复到远端: {target}"}


def _restore_local(rid: str):
    root = local_recovery_root()
    if root.is_symlink():
        raise HTTPException(400, "回收区不能是符号链接")
    entry = root / rid
    if entry.is_symlink() or not entry.is_dir():
        raise HTTPException(404, "回收区条目不存在")
    children = [child for child in entry.iterdir()
                if not child.is_symlink() and child.name != ".DS_Store"]
    if len(children) != 1:
        raise HTTPException(400, "这个备份包含多个条目，无法自动恢复；可以只清理它")
    child = children[0]
    target = None
    if child.is_dir():
        meta = None
        try:
            meta = json.loads((child / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = None
        topic = str((meta or {}).get("topic") or "")
        year = str((meta or {}).get("year") or "")
        slug = str((meta or {}).get("slug") or "")
        if SEG.fullmatch(topic) and YEAR.fullmatch(year) and SEG.fullmatch(slug):
            target = local_path(topic, year, slug)
        else:
            years = [folder for folder in child.iterdir()
                     if folder.is_dir() and not folder.is_symlink() and YEAR.fullmatch(folder.name)]
            papers = [paper for folder in years for paper in folder.iterdir()
                      if paper.is_dir() and not paper.is_symlink() and SEG.fullmatch(paper.name)]
            if SEG.fullmatch(child.name) and child.name != "paper" and papers:
                target = local_path(child.name)
            else:
                keys = remote_keys_for_slug(child.name) if child.name != "paper" else []
                if len(keys) == 1:
                    target = local_path(*keys[0].split("/", 2))
                else:
                    pdfs = sorted(p for p in child.glob("*.pdf")
                                  if p.is_file() and not p.is_symlink())
                    if len(pdfs) == 1:
                        keys = remote_keys_for_slug(pdfs[0].stem)
                        if len(keys) == 1:
                            target = local_path(*keys[0].split("/", 2))
    elif child.is_file() and child.name.lower().endswith(".pdf"):
        keys = remote_keys_for_slug(child.name[:-4])
        if len(keys) == 1:
            target = local_path(*keys[0].split("/", 2)) / child.name
    if target is None:
        raise HTTPException(400, "无法判断这个备份原来的位置，只能清理，不能恢复")
    if target.exists():
        raise HTTPException(409, f"恢复目标已存在: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    update_progress("恢复本机备份")
    child.rename(target)
    try:
        entry.rmdir()
    except OSError:
        pass
    return {"ok": True, "source": "local", "id": rid, "target": str(target),
            "notice": f"已恢复到本机: {target.name}"}


@app.post("/api/recovery/restore")
@tracked_operation
@serialized_write
def restore_recovery(payload: dict, operation_id: Optional[str] = None):
    source = payload.get("source")
    rid = str(payload.get("id") or "")
    if source not in ("local", "remote"):
        raise HTTPException(400, "source 只能是 local 或 remote")
    if not RECOVERY_ID.fullmatch(rid):
        raise HTTPException(400, "非法的回收区编号")
    return _restore_remote(rid) if source == "remote" else _restore_local(rid)


@app.post("/api/recovery/purge")
@tracked_operation
@serialized_write
def purge_recovery(payload: dict, operation_id: Optional[str] = None):
    source = payload.get("source")
    ids = payload.get("ids")
    if source not in ("local", "remote", "both"):
        raise HTTPException(400, "source 只能是 local/remote/both")
    if payload.get("confirm") != RECOVERY_CONFIRM:
        raise HTTPException(400, f"确认文字必须是「{RECOVERY_CONFIRM}」")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 200:
        raise HTTPException(400, "ids 必须是 1-200 个编号的数组")
    for rid in ids:
        if not isinstance(rid, str) or not RECOVERY_ID.fullmatch(rid):
            raise HTTPException(400, f"非法回收区编号: {rid!r}")

    purged, warnings = [], []
    for rid in ids:
        if source in ("remote", "both"):
            entry_dir = remote_recovery_root() + "/" + rid
            p = ssh_run(remote_guard(entry_dir) +
                        f"if [ ! -d {q(entry_dir)} ]; then exit 44; fi; "
                        f"links=$(find {q(entry_dir)} -type l -print -quit); "
                        "[ -z \"$links\" ] || exit 73")
            if p.returncode == 73:
                warnings.append(f"远端备份 {rid} 含符号链接或无法检查，未清理")
            elif p.returncode not in (0, 44):
                warnings.append(f"远端备份 {rid} 无法检查，未清理")
            elif p.returncode == 0:
                update_progress(f"清理远端回收区：{rid}")
                p = ssh_run(remote_guard(entry_dir) + f"rm -rf -- {q(entry_dir)}")
                if p.returncode != 0:
                    warnings.append(f"远端备份 {rid} 清理失败")
                else:
                    purged.append({"source": "remote", "id": rid})
        if source in ("local", "both"):
            root = local_recovery_root()
            entry = root / rid
            if root.is_symlink() or entry.is_symlink() or not entry.is_dir():
                continue
            if any(os.path.islink(os.path.join(folder, name))
                   for folder, dirs, files in os.walk(entry, followlinks=False)
                   for name in dirs + files):
                warnings.append(f"本机备份 {rid} 含符号链接，未清理")
                continue
            update_progress(f"清理本机回收区：{rid}")
            try:
                shutil.rmtree(entry)
            except OSError:
                warnings.append(f"本机备份 {rid} 清理失败")
                continue
            purged.append({"source": "local", "id": rid})
    return {"ok": True, "purged": purged, "warnings": warnings,
            "notice": f"已清理 {len(purged)} 个回收区条目"}


PROJECT_SOURCES = ("remote", "local")


def project_source(value=None) -> str:
    source = "remote" if value is None or value == "" else value
    if source not in PROJECT_SOURCES:
        raise HTTPException(400, "项目来源必须是 remote 或 local")
    return source


def source_label(source: str) -> str:
    return "本机" if source == "local" else "远端"


def project_library(source: str) -> str:
    return str(LIB_LOCAL) if source == "local" else LIB_REMOTE


def load_projects(source: str = "remote") -> list:
    path = PROJECTS_LOCAL_FILE if source == "local" else PROJECTS_FILE
    if source == "local":
        return list(dict.fromkeys(local_abs_path(p) for p in read_local_list(path)))
    return list(dict.fromkeys(project_rel(p) for p in read_local_list(path)))


def save_projects(rels: list, source: str = "remote") -> None:
    write_local_json(PROJECTS_LOCAL_FILE if source == "local" else PROJECTS_FILE, rels)


def project_rel(project: str) -> str:
    rel = relative_file(project)
    if any(not SEG.fullmatch(part) for part in rel.split("/")):
        raise HTTPException(400, f"项目路径不合法: {project!r}")
    return rel


def local_abs_path(value) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value:
        raise HTTPException(400, "本机项目必须是绝对路径")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise HTTPException(400, "本机项目路径包含控制字符")
    parts = value.split("/")[1:]
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise HTTPException(400, f"本机项目路径不合法: {value!r}")
    return "/" + "/".join(parts)


def local_project_root(project: str, must_exist: bool = True) -> Path:
    norm = local_abs_path(project)
    path = Path("/")
    for part in norm.split("/")[1:]:
        path = path / part
        if path.is_symlink():
            raise HTTPException(400, "项目路径包含符号链接，拒绝访问")
        if not path.exists():
            if must_exist:
                raise HTTPException(404, detail={
                    "message": f"本机项目目录不存在: {norm}",
                    "missing_dir": True,
                    "project": norm,
                })
            break
    if path.exists() and not path.is_dir():
        raise HTTPException(400, f"本机项目路径不是目录: {norm}")
    return Path(norm)


def refs_file(project: str, source: str = "remote"):
    if source == "local":
        return local_project_root(project, must_exist=False) / "reference-tex.json"
    return f"{WORKSHOP}/{project_rel(project)}/reference-tex.json"


def ensure_project_dir(rel: str) -> str:
    root = f"{WORKSHOP}/{project_rel(rel)}"
    p = ssh_run(remote_guard(root) + f"set -e; mkdir -p -- {q(root)}")
    if p.returncode == 73:
        raise HTTPException(400, "项目路径包含符号链接，拒绝创建")
    if p.returncode != 0:
        raise HTTPException(502, "创建远端项目目录失败")
    return root


def project_view_types(value: Optional[str]) -> set:
    if value is None:
        return set(PROJECT_VIEW_TYPES)
    parts = [part.strip().lower() for part in value.split(",") if part.strip()]
    if len(parts) > len(PROJECT_VIEW_TYPES):
        raise HTTPException(400, "文件类型参数过多")
    unknown = set(parts) - set(PROJECT_VIEW_TYPES)
    if unknown:
        raise HTTPException(400, "不支持的文件类型: " + ", ".join(sorted(unknown)))
    return set(parts)


def registered_project_root(project: str, source: str = "remote") -> tuple:
    if source == "local":
        rel = local_abs_path(project)
        if rel not in load_projects("local"):
            raise HTTPException(403, "项目未登记")
        return rel, local_project_root(rel, must_exist=False)
    rel = project_rel(project)
    if rel not in load_projects():
        raise HTTPException(403, "项目未登记")
    return rel, f"{WORKSHOP}/{rel}"


def project_ext(name: str) -> str:
    if "." not in name or name.endswith("."):
        return ""
    return name.rsplit(".", 1)[-1].lower()


def project_has_control(name: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in name)


def parse_project_entries(payload: bytes, selected: set):
    fields = payload.split(b"\0")
    truncated = len(payload) >= PROJECT_LISTING_LIMIT
    if fields and fields[-1] == b"":
        fields.pop()
    else:
        truncated = True
    entries = []
    for index in range(0, len(fields) - 3, 4):
        kind, raw_size, raw_mtime, raw_name = fields[index:index + 4]
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not name or "/" in name or name.startswith(".") or project_has_control(name):
            continue
        if kind == b"d":
            entries.append({"name": name, "kind": "dir"})
            continue
        if kind != b"f":
            continue
        ext = project_ext(name)
        view = PROJECT_EXT_VIEW.get(ext)
        if view not in selected:
            continue
        try:
            entries.append({
                "name": name, "kind": "file", "type": view, "ext": ext,
                "size": int(raw_size), "mtime": round(float(raw_mtime), 3),
            })
        except ValueError:
            continue
    entries.sort(key=lambda item: (
        (0, item["name"].casefold()) if item["kind"] == "dir"
        else (1, -item["mtime"], item["name"].casefold())
    ))
    if len(entries) > PROJECT_MAX_ENTRIES:
        entries = entries[:PROJECT_MAX_ENTRIES]
        truncated = True
    return entries, truncated


def parse_project_recent(payload: bytes, selected: set):
    files = []
    for record in payload.split(b"\0"):
        if not record:
            continue
        parts = record.split(b"\t", 2)
        if len(parts) != 3:
            continue
        raw_mtime, raw_size, raw_path = parts
        try:
            path_value = raw_path.decode("utf-8")
            relative_file(path_value)
            if any(part.startswith(".") for part in path_value.split("/")):
                continue
            if path_value == "references" or path_value.startswith("references/"):
                continue
            file_type = PROJECT_EXT_VIEW.get(project_ext(path_value.rsplit("/", 1)[-1]))
            if file_type not in selected:
                continue
            files.append({
                "path": path_value,
                "name": path_value.rsplit("/", 1)[-1],
                "type": file_type,
                "size": int(raw_size),
                "mtime": round(float(raw_mtime), 3),
            })
        except (UnicodeDecodeError, ValueError, HTTPException):
            continue
    return files


def local_project_descend(root: Path, rel_path: str = "") -> Path:
    path = root
    if rel_path:
        relative_file(rel_path)
        for part in rel_path.split("/"):
            path = path / part
            if path.is_symlink():
                raise HTTPException(400, "项目路径包含符号链接，拒绝访问")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise HTTPException(400, "路径超出项目目录")
    return path


def local_project_entries(root: Path, rel_path: str, selected: set):
    target = local_project_descend(root, rel_path)
    if not target.is_dir():
        raise HTTPException(404, "项目目录不存在")
    entries = []
    try:
        children = list(os.scandir(target))
    except OSError:
        raise HTTPException(502, "项目目录读取失败")
    for child in children:
        name = child.name
        if name.startswith(".") or project_has_control(name) or "/" in name:
            continue
        if child.is_symlink():
            continue
        try:
            if child.is_dir(follow_symlinks=False):
                entries.append({"name": name, "kind": "dir"})
                continue
            if not child.is_file(follow_symlinks=False):
                continue
            view = PROJECT_EXT_VIEW.get(project_ext(name))
            if view not in selected:
                continue
            stat = child.stat(follow_symlinks=False)
        except OSError:
            continue
        entries.append({
            "name": name, "kind": "file", "type": view, "ext": project_ext(name),
            "size": stat.st_size, "mtime": round(stat.st_mtime, 3),
        })
    entries.sort(key=lambda item: (
        (0, item["name"].casefold()) if item["kind"] == "dir"
        else (1, -item["mtime"], item["name"].casefold())
    ))
    truncated = len(entries) > PROJECT_MAX_ENTRIES
    return entries[:PROJECT_MAX_ENTRIES], truncated


def local_project_recent(root: Path, selected: set, limit: int):
    if not root.is_dir():
        raise HTTPException(404, "项目目录不存在")
    files = []
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs
                   if name not in PROJECT_RECENT_PRUNE and not name.startswith(".")
                   and not os.path.islink(os.path.join(folder, name))]
        for name in names:
            full = os.path.join(folder, name)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            view = PROJECT_EXT_VIEW.get(project_ext(name))
            if view not in selected:
                continue
            try:
                stat = os.stat(full, follow_symlinks=False)
            except OSError:
                continue
            if not os.path.isfile(full):
                continue
            files.append({
                "path": rel, "name": name, "type": view,
                "size": stat.st_size, "mtime": round(stat.st_mtime, 3),
            })
    files.sort(key=lambda item: (item["mtime"], item["path"]), reverse=True)
    return files[:limit]


def local_project_file(root: Path, rel_path: str) -> Path:
    target = local_project_descend(root, rel_path)
    if not target.is_file():
        raise HTTPException(404, "项目文件不存在")
    try:
        size = target.stat(follow_symlinks=False).st_size
    except OSError:
        raise HTTPException(502, "项目文件读取失败")
    if size > PROJECT_MAX_FILE_BYTES:
        raise HTTPException(413, "项目文件超过预览大小限制")
    return target


class RefsDocument(dict):
    def __init__(self, data, revision):
        super().__init__(data)
        self.revision = revision


# Tells an AI reader to use the tex entry first and fall back to the PDF.
REFS_READ_ORDER = ["entry", "pdf"]


def refs_ordered(doc: dict, source: str = "remote") -> dict:
    ordered = {"library": project_library(source), "read_order": list(REFS_READ_ORDER),
               "references": doc.get("references", [])}
    ordered.update((key, value) for key, value in doc.items() if key not in ordered)
    return ordered


def read_local_document(path: Path):
    if path.is_symlink():
        raise HTTPException(400, "引用清单不能是符号链接")
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None, "missing"
    except OSError:
        raise HTTPException(500, f"无法读取引用清单，未改写原文件: {path}")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(500, f"引用清单不是合法 JSON，不会覆盖原文件: {path}")
    return data, hashlib.sha256(raw).hexdigest()


def write_local_document(path: Path, text: str, expected: Optional[str] = None) -> None:
    if path.is_symlink():
        raise HTTPException(400, "引用清单不能是符号链接")
    if not path.parent.is_dir():
        raise HTTPException(404, f"项目目录不存在: {path.parent}")
    if expected == "missing":
        if os.path.lexists(path):
            raise HTTPException(409, "引用清单已存在，请刷新后重试")
    elif expected is not None:
        try:
            current = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            raise HTTPException(409, "引用清单已被其他操作修改，请刷新后重试")
        if current != expected:
            raise HTTPException(409, "引用清单已被其他操作修改，请刷新后重试")
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as fp:
            tmp = Path(fp.name)
            fp.write(text)
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        raise HTTPException(500, f"引用清单写入失败，原文件未改动: {path}")
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def read_refs_doc(project: str, source: str = "remote"):
    update_progress(f"读取{source_label(source)}项目引用：{project}")
    if source == "local":
        doc, revision = read_local_document(refs_file(project, source))
    else:
        doc, revision = read_remote_json(refs_file(project, source))
    if revision == "missing":
        return RefsDocument(refs_ordered({"references": []}, source), revision), False
    if not isinstance(doc, dict) or not isinstance(doc.get("references"), list):
        raise HTTPException(500, f"{project}/reference-tex.json 结构不正确，不会覆盖原文件")
    for ref in doc["references"]:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            raise HTTPException(500, "引用清单含非法条目，不会覆盖原文件")
        if KNOWLEDGE_KEY.fullmatch(ref["path"]):
            if ref.get("kind") != "knowledge":
                raise HTTPException(500, "引用清单含非法资料条目，不会覆盖原文件")
        else:
            parts = ref["path"].split("/")
            if len(parts) != 3 or not all(SEG.fullmatch(p) for p in parts):
                raise HTTPException(500, "引用清单含非法路径，不会覆盖原文件")
        for key in ("entry", "pdf"):
            if ref.get(key):
                relative_file(ref[key])
        url = ref.get("url")
        if url is not None:
            if not isinstance(url, str) or kb.URL.fullmatch(url) is None:
                raise HTTPException(500, "引用清单含非法链接，不会覆盖原文件")
    doc["library"] = project_library(source)
    doc["read_order"] = list(REFS_READ_ORDER)
    return RefsDocument(refs_ordered(doc, source), revision), True


def write_refs_doc(project: str, doc: RefsDocument, source: str = "remote") -> None:
    update_progress(f"保存{source_label(source)}项目引用：{project}")
    ordered = refs_ordered(doc, source)
    doc.clear()
    doc.update(ordered)
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if source == "local":
        write_local_document(refs_file(project, source), text, expected=doc.revision)
    else:
        write_remote_text(refs_file(project, source), text, expected=doc.revision)
    doc.revision = hashlib.sha256(text.encode()).hexdigest()


def apply_meta_to_ref(ref: dict, meta: dict, source: str = "remote") -> bool:
    if source == "local":
        changed = False
        if ref.pop("entry", None) is not None:
            changed = True
        pdf = (meta.get("pdf") or "") if meta.get("has_pdf") else ""
        if pdf:
            if ref.get("pdf") != pdf:
                ref["pdf"] = pdf
                changed = True
        elif ref.pop("pdf", None) is not None:
            changed = True
        title = meta.get("title") or ""
        if title and ref.get("title") != title:
            ref["title"] = title
            changed = True
        return changed
    changed = False
    entry = meta.get("entry") or ""
    if entry:
        if ref.get("entry") != entry:
            ref["entry"] = entry
            changed = True
    elif ref.pop("entry", None) is not None:
        changed = True
    pdf = (meta.get("pdf") or "") if meta.get("has_pdf") else ""
    if pdf:
        if ref.get("pdf") != pdf:
            ref["pdf"] = pdf
            changed = True
    elif ref.pop("pdf", None) is not None:
        changed = True
    title = meta.get("title") or ""
    if title and ref.get("title") != title:
        ref["title"] = title
        changed = True
    code = meta.get("code") or ""
    if code:
        if ref.get("code") != code:
            ref["code"] = code
            changed = True
    elif ref.pop("code", None) is not None:
        changed = True
    openreview = meta.get("openreview") or ""
    if openreview:
        if ref.get("openreview") != openreview:
            ref["openreview"] = openreview
            changed = True
    elif ref.pop("openreview", None) is not None:
        changed = True
    return changed


def local_ref_meta(papers: dict, key: str):
    info = papers.get(key)
    if not info or not info.get("has_pdf"):
        return None
    return {"has_pdf": True, "pdf": info.get("pdf") or "", "title": info.get("title") or ""}


def sync_projects_for_paper(key: str, meta: dict):
    touched, errors = [], []
    new_path = f"{meta['topic']}/{meta['year']}/{meta['slug']}"
    local_papers = None
    for source in PROJECT_SOURCES:
        try:
            registered = load_projects(source)
        except HTTPException as exc:
            errors.append(f"资料已保存，但{source_label(source)}项目登记文件读取失败: {exc.detail}")
            continue
        if not registered:
            continue
        if source == "local":
            try:
                local_papers = scan_local() if local_papers is None else local_papers
            except (HTTPException, OSError) as exc:
                errors.append(f"资料已保存，但本机资料库扫描失败: {getattr(exc, 'detail', str(exc))}")
                continue
        for index, project in enumerate(registered, 1):
            update_progress(f"同步{source_label(source)}项目引用（{index}/{len(registered)}）：{project}")
            try:
                doc, found = read_refs_doc(project, source)
            except HTTPException as exc:
                errors.append(f"项目 {project} 引用未更新: {exc.detail}；资料新路径: {new_path}")
                continue
            if not found:
                continue
            ref_meta = meta if source == "remote" else local_ref_meta(local_papers, new_path)
            if ref_meta is not None and not ref_meta.get("title"):
                ref_meta["title"] = meta.get("title") or ""
            changed = False
            for ref in doc["references"]:
                if not isinstance(ref, dict) or ref.get("path") != key:
                    continue
                if ref.get("path") != new_path:
                    ref["path"] = new_path
                    changed = True
                if ref_meta is not None and apply_meta_to_ref(ref, ref_meta, source):
                    changed = True
            if changed:
                try:
                    write_refs_doc(project, doc, source)
                    touched.append(project)
                except (HTTPException, OSError) as exc:
                    errors.append(f"项目 {project} 引用写入失败: {getattr(exc, 'detail', str(exc))}；资料新路径: {new_path}")
    return touched, errors


def sync_projects_for_knowledge(item_id: str, item: dict):
    key = kb.key_for(item_id)
    want = {
        "kind": "knowledge",
        "title": item["title"],
        "entry": "note.md" if item["kind"] == "note" else "",
        "url": item["url"],
    }
    touched, errors = [], []
    for source in PROJECT_SOURCES:
        try:
            registered = load_projects(source)
        except HTTPException as exc:
            errors.append(f"资料已保存，但{source_label(source)}项目登记文件读取失败: {exc.detail}")
            continue
        for index, project in enumerate(registered, 1):
            update_progress(f"同步{source_label(source)}项目引用（{index}/{len(registered)}）：{project}")
            try:
                doc, found = read_refs_doc(project, source)
            except HTTPException as exc:
                errors.append(f"项目 {project} 引用未更新: {exc.detail}；资料: {key}")
                continue
            if not found:
                continue
            changed = False
            for ref in doc["references"]:
                if not isinstance(ref, dict) or ref.get("path") != key:
                    continue
                for field, value in want.items():
                    if str(ref.get(field) or "") != value:
                        if value:
                            ref[field] = value
                        else:
                            ref.pop(field, None)
                        changed = True
            if not changed:
                continue
            try:
                write_refs_doc(project, doc, source)
                touched.append(project)
            except (HTTPException, OSError) as exc:
                errors.append(f"项目 {project} 引用写入失败: {getattr(exc, 'detail', str(exc))}；资料: {key}")
    return touched, errors


def remap_paper_refs(old_keys: set, meta: dict):
    keep = f"{meta['topic']}/{meta['year']}/{meta['slug']}"
    touched, errors = [], []
    local_papers = None
    for source in PROJECT_SOURCES:
        try:
            registered = load_projects(source)
        except HTTPException as exc:
            errors.append(f"资料已合并，但{source_label(source)}项目登记文件读取失败: {exc.detail}")
            continue
        if not registered:
            continue
        if source == "local":
            try:
                local_papers = scan_local() if local_papers is None else local_papers
            except (HTTPException, OSError) as exc:
                errors.append(f"资料已合并，但本机资料库扫描失败: {getattr(exc, 'detail', str(exc))}")
                continue
        ref_meta = meta if source == "remote" else local_ref_meta(local_papers, keep)
        for index, project in enumerate(registered, 1):
            update_progress(f"同步{source_label(source)}项目引用（{index}/{len(registered)}）：{project}")
            try:
                doc, found = read_refs_doc(project, source)
            except HTTPException as exc:
                errors.append(f"项目 {project} 引用未更新: {exc.detail}")
                continue
            if not found:
                continue
            changed = False
            for ref in doc["references"]:
                if ref.get("path") not in old_keys:
                    continue
                ref["path"] = keep
                changed = True
                if ref_meta is not None and apply_meta_to_ref(ref, ref_meta, source):
                    changed = True
            if not changed:
                continue
            merged, seen = [], {}
            for ref in doc["references"]:
                path = ref.get("path")
                if path not in seen:
                    seen[path] = len(merged)
                    merged.append(ref)
                    continue
                first = merged[seen[path]]
                note = (ref.get("note") or "").strip()
                if note and note not in (first.get("note") or ""):
                    first["note"] = (first.get("note") or "") + (" / " if first.get("note") else "") + note
            doc["references"] = merged
            try:
                write_refs_doc(project, doc, source)
                touched.append(project)
            except (HTTPException, OSError) as exc:
                errors.append(f"项目 {project} 引用写入失败: {getattr(exc, 'detail', str(exc))}")
    return touched, errors


def remote_tree_has_symlink(path: str) -> bool:
    p = ssh_run(remote_guard(path) +
                f"if [ -e {q(path)} ]; then find {q(path)} -type l -print -quit; fi", timeout=300)
    if p.returncode != 0:
        raise HTTPException(502, "检查远端符号链接失败，未执行主题操作")
    return bool(p.stdout.strip())


def local_tree_has_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    for folder, dirs, files in os.walk(path, followlinks=False):
        if any(os.path.islink(os.path.join(folder, name)) for name in dirs + files):
            return True
    return False


def remote_topic_papers(topic: str) -> list:
    root = f"{LIB_REMOTE}/{topic}"
    p = ssh_run(remote_guard(root) +
                f"if [ -d {q(root)} ]; then find {q(root)} -type d -print; fi", timeout=300)
    if p.returncode != 0:
        raise HTTPException(502, "读取远端主题目录失败")
    prefix = root.rstrip("/") + "/"
    papers = []
    for line in p.stdout.splitlines():
        if not line.startswith(prefix):
            continue
        parts = line[len(prefix):].split("/")
        if len(parts) == 2 and all(SEG.fullmatch(part) for part in parts):
            papers.append(tuple(parts))
    return papers


def set_meta_topic(rdir: str, topic: str) -> bool:
    data, revision = read_remote_json(rdir + "/meta.json")
    if revision == "missing":
        return False
    if not isinstance(data, dict):
        raise HTTPException(500, f"{rdir}/meta.json 必须是对象，未覆盖原文件")
    if data.get("topic") == topic:
        return False
    data["topic"] = topic
    write_remote_json(rdir + "/meta.json", data, expected=revision)
    return True


def move_stars_topic(old: str, new: str) -> int:
    items = load_stars()
    prefix = old + "/"
    moved, seen = [], set()
    for key in items:
        target = new + "/" + key[len(prefix):] if key.startswith(prefix) else key
        if target not in seen:
            moved.append(target)
            seen.add(target)
    if moved != items:
        save_stars(moved)
    return sum(1 for key in items if key.startswith(prefix))


def drop_stars_topic(topic: str) -> int:
    items = load_stars()
    prefix = topic + "/"
    kept = [key for key in items if not key.startswith(prefix)]
    if kept != items:
        save_stars(kept)
    return len(items) - len(kept)


def rename_topic_refs(old: str, new: str):
    touched, errors = [], []
    prefix = old + "/"
    for source in PROJECT_SOURCES:
        try:
            registered = load_projects(source)
        except HTTPException as exc:
            errors.append(f"资料已重命名，但{source_label(source)}项目登记文件读取失败: {exc.detail}")
            continue
        for index, project in enumerate(registered, 1):
            update_progress(f"同步{source_label(source)}项目引用（{index}/{len(registered)}）：{project}")
            try:
                doc, found = read_refs_doc(project, source)
            except HTTPException as exc:
                errors.append(f"项目 {project} 引用读取失败: {exc.detail}")
                continue
            if not found:
                continue
            changed = False
            for ref in doc["references"]:
                path = ref.get("path", "")
                if path.startswith(prefix):
                    ref["path"] = new + "/" + path[len(prefix):]
                    changed = True
            if not changed:
                continue
            try:
                write_refs_doc(project, doc, source)
                touched.append(project)
            except (HTTPException, OSError) as exc:
                errors.append(f"项目 {project} 引用写入失败: {getattr(exc, 'detail', str(exc))}")
    return touched, errors


@app.post("/api/topic/rename")
@tracked_operation
@serialized_write
def rename_topic(payload: dict, operation_id: Optional[str] = None):
    old = str(payload.get("topic") or "").strip()
    new = str(payload.get("new") or "").strip()
    seg(old)
    seg(new)
    if len(new) > 120:
        raise HTTPException(400, "主题名过长")
    if old == new:
        raise HTTPException(400, "新旧主题名相同")
    old_remote = f"{LIB_REMOTE}/{old}"
    new_remote = f"{LIB_REMOTE}/{new}"
    old_local = local_path(old)
    new_local = local_path(new)
    remote_present = remote_exists(old_remote, "d")
    local_present = old_local.is_dir()
    if not remote_present and not local_present:
        raise HTTPException(404, "主题不存在")
    if remote_exists(new_remote, "d") or new_local.exists():
        raise HTTPException(409, "目标主题已存在")
    if remote_present and remote_tree_has_symlink(old_remote):
        raise HTTPException(400, "远端主题目录内含符号链接，未执行重命名")
    if local_present and local_tree_has_symlink(old_local):
        raise HTTPException(400, "本机主题目录内含符号链接，未执行重命名")

    old_keys = {key for key in read_research()["items"] if key.startswith(old + "/")}
    if local_present:
        old_keys.update(key for key in scan_local() if key.startswith(old + "/"))
    if remote_present:
        old_keys.update(f"{old}/{year}/{slug}" for year, slug in remote_topic_papers(old))
    research_migration = prepare_research_migration(
        {key: new + key[len(old):] for key in old_keys}, payload)
    warnings = []
    if remote_present:
        update_progress("移动远端主题目录")
        script = (remote_guard(old_remote) + remote_guard(new_remote) +
                  "set -e; umask 077; "
                  f"[ ! -e {q(new_remote)} ] || exit 75; "
                  f"mv -- {q(old_remote)} {q(new_remote)}")
        p = ssh_run(script, timeout=300)
        if p.returncode == 75:
            raise HTTPException(409, "远端目标主题已存在")
        if p.returncode != 0:
            raise HTTPException(502, "远端主题重命名失败，未改动本机；原目录仍在原位置")

    local_moved = False
    if local_present:
        update_progress("移动本机主题目录")
        try:
            old_local.rename(new_local)
            local_moved = True
        except OSError as exc:
            warnings.append(f"远端已重命名，但本机目录移动失败: {exc}")

    papers, meta_updated = 0, 0
    if remote_present:
        try:
            entries = remote_topic_papers(new)
        except HTTPException as exc:
            entries = []
            warnings.append(f"远端已重命名，但读取新目录失败: {exc.detail}")
        for index, (year, slug) in enumerate(entries, 1):
            update_progress(f"更新论文元数据（{index}/{len(entries)}）")
            papers += 1
            try:
                if set_meta_topic(f"{LIB_REMOTE}/{new}/{year}/{slug}", new):
                    meta_updated += 1
            except (HTTPException, OSError) as exc:
                warnings.append(
                    f"{year}/{slug}/meta.json 未更新: {getattr(exc, 'detail', str(exc))}")

    if remote_present or local_moved:
        finish_research_migration(research_migration, warnings)
    else:
        raise HTTPException(500, "本机主题改名失败，目录与研究记录均未移动")
    try:
        stars_moved = move_stars_topic(old, new)
    except (HTTPException, OSError) as exc:
        stars_moved = 0
        warnings.append(f"星标更新失败: {getattr(exc, 'detail', str(exc))}")
    touched, errors = rename_topic_refs(old, new)
    warnings.extend(errors)
    invalidate_options()
    return {"ok": True, "topic": new, "papers": papers, "meta_updated": meta_updated,
            "local_moved": local_moved, "stars_moved": stars_moved,
            "projects_updated": touched, "warnings": warnings}


@app.delete("/api/topic", dependencies=[Depends(consume_write_body)])
@tracked_operation
@serialized_write
def delete_topic(topic: str, confirm: str, operation_id: Optional[str] = None):
    seg(topic)
    if confirm != topic:
        raise HTTPException(400, "确认名与主题名不一致")
    remote_root = f"{LIB_REMOTE}/{topic}"
    local_root = local_path(topic)
    remote_present = remote_exists(remote_root, "d")
    local_present = local_root.is_dir()
    if not remote_present and not local_present:
        raise HTTPException(404, "主题不存在")
    if remote_present and remote_tree_has_symlink(remote_root):
        raise HTTPException(400, "远端主题目录内含符号链接，未执行删除")
    if local_present and local_tree_has_symlink(local_root):
        raise HTTPException(400, "本机主题目录内含符号链接，未执行删除")

    recovery, warnings = {}, []
    if remote_present:
        update_progress("将远端主题移入回收区")
        backup = f"{LIB_REMOTE}.recovery/{uuid4().hex}/{topic}"
        script = (remote_guard(remote_root) + remote_guard(backup) +
                  "set -e; umask 077; "
                  f"mkdir -p -- {q(backup.rsplit('/', 1)[0])}; "
                  f"if [ -e {q(remote_root)} ]; then mv -- {q(remote_root)} {q(backup)}; fi")
        p = ssh_run(script, timeout=300)
        if p.returncode != 0:
            raise HTTPException(502, f"远端主题移入回收区失败，未改动本机。恢复目标: {backup}")
        recovery["remote"] = backup
    if local_present:
        update_progress("将本机主题移入回收区")
        try:
            backup = local_recovery_path(topic)
            local_root.rename(backup)
            recovery["local"] = str(backup)
        except (OSError, HTTPException) as exc:
            if not recovery:
                raise HTTPException(500, "本机主题移入回收区失败，原目录未删除")
            warnings.append(
                f"远端已移入回收区，但本机目录移动失败: {getattr(exc, 'detail', str(exc))}")
    try:
        stars_removed = drop_stars_topic(topic)
    except (HTTPException, OSError) as exc:
        stars_removed = 0
        warnings.append(f"星标清理失败: {getattr(exc, 'detail', str(exc))}")
    invalidate_options()
    return {"ok": True, "topic": topic, "recovery": recovery,
            "stars_removed": stars_removed, "warnings": warnings,
            "notice": "主题已移入回收区（不会自动清理）。项目引用保留并标记缺失。"}


@app.get("/api/projects")
def projects(source: str = "remote"):
    source = project_source(source)
    items = []
    for rel in load_projects(source):
        if source == "local":
            exists = local_project_root(rel, must_exist=False).is_dir()
        else:
            exists = remote_exists(f"{WORKSHOP}/{rel}", "d")
        doc, found = read_refs_doc(rel, source)
        items.append({"project": rel, "source": source, "exists": exists,
                      "has_refs": found, "count": len(doc["references"])})
    return {"source": source, "items": items}


@app.post("/api/projects")
@serialized_write
def add_project(payload: dict):
    source = project_source(payload.get("source"))
    if source == "local":
        rel = local_abs_path(payload.get("project", ""))
        root = local_project_root(rel)
        if not root.is_dir():
            raise HTTPException(404, detail={
                "message": f"本机项目目录不存在: {rel}",
                "missing_dir": True,
                "project": rel,
            })
        rels = load_projects("local")
        created = False
        doc, found = read_refs_doc(rel, "local")
        if not found:
            write_refs_doc(rel, doc, "local")
            created = True
        if rel not in rels:
            save_projects(rels + [rel], "local")
        return {"ok": True, "project": rel, "source": source,
                "created": created, "missing": False}
    rel = project_rel(payload.get("project", ""))
    d = f"{WORKSHOP}/{rel}"
    exists = remote_exists(d, "d")
    if not exists and not bool(payload.get("allow_missing")):
        raise HTTPException(404, detail={
            "message": f"远端上还没有这个项目目录: {rel}",
            "missing_dir": True,
            "project": rel,
        })
    rels = load_projects()
    created = False
    if exists:
        doc, found = read_refs_doc(rel)
        if not found:
            write_refs_doc(rel, doc)
            created = True
    if rel not in rels:
        save_projects(rels + [rel])
    return {"ok": True, "project": rel, "source": source,
            "created": created, "missing": not exists}


@app.delete("/api/projects", dependencies=[Depends(consume_write_body)])
@serialized_write
def remove_project(project: str, source: str = "remote"):
    source = project_source(source)
    if source == "local":
        rel = local_abs_path(project)
        rels = load_projects("local")
        if rel in rels:
            save_projects([x for x in rels if x != rel], "local")
        return {"ok": True, "source": source}
    rel = project_rel(project)
    rels = load_projects()
    if rel in rels:
        save_projects([x for x in rels if x != rel])
    return {"ok": True, "source": source}


@app.get("/api/project/files")
def project_files(project: str, path: str = "", types: Optional[str] = None,
                  source: str = "remote"):
    source = project_source(source)
    rel, root = registered_project_root(project, source)
    if path:
        relative_file(path)
    selected = project_view_types(types)
    if source == "local":
        entries, truncated = local_project_entries(root, path, selected)
        return {"project": rel, "path": path, "entries": entries,
                "truncated": truncated, "source": source}
    target = f"{root}/{path}" if path else root
    script = remote_guard(target) + (
        f"if [ ! -d {q(target)} ]; then exit 44; fi; "
        f"find {q(target)} -mindepth 1 -maxdepth 1 \\( -type d -o -type f \\) "
        f"-printf '%y\\0%s\\0%T@\\0%f\\0' | head -c {PROJECT_LISTING_LIMIT}"
    )
    result = ssh_run(script, timeout=30, binary=True)
    if result.returncode == 44:
        raise HTTPException(404, "项目目录不存在")
    if result.returncode == 73:
        raise HTTPException(400, "项目路径包含符号链接，拒绝访问")
    if result.returncode != 0:
        raise HTTPException(502, "项目目录读取失败")
    entries, truncated = parse_project_entries(result.stdout, selected)
    return {"project": rel, "path": path, "entries": entries,
            "truncated": truncated, "source": source}


@app.get("/api/project/recent")
def project_recent(project: str, types: Optional[str] = None, limit: int = 30,
                   source: str = "remote"):
    source = project_source(source)
    rel, root = registered_project_root(project, source)
    selected = project_view_types(types)
    limit = max(1, min(limit, 100))
    if not selected:
        return {"project": rel, "files": [], "source": source}
    if source == "local":
        return {"project": rel, "files": local_project_recent(root, selected, limit),
                "source": source}
    patterns = [
        f"-iname {q('*.' + ext)}"
        for view in sorted(selected) for ext in PROJECT_VIEW_TYPES[view]
    ]
    expression = " -o ".join(patterns)
    prune = " -o ".join(f"-name {q(name)}" for name in PROJECT_RECENT_PRUNE)
    script = remote_guard(root) + (
        f"if [ ! -d {q(root)} ]; then exit 44; fi; "
        f"find {q(root)} \\( {prune} \\) -prune -o -type f \\( {expression} \\) "
        f"-printf '%T@\\t%s\\t%P\\0' | sort -z -nr | head -z -n {limit}"
    )
    result = ssh_run(script, timeout=60, binary=True)
    if result.returncode == 44:
        raise HTTPException(404, "项目目录不存在")
    if result.returncode == 73:
        raise HTTPException(400, "项目路径包含符号链接，拒绝访问")
    if result.returncode != 0:
        raise HTTPException(502, "项目文件读取失败")
    return {"project": rel, "files": parse_project_recent(result.stdout, selected),
            "source": source}


@app.get("/api/project/file")
def project_file(project: str, path: str, source: str = "remote"):
    source = project_source(source)
    rel, root = registered_project_root(project, source)
    rel_path = relative_file(path)
    ext = project_ext(rel_path)
    media_type = PROJECT_FILE_TYPES.get(ext)
    if not media_type:
        raise HTTPException(400, "该文件类型不支持预览")
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if source == "local":
        return FileResponse(local_project_file(root, rel_path), media_type=media_type,
                            headers=headers)
    target = f"{root}/{rel_path}"
    script = remote_guard(target) + (
        f"if [ ! -f {q(target)} ]; then exit 44; fi; "
        f"size=$(stat -c %s -- {q(target)}) || exit 1; "
        f"if [ \"$size\" -gt {PROJECT_MAX_FILE_BYTES} ]; then exit 45; fi; "
        f"head -c {PROJECT_MAX_FILE_BYTES + 1} -- {q(target)}"
    )
    with tempfile.NamedTemporaryFile(suffix="." + ext, delete=False) as fp:
        tmp = Path(fp.name)
        try:
            result = ssh_stream(script, fp)
            fp.flush()
            if result.returncode == 44:
                raise HTTPException(404, "项目文件不存在")
            if result.returncode == 45:
                raise HTTPException(413, "项目文件超过预览大小限制")
            if result.returncode == 73:
                raise HTTPException(400, "项目路径包含符号链接，拒绝访问")
            if result.returncode != 0:
                raise HTTPException(502, "项目文件读取失败")
            if tmp.stat().st_size > PROJECT_MAX_FILE_BYTES:
                raise HTTPException(413, "项目文件超过预览大小限制")
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    return FileResponse(
        tmp, media_type=media_type,
        background=BackgroundTask(tmp.unlink, missing_ok=True),
        headers=headers,
    )


@app.get("/api/refs")
def get_refs(project: str, source: str = "remote"):
    source = project_source(source)
    rel = local_abs_path(project) if source == "local" else project_rel(project)
    doc, found = read_refs_doc(rel, source)
    entries = doc["references"]
    has_knowledge = any(
        isinstance(ref, dict) and KNOWLEDGE_KEY.fullmatch(str(ref.get("path", "")))
        for ref in entries
    )
    has_papers = any(
        isinstance(ref, dict) and not KNOWLEDGE_KEY.fullmatch(str(ref.get("path", "")))
        for ref in entries
    )
    if has_papers:
        papers = scan_local() if source == "local" else scan_remote()
    else:
        papers = {}
    knowledge = {}
    if has_knowledge:
        listed = scan_knowledge_local() if source == "local" else scan_knowledge_remote()
        knowledge = {item["key"]: item for item in listed}
    refs = []
    for ref in entries:
        if not isinstance(ref, dict):
            continue
        key = str(ref.get("path", ""))
        if KNOWLEDGE_KEY.fullmatch(key):
            info = knowledge.get(key)
            out = dict(ref)
            if info is None:
                out.update({"missing": True, "stale": False, "current": {}})
            else:
                cur = {
                    "kind": info["kind"],
                    "entry": "note.md" if info["kind"] == "note" else "",
                    "pdf": "",
                    "title": info["title"],
                    "url": info["url"],
                }
                out["current"] = cur
                out["missing"] = False
                out["stale"] = (
                    str(ref.get("entry") or "") != cur["entry"]
                    or str(ref.get("url") or "") != cur["url"]
                    or (bool(cur["title"]) and cur["title"] != ref.get("title"))
                )
            refs.append(out)
            continue
        info = papers.get(key)
        out = dict(ref)
        if info is None:
            out.update({"missing": True, "stale": False, "current": {}})
        elif source == "local":
            cur = {"entry": "", "pdf": info.get("pdf") or "",
                   "title": info.get("title") or ""}
            out["current"] = cur
            out["missing"] = not info.get("has_pdf")
            out["stale"] = (
                str(ref.get("entry") or "") != ""
                or str(ref.get("pdf") or "") != cur["pdf"]
            )
        else:
            cur = {
                "entry": info.get("entry") or "",
                "pdf": info.get("pdf") or "",
                "title": info.get("title") or "",
                "code": info.get("code") or "",
                "openreview": info.get("openreview") or "",
            }
            out["current"] = cur
            out["missing"] = not (info.get("has_pdf") or info.get("has_source"))
            out["stale"] = (
                str(ref.get("entry") or "") != cur["entry"]
                or str(ref.get("pdf") or "") != cur["pdf"]
                or str(ref.get("code") or "") != cur["code"]
                or str(ref.get("openreview") or "") != cur["openreview"]
            )
        refs.append(out)
    return {
        "project": rel,
        "source": source,
        "file": str(refs_file(rel, source)),
        "exists": found,
        "library": project_library(source),
        "read_order": list(REFS_READ_ORDER),
        "references": refs,
        "revision": doc.revision,
    }


@app.put("/api/refs")
@serialized_write
def put_refs(payload: dict):
    source = project_source(payload.get("source"))
    rel = local_abs_path(payload.get("project", "")) if source == "local" \
        else project_rel(payload.get("project", ""))
    refs = payload.get("references", [])
    if not isinstance(refs, list):
        raise HTTPException(400, "references 必须是数组")
    doc, _ = read_refs_doc(rel, source)
    if payload.get("revision") != doc.revision:
        raise HTTPException(409, "引用清单已变化或缺少版本，请重新选择项目加载后再保存")
    previous = {ref["path"]: ref for ref in doc["references"]}
    clean, checks, seen = [], [], set()
    for ref in refs:
        if not isinstance(ref, dict):
            raise HTTPException(400, "references 条目必须是对象")
        rel_path = relative_file(ref.get("path", ""))
        is_knowledge = KNOWLEDGE_KEY.fullmatch(rel_path) is not None
        if is_knowledge:
            if ref.get("kind", "knowledge") != "knowledge":
                raise HTTPException(400, "资料引用的 kind 必须是 knowledge")
        else:
            parts = rel_path.split("/")
            if len(parts) != 3:
                raise HTTPException(400, "path 必须是 主题/年份/slug 或 _knowledge/<id>")
            for part in parts:
                seg(part)
        if rel_path in seen:
            raise HTTPException(409, "同一路径不能重复加入项目")
        seen.add(rel_path)
        item = {"path": rel_path}
        if is_knowledge:
            item["kind"] = "knowledge"
        for field in ("entry", "pdf"):
            if ref.get(field):
                item[field] = relative_file(ref[field])
        if not is_knowledge and not item.get("entry") and not item.get("pdf"):
            raise HTTPException(400, "引用至少需要 entry 或 pdf")
        for field in ("title", "note"):
            if ref.get(field):
                item[field] = str(ref[field])
        if is_knowledge:
            url = ref.get("url") or ""
            if url:
                if not isinstance(url, str) or kb.URL.fullmatch(url) is None:
                    raise HTTPException(400, "资料链接必须是 http(s) 地址")
                item["url"] = url
        code = code_link(ref.get("code"))
        if code:
            item["code"] = code
        openreview = openreview_link(ref.get("openreview"))
        if openreview:
            item["openreview"] = openreview
        clean.append(item)
        old = previous.get(rel_path, {})
        if is_knowledge:
            if rel_path not in previous:
                checks.append(f"{rel_path}/meta.json")
        elif any(item.get(k) != old.get(k) for k in ("path", "entry", "pdf")):
            checks.extend(rel_path + "/" + item[k] for k in ("entry", "pdf") if item.get(k))
    library = project_library(source)
    if checks:
        if source == "local":
            missing = []
            for check in checks:
                target = local_path(*check.split("/"))
                if not target.is_file():
                    missing.append(str(target))
            if missing:
                raise HTTPException(400, "以下引用文件不存在:\n" + "\n".join(missing))
        else:
            targets = [f"{library}/{check}" for check in checks]
            command = "".join(remote_guard(c) for c in targets)
            command += "; ".join(f"if [ ! -f {q(c)} ]; then printf '%s\\n' {q(c)}; fi" for c in targets)
            result = ssh_need(command, timeout=120)
            if result.stdout.strip():
                raise HTTPException(400, "以下引用文件不存在:\n" + result.stdout.strip())
    doc["references"] = clean
    doc["library"] = library
    write_refs_doc(rel, doc, source)
    return {"ok": True, "count": len(clean), "revision": doc.revision, "source": source}


@app.post("/api/refs/add")
@tracked_operation
@serialized_write
def add_ref(payload: dict, operation_id: Optional[str] = None):
    source = project_source(payload.get("source"))
    rel = local_abs_path(payload.get("project", "")) if source == "local" \
        else project_rel(payload.get("project", ""))
    key = str(payload.get("path", "")).strip().strip("/")
    is_knowledge_key = KNOWLEDGE_KEY.fullmatch(key) is not None
    if not is_knowledge_key:
        parts = key.split("/")
        if len(parts) != 3:
            raise HTTPException(400, "path 必须是 主题/年份/slug 或 _knowledge/<id>")
        for part in parts:
            seg(part)
    note = str(payload.get("note", "") or "").strip()
    update = bool(payload.get("update"))

    if is_knowledge_key:
        item_id = key.split("/", 1)[1]
        info = read_knowledge_local(item_id) if source == "local" else read_knowledge_remote(item_id)
        if info is None:
            raise HTTPException(404, f"库中没有这条资料: {key}")
        ref = {"kind": "knowledge", "path": key}
        if info["kind"] == "note":
            ref["entry"] = "note.md"
        if info["url"]:
            ref["url"] = info["url"]
        if info["title"]:
            ref["title"] = info["title"]
        label = "资料"
    else:
        found = scan_local() if source == "local" else scan_remote()
        info = found.get(key)
        if source == "local":
            if not info or not info.get("has_pdf"):
                raise HTTPException(404, f"本机资料库中没有这篇论文的 pdf: {key}")
            ref = {"path": key}
            if info.get("pdf"):
                ref["pdf"] = info["pdf"]
            if info.get("title"):
                ref["title"] = info["title"]
        else:
            if not info or not (info.get("has_pdf") or info.get("has_source")):
                raise HTTPException(404, f"库中没有这篇论文: {key}")
            ref = {"path": key}
            if info.get("entry"):
                ref["entry"] = info["entry"]
            if info.get("pdf"):
                ref["pdf"] = info["pdf"]
            if info.get("title"):
                ref["title"] = info["title"]
            code = str(info.get("code") or "").strip()
            if code and len(code) <= 500 and CODE_LINK.fullmatch(code):
                ref["code"] = code
            openreview = str(info.get("openreview") or "").strip()
            if openreview:
                ref["openreview"] = openreview
        label = "论文"

    doc, _ = read_refs_doc(rel, source)
    created_dir = False
    if source == "local":
        if not local_project_root(rel, must_exist=False).is_dir():
            raise HTTPException(409, detail={
                "message": f"本机项目目录不存在: {rel}",
                "missing_dir": True,
                "project": rel,
            })
    elif not remote_exists(f"{WORKSHOP}/{rel}", "d"):
        if not bool(payload.get("create_missing")):
            raise HTTPException(409, detail={
                "message": f"远端上还没有项目目录: {rel}",
                "missing_dir": True,
                "project": rel,
            })
        ensure_project_dir(rel)
        created_dir = True
    existing = next((r for r in doc["references"]
                     if isinstance(r, dict) and r.get("path") == key), None)
    if existing is not None and not update:
        raise HTTPException(409, detail={
            "message": f"项目 {rel} 已包含这{label}",
            "existing": existing,
        })

    keep_note = note or (existing or {}).get("note", "")
    if keep_note:
        ref["note"] = keep_note

    if existing is not None:
        doc["references"] = [ref if r is existing else r for r in doc["references"]]
        action = "updated"
    else:
        doc["references"].append(ref)
        action = "added"
    write_refs_doc(rel, doc, source)
    return {"ok": True, "action": action, "ref": ref, "count": len(doc["references"]),
            "created_dir": created_dir, "source": source}


@app.post("/api/refs/sync")
@tracked_operation
@serialized_write
def sync_refs(payload: dict, operation_id: Optional[str] = None):
    source = project_source(payload.get("source"))
    rel = local_abs_path(payload.get("project", "")) if source == "local" \
        else project_rel(payload.get("project", ""))
    only = str(payload.get("path", "") or "").strip()
    doc, _ = read_refs_doc(rel, source)
    entries = doc["references"]
    has_knowledge = any(
        isinstance(ref, dict) and KNOWLEDGE_KEY.fullmatch(str(ref.get("path", "")))
        for ref in entries
    )
    has_papers = any(
        isinstance(ref, dict) and not KNOWLEDGE_KEY.fullmatch(str(ref.get("path", "")))
        for ref in entries
    )
    if has_papers:
        papers = scan_local() if source == "local" else scan_remote()
    else:
        papers = {}
    if has_knowledge:
        listed = scan_knowledge_local() if source == "local" else scan_knowledge_remote()
        knowledge = {item["key"]: item for item in listed}
    else:
        knowledge = {}
    updated, missing = [], []
    for ref in entries:
        if not isinstance(ref, dict):
            continue
        key = str(ref.get("path", ""))
        if only and key != only:
            continue
        if KNOWLEDGE_KEY.fullmatch(key):
            info = knowledge.get(key)
            if info is None:
                missing.append(key)
                continue
            want = {
                "kind": "knowledge",
                "entry": "note.md" if info["kind"] == "note" else "",
                "url": info["url"] or "",
                "title": info["title"] or "",
            }
            changes = {
                field: value for field, value in want.items()
                if str(ref.get(field) or "") != value
            }
            if not changes:
                continue
            for field, value in changes.items():
                if value:
                    ref[field] = value
                else:
                    ref.pop(field, None)
            updated.append({"path": key, "changes": changes})
            continue
        info = papers.get(key)
        if source == "local":
            if not info or not info.get("has_pdf"):
                missing.append(key)
                continue
            changes = {}
            if ref.get("entry"):
                changes["entry"] = ""
            if (info.get("pdf") or "") != str(ref.get("pdf") or ""):
                changes["pdf"] = info.get("pdf") or ""
            if (info.get("title") or "") and info.get("title") != ref.get("title"):
                changes["title"] = info["title"]
            if not changes:
                continue
            for field, val in changes.items():
                if val:
                    ref[field] = val
                else:
                    ref.pop(field, None)
            updated.append({"path": key, "changes": changes})
            continue
        if not info or not (info.get("has_pdf") or info.get("has_source")):
            missing.append(key)
            continue
        changes = {}
        if (info.get("entry") or "") != str(ref.get("entry") or ""):
            changes["entry"] = info.get("entry") or ""
        if (info.get("pdf") or "") != str(ref.get("pdf") or ""):
            changes["pdf"] = info.get("pdf") or ""
        if (info.get("title") or "") and info.get("title") != ref.get("title"):
            changes["title"] = info["title"]
        if str(ref.get("code") or "") != (info.get("code") or ""):
            changes["code"] = info.get("code") or ""
        if str(ref.get("openreview") or "") != (info.get("openreview") or ""):
            changes["openreview"] = info.get("openreview") or ""
        if not changes:
            continue
        for field, val in changes.items():
            if val:
                ref[field] = val
            else:
                ref.pop(field, None)
        updated.append({"path": key, "changes": changes})
    if updated:
        write_refs_doc(rel, doc, source)
    return {"ok": True, "updated": updated, "missing": missing,
            "count": len(doc["references"]), "source": source}


app.mount("/", StaticFiles(directory=str(BASE / "app" / "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CFG.get("host", "127.0.0.1"), port=int(CFG.get("port", 8817)),
                log_level="info")
