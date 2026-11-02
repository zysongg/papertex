"""IEEE Xplore import: parse links/DOIs, read metadata, fetch PDF via campus access."""

import html
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from contextlib import contextmanager
from urllib.parse import quote, parse_qs, urlsplit

from fastapi import HTTPException

from ingest import MAX_UPLOAD_BYTES, _PROXY_ENV, _download_proxy

SITE = "ieeexplore.ieee.org"
_SITE_HOSTS = {SITE}
_DOI_HOSTS = {"doi.org", "www.doi.org", "dx.doi.org"}
_API_HOSTS = {"api.crossref.org"}
_DOC = "https://ieeexplore.ieee.org/document/{}/"
_STAMP = "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={}&ref="
_CROSSREF = "https://api.crossref.org/works/"
_DOI_REF = re.compile(r"^(?:doi:\s*)?(10\.\d{4,9}/[^\s\"'<>]+)$", re.I)
_DOC_PATH = re.compile(r"^/(?:abstract/)?document/(\d{1,12})/?$")
_NUMBER = re.compile(r"^\d{1,12}$")
_MAX_PAGE = 4 * 1024 * 1024
_MAX_JSON = 1024 * 1024
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_JOURNALS = {
    "IEEE Transactions on Dependable and Secure Computing": "TDSC",
    "IEEE Transactions on Information Forensics and Security": "TIFS",
    "IEEE Transactions on Knowledge and Data Engineering": "TKDE",
    "IEEE Transactions on Mobile Computing": "TMC",
    "IEEE Transactions on Parallel and Distributed Systems": "TPDS",
    "IEEE Transactions on Computers": "TC",
    "IEEE Transactions on Networking": "TON",
    "IEEE/ACM Transactions on Networking": "TON",
    "IEEE Transactions on Cloud Computing": "TCC",
    "IEEE Transactions on Services Computing": "TSC",
    "IEEE Transactions on Industrial Informatics": "TII",
    "IEEE Transactions on Smart Grid": "TSG",
    "IEEE Transactions on Cybernetics": "TCyb",
    "IEEE Transactions on Neural Networks and Learning Systems": "TNNLS",
    "IEEE Transactions on Pattern Analysis and Machine Intelligence": "TPAMI",
    "IEEE Transactions on Information Theory": "TIT",
    "IEEE Transactions on Wireless Communications": "TWC",
    "IEEE Transactions on Communications": "TCOM",
    "IEEE Transactions on Signal Processing": "TSP",
    "IEEE Transactions on Vehicular Technology": "TVT",
    "IEEE Transactions on Big Data": "TBD",
    "IEEE Transactions on Emerging Topics in Computing": "TETC",
    "IEEE Transactions on Sustainable Computing": "TSUSC",
    "IEEE Transactions on Computational Social Systems": "TCSS",
    "IEEE Transactions on Network and Service Management": "TNSM",
    "IEEE Transactions on Network Science and Engineering": "TNSE",
    "IEEE Transactions on Cognitive Communications and Networking": "TCCN",
    "IEEE Transactions on Software Engineering": "TSE",
    "IEEE Transactions on Reliability": "TR",
    "IEEE Transactions on Instrumentation and Measurement": "TIM",
    "IEEE Transactions on Multimedia": "TMM",
    "IEEE Transactions on Artificial Intelligence": "TAI",
    "IEEE Transactions on Consumer Electronics": "TCE",
    "IEEE Journal on Selected Areas in Communications": "JSAC",
    "IEEE Journal of Selected Topics in Signal Processing": "JSTSP",
    "IEEE Internet of Things Journal": "IoTJ",
    "IEEE Wireless Communications Letters": "WCL",
    "IEEE Communications Letters": "CL",
    "IEEE Signal Processing Letters": "SPL",
    "IEEE Systems Journal": "ISJ",
    "IEEE Access": "Access",
    "IEEE Network": "NET",
    "IEEE Communications Magazine": "CommMag",
    "IEEE Wireless Communications": "WCM",
    "IEEE Communications Surveys & Tutorials": "COMST",
}

