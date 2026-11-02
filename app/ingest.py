import gzip
import io
import json
import os
import re
import selectors
import socket
import stat
import struct
import subprocess
import tarfile
import tempfile
import time
import unicodedata
import urllib.request
import zipfile
import zlib
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from fastapi import HTTPException

MAX_UPLOAD_BYTES = 300 * 1024 * 1024
MAX_MEMBER_BYTES = 100 * 1024 * 1024
MAX_EXTRACT_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 10000
_CHUNK = 64 * 1024
_MAX_METADATA = 64 * 1024
_MAX_HEADERS = 32 * 1024
_MAX_REDIRECTS = 5
_HOSTS = {"arxiv.org", "export.arxiv.org"}
_ID = r"[0-9]{2}(?:0[1-9]|1[0-2])\.[0-9]{4,5}(?:v[1-9][0-9]*)?"


def read_limited(stream, limit=MAX_UPLOAD_BYTES) -> bytes:
    if not isinstance(limit, int) or limit < 0:
        raise HTTPException(400, "无效的读取上限")
    data = bytearray()
    while True:
        try:
            chunk = stream.read(min(_CHUNK, limit - len(data) + 1))
        except (OSError, EOFError, ValueError, zlib.error) as exc:
            raise HTTPException(400, "无法读取完整的二进制输入") from exc
        if not isinstance(chunk, bytes):
            raise HTTPException(400, "输入必须是二进制流")
        if not chunk:
            return bytes(data)
        if len(data) + len(chunk) > limit:
            raise HTTPException(413, "文件超过大小上限")
        data.extend(chunk)


def _parts(name, directory=False):
    if (not isinstance(name, str) or not name or name.startswith("/")
            or "\\" in name or ":" in name
            or any(unicodedata.category(c).startswith("C") for c in name)):
        raise HTTPException(400, "压缩包包含不安全路径")
    if len(name.encode("utf-8")) > 4096:
        raise HTTPException(400, "压缩包路径过长")
    while name.startswith("./"):
        name = name[2:]
    if directory:
        name = name.removesuffix("/")
        if name in ("", "."):
            return ()
    parts = tuple(name.split("/"))
    if any(p in ("", ".", "..") or len(p.encode("utf-8")) > 255 for p in parts):
        raise HTTPException(400, "压缩包包含不安全路径")
    return parts


def _ignored(parts):
    return any(p in ("__MACOSX", ".DS_Store") or p.startswith("._")
               or p.lower().endswith(".md") for p in parts)


class _Plan:
    def __init__(self):
        self.paths = {}
        self.count = 0
        self.size = 0

    def add(self, name, directory, size):
        parts = _parts(name, directory)
        self.count += 1
        if self.count > MAX_ARCHIVE_FILES:
            raise HTTPException(413, "压缩包文件数量超限")
        if size < 0 or (directory and size):
            raise HTTPException(400, "压缩包成员大小无效")
        if size > MAX_MEMBER_BYTES:
            raise HTTPException(413, "压缩包单文件过大")
        self.size += size
        if self.size > MAX_EXTRACT_BYTES:
            raise HTTPException(413, "压缩包解压总大小超限")
        key = tuple(unicodedata.normalize("NFC", p).casefold() for p in parts)
        for i in range(1, len(parts)):
            parent = key[:i]
            previous = self.paths.get(parent)
            if previous and (previous[0] != parts[:i] or not previous[1]):
                raise HTTPException(400, "压缩包路径冲突")
            self.paths.setdefault(parent, (parts[:i], True, False))
        previous = self.paths.get(key)
        if previous and (previous[0] != parts or previous[2]
                         or not directory or not previous[1]):
            raise HTTPException(400, "压缩包路径重复或冲突")
        self.paths[key] = (parts, directory, True)
        return parts


class _TarStream:
    def __init__(self, stream):
        self.stream = stream
        self.total = 0

    def read(self, size):
        budget = MAX_EXTRACT_BYTES + MAX_ARCHIVE_FILES * 2048
        data = self.stream.read(min(size, budget - self.total + 1))
        self.total += len(data)
        if self.total > budget:
            raise HTTPException(413, "tar 数据流解压大小超限")
        return data


def _exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise HTTPException(400, "压缩包不完整")
    return data


