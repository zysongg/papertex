"""Write handlers must consume their request body before performing side effects."""

import json
import re
import secrets
from urllib.parse import parse_qs


_LOOPBACK_BIND_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))
_AUTHORITY = re.compile(
    rb"(localhost|127\.0\.0\.1|\[::1\])(?::([1-9][0-9]{0,4}))?",
    re.IGNORECASE | re.ASCII,
)
_ORIGIN = re.compile(rb"(https?)://(.+)", re.IGNORECASE | re.ASCII)
_SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))


def validate_bind_host(host):
    """Return an explicitly allowed bind host; never resolve arbitrary names."""
    if not isinstance(host, str) or host not in _LOOPBACK_BIND_HOSTS:
        raise ValueError("bind host must be localhost, 127.0.0.1, or ::1")
    return host


class RequestBodyTooLarge(Exception):
    """Raised by guarded receive/send; the guard owns the resulting HTTP 413."""


def _authority(value, scheme):
    """Parse a strict HTTP authority into its canonical host and effective port."""
    match = _AUTHORITY.fullmatch(value)
    if match is None:
        raise ValueError("invalid loopback authority")
    host, raw_port = match.groups()
    port = int(raw_port) if raw_port else (443 if scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    return host.lower(), port


async def _reply(send, status, detail=None):
    body = json.dumps({"detail": detail}).encode("utf-8") if detail else b""
    headers = []
    if status != 204:
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class LocalRequestGuard:

    def __init__(self, app, port, max_body_bytes=601 * 1024 * 1024, pdf_token=None):
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or max_body_bytes < 0
        ):
            raise ValueError("max_body_bytes must be a nonnegative integer")
        if pdf_token is not None and (
            not isinstance(pdf_token, str)
            or not pdf_token
            or len(pdf_token) > 512
            or not pdf_token.isascii()
        ):
            raise ValueError("pdf_token must be a nonempty ASCII string of at most 512 characters")
        self.app = app
        self.port = port
        self.max_body_bytes = max_body_bytes
        self.pdf_token = pdf_token

    def _pdf_token_matches(self, scope):
        """Authorize read-only PDF/image loads even when the browser marks them cross-site."""
        if not self.pdf_token:
            return False
        if scope.get("method", "GET").upper() not in ("GET", "HEAD"):
            return False
        if scope.get("path") not in ("/api/pdf", "/api/project/file"):
            return False
        raw = scope.get("query_string", b"")
        if not isinstance(raw, (bytes, bytearray)) or len(raw) > 4096:
            return False
        try:
            query = parse_qs(raw.decode("ascii"), keep_blank_values=True)
        except (UnicodeDecodeError, ValueError):
            return False
        values = query.get("token", [])
        if len(values) != 1 or not values[0].isascii():
            return False
        return secrets.compare_digest(values[0], self.pdf_token)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {}
        for name, value in scope.get("headers", ()):
            headers.setdefault(name.lower(), []).append(value)

        pdf_token_ok = self._pdf_token_matches(scope)

        sites = headers.get(b"sec-fetch-site", [])
        if sites and (
            len(sites) != 1
            or sites[0].strip().lower() not in (b"same-origin", b"same-site", b"none")
        ) and not pdf_token_ok:
            await _reply(send, 403, "Cross-site requests are not allowed")
            return

        scheme = scope.get("scheme", "http")
        hosts = headers.get(b"host", [])
        try:
            if scheme not in ("http", "https") or len(hosts) != 1:
                raise ValueError("invalid Host")
            authority = _authority(hosts[0], scheme)
            if authority[1] != self.port:
                raise ValueError("unexpected port")
        except ValueError:
            await _reply(send, 400, "Invalid Host header")
            return

        origins = headers.get(b"origin", [])
        if origins and not pdf_token_ok:
            try:
                if len(origins) != 1:
                    raise ValueError("duplicate Origin")
                origin = _ORIGIN.fullmatch(origins[0])
                if origin is None:
                    raise ValueError("invalid Origin")
                origin_scheme = origin[1].decode("ascii").lower()
                if origin_scheme != scheme or _authority(origin[2], origin_scheme) != authority:
                    raise ValueError("different origin")
            except ValueError:
                await _reply(send, 403, "Origin does not match this server")
                return

        method = scope.get("method", "GET").upper()
        if method == "OPTIONS":
            await _reply(send, 204)
            return
        if method in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        if headers.get(b"x-papertex-request", []) != [b"1"]:
            await _reply(send, 403, "X-Papertex-Request: 1 is required")
            return

        lengths = headers.get(b"content-length", [])
        if lengths:
            if len(lengths) != 1 or re.fullmatch(rb"[0-9]+", lengths[0]) is None:
                await _reply(send, 400, "Invalid Content-Length header")
                return
            # Compare decimal strings to avoid huge-integer parsing or digit limits.
            length = lengths[0].lstrip(b"0") or b"0"
            limit = str(self.max_body_bytes).encode("ascii")
            if len(length) > len(limit) or (len(length) == len(limit) and length > limit):
                await _reply(send, 413, "Request body too large")
                return

        received_bytes = 0
        body_complete = False
        disconnected = False
        too_large = False

        async def guarded_receive():
            nonlocal received_bytes, body_complete, disconnected, too_large
            if too_large:
                raise RequestBodyTooLarge("Request body too large")
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    too_large = True
                    # Fail the body parser before it can call a write endpoint.
                    raise RequestBodyTooLarge("Request body too large")
                body_complete = not message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def guarded_send(message):
            if too_large:
                # Unwind even if downstream translated the receive error to 400.
                raise RequestBodyTooLarge("Request body too large")
            if message["type"] == "http.response.start":
                # Hold early responses until the remaining body's size is known.
                while not body_complete and not disconnected:
                    await guarded_receive()
            await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except Exception:
            # Frameworks may wrap our exception; preserve unrelated errors.
            if not too_large:
                raise
        if too_large:
            # Only this layer emits the failure, once, after downstream unwinds.
            await _reply(send, 413, "Request body too large")