_CONFERENCES = (
    (r"IEEE INFOCOM|IEEE Conference on Computer Communications", "INFOCOM"),
    (r"IEEE International Conference on Distributed Computing Systems", "ICDCS"),
    (r"IEEE/IFIP International Conference on Dependable Systems", "DSN"),
    (r"IEEE International Conference on Communications\b", "ICC"),
    (r"IEEE Global Communications Conference", "GLOBECOM"),
    (r"IEEE Conference on Communications and Network Security", "CNS"),
    (r"IEEE International Conference on Smart Grid Communications", "SmartGridComm"),
    (r"IEEE International Conference on Data Engineering|IEEE ICDE", "ICDE"),
    (r"IEEE Symposium on Security and Privacy", "S&P"),
    (r"IEEE European Symposium on Security and Privacy", "EuroS&P"),
    (r"IEEE International Conference on Computer Vision", "ICCV"),
    (r"IEEE/CVF Conference on Computer Vision and Pattern Recognition", "CVPR"),
    (r"IEEE International Conference on Acoustics, Speech and Signal Processing", "ICASSP"),
    (r"IEEE International Symposium on Information Theory", "ISIT"),
    (r"IEEE International Conference on Trust, Security and Privacy", "TrustCom"),
    (r"IEEE Wireless Communications and Networking Conference", "WCNC"),
    (r"IEEE International Conference on Network Protocols", "ICNP"),
    (r"IEEE International Conference on Sensing, Communication and Networking", "SECON"),
    (r"IEEE International Conference on Big Data", "BigData"),
)


def _text(value) -> str:
    if value is None:
        return ""
    text = re.sub(r"</?[A-Za-z][A-Za-z0-9:.-]{0,15}\s*/?>", "", str(value))
    return " ".join(html.unescape(text).split())


def _clean_doi(value) -> str:
    doi = _text(value).strip().rstrip(".,;，。；")
    return doi


def _check_url(url, hosts) -> None:
    if not isinstance(url, str) or not url or len(url) > 2000 or "\\" in url:
        raise HTTPException(400, "无效的 URL")
    if any(c.isspace() or unicodedata.category(c).startswith("C") for c in url):
        raise HTTPException(400, "URL 包含不安全字符")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(400, "无效的 URL") from exc
    if (parsed.scheme != "https" or port not in (None, 443)
            or parsed.username is not None or parsed.password is not None
            or (parsed.hostname or "").lower() not in hosts):
        raise HTTPException(400, f"只允许访问 {', '.join(sorted(hosts))}")


def parse_ref(raw) -> dict:
    """Return {'doi': str, 'arnumber': str} for an IEEE link or DOI."""
    if not isinstance(raw, str):
        raise HTTPException(400, "IEEE 链接或 DOI 必须是字符串")
    value = raw.strip()
    if not value:
        raise HTTPException(400, "先填 IEEE 链接或 DOI")
    if len(value) > 500:
        raise HTTPException(400, "IEEE 链接或 DOI 过长")

    match = _DOI_REF.fullmatch(value)
    if match:
        doi = _clean_doi(match.group(1))
        if not doi.lower().startswith("10.1109/"):
            raise HTTPException(400, "只支持 IEEE 的 10.1109 DOI；其他出版商的论文请直接上传 PDF")
        return {"doi": doi, "arnumber": ""}

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(400, "IEEE 链接格式无法识别") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or port not in (None, 80, 443):
        raise HTTPException(400, "IEEE 链接格式无法识别")
    if host in _DOI_HOSTS:
        doi = _clean_doi(parsed.path.lstrip("/"))
        if not _DOI_REF.fullmatch(doi) or not doi.lower().startswith("10.1109/"):
            raise HTTPException(400, "只支持 IEEE 的 10.1109 DOI")
        return {"doi": doi, "arnumber": ""}
    if host != SITE:
        raise HTTPException(400, "只支持 ieeexplore.ieee.org 的论文链接或 10.1109 DOI")

    match = _DOC_PATH.fullmatch(parsed.path)
    if match:
        return {"doi": "", "arnumber": match.group(1)}
    numbers = parse_qs(parsed.query).get("arnumber") or []
    if len(numbers) == 1 and _NUMBER.fullmatch(numbers[0]):
        return {"doi": "", "arnumber": numbers[0]}
    raise HTTPException(400, "无法从该 IEEE 链接识别论文编号；请使用论文页链接（/document/编号/）或 DOI")


