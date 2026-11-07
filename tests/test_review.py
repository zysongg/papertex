import importlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from fastapi import HTTPException
from starlette.datastructures import UploadFile

import review as rv


def sha256_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class ReviewLogicTests(unittest.TestCase):
    def test_key_requires_safe_segments(self):
        self.assertEqual(rv.key_of('NeurIPS-2026', '1234'), 'NeurIPS-2026/1234')
        self.assertEqual(rv.parse_key(' ICML/42 '), ('ICML', '42'))
        for key in ('', 'a', 'a/b/c', '../etc/passwd', 'a/b/../c', 'a/b c', 'a/$x', 'a/\x00'):
            with self.assertRaises(rv.ReviewError):
                rv.parse_key(key)

    def test_new_task_validates_fields(self):
        task = rv.new_task({'venue': 'ICML', 'submission': '7', 'title': '  A paper  ',
                            'due': '2026-12-01', 'status': 'writing', 'rating': '4',
                            'confidence': '3', 'note': 'x'})
        self.assertEqual(task['key'], 'ICML/7')
        self.assertEqual(task['title'], 'A paper')
        self.assertEqual(task['status'], 'writing')
        self.assertEqual(rv.new_task({'venue': 'ICML', 'submission': '7'})['status'], 'pending')
        for payload in ({'venue': '', 'submission': '1'},
                        {'venue': 'ICML', 'submission': '1', 'due': '2026-12-1'},
                        {'venue': 'ICML', 'submission': '1', 'due': '2026-13-01'},
                        {'venue': 'ICML', 'submission': '1', 'status': 'nope'},
                        {'venue': 'ICML', 'submission': '1', 'title': 'x' * 301},
                        {'venue': 'ICML', 'submission': '1', 'title': 'bad\x00'}):
            with self.assertRaises(rv.ReviewError):
                rv.new_task(payload)

    def test_existing_supplies_missing_fields(self):
        old = {'venue': 'ICML', 'submission': '7', 'title': 'Old', 'due': '2026-12-01',
               'status': 'reading', 'rating': '5', 'confidence': '2', 'note': 'keep'}
        task = rv.new_task({'venue': 'ICML', 'submission': '7', 'status': 'submitted'},
                           existing=old)
        self.assertEqual(task['title'], 'Old')
        self.assertEqual(task['note'], 'keep')
        self.assertEqual(task['due'], '2026-12-01')
        self.assertEqual(task['status'], 'submitted')

    def test_parse_task_is_tolerant(self):
        task = rv.parse_task({'title': 'x' * 400, 'due': 'not-a-date', 'status': 3,
                              'rating': 5}, 'ICML', '7')
        self.assertEqual(task['key'], 'ICML/7')
        self.assertEqual(task['title'], '')
        self.assertEqual(task['due'], '')
        self.assertEqual(task['status'], 'pending')
        self.assertEqual(task['rating'], '')
        self.assertEqual(rv.parse_task(None, 'ICML', '7')['key'], 'ICML/7')

    def test_deadline_of(self):
        from datetime import date
        days, overdue = rv.deadline_of('2026-12-01', date(2026, 11, 1))
        self.assertEqual((days, overdue), (30, False))
        days, overdue = rv.deadline_of('2026-11-01', date(2026, 12, 1))
        self.assertEqual((days, overdue), (-30, True))
        self.assertEqual(rv.deadline_of('2026-11-01', date(2026, 11, 1)), (0, False))
        self.assertEqual(rv.deadline_of('', date(2026, 11, 1)), (None, False))
        self.assertEqual(rv.deadline_of('bad', date(2026, 11, 1)), (None, False))

    def test_templates_always_keep_generic_and_ignore_bad_entries(self):
        templates = rv.templates_from({
            '通用': {'sections': ['Hacked']},
            'ICML': {'sections': ['Summary', 'Strengths'], 'fields': ['confidence', 'bogus']},
            'Broken': {'sections': 'nope'},
            'Empty': {'sections': []},
        })
        self.assertEqual(templates['通用']['sections'], rv.DEFAULT_TEMPLATE['sections'])
        self.assertEqual(templates['ICML']['sections'], ['Summary', 'Strengths'])
        self.assertEqual(templates['ICML']['fields'], ['confidence'])
        self.assertNotIn('Broken', templates)
        self.assertNotIn('Empty', templates)
        self.assertEqual(rv.templates_from({})['通用']['fields'], ['rating', 'confidence'])

    def test_skeleton_and_export(self):
        text = rv.skeleton({'sections': ['Summary', 'Weaknesses']})
        self.assertEqual(text, '<!-- papertex 审稿笔记：本文件是唯一事实源 -->\n\n'
                               '## Summary\n\n## Weaknesses\n')
        self.assertNotIn('\n\n\n', text)
        self.assertTrue(text.startswith('<!--'))
        task = rv.new_task({'venue': 'ICML', 'submission': '7', 'title': 'P', 'due': '2026-12-01',
                            'rating': '4', 'confidence': '3', 'round': 'R1'})
        out = rv.render_export(task, '正文\n')
        self.assertTrue(out.startswith('# ICML / 7\n稿件：P\n轮次：R1\n截止：2026-12-01\n评分：4 / 置信度：3'))
        self.assertTrue(out.endswith('正文\n'))
        self.assertEqual(rv.render_export(rv.new_task({'venue': 'a', 'submission': '1'}), ''),
                         '# a / 1\n')


class ReviewAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory(prefix='papertex-review-app-')
        cls.addClassCleanup(temp.cleanup)
        root = Path(temp.name)
        project = Path(__file__).resolve().parents[1]
        shutil.copytree(project / 'app', root / 'app', ignore=shutil.ignore_patterns('__pycache__'))
        (root / 'config').mkdir()
        shutil.copyfile(project / 'config/config.example.json', root / 'config/config.json')
        spec = importlib.util.spec_from_file_location('papertex_review_server', root / 'app/server.py')
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='papertex-review-')
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        s = self.server
        self.review = self.root / 'ReviewSpace'
        (self.root / 'config').mkdir()
        self.templates = self.root / 'config' / 'review-templates.json'
        for key, value in (('REVIEW_LOCAL', self.review), ('REVIEW_TEMPLATE_FILE', self.templates)):
            patcher = patch.object(s, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def payload(self, **fields):
        base = {'venue': 'ICML-2026', 'submission': 'aX9', 'title': '时序生成论文',
                'venue_label': 'ICML 2026', 'round': 'R1', 'due': '2026-12-20',
                'status': 'pending', 'template': '通用'}
        base.update(fields)
        return base

    def create(self, **fields):
        return self.server.create_review_task(self.payload(**fields))

    def detail(self, key='ICML-2026/aX9'):
        return self.server.review_task_detail(key)

    def test_create_writes_only_inside_review_root(self):
        result = self.create()
        self.assertEqual(result['task']['key'], 'ICML-2026/aX9')
        task_dir = self.review / 'ICML-2026' / 'aX9'
        self.assertTrue((task_dir / 'task.json').is_file())
        self.assertTrue((task_dir / 'review.md').is_file())
        self.assertEqual(oct(task_dir.stat().st_mode & 0o777), '0o700')
        self.assertEqual(oct((task_dir / 'task.json').stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct(self.review.stat().st_mode & 0o777), '0o700')
        stored = json.loads((task_dir / 'task.json').read_text(encoding='utf-8'))
        self.assertNotIn('key', stored)
        self.assertNotIn('template', stored)
        self.assertEqual(stored['due'], '2026-12-20')

    def test_create_rejects_duplicate_and_unknown_template(self):
        self.create()
        with self.assertRaises(HTTPException) as ctx:
            self.create()
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(HTTPException) as ctx:
            self.create(template='ICLR-2026')
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse((self.review / 'ICML-2026' / 'aX9' / 'notes.md').exists())

    def test_template_file_uses_custom_sections(self):
        self.templates.write_text(json.dumps(
            {'自定义': {'sections': ['My Section'], 'fields': ['rating']}}, ensure_ascii=False),
            encoding='utf-8')
        self.create(template='自定义')
        body = (self.review / 'ICML-2026' / 'aX9' / 'review.md').read_text(encoding='utf-8')
        self.assertIn('## My Section', body)
        self.assertNotIn('## Strengths', body)

    def test_bad_template_config_is_reported_without_touching_files(self):
        self.templates.write_text('[1, 2]', encoding='utf-8')
        with self.assertRaises(HTTPException) as ctx:
            self.server.review_list()
        self.assertEqual(ctx.exception.status_code, 500)
        self.templates.write_text('not json', encoding='utf-8')
        with self.assertRaises(HTTPException):
            self.create()

    def test_list_sorts_by_deadline_and_reports_flags(self):
        self.create()
        self.create(submission='late', due='2020-01-01', venue='ICML-2026')
        self.create(venue='NeurIPS', submission='n1', due='')
        data = self.server.review_list()
        self.assertEqual(data['count'], 3)
        self.assertEqual([item['submission'] for item in data['items']], ['late', 'aX9', 'n1'])
        self.assertTrue(data['items'][0]['overdue'])
        self.assertFalse(data['items'][0]['has_manuscript'])
        self.assertEqual(data['items'][0]['status_label'], '待读')
        self.assertIsNone(data['items'][2]['days_left'])
        self.assertEqual(data['root'], str(self.review))
        self.assertIn('通用', [t['name'] for t in data['templates']])

    def test_body_autosave_tracks_revision(self):
        self.create()
        detail = self.detail()
        self.assertEqual(detail['revision'], sha256_text(detail['review']))
        saved = self.server.save_review_task_endpoint(
            {'key': 'ICML-2026/aX9', 'review': '我的评审意见', 'expected': detail['revision'],
             'fields': {'status': 'writing', 'rating': '4'}})
        self.assertEqual(saved['task']['status'], 'writing')
        self.assertEqual(saved['task']['rating'], '4')
        detail = self.detail()
        self.assertEqual(detail['review'], '我的评审意见')
        with self.assertRaises(HTTPException) as ctx:
            self.server.save_review_task_endpoint(
                {'key': 'ICML-2026/aX9', 'review': '覆盖', 'expected': detail['revision'][:-1] + '0'})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.detail()['review'], '我的评审意见')

    def test_body_rejects_control_chars_and_oversize(self):
        self.create()
        for body in ('含\x00字符', 'x' * (400_001)):
            with self.assertRaises(HTTPException):
                self.server.save_review_task_endpoint({'key': 'ICML-2026/aX9', 'review': body})
        self.assertEqual(self.detail()['review'], rv.skeleton(rv.DEFAULT_TEMPLATE))

    def test_fields_only_save_keeps_body_and_created_at(self):
        self.create()
        created = json.loads((self.review / 'ICML-2026' / 'aX9' / 'task.json')
                             .read_text(encoding='utf-8'))['created_at']
        saved = self.server.save_review_task_endpoint(
            {'key': 'ICML-2026/aX9', 'fields': {'note': '只改备注'}})
        self.assertEqual(saved['task']['note'], '只改备注')
        self.assertEqual(saved['task']['created_at'], created)
        self.assertEqual(self.detail()['review'], rv.skeleton(rv.DEFAULT_TEMPLATE))

    def test_missing_task_returns_404(self):
        for call in (lambda: self.detail('ICML-2026/nope'),
                     lambda: self.server.save_review_task_endpoint({'key': 'ICML/nope'})):
            with self.assertRaises(HTTPException) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 404)

    def upload(self, key='ICML-2026/aX9', data=b'%PDF-1.7 fake', overwrite=False,
               filename='paper.PDF'):
        return self.server.upload_review_manuscript(
            key=key, overwrite=overwrite,
            pdf=UploadFile(file=io.BytesIO(data), filename=filename))

    def test_manuscript_upload_requires_pdf_and_confirmation_to_overwrite(self):
        self.create()
        with self.assertRaises(HTTPException) as ctx:
            self.upload(data=b'not a pdf')
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            self.upload(filename='paper.txt')
        self.assertEqual(ctx.exception.status_code, 400)
        result = self.upload()
        self.assertEqual(result['file'], {'name': 'manuscript.pdf', 'size': 13})
        target = self.review / 'ICML-2026' / 'aX9' / 'manuscript.pdf'
        self.assertEqual(oct(target.stat().st_mode & 0o777), '0o600')
        with self.assertRaises(HTTPException) as ctx:
            self.upload(data=b'%PDF-2.0 other')
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(target.read_bytes(), b'%PDF-1.7 fake')
        self.upload(data=b'%PDF-2.0 other', overwrite=True)
        self.assertEqual(target.read_bytes(), b'%PDF-2.0 other')
        files = self.detail()['files']
        self.assertEqual([f['name'] for f in files], ['manuscript.pdf'])
        self.assertTrue(self.detail()['files'][0]['size'] > 0)

    def test_pdf_preview_is_fenced_to_review_root(self):
        self.create()
        self.upload()
        outside = self.root / 'outside.pdf'
        outside.write_bytes(b'%PDF-1.7 outside')
        response = self.server.review_pdf(key='ICML-2026/aX9')
        self.assertEqual(response.media_type, 'application/pdf')
        for key, name in (('ICML-2026/aX9', 'review.md'), ('ICML-2026/aX9', '../outside.pdf'),
                          ('ICML-2026', 'aX9/manuscript.pdf'), ('ICML-2026/aX9', 'nope.pdf')):
            with self.assertRaises(HTTPException):
                self.server.review_pdf(key=key, name=name)
        (self.review / 'ICML-2026' / 'aX9' / 'link.pdf').symlink_to(outside)
        with self.assertRaises(HTTPException):
            self.server.review_pdf(key='ICML-2026/aX9', name='link.pdf')

    def test_purge_removes_manuscript_and_keeps_review_text(self):
        self.create()
        self.server.save_review_task_endpoint({'key': 'ICML-2026/aX9', 'review': '已经写好的意见'})
        self.upload()
        (self.review / 'ICML-2026' / 'aX9' / 'supplement').mkdir()
        (self.review / 'ICML-2026' / 'aX9' / 'supplement' / 'appendix.pdf').write_bytes(b'%PDF-1')
        with self.assertRaises(HTTPException) as ctx:
            self.server.purge_review_manuscript({'key': 'ICML-2026/aX9', 'confirm': '清除'})
        self.assertEqual(ctx.exception.status_code, 400)
        result = self.server.purge_review_manuscript(
            {'key': 'ICML-2026/aX9', 'confirm': '清除稿件'})
        self.assertEqual(result['removed'], ['manuscript.pdf', 'supplement/'])
        self.assertTrue(result['task']['purged_at'])
        task_dir = self.review / 'ICML-2026' / 'aX9'
        self.assertFalse((task_dir / 'manuscript.pdf').exists())
        self.assertFalse((task_dir / 'supplement').exists())
        self.assertEqual(self.detail()['review'], '已经写好的意见')
        stored = json.loads((task_dir / 'task.json').read_text(encoding='utf-8'))
        self.assertNotIn('key', stored)
        self.assertEqual(self.server.purge_review_manuscript(
            {'key': 'ICML-2026/aX9', 'confirm': '清除稿件'})['removed'], [])

    def test_purge_refuses_symlinked_supplement(self):
        self.create()
        outside = self.root / 'elsewhere'
        outside.mkdir()
        (self.review / 'ICML-2026' / 'aX9' / 'supplement').symlink_to(outside)
        with self.assertRaises(HTTPException) as ctx:
            self.server.purge_review_manuscript({'key': 'ICML-2026/aX9', 'confirm': '清除稿件'})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertTrue(outside.is_dir())

    def test_purge_with_nested_symlink_deletes_nothing(self):
        self.create()
        self.upload()
        task_dir = self.review / 'ICML-2026' / 'aX9'
        (task_dir / 'supplement').mkdir()
        (task_dir / 'supplement' / 'appendix.pdf').write_bytes(b'%PDF-1')
        (task_dir / 'supplement' / 'link.pdf').symlink_to(self.root / 'outside.pdf')
        with self.assertRaises(HTTPException) as ctx:
            self.server.purge_review_manuscript({'key': 'ICML-2026/aX9', 'confirm': '清除稿件'})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertTrue((task_dir / 'manuscript.pdf').is_file())
        self.assertEqual(sorted(p.name for p in (task_dir / 'supplement').iterdir()),
                         ['appendix.pdf', 'link.pdf'])
        self.assertFalse(json.loads((task_dir / 'task.json').read_text(
            encoding='utf-8'))['purged_at'])

    def test_delete_moves_to_trash_and_restore_returns_it(self):
        self.create()
        self.upload()
        self.server.save_review_task_endpoint({'key': 'ICML-2026/aX9', 'review': '保留的意见'})
        with self.assertRaises(HTTPException) as ctx:
            self.server.delete_review_task(key='ICML-2026/aX9', confirm='删')
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertTrue((self.review / 'ICML-2026' / 'aX9').is_dir())
        moved = self.server.delete_review_task(key='ICML-2026/aX9', confirm='删除')
        self.assertFalse((self.review / 'ICML-2026' / 'aX9').exists())
        self.assertEqual(self.server.review_list()['trash_count'], 1)
        trash = self.server.review_trash_list()
        self.assertEqual(trash['count'], 1)
        entry = trash['items'][0]
        self.assertEqual(entry['key'], 'ICML-2026/aX9')
        self.assertEqual([f['name'] for f in entry['files']], ['manuscript.pdf'])
        self.assertTrue(entry['has_draft'])
        self.create()
        with self.assertRaises(HTTPException) as ctx:
            self.server.restore_review_task({'trash_id': entry['trash_id']})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertTrue((self.review / '.trash' / entry['trash_id']).is_dir())
        self.server.delete_review_task(key='ICML-2026/aX9', confirm='删除')
        restored = self.server.restore_review_task({'trash_id': entry['trash_id']})
        self.assertEqual(restored['key'], 'ICML-2026/aX9')
        self.assertEqual(self.detail()['review'], '保留的意见')
        leftover = self.server.review_trash_list()
        self.assertEqual(leftover['count'], 1)
        self.assertNotEqual(leftover['items'][0]['trash_id'], entry['trash_id'])
        self.server.purge_review_trash(trash_id=leftover['items'][0]['trash_id'], confirm='清理')
        self.assertEqual(self.server.review_trash_list()['count'], 0)
        with self.assertRaises(HTTPException) as ctx:
            self.server.restore_review_task({'trash_id': 'nope'})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_trash_purge_needs_recovery_word_and_only_touches_entry(self):
        self.create()
        moved = self.server.delete_review_task(key='ICML-2026/aX9', confirm='删除')
        for confirm in ('清理清理', '确认'):
            with self.assertRaises(HTTPException):
                self.server.purge_review_trash(trash_id=moved['trash_id'], confirm=confirm)
        with self.assertRaises(HTTPException):
            self.server.purge_review_trash(trash_id='../evil', confirm='清理')
        result = self.server.purge_review_trash(trash_id=moved['trash_id'], confirm='清理')
        self.assertTrue(result['ok'])
        self.assertFalse((self.review / '.trash' / moved['trash_id']).exists())
        self.assertEqual(self.server.review_trash_list()['count'], 0)

    def test_scan_survives_corrupt_task_json(self):
        self.create()
        (self.review / 'ICML-2026' / 'aX9' / 'task.json').write_text('{ broken', encoding='utf-8')
        (self.review / 'ICML-2026' / 'ignored.txt').write_text('x', encoding='utf-8')
        (self.review / 'ICML-2026' / '.hidden').mkdir()
        item = self.server.review_list()['items'][0]
        self.assertEqual(item['key'], 'ICML-2026/aX9')
        self.assertFalse(item['meta_ok'])
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(self.server.review_list()['count'], 1)

    def test_review_root_rejects_symlink(self):
        real = self.root / 'real-review'
        real.mkdir()
        link = self.root / 'link-review'
        link.symlink_to(real)
        with patch.object(self.server, 'REVIEW_LOCAL', link):
            with self.assertRaises(HTTPException) as ctx:
                self.server.review_root()
        self.assertEqual(ctx.exception.status_code, 400)

    def test_task_paths_never_escape_review_root(self):
        self.review.mkdir(parents=True)
        escape = self.root / 'escape'
        escape.mkdir()
        (self.review / 'ICML-2026').symlink_to(escape)
        with self.assertRaises(HTTPException) as ctx:
            self.server.review_path('ICML-2026', 'aX9')
        self.assertEqual(ctx.exception.status_code, 400)