def _pax(data):
    result = {}
    while data:
        length, sep, _ = data.partition(b" ")
        if not sep or not length.isdigit() or len(length) > 10:
            raise HTTPException(400, "无效的 tar 扩展头")
        size = int(length)
        if size <= len(length) + 3 or size > len(data) or data[size - 1:size] != b"\n":
            raise HTTPException(400, "无效的 tar 扩展头")
        key, sep, value = data[len(length) + 1:size - 1].partition(b"=")
        if not sep or b"sparse" in key.lower() or key in (b"linkpath", b"SCHILY.filetype"):
            raise HTTPException(400, "不支持链接或稀疏 tar 成员")
        key = key.decode("utf-8")
        if key in result:
            raise HTTPException(400, "重复的 tar 扩展字段")
        result[key] = value.decode("utf-8")
        data = data[size:]
    return result


def _scan_tar(archive, destination=None):
    plan = _Plan()
    global_pax = {}
    pending = {}
    headers = 0
    with ExitStack() as stack:
        raw = stack.enter_context(archive.open("rb"))
        signature = raw.read(2)
        raw.seek(0)
        decoded = stack.enter_context(gzip.GzipFile(fileobj=raw)) if signature == b"\x1f\x8b" else raw
        stream = _TarStream(decoded)
        while True:
            block = stream.read(512)
            if not block:
                raise HTTPException(400, "tar 缺少结束标记")
            if len(block) != 512:
                raise HTTPException(400, "tar 首部不完整")
            if block == bytes(512):
                if pending:
                    raise HTTPException(400, "tar 扩展头缺少对应文件")
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        return
                    if chunk.strip(b"\0"):
                        raise HTTPException(400, "tar 结束标记后包含额外数据")
            headers += 1
            if headers > MAX_ARCHIVE_FILES:
                raise HTTPException(413, "tar 成员数量超限")
            info = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
            _parts(info.name, info.isdir())
            if info.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME):
                if info.size < 0 or info.size > _MAX_METADATA:
                    raise HTTPException(413, "tar 扩展头过大")
                payload = _exact(stream, info.size)
                _exact(stream, (-info.size) % 512)
                if info.type == tarfile.GNUTYPE_LONGNAME:
                    if "path" in pending:
                        raise HTTPException(400, "重复的 tar 长文件名")
                    pending["path"] = payload.removesuffix(b"\0").decode("utf-8")
                else:
                    fields = _pax(payload)
                    target = global_pax if info.type == tarfile.XGLTYPE else pending
                    if info.type == tarfile.XHDTYPE and target.keys() & fields.keys():
                        raise HTTPException(400, "重复的 tar 扩展头")
                    target.update(fields)
                if "path" in pending:
                    _parts(pending["path"], pending["path"].endswith("/"))
                if "path" in global_pax:
                    _parts(global_pax["path"], global_pax["path"].endswith("/"))
                continue
            if info.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE) or info.linkname:
                raise HTTPException(400, "压缩包不能包含链接、设备或特殊文件")
            fields = global_pax | pending
            pending = {}
            name = fields.get("path", info.name)
            size = info.size
            if "size" in fields:
                value = fields["size"]
                if not re.fullmatch(r"[0-9]{1,20}", value):
                    raise HTTPException(400, "无效的 tar 成员大小")
                size = int(value)
            parts = plan.add(name, info.isdir(), size)
            output = None
            try:
                if destination is not None and parts and not _ignored(parts):
                    path = destination.joinpath(*parts)
                    if info.isdir():
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        output = path.open("xb")
                remaining = size
                while remaining:
                    chunk = _exact(stream, min(_CHUNK, remaining))
                    if output is not None:
                        output.write(chunk)
                    remaining -= len(chunk)
                _exact(stream, (-size) % 512)
            finally:
                if output is not None:
                    output.close()


def _check_zip_directory(archive):
    with archive.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        length = stream.tell()
        stream.seek(max(0, length - 65557))
        end = stream.read(65557)
        offset = end.rfind(b"PK\x05\x06")
        if offset < 0 or len(end) - offset < 22:
            raise HTTPException(400, "ZIP 缺少目录")
        _, disk, directory_disk, disk_count, count, size, start, comment = struct.unpack(
            "<4s4H2IH", end[offset:offset + 22])
        if count > MAX_ARCHIVE_FILES:
            raise HTTPException(413, "压缩包文件数量超限")
        if (disk or directory_disk or disk_count != count or count == 65535
                or size == 0xFFFFFFFF or start == 0xFFFFFFFF
                or len(end) - offset != 22 + comment
                or start + size != length - len(end) + offset):
            raise HTTPException(400, "不支持分卷、ZIP64 或损坏的 ZIP 目录")
        stream.seek(start)
        actual = 0
        while stream.tell() < start + size:
            header = _exact(stream, 46)
            fields = struct.unpack("<4s6H3I5H2I", header)
            if fields[0] != b"PK\x01\x02":
                raise HTTPException(400, "无效的 ZIP 目录成员")
            actual += 1
            if actual > MAX_ARCHIVE_FILES:
                raise HTTPException(413, "压缩包文件数量超限")
            name_size, extra_size, comment_size = fields[10:13]
            if name_size > 4096:
                raise HTTPException(400, "ZIP 路径过长")
            next_offset = stream.tell() + name_size + extra_size + comment_size
            if next_offset > start + size:
                raise HTTPException(400, "ZIP 目录不完整")
            stream.seek(next_offset)
        if actual != count:
            raise HTTPException(400, "ZIP 目录数量不匹配")