def _proxy_env():
    try:
        proxy, unavailable = _download_proxy()
    except (OSError, ValueError, KeyError):
        return None
    if not proxy and not unavailable:
        return None
    env = os.environ.copy()
    # Drop inherited proxy settings so a dead proxy cannot leak into this attempt.
    for key in _PROXY_ENV:
        env.pop(key, None)
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy
    return env


def _curl(url, jar, timeout, limit, follow, env):
    with tempfile.TemporaryDirectory(prefix="papertex-ieee-") as folder:
        body_path = os.path.join(folder, "body")
        command = ["curl", "-q", "--silent", "--show-error", "--globoff",
                   "--proto", "=https", "--proto-redir", "=https",
                   "--connect-timeout", str(min(20, timeout)),
                   "--max-time", str(timeout), "--max-filesize", str(limit),
                   "--user-agent", _USER_AGENT,
                   "--header", "Accept-Language: en-US,en;q=0.9",
                   "--output", body_path,
                   "--write-out",
                   "%{http_code}\t%{url_effective}\t%{content_type}\t%{size_download}\t%{redirect_url}",
                   "--url", url]
        if follow:
            command[5:5] = ["--location", "--max-redirs", "5"]
        if jar:
            command[5:5] = ["--cookie", jar, "--cookie-jar", jar]
        try:
            proc = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=timeout + 5, env=env)
        except FileNotFoundError as exc:
            raise HTTPException(502, "未找到 curl 命令，无法访问 IEEE") from exc
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, "IEEE 请求超时") from exc
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        if proc.returncode:
            if proc.returncode == 28:
                raise HTTPException(504, "IEEE 请求超时")
            if proc.returncode == 63:
                raise HTTPException(413, "IEEE 文件超过大小上限")
            detail = "".join(c for c in stderr if c.isprintable())[:200]
            if proc.returncode in (6, 7):
                raise HTTPException(502, f"无法连接 IEEE，请检查校园网/VPN 或代理设置：{detail}")
            if proc.returncode in (35, 60):
                raise HTTPException(502, f"IEEE TLS 连接失败：{detail}")
            raise HTTPException(502, f"IEEE 请求失败（curl {proc.returncode}）：{detail}")
        fields = proc.stdout.decode("utf-8", "replace").split("\t") if proc.stdout else []
        if len(fields) != 5 or not fields[0].isdigit():
            raise HTTPException(502, "IEEE 返回了无法解析的响应")
        try:
            size = int(fields[3])
        except ValueError as exc:
            raise HTTPException(502, "IEEE 返回了无法解析的响应") from exc
        if size > limit:
            raise HTTPException(413, "IEEE 文件超过大小上限")
        body = b""
        try:
            with open(body_path, "rb") as fp:
                body = fp.read(limit + 1)
        except OSError as exc:
            raise HTTPException(502, "读取 IEEE 响应失败") from exc
        if len(body) > limit:
            raise HTTPException(413, "IEEE 文件超过大小上限")
        return {
            "status": int(fields[0]), "url": fields[1], "type": fields[2],
            "redirect": fields[4], "body": body,
        }


class _Client:
    def __init__(self, jar, env):
        self.jar = jar
        self.env = env

    def get(self, url, timeout=45, limit=_MAX_PAGE, follow=False, hosts=_SITE_HOSTS):
        _check_url(url, hosts)
        response = _curl(url, self.jar, timeout, limit, follow, self.env)
        if response["url"]:
            final = urlsplit(response["url"]).hostname or ""
            if final.lower() not in hosts:
                raise HTTPException(502, "IEEE 跳转到未授权域名，已中止")
        return response


@contextmanager
def _session():
    with tempfile.TemporaryDirectory(prefix="papertex-ieee-session-") as folder:
        yield _Client(os.path.join(folder, "cookies.txt"), _proxy_env())


