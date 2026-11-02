import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

from fastapi import HTTPException

import ieee


META = {
    'title': 'USMD: UnSupervised <i>Misbehaviour</i> Detection',
    'authors': [{'name': 'Abdullah Alsaedi'}, {'name': 'Zahir Tari'}],
    'publicationTitle': 'IEEE Transactions on Dependable and Secure Computing',
    'publicationYear': 2023,
    'doi': '10.1109/TDSC.2022.3143493',
    'articleNumber': 9684703,
    'abstract': '  Cyber-Physical   Systems with {braces}. ',
    'isEarlyAccess': False,
    'publicationDate': '01 Jan.-Feb. 2023',
}
PAGE = ('<html><script>var xplGlobal={};xplGlobal.document.metadata='
        + json.dumps(META) + ';</script></html>').encode()
CROSSREF = {
    'message': {
        'DOI': '10.1109/tdsc.2022.3143493',
        'title': ['<i>Deep</i>   Detection'],
        'author': [{'given': 'Ada', 'family': 'Lovelace'}, {'name': 'Some Org'}],
        'container-title': ['IEEE Transactions on Information Forensics and Security'],
        'published-print': {'date-parts': [[2022, 5]]},
        'abstract': '<jats:p>Hello  world.</jats:p>',
    }
}


def respond(body, status=200, url='', follow_url=None):
    return {'status': status, 'url': url, 'type': '', 'redirect': follow_url or '', 'body': body}


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, timeout=45, limit=ieee._MAX_PAGE, follow=False, hosts=None):
        self.calls.append((url, follow))
        return self.handler(url, follow)


def session_with(client):
    @contextmanager
    def fake():
        yield client
    return fake


class IeeeParseTests(unittest.TestCase):
    def assert_status(self, status, function, *args):
        with self.assertRaises(HTTPException) as caught:
            function(*args)
        self.assertEqual(caught.exception.status_code, status, caught.exception.detail)

    def test_parse_ref_accepts_links_and_dois(self):
        cases = [
            ('https://ieeexplore.ieee.org/document/9684703', '9684703', ''),
            ('https://ieeexplore.ieee.org/abstract/document/9684703/?casa_token=x', '9684703', ''),
            ('https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=9684703', '9684703', ''),
            ('http://ieeexplore.ieee.org/document/9684703/', '9684703', ''),
            ('10.1109/TDSC.2022.3143493', '', '10.1109/TDSC.2022.3143493'),
            ('doi:10.1109/TDSC.2022.3143493', '', '10.1109/TDSC.2022.3143493'),
            ('https://doi.org/10.1109/TDSC.2022.3143493', '', '10.1109/TDSC.2022.3143493'),
        ]
        for value, number, doi in cases:
            parsed = ieee.parse_ref(value)
            self.assertEqual(parsed['arnumber'], number, value)
            self.assertEqual(parsed['doi'], doi, value)

    def test_parse_ref_rejects_unsafe_or_unsupported_values(self):
        for value in ('', '  ', None, 42, 'https://evil.test/document/1',
                      'ftp://ieeexplore.ieee.org/document/1', '10.48550/arXiv.2401.00001',
                      'https://ieeexplore.ieee.org/document/abc',
                      'https://ieeexplore.ieee.org/iel7/1/2/09684703.pdf',
                      'https://ieeexplore.ieee.org/system/1'):
            self.assert_status(400, ieee.parse_ref, value)

    def test_check_url_enforces_https_host_and_no_credentials(self):
        ieee._check_url('https://ieeexplore.ieee.org/document/1/', ieee._SITE_HOSTS)
        for value in ('http://ieeexplore.ieee.org/document/1/',
                      'https://evil.test/document/1/',
                      'https://user:pass@ieeexplore.ieee.org/document/1/',
                      'https://ieeexplore.ieee.org\\@evil.test/document/1/'):
            self.assert_status(400, ieee._check_url, value, ieee._SITE_HOSTS)