def _zip_plan(archive):
    plan = _Plan()
    members = []
    for info in archive.infolist():
        kind = stat.S_IFMT(info.external_attr >> 16)
        directory = info.is_dir()
        if (kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                or (kind == stat.S_IFDIR and not directory)
                or (kind == stat.S_IFREG and directory)
                or (info.external_attr & 16 and not directory) or info.flag_bits & 1):
            raise HTTPException(400, "ZIP 包含链接、特殊文件或加密成员")
        parts = plan.add(info.orig_filename, directory, info.file_size)
        members.append((info, parts))
    return members


def _extract_zip(archive, members, destination):
    total = 0
    for info, parts in members:
        if info.is_dir():
            if parts and not _ignored(parts):
                destination.joinpath(*parts).mkdir(parents=True, exist_ok=True)
            continue
        with ExitStack() as stack:
            source = stack.enter_context(archive.open(info))
            output = None
            if not _ignored(parts):
                path = destination.joinpath(*parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                output = stack.enter_context(path.open("xb"))
            size = 0
            while True:
                chunk = source.read(min(_CHUNK, MAX_MEMBER_BYTES - size + 1,
                                        MAX_EXTRACT_BYTES - total + 1))
                if not chunk:
                    break
                size += len(chunk)
                total += len(chunk)
                if size > MAX_MEMBER_BYTES or total > MAX_EXTRACT_BYTES:
                    raise HTTPException(413, "ZIP 实际解压大小超限")
                if size > info.file_size:
                    raise HTTPException(400, "ZIP 实际大小与目录不符")
                if output is not None:
                    output.write(chunk)
            if size != info.file_size:
                raise HTTPException(400, "ZIP 成员不完整")


def extract_archive(archive: Path, tmp: Path) -> Path:
    archive, tmp = Path(archive), Path(tmp)
    try:
        if archive.is_symlink() or not archive.is_file():
            raise HTTPException(400, "压缩包必须是普通文件")
        if archive.stat().st_size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "上传文件超过大小上限")
        if tmp.is_symlink() or (tmp.exists() and (not tmp.is_dir() or any(tmp.iterdir()))):
            raise HTTPException(400, "解压目标必须是空的真实目录")
        with ExitStack() as stack:
            with archive.open("rb") as source:
                is_zip = source.read(4) in (b"PK\x03\x04", b"PK\x05\x06")
            if is_zip:
                _check_zip_directory(archive)
                zipped = stack.enter_context(zipfile.ZipFile(archive))
                members = _zip_plan(zipped)
            else:
                _scan_tar(archive)
            tmp.mkdir(parents=True, exist_ok=True)
            stage = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".ingest-", dir=tmp)))
            if is_zip:
                _extract_zip(zipped, members, stage)
            else:
                _scan_tar(archive, stage)
            for child in stage.iterdir():
                child.rename(tmp / child.name)
        children = list(tmp.iterdir())
        return children[0] if len(children) == 1 and children[0].is_dir() else tmp
    except HTTPException:
        raise
    except (OSError, EOFError, ValueError, tarfile.TarError, zipfile.BadZipFile,
            zlib.error, RuntimeError, NotImplementedError) as exc:
        raise HTTPException(400, f"无效或损坏的压缩包: {exc}") from exc