def _failure(status, what):
    if status in (401, 402, 403):
        return HTTPException(403, f"{what}需要 IEEE 访问权限，请连接校园网或学校 VPN 后重试")
    if status == 404:
        return HTTPException(404, f"{what}不存在，请核对链接")
    if status == 202:
        return HTTPException(502, f"{what}被 IEEE 人机验证拦截，请稍后在浏览器打开该链接")
    if status == 429:
        return HTTPException(503, "IEEE 请求过于频繁，请稍后重试")
    if status in (408, 504):
        return HTTPException(504, f"{what}超时")
    if status >= 500:
        return HTTPException(502, f"IEEE 服务暂时不可用（HTTP {status}）")
    return HTTPException(502, f"IEEE 返回 HTTP {status}")


def _resolve_doi(doi, client, progress=None):
    if progress:
        progress("解析 IEEE 论文编号")
    response = client.get("https://doi.org/" + quote(doi, safe="/"), timeout=30,
                          limit=_MAX_JSON, hosts=_DOI_HOSTS)
    if response["status"] not in (301, 302, 303, 307, 308) or not response["redirect"]:
        raise HTTPException(502, "DOI 解析失败，未能定位 IEEE 论文页")
    target = response["redirect"]
    _check_url(target, _SITE_HOSTS)
    match = _DOC_PATH.fullmatch(urlsplit(target).path)
    if not match:
        raise HTTPException(502, "DOI 解析结果不是 IEEE 论文页")
    return match.group(1)


