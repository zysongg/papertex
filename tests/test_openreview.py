import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

from fastapi import HTTPException

import openreview as orx


FORUM = 'https://openreview.net/forum?id=VKGTGGcwl6'
NOTE = {
    'id': 'VKGTGGcwl6',
    'pdate': 1767225600000,
    'content': {
        'title': {'value': '  Deep   Learning for Time Series '},
        'authors': {'value': ['Ada Lovelace', ' Bob  Stone ']},
        'venue': {'value': 'ICLR 2026 Poster'},
        'abstract': {'value': 'An abstract.'},
        '_bibtex': {'value': '@inproceedings{k,\n  eprint = {2401.01234}\n}'},
    },
}


class OpenReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='papertex-openreview-')
        self.addCleanup(temp.cleanup)
        self.session = Path(temp.name) / 'config/openreview.json'
        for name, value in (('SESSION_FILE', self.session),):
            p = patch.object(orx, name, value)
            p.start()
            self.addCleanup(p.stop)

    def assert_status(self, status, function, *args, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status_code, status, caught.exception.detail)
        return caught.exception

    def login_session(self):
        orx._write_session({'token': 'secret-token', 'user': 'me@example.com', 'saved_at': 'now'})

    def test_parse_url_accepts_bare_id_and_forum_links(self):
        self.assertEqual(orx.parse_url('VKGTGGcwl6'), 'VKGTGGcwl6')
        self.assertEqual(orx.parse_url(FORUM), 'VKGTGGcwl6')
        self.assertEqual(orx.parse_url('https://openreview.net/pdf?id=VKGTGGcwl6'), 'VKGTGGcwl6')
        self.assertEqual(
            orx.parse_url('https://openreview.net/attachment?id=VKGTGGcwl6&name=pdf'),
            'VKGTGGcwl6')
        self.assertEqual(orx.parse_url(FORUM + '&noteId=x'), 'VKGTGGcwl6')
        self.assertEqual(orx.parse_url(' ' + FORUM + ' '), 'VKGTGGcwl6')

    def test_parse_url_rejects_unsafe_or_ambiguous_values(self):
        for value in (
            '', 'abc', 42, None,
            'http://openreview.net/forum?id=VKGTGGcwl6',
            'https://evil.example/forum?id=VKGTGGcwl6',
            'https://user@openreview.net/forum?id=VKGTGGcwl6',
            'https://openreview.net:8443/forum?id=VKGTGGcwl6',
            'https://openreview.net/forum',
            'https://openreview.net/forum?id=bad id',
            'https://openreview.net/forum?id=VKGTGGcwl6&id=ABCD1234',
            '/forum?id=VKGTGGcwl6',
        ):
            with self.subTest(value=value):
                self.assert_status(400, orx.parse_url, value)

    def test_canonical_url_keeps_empty_and_normalizes_links(self):
        self.assertEqual(orx.canonical_url(''), '')
        self.assertEqual(orx.canonical_url('  '), '')
        self.assertEqual(orx.canonical_url('VKGTGGcwl6'), FORUM)
        self.assertEqual(orx.canonical_url('https://openreview.net/pdf?id=VKGTGGcwl6'), FORUM)

    def test_session_is_absent_until_login_and_written_with_0600(self):
        self.assertEqual(orx.session_status(),
                         {'logged_in': False, 'user': '', 'saved_at': ''})
        self.assert_status(401, orx._token)
        orx._write_session({'token': 't', 'user': 'u', 'saved_at': 's'})
        self.assertEqual(self.session.stat().st_mode & 0o777, 0o600)
        self.assertEqual(orx.session_status(), {'logged_in': True, 'user': 'u', 'saved_at': 's'})
        self.assertEqual(orx.clear_session()['logged_in'], False)
        self.assertFalse(self.session.exists())

    def test_corrupt_session_and_symlink_are_rejected(self):
        self.session.parent.mkdir(parents=True)
        self.session.write_text('{broken', encoding='utf-8')
        self.assert_status(500, orx.session_status)
        self.session.unlink()
        target = self.session.parent / 'elsewhere.json'
        target.write_text('{}', encoding='utf-8')
        self.session.symlink_to(target)
        self.assert_status(400, orx.clear_session)
        self.assert_status(400, orx._write_session, {'token': 't'})

    def test_lookup_parses_v2_content(self):
        self.login_session()
        body = json.dumps({'notes': [NOTE]}).encode()
        with patch.object(orx, '_send', return_value=(200, body)):
            info = orx.lookup('VKGTGGcwl6')
        self.assertEqual(info['openreview_url'], FORUM)
        self.assertEqual(info['title'], 'Deep Learning for Time Series')
        self.assertEqual(info['authors'], ['Ada Lovelace', 'Bob Stone'])
        self.assertEqual(info['venue'], 'ICLR 2026 Poster')
        self.assertEqual(info['abstract'], 'An abstract.')
        self.assertEqual(info['year'], 2026)
        self.assertEqual(info['arxiv_id'], '2401.01234')
        self.assertIn('@inproceedings', info['bibtex'])

    def test_lookup_parses_v1_content(self):
        self.login_session()
        note = {'id': 'VKGTGGcwl6', 'cdate': 1735689600000,
                'content': {'title': 'Plain', 'authors': ['A'], 'venue': 'NeurIPS 2025'}}
        with patch.object(orx, '_send', return_value=(200, json.dumps({'notes': [note]}).encode())):
            info = orx.lookup('VKGTGGcwl6')
        self.assertEqual(info['title'], 'Plain')
        self.assertEqual(info['authors'], ['A'])
        self.assertEqual(info['year'], 2025)

    def test_lookup_rejects_missing_note_and_blank_title(self):
        self.login_session()
        with patch.object(orx, '_send', return_value=(200, b'{"notes": []}')):
            self.assert_status(404, orx.lookup, 'VKGTGGcwl6')
        other = dict(NOTE, id='OTHER12345')
        with patch.object(orx, '_send', return_value=(200, json.dumps({'notes': [other]}).encode())):
            self.assert_status(404, orx.lookup, 'VKGTGGcwl6')
        blank = {'id': 'VKGTGGcwl6', 'content': {'title': {'value': '  '}}}
        with patch.object(orx, '_send', return_value=(200, json.dumps({'notes': [blank]}).encode())):
            self.assert_status(404, orx.lookup, 'VKGTGGcwl6')

    def test_lookup_requires_login(self):
        self.assert_status(401, orx.lookup, 'VKGTGGcwl6')

    def test_http_errors_map_to_actionable_messages(self):
        self.login_session()
        cases = [
            (401, b'{}', 401, '登录已失效'),
            (403, b'{"name": "ChallengeRequiredError"}', 403, '人机验证'),
            (403, b'{}', 403, '拒绝访问'),
            (404, b'{}', 404, '找不到'),
            (429, b'{}', 503, '过于频繁'),
            (502, b'{}', 502, '暂时不可用'),
        ]
        for status, body, expected, text in cases:
            with self.subTest(status=status):
                with patch.object(orx, '_send', return_value=(status, body)):
                    error = self.assert_status(expected, orx.lookup, 'VKGTGGcwl6')
                self.assertIn(text, error.detail)

    def test_metadata_must_be_valid_json(self):
        self.login_session()
        with patch.object(orx, '_send', return_value=(200, b'<html>')):
            self.assert_status(502, orx.lookup, 'VKGTGGcwl6')

    def test_pdf_bytes_requires_login_and_pdf_signature(self):
        self.assert_status(401, orx.pdf_bytes, 'VKGTGGcwl6')
        self.login_session()
        with patch.object(orx, '_send', return_value=(200, b'%PDF-1.7 data')):
            self.assertEqual(orx.pdf_bytes('VKGTGGcwl6'), b'%PDF-1.7 data')
        with patch.object(orx, '_send', return_value=(200, b'<html>')):
            self.assert_status(502, orx.pdf_bytes, 'VKGTGGcwl6')

    def test_login_stores_token_and_maps_rejections(self):
        with patch.object(orx, '_send',
                          return_value=(200, b'{"token": "tok", "user": {"id": "me"}}')) as send:
            result = orx.login('me@example.com', 'pw')
        self.assertEqual(result['user'], 'me')
        self.assertTrue(result['logged_in'])
        self.assertEqual(self.session.stat().st_mode & 0o777, 0o600)
        request = send.call_args.args[0]
        self.assertEqual(json.loads(request.data),
                         {'id': 'me@example.com', 'password': 'pw'})
        with patch.object(orx, '_send', return_value=(400, b'{}')):
            error = self.assert_status(401, orx.login, 'me@example.com', 'bad')
        self.assertIn('不正确', error.detail)
        with patch.object(orx, '_send', return_value=(403, b'{}')):
            error = self.assert_status(403, orx.login, 'me@example.com', 'pw')
        self.assertIn('人机验证', error.detail)
        self.assert_status(400, orx.login, '', '')
        self.assert_status(400, orx.login, 'x' * 201, 'pw')

    def test_save_token_validates_and_verifies_the_token(self):
        for token in ('', 'has space', 'x' * 4097, '中文'):
            with self.subTest(token=token[:12]):
                self.assert_status(400, orx.save_token, token)
        with patch.object(orx, '_send', return_value=(200, b'{"notes": []}')):
            result = orx.save_token('token.with-chars_1')
        self.assertTrue(result['logged_in'])
        self.assertEqual(orx.read_session()['token'], 'token.with-chars_1')
        with patch.object(orx, '_send', return_value=(401, b'{}')):
            self.assert_status(401, orx.save_token, 'another.token')

    def test_empty_or_invalid_session_file_is_ignored_or_reported(self):
        self.assertEqual(orx.read_session(), {})
        self.session.parent.mkdir(parents=True, exist_ok=True)
        self.session.write_text(json.dumps({'user': 'no-token'}), encoding='utf-8')
        self.assertEqual(orx.read_session(), {})
        self.assertEqual(orx.session_status()['logged_in'], False)


if __name__ == '__main__':
    unittest.main()