def _tree_files(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise HTTPException(400, "源码根目录必须是真实目录")
    root = root.resolve()
    files = {}
    pending = [root]
    count = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_ARCHIVE_FILES:
                    raise HTTPException(413, "源码文件数量超限")
                if entry.is_symlink():
                    continue
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                if _ignored(_parts(relative, entry.is_dir(follow_symlinks=False))):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    files[relative] = path
                else:
                    raise HTTPException(400, "源码包含特殊文件")
    return files


def _open_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HTTPException(400, "源码必须是普通文件")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def find_entry(root) -> str:
    try:
        files = _tree_files(root)
        tex = {name: path for name, path in files.items() if path.suffix.lower() == ".tex"}
        if "00README.json" in files:
            with _open_file(files["00README.json"]) as stream:
                raw = read_limited(stream, 1024 * 1024)
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                data = {}
            sources = data.get("sources") if isinstance(data, dict) else None
            for source in sources if isinstance(sources, list) else []:
                if not isinstance(source, dict) or str(source.get("usage", "")).lower() != "toplevel":
                    continue
                name = source.get("filename")
                if not isinstance(name, str) or not name:
                    continue
                name = "/".join(_parts(name))
                if name in tex:
                    return name
        for name in ("main.tex", "paper.tex"):
            if name in tex:
                return name
        root_tex = sorted(name for name in tex if "/" not in name)
        for name in root_tex:
            with _open_file(tex[name]) as stream:
                if b"\\documentclass" in stream.read(4096):
                    return name
        return root_tex[0] if root_tex else next(iter(sorted(tex)), "")
    except HTTPException:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise HTTPException(400, f"无法读取源码入口: {exc}") from exc


def make_tgz(root, out):
    out = Path(out)
    created = False
    try:
        files = _tree_files(root)
        with out.open("xb") as output:
            created = True
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                total = 0
                for name, path in sorted(files.items()):
                    with _open_file(path) as source:
                        size = os.fstat(source.fileno()).st_size
                        total += size
                        if size > MAX_MEMBER_BYTES or total > MAX_EXTRACT_BYTES:
                            raise HTTPException(413, "源码打包大小超限")
                        info = tarfile.TarInfo(name)
                        info.size = size
                        info.mode = 0o644
                        archive.addfile(info, source)
    except BaseException as exc:
        if created:
            out.unlink(missing_ok=True)
        if isinstance(exc, (OSError, ValueError, tarfile.TarError)):
            raise HTTPException(400, f"无法安全打包源码: {exc}") from exc
        raise


def _validate_url(url):
    if (not isinstance(url, str) or not url or "\\" in url
            or any(c.isspace() or unicodedata.category(c).startswith("C") for c in url)):
        raise HTTPException(400, "无效的 arXiv URL")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in _HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)
                or parsed.netloc.lower() not in _HOSTS | {host + ":443" for host in _HOSTS}
                or parsed.fragment):
            raise ValueError("URL 不在 HTTPS arXiv 白名单")
    except ValueError as exc:
        raise HTTPException(400, "只允许 arxiv.org 或 export.arxiv.org 的 HTTPS 默认端口 URL") from exc
    return parsed


def _force_https(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme == "http" and parsed.hostname in _HOSTS
                and parsed.port in (None, 80)
                and parsed.username is None and parsed.password is None
                and not parsed.fragment):
            return urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, ""))
    except ValueError:
        return url
    return url


def arxiv_id(raw: str) -> str:
    if not isinstance(raw, str):
        raise HTTPException(400, "无效的 arXiv ID")
    raw = raw.strip(" ")
    if re.fullmatch(_ID, raw):
        return raw
    parsed = _validate_url(raw)
    match = re.fullmatch(r"/(abs|pdf)/(" + _ID + r")(\.pdf)?", parsed.path)
    if (not match or parsed.query or (match.group(1) != "pdf" and match.group(3))):
        raise HTTPException(400, "只支持新式 arXiv ID 或 abs/pdf 链接")
    return match.group(2)


def _response_headers(raw):
    lines = raw.decode("iso-8859-1").split("\r\n")
    match = re.fullmatch(r"HTTP/(?:1\.[01]|2|3) ([0-9]{3})(?: .*)?", lines[0])
    if not match:
        raise HTTPException(502, "arXiv 返回无效 HTTP 状态行")
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if not sep or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            raise HTTPException(502, "arXiv 返回无效 HTTP 首部")
        key = key.lower()
        if key in headers and key in ("location", "content-length"):
            raise HTTPException(502, "arXiv 返回重复 HTTP 首部")
        headers[key] = value.strip(" \t")
    return int(match.group(1)), headers