class IeeeMetadataTests(unittest.TestCase):
    def test_extract_metadata_handles_braces_inside_strings(self):
        data = ieee._extract_metadata(PAGE.decode())
        self.assertEqual(data['articleNumber'], 9684703)

    def test_extract_metadata_rejects_page_without_metadata(self):
        with self.assertRaises(HTTPException) as caught:
            ieee._extract_metadata('<html>challenge</html>')
        self.assertEqual(caught.exception.status_code, 502)

    def test_normalize_page_cleans_text_and_maps_venue(self):
        info = ieee._normalize_page(META, '9684703')
        self.assertEqual(info['title'], 'USMD: UnSupervised Misbehaviour Detection')
        self.assertEqual(info['authors'], ['Abdullah Alsaedi', 'Zahir Tari'])
        self.assertEqual(info['venue'], 'TDSC')
        self.assertEqual(info['venue_raw'], 'IEEE Transactions on Dependable and Secure Computing')
        self.assertEqual(info['year'], 2023)
        self.assertEqual(info['doi'], '10.1109/TDSC.2022.3143493')
        self.assertEqual(info['arnumber'], '9684703')
        self.assertEqual(info['abstract'], 'Cyber-Physical Systems with {braces}.')
        self.assertFalse(info['early_access'])

    def test_normalize_page_maps_conference_and_keeps_unknown_venue(self):
        conference = dict(META, publicationTitle='IEEE INFOCOM 2024 - IEEE Conference on Computer Communications')
        self.assertEqual(ieee._normalize_page(conference, '1')['venue'], 'INFOCOM')
        unknown = dict(META, publicationTitle='Journal of Fictional Studies')
        self.assertEqual(ieee._normalize_page(unknown, '1')['venue'], 'Journal of Fictional Studies')

    def test_crossref_normalizes_tags_and_dates(self):
        client = FakeClient(lambda url, follow: respond(json.dumps(CROSSREF).encode()))
        info = ieee._crossref('10.1109/tdsc.2022.3143493', client)
        self.assertEqual(info['title'], 'Deep Detection')
        self.assertEqual(info['authors'], ['Ada Lovelace', 'Some Org'])
        self.assertEqual(info['venue'], 'TIFS')
        self.assertEqual(info['year'], 2022)
        self.assertEqual(info['abstract'], 'Hello world.')
        self.assertEqual(info['doi'], '10.1109/tdsc.2022.3143493')

    def test_lookup_reads_ieee_page_for_article_links(self):
        def handler(url, follow):
            if url.startswith(ieee._STAMP.format('9684703')):
                return respond(b'', status=302, url=url, follow_url=url + '&tag=1')
            if url == ieee._DOC.format('9684703'):
                return respond(PAGE, url=url)
            raise AssertionError(url)
        client = FakeClient(handler)
        with patch.object(ieee, '_session', session_with(client)):
            info = ieee.lookup('https://ieeexplore.ieee.org/document/9684703/')
        self.assertEqual(info['title'], 'USMD: UnSupervised Misbehaviour Detection')
        self.assertEqual(info['doi'], '10.1109/TDSC.2022.3143493')
        self.assertEqual([follow for _, follow in client.calls], [False, True])

    def test_lookup_resolves_doi_and_falls_back_to_crossref(self):
        def handler(url, follow):
            if url.startswith('https://doi.org/'):
                return respond(b'', status=302, url=url,
                               follow_url='https://ieeexplore.ieee.org/document/9684703/')
            if url.startswith(ieee._STAMP.format('9684703')):
                return respond(b'', status=302, url=url)
            if url == ieee._DOC.format('9684703'):
                return respond(b'', status=202, url=url)
            if url.startswith('https://api.crossref.org/works/'):
                return respond(json.dumps(CROSSREF).encode(), url=url)
            raise AssertionError(url)
        client = FakeClient(handler)
        with patch.object(ieee, '_session', session_with(client)):
            info = ieee.lookup('10.1109/tdsc.2022.3143493')
        self.assertEqual(info['title'], 'Deep Detection')
        self.assertEqual(info['arnumber'], '9684703')

    def test_lookup_surfaces_page_error_without_doi(self):
        client = FakeClient(lambda url, follow: respond(b'', status=403, url=url))
        with patch.object(ieee, '_session', session_with(client)):
            with self.assertRaises(HTTPException) as caught:
                ieee.lookup('https://ieeexplore.ieee.org/document/9684703/')
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn('校园网', caught.exception.detail)