def _extract_metadata(page):
    marker = re.search(r"xplGlobal\.document\.metadata\s*=\s*", page)
    if not marker:
        raise HTTPException(502, "IEEE 页面结构变化，未找到论文元数据")
    start = page.find("{", marker.end())
    if start < 0:
        raise HTTPException(502, "IEEE 页面元数据损坏")
    depth, in_string, escaped = 0, False, False
    limit = min(len(page), start + _MAX_PAGE)
    for index in range(start, limit):
        char = page[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(page[start:index + 1])
                except ValueError as exc:
                    raise HTTPException(502, "IEEE 页面元数据不是合法 JSON") from exc
                if not isinstance(data, dict):
                    raise HTTPException(502, "IEEE 页面元数据格式异常")
                return data
    raise HTTPException(502, "IEEE 页面元数据不完整")


def _venue(name, raw):
    cleaned = _text(name)
    if cleaned in _JOURNALS:
        return _JOURNALS[cleaned]
    for pattern, label in _CONFERENCES:
        if re.search(pattern, cleaned, re.I) or re.search(pattern, _text(raw), re.I):
            return label
    return cleaned


def _year(value):
    text = str(value or "").strip()
    return int(text) if len(text) == 4 and text.isdigit() else ""


def _normalize_page(meta, arnumber):
    title = _text(meta.get("title") or meta.get("formulaStrippedArticleTitle")
                  or meta.get("displayDocTitle"))
    authors = []
    for author in meta.get("authors") or []:
        if isinstance(author, dict):
            name = _text(author.get("name"))
            if name:
                authors.append(name)
    if not authors:
        authors = [_text(x) for x in str(meta.get("authorNames") or "").split(";") if _text(x)]
    venue_raw = _text(meta.get("publicationTitle") or meta.get("displayPublicationTitle"))
    return {
        "doi": _clean_doi(meta.get("doi")),
        "arnumber": str(meta.get("articleNumber") or arnumber),
        "title": title,
        "authors": authors,
        "year": _year(meta.get("publicationYear")),
        "venue": _venue(venue_raw, venue_raw),
        "venue_raw": venue_raw,
        "abstract": _text(meta.get("abstract")),
        "early_access": bool(meta.get("isEarlyAccess")),
        "published": _text(meta.get("publicationDate") or meta.get("displayPublicationDate")),
    }


def _crossref(doi, client, progress=None):
    if progress:
        progress("读取 Crossref 元数据")
    response = client.get(_CROSSREF + quote(doi, safe="/"), timeout=30,
                          limit=_MAX_JSON, hosts=_API_HOSTS)
    if response["status"] != 200:
        if response["status"] == 404:
            raise HTTPException(404, f"Crossref 上找不到 DOI: {doi}")
        raise _failure(response["status"], "Crossref 元数据")
    try:
        message = json.loads(response["body"].decode("utf-8"))["message"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, "Crossref 返回的元数据不是合法 JSON") from exc
    if not isinstance(message, dict):
        raise HTTPException(502, "Crossref 返回的元数据格式异常")
    titles = message.get("title") or []
    title = _text(titles[0]) if isinstance(titles, list) and titles else ""
    authors = []
    for author in message.get("author") or []:
        if not isinstance(author, dict):
            continue
        name = " ".join(x for x in (_text(author.get("given")), _text(author.get("family"))) if x) \
            or _text(author.get("name"))
        if name:
            authors.append(name)
    containers = message.get("container-title") or []
    venue_raw = _text(containers[0]) if isinstance(containers, list) and containers else ""
    year = ""
    for field in ("published-print", "published-online", "issued", "created"):
        parts = (message.get(field) or {}).get("date-parts") or []
        if parts and parts[0]:
            year = _year(parts[0][0])
            if year:
                break
    return {
        "doi": _clean_doi(message.get("DOI")) or doi,
        "arnumber": "",
        "title": title,
        "authors": authors,
        "year": year,
        "venue": _venue(venue_raw, venue_raw),
        "venue_raw": venue_raw,
        "abstract": _text(message.get("abstract")),
        "early_access": False,
        "published": "",
    }


def _page_metadata(arnumber, client, progress=None):
    last = None
    for _attempt in (1, 2):
        if progress:
            progress("读取 IEEE 论文页")
        try:
            # The stampPDF redirect issues the AWS WAF token cookies the page needs.
            client.get(_STAMP.format(arnumber), timeout=30, limit=_MAX_JSON)
            response = client.get(_DOC.format(arnumber), timeout=45, limit=_MAX_PAGE, follow=True)
        except HTTPException as exc:
            last = exc
            continue
        if response["status"] == 200:
            return _normalize_page(
                _extract_metadata(response["body"].decode("utf-8", "replace")), arnumber)
        last = _failure(response["status"], "IEEE 论文页")
    raise last or HTTPException(502, "IEEE 论文页读取失败")


def lookup(ref, progress=None) -> dict:
    parsed = parse_ref(ref)
    doi, arnumber = parsed["doi"], parsed["arnumber"]
    info, errors = {}, []
    with _session() as client:
        if not arnumber and doi:
            try:
                arnumber = _resolve_doi(doi, client, progress)
            except HTTPException as exc:
                errors.append(str(exc.detail))
        if arnumber:
            try:
                info = _page_metadata(arnumber, client, progress)
            except HTTPException as exc:
                if not doi:
                    raise
                errors.append(str(exc.detail))
        if not info and doi:
            info = _crossref(doi, client, progress)
    if not info.get("title"):
        detail = "；".join(errors) or "没有返回标题"
        raise HTTPException(502, f"IEEE 元数据读取失败：{detail}")
    info["doi"] = info.get("doi") or doi
    info["arnumber"] = info.get("arnumber") or arnumber
    return info


def pdf_bytes(ref, progress=None) -> bytes:
    parsed = parse_ref(ref)
    with _session() as client:
        arnumber = parsed["arnumber"]
        doi = parsed["doi"]
        if not arnumber:
            arnumber = _resolve_doi(doi, client, progress)
        if progress:
            progress("下载 IEEE PDF")
        response = client.get(_STAMP.format(arnumber), timeout=180,
                              limit=MAX_UPLOAD_BYTES, follow=True)
    if response["status"] != 200:
        raise _failure(response["status"], "下载 IEEE PDF ")
    if not response["body"].startswith(b"%PDF-"):
        raise HTTPException(
            502, "IEEE 没有返回 PDF（本校可能未订阅这篇论文，或当前网络不在校园网/VPN 内）；"
                 "请在浏览器下载后手动上传")
    return response["body"]