def _download_proxy():
    """Select an available system/env proxy; mark a configured-but-dead proxy."""
    try:
        proxies = urllib.request.getproxies()
    except (OSError, ValueError, KeyError):
        return None, False
    value = proxies.get("https") or proxies.get("http")
    if not value:
        return None, False
    if "://" not in value:
        value = "http://" + value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None, True
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5", "socks5h"} or not parsed.hostname:
        return None, True
    if port is None:
        port = 443 if parsed.scheme == "https" else 1080 if parsed.scheme.startswith("socks") else 80
    try:
        with socket.create_connection((parsed.hostname, port), timeout=0.5):
            return value, False
    except OSError:
        return None, True


_PROXY_ENV = ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def _curl_response(url, deadline, limit, progress=None, proxy=None, proxy_unavailable=False):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HTTPException(504, "arXiv 下载总超时")
    command = ["curl", "-q", "--silent", "--show-error", "--include", "--no-buffer",
               "--globoff", "--no-location", "--proto", "=https", "--suppress-connect-headers",
               "--max-time", str(remaining), "--connect-timeout", str(min(30, remaining)),
               "--header", "Accept-Encoding: identity", "--user-agent", "papertex/0.1 (local library)",
               "--url", url]
    popen_kwargs = {}
    # Replace inherited proxy settings so a dead proxy cannot leak into this attempt.
    if proxy or proxy_unavailable:
        env = os.environ.copy()
        for key in _PROXY_ENV:
            env.pop(key, None)
        if proxy:
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
            env["ALL_PROXY"] = proxy
        popen_kwargs["env"] = env
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                **popen_kwargs)
    except OSError as exc:
        raise HTTPException(502, f"无法启动 arXiv curl 下载: {exc}") from exc
    data, header, errors = bytearray(), bytearray(), bytearray()
    status = None
    headers = {}
    informational = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            selector.register(proc.stderr, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HTTPException(504, "arXiv 下载总超时")
                events = selector.select(remaining)
                if not events:
                    raise HTTPException(504, "arXiv 下载总超时")
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), _CHUNK)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.fileobj is proc.stderr:
                        errors.extend(chunk[:max(0, 1024 - len(errors))])
                        continue
                    if status is None:
                        header.extend(chunk)
                        while status is None:
                            boundary = header.find(b"\r\n\r\n")
                            if boundary < 0:
                                if len(header) > _MAX_HEADERS:
                                    raise HTTPException(502, "arXiv HTTP 首部过大")
                                break
                            if boundary > _MAX_HEADERS:
                                raise HTTPException(502, "arXiv HTTP 首部过大")
                            code, fields = _response_headers(bytes(header[:boundary]))
                            del header[:boundary + 4]
                            if 100 <= code < 200:
                                informational += 1
                                if code == 101 or informational > 5:
                                    raise HTTPException(502, "arXiv HTTP 临时响应无效")
                                continue
                            status, headers = code, fields
                        if status is None:
                            continue
                        chunk = bytes(header)
                        header.clear()
                        if status in (301, 302, 303, 307, 308):
                            return status, headers.get("location"), b""
                        if status == 404:
                            raise HTTPException(404, "arXiv 文件确实不存在")
                        if status in (408, 504):
                            raise HTTPException(504, "arXiv 上游超时")
                        if status == 429:
                            raise HTTPException(503, "arXiv 请求过于频繁（429），请稍后重试")
                        if status >= 500:
                            raise HTTPException(502, f"arXiv 服务暂时不可用（HTTP {status}），请稍后重试")
                        if status != 200:
                            raise HTTPException(502, f"arXiv HTTP {status}")
                        length = headers.get("content-length")
                        if length is not None:
                            if not re.fullmatch(r"[0-9]{1,20}", length):
                                raise HTTPException(502, "arXiv Content-Length 无效")
                            if int(length) > limit:
                                raise HTTPException(413, "arXiv 下载超过大小上限")
                        if headers.get("content-encoding", "identity").lower() != "identity":
                            raise HTTPException(502, "arXiv 返回不支持的 HTTP 内容编码")
                    if len(data) + len(chunk) > limit:
                        raise HTTPException(413, "arXiv 下载超过大小上限")
                    data.extend(chunk)
                    if progress is not None:
                        length = headers.get("content-length")
                        progress(received_bytes=len(data), total_bytes=int(length) if length is not None else None)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPException(504, "arXiv 下载总超时")
            code = proc.wait(timeout=remaining)
            if code == 28:
                raise HTTPException(504, "arXiv curl 下载超时")
            if code:
                detail = errors.decode("utf-8", "replace")[:300]
                raise HTTPException(502, f"arXiv curl/TLS 下载失败 ({code}): {detail}")
            if status is None:
                raise HTTPException(502, "arXiv 未返回 HTTP 响应")
            if "content-length" in headers and len(data) != int(headers["content-length"]):
                raise HTTPException(502, "arXiv 下载不完整")
            return status, None, bytes(data)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "arXiv 下载总超时") from exc
    except OSError as exc:
        raise HTTPException(502, f"arXiv 下载失败: {exc}") from exc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def arxiv_http(url, timeout=120, limit=MAX_UPLOAD_BYTES, progress=None) -> bytes:
    if (not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf")
            or not isinstance(limit, int) or limit < 0):
        raise HTTPException(400, "无效的下载上限或超时")
    _validate_url(url)
    deadline = time.monotonic() + timeout
    proxy, proxy_unavailable = _download_proxy()
    visited = set()
    for hop in range(_MAX_REDIRECTS + 1):
        _validate_url(url)
        if url in visited:
            raise HTTPException(502, "arXiv 重定向循环")
        visited.add(url)
        if progress is not None:
            progress(host=urlsplit(url).hostname, received_bytes=0, total_bytes=None, timeout_seconds=timeout)
        status, location, data = _curl_response(url, deadline, limit, progress,
                                                proxy=proxy, proxy_unavailable=proxy_unavailable)
        if status == 200:
            return data
        if not location:
            raise HTTPException(502, "arXiv 重定向缺少 Location")
        if ("\\" in location or any(c.isspace() or unicodedata.category(c).startswith("C")
                                      for c in location)):
            raise HTTPException(400, "arXiv 重定向包含不安全 URL")
        try:
            next_url = urljoin(url, location)
        except ValueError as exc:
            raise HTTPException(400, "arXiv 重定向 URL 无效") from exc
        next_url = _force_https(next_url)
        _validate_url(next_url)
        if hop == _MAX_REDIRECTS:
            raise HTTPException(502, "arXiv 重定向次数超限")
        url = next_url
    raise HTTPException(502, "arXiv 重定向失败")


