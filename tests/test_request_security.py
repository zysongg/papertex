"""Offline stdlib tests: exercise the middleware directly through ASGI."""

import asyncio
import importlib.util
import json
from pathlib import Path
import unittest


_MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "request_security.py"
_SPEC = importlib.util.spec_from_file_location("papertex_request_security", _MODULE_PATH)
_SECURITY = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SECURITY)
LocalRequestGuard = _SECURITY.LocalRequestGuard
RequestBodyTooLarge = _SECURITY.RequestBodyTooLarge
validate_bind_host = _SECURITY.validate_bind_host


HOST = (b"host", b"localhost:8080")
TOKEN = (b"x-papertex-request", b"1")
ORIGIN = (b"origin", b"http://localhost:8080")
CROSS_SITE = (b"sec-fetch-site", b"cross-site")
PDF_SECRET = "pdf-token_abc123"
PDF_QUERY = b"source=remote&topic=TS&year=2025&slug=paper&token=" + PDF_SECRET.encode("ascii")
PROJECT_QUERY = (
    b"project=project_ts%2FSRNet&path=paper%2Fmain.pdf&token=" + PDF_SECRET.encode("ascii")
)
REVIEW_QUERY = b"key=ICML%2F7&name=manuscript.pdf&token=" + PDF_SECRET.encode("ascii")


def chunks(*parts):
    return [
        {"type": "http.request", "body": part, "more_body": i < len(parts) - 1}
        for i, part in enumerate(parts)
    ]


async def respond(send, status=200, body=b"application response", headers=()):
    await send({"type": "http.response.start", "status": status, "headers": list(headers)})
    await send({"type": "http.response.body", "body": body})