class IeeeDownloadTests(unittest.TestCase):
    def assert_status(self, status, function, *args):
        with self.assertRaises(HTTPException) as caught:
            function(*args)
        self.assertEqual(caught.exception.status_code, status, caught.exception.detail)
        return caught.exception

    def test_pdf_bytes_returns_pdf_after_redirect(self):
        def handler(url, follow):
            if url.startswith(ieee._STAMP.format('9684703')):
                return respond(b'%PDF-1.7 ieee', url=url)
            raise AssertionError(url)
        client = FakeClient(handler)
        with patch.object(ieee, '_session', session_with(client)):
            data = ieee.pdf_bytes('https://ieeexplore.ieee.org/document/9684703/')
        self.assertEqual(data, b'%PDF-1.7 ieee')
        self.assertTrue(client.calls[0][1])

    def test_pdf_bytes_reports_access_denied_html(self):
        client = FakeClient(lambda url, follow: respond(b'<html>buy this</html>', url=url))
        with patch.object(ieee, '_session', session_with(client)):
            error = self.assert_status(502, ieee.pdf_bytes,
                                       'https://ieeexplore.ieee.org/document/9684703/')
        self.assertIn('手动上传', error.detail)

    def test_pdf_bytes_rejects_empty_arnumber(self):
        client = FakeClient(lambda url, follow: respond(b'', status=500, url=url))
        with patch.object(ieee, '_session', session_with(client)):
            error = self.assert_status(502, ieee.pdf_bytes, '10.1109/TDSC.2022.3143493')
        self.assertIn('DOI 解析失败', error.detail)

    def test_client_rejects_redirect_to_other_host(self):
        with patch.object(ieee, '_curl', return_value=respond(
                b'%PDF-1.4', url='https://evil.test/paper.pdf')):
            client = ieee._Client('', None)
            error = self.assert_status(502, client.get, ieee._DOC.format('1'))
        self.assertIn('未授权域名', error.detail)

    def run_curl(self, command, **kwargs):
        body = Path(command[command.index('--output') + 1])
        body.write_bytes(b'%PDF-1.4 x')
        return subprocess.CompletedProcess(
            command, 0,
            stdout=b'200\thttps://ieeexplore.ieee.org/document/1/\tapplication/pdf\t9\t',
            stderr=b'')

    def test_curl_builds_restricted_command(self):
        with patch.object(ieee.subprocess, 'run', side_effect=self.run_curl) as run:
            result = ieee._curl(ieee._STAMP.format('1'), '/tmp/jar', 30, 1024, True, None)
        command = run.call_args[0][0]
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['body'], b'%PDF-1.4 x')
        self.assertIn('--location', command)
        self.assertEqual(command[command.index('--proto') + 1], '=https')
        self.assertEqual(command[command.index('--proto-redir') + 1], '=https')
        self.assertEqual(command[command.index('--max-redirs') + 1], '5')
        self.assertEqual(command[command.index('--cookie') + 1], '/tmp/jar')

    def test_curl_without_follow_has_no_location_flag(self):
        with patch.object(ieee.subprocess, 'run', side_effect=self.run_curl) as run:
            ieee._curl(ieee._STAMP.format('1'), '/tmp/jar', 30, 1024, False, None)
        self.assertNotIn('--location', run.call_args[0][0])

    def test_curl_maps_exit_codes_and_size_limit(self):
        def failing(code, stderr=b'connect failed'):
            def run(command, **kwargs):
                return subprocess.CompletedProcess(command, code, stdout=b'', stderr=stderr)
            return run
        for code, status in ((28, 504), (63, 413), (7, 502), (60, 502), (22, 502)):
            with patch.object(ieee.subprocess, 'run', side_effect=failing(code)):
                self.assert_status(status, ieee._curl, ieee._STAMP.format('1'), None, 30, 1024, False, None)

        def oversize(command, **kwargs):
            Path(command[command.index('--output') + 1]).write_bytes(b'x' * 40)
            return subprocess.CompletedProcess(
                command, 0, stdout=b'200\thttps://ieeexplore.ieee.org/document/1/\t\t40\t', stderr=b'')
        with patch.object(ieee.subprocess, 'run', side_effect=oversize):
            self.assert_status(413, ieee._curl, ieee._STAMP.format('1'), None, 30, 10, False, None)


if __name__ == '__main__':
    unittest.main()
