import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from fastapi import HTTPException

import knowledge as kb


class ValidationTests(unittest.TestCase):
    def test_note_normalizes_tags_and_body(self):
        fields = kb.normalize({
            'kind': 'note', 'title': '  A note  ', 'url': '',
            'source': ' blog ', 'tags': 'a, b，a', 'body': '\nline 1\nline 2\n',
        })
        self.assertEqual(fields['title'], 'A note')
        self.assertEqual(fields['tags'], ['a', 'b'])
        self.assertEqual(fields['body'], 'line 1\nline 2')

    def test_links_require_http_urls(self):
        for url in ('', 'javascript:alert(1)', 'ftp://example.com/a', 'HTTPS://Example.com'):
            with self.assertRaises(kb.KnowledgeError):
                kb.normalize({'kind': 'link', 'title': 'x', 'url': url})
        fields = kb.normalize({'kind': 'link', 'title': 'x', 'url': 'https://example.com/a'})
        self.assertEqual(fields['url'], 'https://example.com/a')

    def test_limits_and_control_characters(self):
        with self.assertRaises(kb.KnowledgeError):
            kb.normalize({'kind': 'note', 'title': ''})
        with self.assertRaises(kb.KnowledgeError):
            kb.normalize({'kind': 'note', 'title': 'x' * 201})
        with self.assertRaises(kb.KnowledgeError):
            kb.normalize({'kind': 'note', 'title': 'bad\x00title'})
        with self.assertRaises(kb.KnowledgeError):
            kb.normalize({'kind': 'note', 'title': 'x', 'tags': [f't{i}' for i in range(21)]})
        with self.assertRaises(kb.KnowledgeError):
            kb.normalize({'kind': 'note', 'title': 'x', 'body': 'b' * 100001})
        with self.assertRaises(kb.KnowledgeError):
            kb.normalize({'kind': 'other', 'title': 'x'})

    def test_parse_meta_rejects_corrupt_records(self):
        item_id = 'a' * 32
        meta = kb.build_meta(kb.normalize({
            'kind': 'note', 'title': 'ok', 'tags': ['x'], 'body': 'body',
        }))
        item = kb.parse_meta(meta, item_id)
        self.assertEqual(item['key'], '_knowledge/' + item_id)
        self.assertTrue(item['has_body'])
        self.assertIsNone(kb.parse_meta(meta, 'not-an-id'))
        self.assertIsNone(kb.parse_meta({'kind': 'weird', 'title': 'x'}, item_id))
        self.assertIsNone(kb.parse_meta({**meta, 'url': 'javascript:alert(1)'}, item_id))


class KnowledgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory(prefix='papertex-knowledge-app-')
        cls.addClassCleanup(temp.cleanup)
        root = Path(temp.name)
        project = Path(__file__).resolve().parents[1]
        shutil.copytree(project / 'app', root / 'app', ignore=shutil.ignore_patterns('__pycache__'))
        (root / 'config').mkdir()
        shutil.copyfile(project / 'config/config.example.json', root / 'config/config.json')
        spec = importlib.util.spec_from_file_location('papertex_knowledge_server', root / 'app/server.py')
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='papertex-knowledge-')
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        s = self.server
        (self.root / 'config').mkdir()
        self.local = self.root / 'local'
        self.remote = self.root / 'remote'
        self.workshop = self.root / 'projects'
        self.workshop.mkdir()
        values = dict(BASE=self.root, LIB_LOCAL=self.local, LIB_REMOTE=str(self.remote),
                      WORKSHOP=str(self.workshop), PROJECTS_FILE=self.root / 'config/projects.json',
                      STARS_FILE=self.root / 'config/stars.json')
        for key, value in values.items():
            p = patch.object(s, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.real_run = subprocess.run
        p = patch.object(s, 'ssh_run', self.shell)
        p.start()
        self.addCleanup(p.stop)

    def shell(self, command, timeout=60, stdin=None, binary=False):
        return self.real_run(['sh', '-c', command],
                             input=stdin.encode() if binary and stdin is not None else stdin,
                             text=not binary, capture_output=True, timeout=timeout, cwd=self.root)

    def create(self, **fields):
        payload = {'kind': 'note', 'title': '测试笔记', 'url': '', 'source': '',
                   'tags': '时序, 生成', 'body': '第一行\n第二行'}
        payload.update(fields)
        return self.server.create_knowledge(payload)

    def test_note_round_trip_writes_remote_and_local_mirror(self):
        result = self.create()
        item = result['item']
        self.assertEqual(result['warnings'], [])
        folder = self.remote / '_knowledge' / item['id']
        meta = json.loads((folder / 'meta.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['kind'], 'note')
        self.assertEqual(meta['tags'], ['时序', '生成'])
        self.assertTrue(meta['has_body'])
        self.assertEqual((folder / 'note.md').read_text(encoding='utf-8'), '第一行\n第二行')
        mirror = self.local / '_knowledge' / item['id']
        self.assertTrue((mirror / 'meta.json').is_file())
        self.assertTrue((mirror / 'note.md').is_file())

        listing = self.server.knowledge_list()
        self.assertEqual(listing['remote_error'], '')
        self.assertEqual(len(listing['items']), 1)
        self.assertNotIn('body', listing['items'][0])

        detail = self.server.knowledge_detail(item['id'])
        self.assertEqual(detail['source'], 'remote')
        self.assertEqual(detail['item']['body'], '第一行\n第二行')

    def test_link_requires_http_url(self):
        with self.assertRaises(HTTPException) as ctx:
            self.create(kind='link', title='链接', url='')
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            self.create(kind='link', title='链接', url='javascript:alert(1)')
        self.assertEqual(ctx.exception.status_code, 400)
        result = self.create(kind='link', title='链接', url='https://example.com/a')
        self.assertEqual(result['item']['url'], 'https://example.com/a')

    def test_update_preserves_created_at_and_body(self):
        item = self.create()['item']
        meta_path = self.remote / '_knowledge' / item['id'] / 'meta.json'
        created = json.loads(meta_path.read_text(encoding='utf-8'))['created_at']
        updated = self.server.update_knowledge(item['id'], {
            'kind': 'note', 'title': '新标题', 'url': '', 'source': '来源',
            'tags': 'a,b', 'body': '新的正文',
        })
        self.assertEqual(updated['item']['title'], '新标题')
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        self.assertEqual(meta['created_at'], created)
        self.assertEqual(meta['title'], '新标题')
        self.assertEqual((meta_path.parent / 'note.md').read_text(encoding='utf-8'), '新的正文')
        self.assertEqual(self.server.knowledge_detail(item['id'])['item']['body'], '新的正文')

    def test_local_note_edit_is_reported_and_pushed_back(self):
        item = self.create()['item']
        mirror = self.local / '_knowledge' / item['id'] / 'note.md'
        mirror.write_text('本机补写的第一行\n本机补写的第二行', encoding='utf-8')

        detail = self.server.knowledge_detail(item['id'])
        self.assertEqual(detail['local_edit']['body'], '本机补写的第一行\n本机补写的第二行')
        self.assertFalse(detail['local_edit']['conflict'])

        result = self.server.sync_knowledge_local(item['id'])
        self.assertTrue(result['changed'])
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['item']['body'], '本机补写的第一行\n本机补写的第二行')
        remote_note = self.remote / '_knowledge' / item['id'] / 'note.md'
        self.assertEqual(remote_note.read_text(encoding='utf-8'), '本机补写的第一行\n本机补写的第二行')
        self.assertEqual(mirror.read_text(encoding='utf-8'), '本机补写的第一行\n本机补写的第二行')
        detail = self.server.knowledge_detail(item['id'])
        self.assertEqual(detail['item']['body'], '本机补写的第一行\n本机补写的第二行')
        self.assertIsNone(detail['local_edit'])

    def test_untouched_mirror_has_no_local_edit(self):
        item = self.create()['item']
        self.assertIsNone(self.server.knowledge_detail(item['id'])['local_edit'])
        result = self.server.sync_knowledge_local(item['id'])
        self.assertFalse(result['changed'])

    def test_local_note_conflict_keeps_local_until_overwrite(self):
        item = self.create()['item']
        folder = self.remote / '_knowledge' / item['id']
        mirror = self.local / '_knowledge' / item['id'] / 'note.md'
        mirror.write_text('本机版本', encoding='utf-8')
        (folder / 'note.md').write_text('远端版本', encoding='utf-8')
        meta = json.loads((folder / 'meta.json').read_text(encoding='utf-8'))
        meta['updated_at'] = '2030-01-01T00:00:00Z'
        (folder / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')

        detail = self.server.knowledge_detail(item['id'])
        self.assertTrue(detail['local_edit']['conflict'])
        self.assertEqual(detail['item']['body'], '远端版本')
        self.assertEqual(detail['local_edit']['body'], '本机版本')
        with self.assertRaises(HTTPException) as ctx:
            self.server.sync_knowledge_local(item['id'])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual((folder / 'note.md').read_text(encoding='utf-8'), '远端版本')

        result = self.server.sync_knowledge_local(item['id'], overwrite=True)
        self.assertTrue(result['changed'])
        self.assertEqual((folder / 'note.md').read_text(encoding='utf-8'), '本机版本')

    def test_sync_local_pull_discards_local_edit(self):
        item = self.create()['item']
        mirror = self.local / '_knowledge' / item['id'] / 'note.md'
        mirror.write_text('临时本机版本', encoding='utf-8')
        result = self.server.sync_knowledge_local(item['id'], mode='pull')
        self.assertEqual(result['item']['body'], '第一行\n第二行')
        self.assertEqual(mirror.read_text(encoding='utf-8'), '第一行\n第二行')
        self.assertIsNone(self.server.knowledge_detail(item['id'])['local_edit'])

    def test_legacy_mirror_without_digest_treats_local_edit_as_draft(self):
        item = self.create()['item']
        local_meta = self.local / '_knowledge' / item['id'] / 'meta.json'
        meta = json.loads(local_meta.read_text(encoding='utf-8'))
        meta.pop('body_sha256', None)
        local_meta.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        (self.local / '_knowledge' / item['id'] / 'note.md').write_text('旧镜像上的本机修改', encoding='utf-8')
        detail = self.server.knowledge_detail(item['id'])
        self.assertEqual(detail['local_edit']['body'], '旧镜像上的本机修改')
        self.assertFalse(detail['local_edit']['conflict'])

    def test_update_and_delete_missing_items_return_404(self):
        missing = 'f' * 32
        with self.assertRaises(HTTPException) as ctx:
            self.server.update_knowledge(missing, {'kind': 'note', 'title': 'x'})
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException) as ctx:
            self.server.delete_knowledge(missing, 'x')
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException) as ctx:
            self.server.knowledge_detail('../escape')
        self.assertEqual(ctx.exception.status_code, 400)

    def test_delete_moves_to_recovery_and_restore(self):
        item = self.create(title='可恢复资料')['item']
        self.server.set_star({'key': item['key'], 'starred': True})
        with self.assertRaises(HTTPException) as ctx:
            self.server.delete_knowledge(item['id'], '错误标题')
        self.assertEqual(ctx.exception.status_code, 400)
        result = self.server.delete_knowledge(item['id'], '可恢复资料')
        self.assertEqual(result['warnings'], [])
        self.assertFalse((self.remote / '_knowledge' / item['id']).exists())
        self.assertFalse((self.local / '_knowledge' / item['id']).exists())
        self.assertNotIn(item['key'], self.server.get_stars()['items'])

        entries = self.server.recovery_list()['items']
        backup = next(entry for entry in entries if entry['kind'] == 'knowledge')
        self.assertTrue(backup['restorable'])
        self.assertEqual(backup['label'], '可恢复资料')
        self.server.restore_recovery({'source': 'remote', 'id': backup['id']})
        self.assertTrue((self.remote / '_knowledge' / item['id'] / 'meta.json').is_file())
        self.assertEqual(self.server.knowledge_detail(item['id'])['item']['title'], '可恢复资料')

    def test_paper_scans_and_options_ignore_knowledge(self):
        item = self.create()['item']
        self.assertEqual(self.server.scan_local(), {})
        self.assertEqual(self.server.remote_pdf_info(), {})

        # The paper scan runs GNU find on 远端; feed its raw output instead so
        # the `_knowledge` skip is exercised without relying on local find flags.
        raw = (f'{item["key"]}/meta.json\tf\n'
               f'{item["key"]}/note.md\tf\n')
        fake = subprocess.CompletedProcess(['ssh'], 0, raw, '')
        with patch.object(self.server, 'ssh_run', return_value=fake):
            self.assertEqual(self.server.scan_remote(), {})
            self.assertEqual(self.server.options(force=True)['topics'], [])

    def test_star_keys_accept_knowledge_only_with_valid_id(self):
        item = self.create()['item']
        self.server.set_star({'key': item['key'], 'starred': True})
        self.assertIn(item['key'], self.server.get_stars()['items'])
        for key in ('_knowledge/nothex', '_knowledge/' + 'a' * 31, '../x'):
            with self.assertRaises(HTTPException):
                self.server.set_star({'key': key, 'starred': True})

    def test_local_fallback_when_remote_is_offline(self):
        item = self.create()['item']
        with patch.object(self.server, 'scan_knowledge_remote',
                          side_effect=HTTPException(502, '远端 离线')):
            listing = self.server.knowledge_list()
        self.assertEqual(listing['remote_error'], '远端 离线')
        self.assertEqual([entry['id'] for entry in listing['items']], [item['id']])
        with patch.object(self.server, 'read_knowledge_remote',
                          side_effect=HTTPException(502, '远端 离线')):
            detail = self.server.knowledge_detail(item['id'])
        self.assertEqual(detail['source'], 'local')
        self.assertEqual(detail['item']['body'], '第一行\n第二行')

    def test_corrupt_local_metadata_is_skipped(self):
        item = self.create()['item']
        meta_path = self.local / '_knowledge' / item['id'] / 'meta.json'
        meta_path.write_text('{not json', encoding='utf-8')
        with patch.object(self.server, 'scan_knowledge_remote',
                          side_effect=HTTPException(502, '远端 离线')):
            self.assertEqual(self.server.knowledge_list()['items'], [])

    def project(self, refs=None, name='demo'):
        folder = self.workshop / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'reference-tex.json').write_text(json.dumps({
            'library': str(self.remote), 'read_order': ['entry', 'pdf'],
            'references': refs or [],
        }, ensure_ascii=False), encoding='utf-8')
        self.server.save_projects([name])

    def test_add_note_and_link_refs(self):
        note = self.create()['item']
        link = self.create(kind='link', title='链接资料', url='https://example.com/a',
                           body='')['item']
        self.project()
        added = self.server.add_ref({'project': 'demo', 'path': note['key']})
        self.assertEqual(added['ref'], {
            'kind': 'knowledge', 'path': note['key'],
            'entry': 'note.md', 'title': '测试笔记',
        })
        added = self.server.add_ref({'project': 'demo', 'path': link['key']})
        self.assertEqual(added['ref']['url'], 'https://example.com/a')
        self.assertNotIn('entry', added['ref'])
        with self.assertRaises(HTTPException) as ctx:
            self.server.add_ref({'project': 'demo', 'path': note['key']})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('资料', ctx.exception.detail['message'])

        refs = self.server.get_refs('demo')['references']
        self.assertEqual(len(refs), 2)
        for ref in refs:
            self.assertFalse(ref['missing'])
            self.assertFalse(ref['stale'])
        stored = json.loads((self.workshop / 'demo/reference-tex.json').read_text(encoding='utf-8'))
        self.assertEqual([ref['path'] for ref in stored['references']],
                         [note['key'], link['key']])

    def test_add_batch_mixes_added_skipped_and_failed(self):
        note = self.create()['item']
        self.project(refs=[{'path': 'topic/2024/alpha', 'pdf': 'main.pdf'}])
        papers = {
            'topic/2024/alpha': {'has_pdf': True, 'pdf': 'main.pdf', 'title': '旧标题'},
            'topic/2024/beta': {'has_pdf': True, 'pdf': 'beta.pdf', 'entry': 'ref.bib',
                                'title': '新论文'},
            'topic/2024/gamma': {'has_source': True, 'entry': 'ref.bib'},
        }
        with patch.object(self.server, 'scan_remote', return_value=papers):
            result = self.server.add_refs_batch({
                'project': 'demo',
                'paths': ['topic/2024/alpha', 'topic/2024/beta', 'topic/2024/gamma',
                          'topic/2024/beta', note['key'], 'topic/2024/missing'],
            })
            listed = self.server.get_refs('demo')['references']
        self.assertEqual(result['added'],
                         ['topic/2024/beta', 'topic/2024/gamma', note['key']])
        self.assertEqual(result['skipped'], ['topic/2024/alpha'])
        self.assertEqual(result['updated'], [])
        self.assertEqual([item['path'] for item in result['failed']], ['topic/2024/missing'])
        self.assertIn('库中没有', result['failed'][0]['message'])
        self.assertFalse(result['created_dir'])
        self.assertEqual(result['count'], 4)
        stored = json.loads((self.workshop / 'demo/reference-tex.json').read_text(encoding='utf-8'))
        self.assertEqual([ref['path'] for ref in stored['references']],
                         ['topic/2024/alpha', 'topic/2024/beta', 'topic/2024/gamma', note['key']])
        self.assertEqual(stored['references'][0], {'path': 'topic/2024/alpha', 'pdf': 'main.pdf'})
        self.assertEqual(stored['references'][1]['title'], '新论文')
        for ref in listed:
            self.assertFalse(ref['missing'])

    def test_add_batch_skips_existing_without_rewriting(self):
        self.project(refs=[{'path': 'topic/2024/a', 'pdf': 'a.pdf', 'note': 'keep'}])
        path = self.workshop / 'demo/reference-tex.json'
        before = path.read_bytes()
        papers = {'topic/2024/a': {'has_pdf': True, 'pdf': 'a.pdf', 'title': '新标题'}}
        with patch.object(self.server, 'scan_remote', return_value=papers):
            result = self.server.add_refs_batch({'project': 'demo', 'paths': ['topic/2024/a']})
        self.assertEqual(result['skipped'], ['topic/2024/a'])
        self.assertEqual(path.read_bytes(), before)
        with patch.object(self.server, 'scan_remote', return_value=papers):
            updated = self.server.add_refs_batch({'project': 'demo', 'update': True,
                                                  'paths': ['topic/2024/a']})
        self.assertEqual(updated['updated'], ['topic/2024/a'])
        self.assertEqual(updated['skipped'], [])
        stored = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(stored['references'][0]['title'], '新标题')

    def test_add_batch_validates_input_and_bounds_size(self):
        self.project()
        for bad in (None, [], 'topic/2024/a', [None]):
            with self.assertRaises(HTTPException) as ctx:
                self.server.add_refs_batch({'project': 'demo', 'paths': bad})
            self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            self.server.add_refs_batch({'project': 'demo', 'path': 'x'})
        self.assertEqual(ctx.exception.status_code, 400)
        too_many = [f'topic/2024/p{i}' for i in range(self.server.REFS_BATCH_LIMIT + 1)]
        with self.assertRaises(HTTPException) as ctx:
            self.server.add_refs_batch({'project': 'demo', 'paths': too_many})
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            self.server.add_refs_batch({'project': 'demo', 'paths': ['../escape/x/y']})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_add_batch_creates_missing_remote_project_after_confirm(self):
        with self.assertRaises(HTTPException) as ctx:
            self.server.add_refs_batch({'project': 'ghost', 'paths': ['topic/2024/a']})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertTrue(ctx.exception.detail['missing_dir'])
        self.assertFalse((self.workshop / 'ghost').exists())
        papers = {'topic/2024/a': {'has_pdf': True, 'pdf': 'a.pdf'}}
        with patch.object(self.server, 'scan_remote', return_value=papers):
            created = self.server.add_refs_batch({'project': 'ghost',
                                                  'paths': ['topic/2024/a'],
                                                  'create_missing': True})
        self.assertTrue(created['created_dir'])
        self.assertEqual(created['added'], ['topic/2024/a'])
        self.assertTrue((self.workshop / 'ghost/reference-tex.json').is_file())

    def test_add_batch_local_source_requires_existing_directory(self):
        papers = {'topic/2024/a': {'has_pdf': True, 'pdf': 'a.pdf'}}
        with patch.object(self.server, 'scan_local', return_value=papers):
            with self.assertRaises(HTTPException) as ctx:
                self.server.add_refs_batch({'project': str(self.root / 'nope'),
                                            'source': 'local',
                                            'paths': ['topic/2024/a']})
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertTrue(ctx.exception.detail['missing_dir'])
        project = self.root / 'local-project'
        project.mkdir()
        with patch.object(self.server, 'scan_local', return_value=papers):
            result = self.server.add_refs_batch({'project': str(project), 'source': 'local',
                                                 'paths': ['topic/2024/a']})
        self.assertEqual(result['added'], ['topic/2024/a'])
        self.assertEqual(result['source'], 'local')
        self.assertTrue((project / 'reference-tex.json').is_file())

    def test_get_refs_marks_missing_and_stale_knowledge(self):
        item = self.create()['item']
        self.project()
        self.server.add_ref({'project': 'demo', 'path': item['key']})
        meta_path = self.remote / '_knowledge' / item['id'] / 'meta.json'
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        meta_path.write_text(json.dumps(dict(meta, title='外部改名')), encoding='utf-8')
        ref = self.server.get_refs('demo')['references'][0]
        self.assertTrue(ref['stale'])
        self.assertFalse(ref['missing'])
        result = self.server.sync_refs({'project': 'demo', 'path': item['key']})
        self.assertEqual(result['updated'][0]['changes']['title'], '外部改名')
        self.assertFalse(self.server.get_refs('demo')['references'][0]['stale'])

        (self.remote / '_knowledge' / item['id'] / 'meta.json').unlink()
        ref = self.server.get_refs('demo')['references'][0]
        self.assertTrue(ref['missing'])

    def test_update_knowledge_syncs_project_refs(self):
        item = self.create()['item']
        self.project()
        self.server.add_ref({'project': 'demo', 'path': item['key']})
        result = self.server.update_knowledge(item['id'], {
            'kind': 'note', 'title': '改名后的笔记', 'url': 'https://example.com/x',
            'source': '', 'tags': '', 'body': '正文',
        })
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['projects_updated'], ['demo'])
        stored = json.loads((self.workshop / 'demo/reference-tex.json').read_text(encoding='utf-8'))
        ref = stored['references'][0]
        self.assertEqual(ref['title'], '改名后的笔记')
        self.assertEqual(ref['url'], 'https://example.com/x')

    def test_put_refs_accepts_knowledge_paths(self):
        note = self.create()['item']
        self.project()
        doc, _ = self.server.read_refs_doc('demo')
        result = self.server.put_refs({'project': 'demo', 'revision': doc.revision,
                                       'references': [{'path': note['key'],
                                                       'kind': 'knowledge',
                                                       'entry': 'note.md',
                                                       'title': '测试笔记'}]})
        self.assertTrue(result['ok'])
        stored = json.loads((self.workshop / 'demo/reference-tex.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['references'][0]['kind'], 'knowledge')
        revision = self.server.read_refs_doc('demo')[0].revision
        for bad in ('_knowledge/nothex', '_knowledge/' + 'a' * 31):
            self.assert_status(400, lambda bad=bad: self.server.put_refs({
                'project': 'demo', 'revision': revision,
                'references': [{'path': bad}]}))
        missing = '_knowledge/' + 'b' * 32
        self.assert_status(400, lambda: self.server.put_refs({
            'project': 'demo', 'revision': revision,
            'references': [{'path': missing, 'kind': 'knowledge'}]}))

    def assert_status(self, code, call):
        with self.assertRaises(HTTPException) as ctx:
            call()
        self.assertEqual(ctx.exception.status_code, code)

    def test_open_local_uses_mirror_and_rejects_links(self):
        note = self.create()['item']
        with patch.object(self.server.subprocess, 'Popen') as popen, \
                patch.object(self.server, 'read_knowledge_remote', return_value=None):
            result = self.server.open_knowledge_local(note['id'])
        self.assertEqual(result['source'], 'local')
        args, _ = popen.call_args
        self.assertEqual(args[0][1], str(self.local / '_knowledge' / note['id'] / 'note.md'))
        self.assert_status(400, lambda: self.server.open_knowledge_local('../escape'))
        link = self.create(kind='link', title='链接资料', url='https://example.com/b')['item']
        self.assert_status(400, lambda: self.server.open_knowledge_local(link['id']))
        self.assert_status(404, lambda: self.server.open_knowledge_local('c' * 32))

    def test_open_local_pulls_missing_mirror_from_remote(self):
        item = self.create()['item']
        shutil.rmtree(self.local / '_knowledge' / item['id'])
        note, source = self.server.ensure_knowledge_local_note(item['id'])
        self.assertEqual(source, 'pulled')
        self.assertTrue(note.is_file())
        self.assertEqual(note.read_text(encoding='utf-8'), '第一行\n第二行')
        mirror = self.local / '_knowledge' / item['id']
        meta = json.loads((mirror / 'meta.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['title'], '测试笔记')
        self.assertEqual(meta['created_at'], json.loads(
            (self.remote / '_knowledge' / item['id'] / 'meta.json').read_text(encoding='utf-8'))['created_at'])


if __name__ == '__main__':
    unittest.main()