class BindHostTests(unittest.TestCase):
    def test_exact_loopback_bind_hosts(self):
        for host in ("localhost", "127.0.0.1", "::1"):
            with self.subTest(host=host):
                self.assertEqual(validate_bind_host(host), host)

    def test_non_loopback_and_malformed_bind_hosts(self):
        for host in (
            "0.0.0.0", "::", "[::1]", "127.0.0.2", "127.1", "2130706433",
            "::ffff:127.0.0.1", "localhost.", "localhost:8080", "localhost.evil",
            " LOCALHOST", "localhost\n", "192.168.1.1", "", None, [], 1,
        ):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    validate_bind_host(host)

    def test_constructor_validation_and_default_limit(self):
        self.assertEqual(LocalRequestGuard(None, 8080).max_body_bytes, 601 * 1024 * 1024)
        self.assertIsNone(LocalRequestGuard(None, 8080).pdf_token)
        self.assertEqual(LocalRequestGuard(None, 8080, pdf_token="abc").pdf_token, "abc")
        for port in (0, -1, 65536, True, "8080", None, 80.5):
            with self.subTest(port=port), self.assertRaises(ValueError):
                LocalRequestGuard(None, port)
        for limit in (-1, True, 1.5, "10", None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                LocalRequestGuard(None, 8080, limit)
        for pdf_token in ("", "a" * 513, "中文", b"abc", 1, True):
            with self.subTest(pdf_token=pdf_token), self.assertRaises(ValueError):
                LocalRequestGuard(None, 8080, pdf_token=pdf_token)


class RequestGuardTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(
        self, *, method="POST", headers=None, messages=None, app=None,
        port=8080, limit=8, scheme="http", path="/api/write",
        query_string=b"", pdf_token=None,
    ):
        incoming = iter(chunks(b"") if messages is None else messages)
        result = {"sent": [], "received": 0, "entered": 0, "delivered": [], "writes": []}
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": scheme, "path": path,
            "query_string": query_string,
            "headers": [HOST, TOKEN] if headers is None else headers,
            "server": ("untrusted-server-value", 9999),
        }

        async def receive():
            result["received"] += 1
            return next(incoming, {"type": "http.disconnect"})

        async def send(message):
            result["sent"].append(message)

        async def downstream(inner_scope, inner_receive, inner_send):
            result["entered"] += 1
            if app is not None:
                await app(inner_scope, inner_receive, inner_send)
                return
            while True:
                message = await inner_receive()
                if message["type"] == "http.disconnect":
                    return
                result["delivered"].append(message)
                if not message.get("more_body", False):
                    break
            if inner_scope["method"] not in ("GET", "HEAD", "OPTIONS"):
                result["writes"].append("endpoint called after complete body parsing")
            await respond(inner_send)

        await LocalRequestGuard(downstream, port, limit, pdf_token=pdf_token)(scope, receive, send)
        return result

    def assert_status(self, result, expected):
        starts = [m for m in result["sent"] if m["type"] == "http.response.start"]
        self.assertEqual([m["status"] for m in starts], [expected])
        self.assertEqual(result["sent"][-1]["type"], "http.response.body")
        self.assertFalse(result["sent"][-1].get("more_body", False))
        if expected >= 400:
            self.assertEqual(len(result["sent"]), 2)
            self.assertNotIn(b"application response", result["sent"][-1]["body"])
            self.assertNotIn(b"private", result["sent"][-1]["body"])
            self.assertIn("detail", json.loads(result["sent"][-1]["body"]))
            self.assertEqual(result["writes"], [])

    async def test_same_origin_all_loopback_hosts(self):
        for host in (b"localhost", b"127.0.0.1", b"[::1]"):
            with self.subTest(host=host):
                result = await self.invoke(headers=[
                    (b"host", host + b":8080"), TOKEN,
                    (b"origin", b"http://" + host + b":8080"),
                    (b"sec-fetch-site", b"same-origin"),
                ])
                self.assert_status(result, 200)
                self.assertEqual(len(result["writes"]), 1)

    async def test_origin_equivalence_includes_default_port_and_host_case(self):
        for host, origin, port, scheme in (
            (b"LOCALHOST:8080", b"HTTP://localhost:8080", 8080, "http"),
            (b"localhost", b"http://localhost:80", 80, "http"),
            (b"localhost:80", b"http://localhost", 80, "http"),
            (b"[::1]", b"https://[::1]:443", 443, "https"),
            (b"127.0.0.1:443", b"https://127.0.0.1", 443, "https"),
        ):
            with self.subTest(host=host, origin=origin):
                result = await self.invoke(
                    headers=[(b"host", host), TOKEN, (b"origin", origin)],
                    port=port, scheme=scheme,
                )
                self.assert_status(result, 200)

    async def test_rejects_foreign_and_malformed_origins(self):
        for origin in (
            b"http://evil.test:8080", b"http://127.0.0.1:8080", b"http://[::1]:8080",
            b"https://localhost:8080", b"http://localhost:8081", b"http://localhost",
            b"null", b"file://localhost:8080", b"ftp://localhost:8080",
            b"http://user@localhost:8080", b"http://user:pass@localhost:8080",
            b"http://localhost:8080@evil.test", b"http://@localhost:8080",
            b"http://localhost:8080/", b"http://localhost:8080/path",
            b"http://localhost:8080?", b"http://localhost:8080#",
            b"http://localhost:8080\\evil", b"http://localhost:8080\n",
            b" http://localhost:8080", b"http://localhost:8080 ",
            b"http://local\thost:8080", b"http://localhost:8080\x00",
            b"http://localhost:8080 http://evil.test", b"http://localhost:8080, null",
            b"http://localhost.:8080", b"http://%6cocalhost:8080",
            b"http://localhost:08080", b"http://localhost:65536", b"\xff", b"",
        ):
            with self.subTest(origin=origin):
                result = await self.invoke(headers=[HOST, TOKEN, (b"origin", origin)])
                self.assert_status(result, 403)
                self.assertEqual(result["entered"], 0)

    async def test_origin_checked_for_reads_as_well(self):
        result = await self.invoke(method="GET", headers=[HOST, (b"origin", b"null")])
        self.assert_status(result, 403)

    async def test_rejects_duplicate_origins(self):
        result = await self.invoke(headers=[HOST, TOKEN, ORIGIN, ORIGIN])
        self.assert_status(result, 403)

    async def test_rejects_missing_duplicate_and_malformed_host(self):
        invalid_hosts = (
            b"evil.test:8080", b"localhost.evil:8080", b"0.0.0.0:8080",
            b"127.0.0.2:8080", b"127.1:8080", b"2130706433:8080",
            b"[::ffff:127.0.0.1]:8080", b"[0:0:0:0:0:0:0:1]:8080", b"::1:8080",
            b"localhost:8081", b"localhost", b"[::1]", b"localhost:0",
            b"localhost:65536", b"localhost:+8080", b"localhost:-8080",
            b"localhost:08080", b"localhost:8080:80", b"localhost:",
            b"user@localhost:8080", b"localhost:8080@evil.test", b"localhost.:8080",
            b"localhost:8080/path", b"http://localhost:8080", b"localhost:8080?",
            b"localhost:8080#", b"localhost:8080\\evil", b"localhost:8080\n",
            b"localhost:8080\r\nX-Other: 1", b" localhost:8080", b"localhost:8080 ",
            b"local\thost:8080", b"localhost:8080\x00", b"%6cocalhost:8080",
            b"localhost:8080,localhost:8080", b"localhost:8080 evil.test", b"\xff", b"",
        )
        header_sets = [[TOKEN], [HOST, HOST, TOKEN], [HOST, (b"Host", HOST[1]), TOKEN]]
        header_sets.extend([TOKEN, (b"host", value)] for value in invalid_hosts)
        for headers in header_sets:
            with self.subTest(headers=headers):
                result = await self.invoke(headers=headers)
                self.assert_status(result, 400)
                self.assertEqual(result["entered"], 0)
                self.assertEqual(result["received"], 0)

    async def test_forwarded_headers_cannot_override_host_or_origin(self):
        forwarded = [(b"x-forwarded-host", b"localhost:8080"), (b"x-forwarded-proto", b"https")]
        result = await self.invoke(headers=[(b"host", b"evil.test:8080"), TOKEN] + forwarded)
        self.assert_status(result, 400)
        result = await self.invoke(headers=[HOST, TOKEN, (b"origin", b"https://localhost:8080")] + forwarded)
        self.assert_status(result, 403)

    async def test_cross_site_rejected_even_with_token_and_same_origin(self):
        for method in ("GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"):
            for site_headers in (
                [(b"sec-fetch-site", b"cross-site")],
                [(b"sec-fetch-site", b"Cross-Site")],
                [(b"sec-fetch-site", b"same-origin, cross-site")],
                [(b"sec-fetch-site", b"same-origin"), (b"sec-fetch-site", b"cross-site")],
            ):
                with self.subTest(method=method, site_headers=site_headers):
                    result = await self.invoke(method=method, headers=[HOST, TOKEN, ORIGIN] + site_headers)
                    self.assert_status(result, 403)
                    self.assertEqual(result["entered"], 0)

    async def test_cross_site_pdf_read_with_valid_token_is_allowed(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                result = await self.invoke(
                    method=method, headers=[HOST, CROSS_SITE], path="/api/pdf",
                    query_string=PDF_QUERY, pdf_token=PDF_SECRET,
                )
                self.assert_status(result, 200)
                self.assertEqual(result["entered"], 1)

    async def test_pdf_token_also_covers_viewer_origin_and_fetch_metadata(self):
        for extra in (
            [(b"origin", b"null")],
            [(b"origin", b"chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai")],
        ):
            with self.subTest(extra=extra):
                result = await self.invoke(
                    method="GET", headers=[HOST, CROSS_SITE] + extra, path="/api/pdf",
                    query_string=PDF_QUERY, pdf_token=PDF_SECRET,
                )
                self.assert_status(result, 200)
                self.assertEqual(result["entered"], 1)

    async def test_cross_site_project_file_read_with_valid_token_is_allowed(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                result = await self.invoke(
                    method=method, headers=[HOST, CROSS_SITE], path="/api/project/file",
                    query_string=PROJECT_QUERY, pdf_token=PDF_SECRET,
                )
                self.assert_status(result, 200)
                self.assertEqual(result["entered"], 1)

    async def test_cross_site_project_file_read_with_bad_token_is_rejected(self):
        for query in (
            b"project=project_ts%2FSRNet&path=paper%2Fmain.pdf",
            b"project=project_ts%2FSRNet&path=paper%2Fmain.pdf&token=wrong",
        ):
            with self.subTest(query=query):
                result = await self.invoke(
                    method="GET", headers=[HOST, CROSS_SITE], path="/api/project/file",
                    query_string=query, pdf_token=PDF_SECRET,
                )
                self.assert_status(result, 403)
                self.assertEqual(result["entered"], 0)

    async def test_cross_site_review_manuscript_read_follows_pdf_allowlist(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                result = await self.invoke(
                    method=method, headers=[HOST, CROSS_SITE], path="/api/review/pdf",
                    query_string=REVIEW_QUERY, pdf_token=PDF_SECRET,
                )
                self.assert_status(result, 200)
                self.assertEqual(result["entered"], 1)
        for query in (b"key=ICML%2F7", b"key=ICML%2F7&token=wrong"):
            with self.subTest(query=query):
                result = await self.invoke(
                    method="GET", headers=[HOST, CROSS_SITE], path="/api/review/pdf",
                    query_string=query, pdf_token=PDF_SECRET,
                )
                self.assert_status(result, 403)
                self.assertEqual(result["entered"], 0)
        result = await self.invoke(
            method="GET", headers=[HOST, CROSS_SITE], path="/api/review/trash",
            query_string=REVIEW_QUERY, pdf_token=PDF_SECRET,
        )
        self.assert_status(result, 403)
        self.assertEqual(result["entered"], 0)

    async def test_cross_site_pdf_read_with_bad_token_is_rejected(self):
        for query in (
            b"source=remote", b"token=", b"token=wrong",
            b"token=" + PDF_SECRET.encode("ascii") + b"&token=wrong",
            b"token=%E4%B8%AD", b"token=\xff", b"x=" + b"a" * 5000,
        ):
            with self.subTest(query=query[:40]):
                result = await self.invoke(
                    method="GET", headers=[HOST, CROSS_SITE], path="/api/pdf",
                    query_string=query, pdf_token=PDF_SECRET,
                )
                self.assert_status(result, 403)
                self.assertEqual(result["entered"], 0)

    async def test_pdf_token_is_not_configured_by_default(self):
        result = await self.invoke(
            method="GET", headers=[HOST, CROSS_SITE], path="/api/pdf", query_string=PDF_QUERY,
        )
        self.assert_status(result, 403)
        self.assertEqual(result["entered"], 0)

    async def test_pdf_token_does_not_authorize_other_paths_methods_or_hosts(self):
        cases = (
            ("/api/library", "GET", [HOST, CROSS_SITE], 403),
            ("/api/pdf", "POST", [HOST, CROSS_SITE, TOKEN], 403),
            ("/api/project/file", "POST", [HOST, CROSS_SITE, TOKEN], 403),
            ("/api/pdf", "OPTIONS", [HOST, CROSS_SITE], 403),
            ("/api/pdf", "GET", [(b"host", b"evil.test:8080"), CROSS_SITE], 400),
        )
        for path, method, headers, status in cases:
            with self.subTest(path=path, method=method, headers=headers):
                result = await self.invoke(
                    method=method, headers=headers, path=path,
                    query_string=PDF_QUERY, pdf_token=PDF_SECRET,
                )
                self.assert_status(result, status)
                self.assertEqual(result["entered"], 0)

    async def test_pdf_token_does_not_replace_write_token_or_origin_checks(self):
        result = await self.invoke(
            method="POST", headers=[HOST], path="/api/pdf",
            query_string=PDF_QUERY, pdf_token=PDF_SECRET,
        )
        self.assert_status(result, 403)
        self.assertEqual(result["entered"], 0)
        result = await self.invoke(
            method="POST", headers=[HOST, TOKEN, ORIGIN, CROSS_SITE], path="/api/pdf",
            query_string=PDF_QUERY, pdf_token=PDF_SECRET, messages=chunks(b"ok"),
        )
        self.assert_status(result, 403)
        self.assertEqual(result["entered"], 0)

    async def test_same_origin_pdf_read_does_not_require_the_token(self):
        result = await self.invoke(method="GET", headers=[HOST], path="/api/pdf")
        self.assert_status(result, 200)
        self.assertEqual(result["entered"], 1)

    async def test_cli_without_token_is_rejected_for_every_write_method(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "PROPFIND", "CUSTOM"):
            with self.subTest(method=method):
                result = await self.invoke(method=method, headers=[HOST])
                self.assert_status(result, 403)
                self.assertEqual(result["entered"], 0)

    async def test_same_origin_does_not_replace_required_token(self):
        result = await self.invoke(headers=[HOST, ORIGIN, (b"sec-fetch-site", b"same-origin")])
        self.assert_status(result, 403)

    async def test_cli_token_accepts_writes_without_origin(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                result = await self.invoke(method=method, headers=[HOST, TOKEN], messages=chunks(b"ok"))
                self.assert_status(result, 200)
                self.assertEqual(len(result["writes"]), 1)

    async def test_token_is_exact_and_not_duplicated(self):
        for token_headers in (
            [(b"x-papertex-request", b"0")], [(b"x-papertex-request", b"true")],
            [(b"x-papertex-request", b"1, 1")], [(b"x-papertex-request", b"1 ")],
            [TOKEN, TOKEN], [(b"x-papertex-request", b"")],
        ):
            with self.subTest(token_headers=token_headers):
                result = await self.invoke(headers=[HOST] + token_headers)
                self.assert_status(result, 403)

    async def test_header_names_are_case_insensitive(self):
        result = await self.invoke(headers=[
            (b"Host", HOST[1]), (b"Origin", ORIGIN[1]),
            (b"X-Papertex-Request", b"1"), (b"Content-Length", b"0"),
        ])
        self.assert_status(result, 200)

    async def test_options_answered_locally_without_cors(self):
        async def broad_cors_app(scope, receive, send):
            await respond(send, headers=[(b"access-control-allow-origin", b"*")])

        for extra in ([], [ORIGIN]):
            with self.subTest(extra=extra):
                result = await self.invoke(method="OPTIONS", headers=[HOST] + extra, app=broad_cors_app)
                self.assert_status(result, 204)
                self.assertEqual(result["entered"], 0)
                self.assertEqual(result["received"], 0)
                self.assertEqual(result["sent"][0]["headers"], [])
                self.assertEqual(result["sent"][1]["body"], b"")
        result = await self.invoke(method="OPTIONS", headers=[HOST, (b"origin", b"http://evil.test")])
        self.assert_status(result, 403)

    async def test_content_length_precheck_never_enters_application(self):
        for length in (b"9", b"100", b"0009", b"9" * 10000):
            with self.subTest(length=length[:20]):
                result = await self.invoke(headers=[HOST, TOKEN, (b"content-length", length)])
                self.assert_status(result, 413)
                self.assertEqual(result["entered"], 0)
                self.assertEqual(result["received"], 0)

    async def test_malformed_or_duplicate_content_length(self):
        for length_headers in (
            [(b"content-length", b"-1")], [(b"content-length", b"+1")],
            [(b"content-length", b"1.0")], [(b"content-length", b"1e2")],
            [(b"content-length", b"8, 8")], [(b"content-length", b"8\n")],
            [(b"content-length", b" 8")], [(b"content-length", b"\xff")],
            [(b"content-length", b"")], [(b"content-length", b"8")] * 2,
        ):
            with self.subTest(length_headers=length_headers):
                result = await self.invoke(headers=[HOST, TOKEN] + length_headers)
                self.assert_status(result, 400)
                self.assertEqual(result["entered"], 0)

    async def test_exact_limit_and_leading_zero_declaration_are_allowed(self):
        for length_headers in ([], [(b"content-length", b"8")], [(b"content-length", b"0008")]):
            with self.subTest(length_headers=length_headers):
                result = await self.invoke(headers=[HOST, TOKEN] + length_headers, messages=chunks(b"123", b"45678"))
                self.assert_status(result, 200)
                self.assertEqual(len(result["writes"]), 1)

    async def test_chunked_over_limit_does_not_deliver_offending_chunk(self):
        messages = chunks(b"1234", b"5678", b"9", b"unread")
        result = await self.invoke(headers=[HOST, TOKEN, (b"transfer-encoding", b"chunked")], messages=messages)
        self.assert_status(result, 413)
        self.assertEqual(result["received"], 3)
        self.assertEqual(result["delivered"], messages[:2])
        self.assertIs(result["delivered"][0], messages[0])

    async def test_single_chunk_over_limit(self):
        result = await self.invoke(messages=chunks(b"123456789"))
        self.assert_status(result, 413)
        self.assertEqual(result["delivered"], [])

    async def test_understated_content_length_still_checks_actual_bytes(self):
        result = await self.invoke(headers=[HOST, TOKEN, (b"content-length", b"1")], messages=chunks(b"1234", b"56789"))
        self.assert_status(result, 413)
        self.assertEqual(len(result["delivered"]), 1)

    async def test_byte_count_not_unicode_character_count(self):
        result = await self.invoke(messages=chunks("中文字符".encode("utf-8")))
        self.assert_status(result, 413)

    async def test_zero_limit_allows_only_empty_body(self):
        self.assert_status(await self.invoke(limit=0), 200)
        self.assert_status(await self.invoke(limit=0, messages=chunks(b"x")), 413)

    async def test_caught_receive_error_cannot_send_400_or_execute_endpoint(self):
        events = []

        async def catching_parser(scope, receive, send):
            try:
                while (await receive()).get("more_body", False):
                    pass
            except Exception as exc:
                events.append(type(exc))
                await respond(send, 400, b"private parser error", [(b"x-private", b"secret")])
                events.append("write after failed response")
                return
            events.append("write endpoint")

        result = await self.invoke(app=catching_parser, messages=chunks(b"1234", b"56789"))
        self.assert_status(result, 413)
        self.assertEqual(events, [RequestBodyTooLarge])
        self.assertNotIn((b"x-private", b"secret"), result["sent"][0]["headers"])

    async def test_sticky_overflow_rejects_retries_and_repeated_responses(self):
        failures = []

        async def swallowing_app(scope, receive, send):
            for _ in range(3):
                try:
                    await receive()
                except RequestBodyTooLarge:
                    failures.append("receive")
            for status in (400, 500, 200):
                try:
                    await respond(send, status, b"private")
                except RequestBodyTooLarge:
                    failures.append("send")

        result = await self.invoke(app=swallowing_app, messages=chunks(b"123456789", b"unread"))
        self.assert_status(result, 413)
        self.assertEqual(result["received"], 1)
        self.assertEqual(failures, ["receive"] * 3 + ["send"] * 3)

    async def test_wrapped_receive_exception_still_yields_413(self):
        async def wrapping_app(scope, receive, send):
            try:
                await receive()
            except RequestBodyTooLarge as exc:
                raise RuntimeError("private wrapped parser failure") from exc

        result = await self.invoke(app=wrapping_app, messages=chunks(b"123456789"))
        self.assert_status(result, 413)

    async def test_early_response_is_not_leaked_before_body_checked(self):
        async def early_app(scope, receive, send):
            await respond(send, 400, b"private early response", [(b"x-private", b"secret")])

        result = await self.invoke(app=early_app, messages=chunks(b"1234", b"56789"))
        self.assert_status(result, 413)
        self.assertEqual(result["received"], 2)
        self.assertNotIn((b"x-private", b"secret"), result["sent"][0]["headers"])
        result = await self.invoke(app=early_app, messages=chunks(b"1234"))
        self.assertEqual(result["sent"][0]["status"], 400)
        self.assertEqual(result["sent"][1]["body"], b"private early response")

    async def test_streaming_does_not_prefetch_or_copy_body(self):
        trace = []
        messages = chunks(b"1234", b"5678")
        incoming = iter(messages)

        async def receive():
            trace.append("upstream receive")
            return next(incoming)

        async def send(message):
            trace.append(message["type"])

        async def app(scope, inner_receive, inner_send):
            trace.append("app entered")
            for expected in messages:
                self.assertIs(await inner_receive(), expected)
                trace.append("chunk delivered")
            await respond(inner_send)

        await LocalRequestGuard(app, 8080, 8)(
            {"type": "http", "method": "POST", "scheme": "http", "headers": [HOST, TOKEN]},
            receive, send,
        )
        self.assertEqual(trace, [
            "app entered", "upstream receive", "chunk delivered", "upstream receive",
            "chunk delivered", "http.response.start", "http.response.body",
        ])

    async def test_get_and_head_do_not_require_token_or_enforce_write_limit(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                result = await self.invoke(method=method, headers=[HOST], messages=chunks(b"large read body"), limit=0, path="/index.html")
                self.assert_status(result, 200)
                self.assertEqual(result["received"], 1)

    async def test_unrelated_application_exceptions_propagate(self):
        failure = RuntimeError("application bug")

        async def broken_app(scope, receive, send):
            raise failure

        with self.assertRaises(RuntimeError) as caught:
            await self.invoke(app=broken_app)
        self.assertIs(caught.exception, failure)

    async def test_disconnect_is_forwarded_unchanged(self):
        message = {"type": "http.disconnect"}
        seen = []

        async def disconnected_app(scope, receive, send):
            seen.append(await receive())

        result = await self.invoke(app=disconnected_app, messages=[message])
        self.assertIs(seen[0], message)
        self.assertEqual(result["sent"], [])

    async def test_non_http_scopes_pass_through_unchanged(self):
        for kind in ("websocket", "lifespan"):
            with self.subTest(kind=kind):
                scope = {"type": kind}
                received_message = {"type": kind + ".test"}
                sent_message = {"type": kind + ".reply"}
                seen = []

                async def receive():
                    return received_message

                async def send(message):
                    seen.append(message)

                async def app(inner_scope, inner_receive, inner_send):
                    self.assertIs(inner_scope, scope)
                    self.assertIs(inner_receive, receive)
                    self.assertIs(inner_send, send)
                    self.assertIs(await inner_receive(), received_message)
                    await inner_send(sent_message)

                await LocalRequestGuard(app, 8080)(scope, receive, send)
                self.assertEqual(seen, [sent_message])

    async def test_same_guard_keeps_concurrent_request_state_separate(self):
        async def app(scope, receive, send):
            await asyncio.sleep(0)
            while (await receive()).get("more_body", False):
                await asyncio.sleep(0)
            await respond(send)

        guard = LocalRequestGuard(app, 8080, 8)

        async def request(parts):
            incoming = iter(chunks(*parts))
            sent = []

            async def receive():
                await asyncio.sleep(0)
                return next(incoming, {"type": "http.disconnect"})

            async def send(message):
                sent.append(message)

            await guard(
                {"type": "http", "method": "POST", "headers": [HOST, TOKEN]}, receive, send,
            )
            return [message["status"] for message in sent if message["type"] == "http.response.start"]

        statuses = await asyncio.gather(request([b"1234", b"56789"]), request([b"1234", b"5678"]))
        self.assertEqual(statuses, [[413], [200]])


if __name__ == "__main__":
    unittest.main()