def _tar_header(data):
    if len(data) < 512 or data[:512] == bytes(512):
        return False
    try:
        tarfile.TarInfo.frombuf(data[:512], "utf-8", "strict")
        return True
    except (tarfile.HeaderError, ValueError):
        return False


def _source_payload(data):
    if data.startswith(b"PK\x03\x04"):
        return data, "source.zip"
    if data.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                prefix = stream.read(512)
                if _tar_header(prefix):
                    return data, "source.tar.gz"
                if len(prefix) > MAX_MEMBER_BYTES:
                    raise HTTPException(413, "单个 TeX 源码解压大小超限")
                raw = prefix + read_limited(stream, MAX_MEMBER_BYTES - len(prefix))
        except (OSError, EOFError, ValueError, zlib.error) as exc:
            raise HTTPException(400, "arXiv 返回损坏的 gzip 源码") from exc
    elif _tar_header(data):
        return data, "source.tar"
    else:
        raw = data
    if len(raw) > MAX_MEMBER_BYTES:
        raise HTTPException(413, "单个 TeX 源码大小超限")
    if (b"\0" in raw or re.search(
            br"<\s*(?:!doctype\b|/?\s*(?:html|head|body|div|script|title|meta|p|span|pre|form)\b)",
            raw, re.I)
            or not re.search(br"\\(?:(?:documentclass|documentstyle)\b|begin\s*\{document\})", raw)):
        raise HTTPException(502, "arXiv 返回的不是 tar/zip 或 TeX 源码")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("main.tex")
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    return buffer.getvalue(), "source.tar"


def arxiv_source_bytes(aid, progress=None):
    aid = arxiv_id(aid)
    failures = []
    for attempt, host in enumerate(("export.arxiv.org", "arxiv.org"), 1):
        notify = None
        if progress is not None:
            message = f"下载 arXiv 源码（来源 {attempt}/2）"
            notify = lambda **details: progress(message, attempt=attempt, **details)
        try:
            data = arxiv_http(f"https://{host}/e-print/{aid}", timeout=240, progress=notify)
            if progress is not None:
                progress("检查下载的源码格式")
            return _source_payload(data)
        except HTTPException as exc:
            if exc.status_code in (400, 413):
                raise
            failures.append(exc)
    if all(exc.status_code == 404 for exc in failures):
        return b"", ""
    failure = next(exc for exc in failures if exc.status_code != 404)
    raise HTTPException(failure.status_code, f"arXiv 源码获取失败: {failure.detail}") from failure