class LocalOnlyGuardTests(unittest.TestCase):
    """审稿目录必须与论文库隔离，且只允许落在家目录下的独立子目录。"""

    def base(self, **fields):
        cfg = {'ssh_alias': 'P6000', 'remote_library': '/data/lib',
               'remote_workshop': '/data/workshop'}
        cfg.update(fields)
        return cfg

    def test_default_review_space_is_home_subdirectory(self):
        import settings
        cfg = settings.normalize(self.base(), strict=True)
        self.assertEqual(cfg['review_space'], '~/ReviewSpace')
        self.assertEqual(settings.public(cfg)['review_space'], '~/ReviewSpace')

    def test_review_space_must_not_overlap_library(self):
        import settings
        pairs = (('~/PaperLibrary', '~/PaperLibrary'),
                 ('~/PaperLibrary', '~/PaperLibrary/Review'),
                 ('~/PaperLibrary/Review', '~/PaperLibrary'),
                 ('~/PaperLibrary', '~'),
                 ('~/PaperLibrary', '/Volumes/nas/Review'),
                 ('~/PaperLibrary', '~/../Review'))
        for lib, review in pairs:
            with self.assertRaises(ValueError):
                settings.normalize(self.base(local_library=lib, review_space=review), strict=True)
        cfg = settings.normalize(self.base(local_library='~/PaperLib',
                                           review_space='~/MyReview'), strict=True)
        self.assertEqual(cfg['review_space'], '~/MyReview')


if __name__ == '__main__':
    unittest.main()
