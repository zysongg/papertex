import asyncio
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from fastapi import HTTPException
from starlette.datastructures import UploadFile


class StorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory(prefix='papertex-test-app-')
        cls.addClassCleanup(temp.cleanup)
        root = Path(temp.name)
        project = Path(__file__).resolve().parents[1]
        shutil.copytree(project / 'app', root / 'app', ignore=shutil.ignore_patterns('__pycache__'))
        (root / 'config').mkdir()
        shutil.copyfile(project / 'config/config.example.json', root / 'config/config.json')
        spec = importlib.util.spec_from_file_location('papertex_test_server', root / 'app/server.py')
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='papertex-test-')
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
                      PROJECTS_LOCAL_FILE=self.root / 'config/projects.local.json',
                      STARS_FILE=self.root / 'config/stars.json')
        for key, value in values.items():
            p = patch.object(s, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.real_run = subprocess.run
        self.commands = []
        p = patch.object(s, 'ssh_run', self.shell)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(s, 'ssh_stream', self.stream)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(s, 'copy_to_remote', self.copy_file)
        p.start()
        self.addCleanup(p.stop)
        self.actual_scan_remote = s.scan_remote
        p = patch.object(s, 'scan_remote', self.scan_remote)
        p.start()
        self.addCleanup(p.stop)
        self.topic, self.year, self.slug = 'TS', '2026', '2026-ICML-Li-Test'
        self.key = f'{self.topic}/{self.year}/{self.slug}'
        self.paper = self.remote / self.key

    def shell(self, command, timeout=60, stdin=None, binary=False):
        self.assertIn(str(self.root), command)
        self.assertNotIn('ssh ', command)
        self.commands.append(command)
        return self.real_run(['sh', '-c', command],
                             input=stdin.encode() if binary and stdin is not None else stdin,
                             text=not binary, capture_output=True, timeout=timeout, cwd=self.root)

    def stream(self, command, target, timeout=120):
        self.assertIn(str(self.root), command)
        self.assertNotIn('ssh ', command)
        self.commands.append(command)
        return self.real_run(['sh', '-c', command], stdout=target,
                             stderr=subprocess.PIPE, timeout=timeout, cwd=self.root)

    def copy_file(self, local, remote):
        path = Path(remote)
        self.assertTrue(path.is_relative_to(self.root))
        self.server.ssh_need(self.server.remote_guard(remote) + 'true')
        shutil.copyfile(local, path)

    def scan_remote(self, cache_titles=True):
        data = {}
        if self.remote.exists():
            for paper in self.remote.glob('*/*/*'):
                if not paper.is_dir():
                    continue
                value = {}
                meta = paper / 'meta.json'
                if meta.is_file():
                    value = json.loads(meta.read_text())
                    if not cache_titles:
                        value['_citation_meta'] = dict(value)
                    value['has_meta'] = True
                data[str(paper.relative_to(self.remote))] = value
        return data

    def seed(self):
        self.paper.mkdir(parents=True)
        (self.paper / (self.slug + '.pdf')).write_bytes(b'%PDF-1.4 old')
        (self.paper / 'source').mkdir()
        (self.paper / 'source/main.tex').write_text('old source')
        meta = dict(topic=self.topic, year=int(self.year), slug=self.slug,
                    title='Original', entry='source/main.tex', pdf=self.slug + '.pdf',
                    has_pdf=True, has_source=True, custom='preserve')
        (self.paper / 'meta.json').write_text(json.dumps(meta))
        return meta

    def bundle(self, name='main.tex', text='new source'):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr(name, text)
        return output.getvalue()

    def save(self, pdf=b'%PDF-1.4 new', source=None, filename='paper.zip', local=True):
        with self.server.mutation_lock():
            return self.server.save_paper(self.topic, self.year, self.slug, pdf, source,
                                          filename, local, {})

    def project(self, name='demo', refs=None):
        folder = self.workshop / name
        folder.mkdir()
        (folder / 'reference-tex.json').write_text(json.dumps({
            'library': str(self.remote), 'references': refs or []}))
        self.server.save_projects([name])
        return folder / 'reference-tex.json'

    def local_project(self, name='local-demo'):
        folder = self.root / 'local-projects' / name
        folder.mkdir(parents=True, exist_ok=True)
        self.server.save_projects([str(folder)], 'local')
        return folder

    def seed_local(self, with_pdf=True):
        paper = self.local / self.key
        paper.mkdir(parents=True, exist_ok=True)
        if with_pdf:
            (paper / (self.slug + '.pdf')).write_bytes(b'%PDF-1.7 local')
        return paper

    def assert_status(self, status, action):
        with self.assertRaises(HTTPException) as error:
            action()
        self.assertEqual(error.exception.status_code, status)

    def research_write(self, method, **payload):
        payload.setdefault('revision', self.server.get_research()['revision'])
        return method(payload)

    def test_research_reading_preserves_fields_and_detects_revision_conflict(self):
        self.local_copy()
        s = self.server
        empty = s.get_research()
        self.assertEqual(empty['items'], {})
        self.assertEqual(len(empty['revision']), 64)
        self.assertFalse((self.root / 'config/research.json').exists())
        with patch.object(s, 'remote_exists', side_effect=AssertionError('local first')):
            saved = self.research_write(s.set_citation, key=self.key, fields={'title': 'Saved'})
            saved = self.research_write(s.set_reading, key=self.key, status='reading', note='notes')
        self.assertTrue(saved['ok'])
        self.assertEqual(saved['items'][self.key]['bibliography'], {'title': 'Saved'})
        self.assertEqual(saved['items'][self.key]['note'], 'notes')
        self.assert_status(409, lambda: s.set_reading(dict(key=self.key, status='read', note='',
                                                         revision=empty['revision'])))
        self.assert_status(409, lambda: s.set_reading(dict(key=self.key, status='read', note='')))
        for status in ('', 'unread', 'reading', 'read', 'reproduce'):
            saved = self.research_write(s.set_reading, key=self.key, status=status, note='')
            self.assertEqual(saved['items'][self.key]['status'], status)
        self.assertEqual(s.get_research()['items'][self.key]['bibliography']['title'], 'Saved')

    def test_research_invalid_inputs_and_missing_papers(self):
        s = self.server
        for key in (None, 42, '../2026/a', '/TS/2026/a', 'TS/2026/a/', 'TS/2026', 'TS/2026/a\n'):
            self.assert_status(400, lambda key=key: self.research_write(
                s.set_reading, key=key, status='', note=''))
        for status, note in (('bad', ''), (None, ''), ([], ''), ('read', None), ('read', 'x' * 1001)):
            self.assert_status(400, lambda: self.research_write(s.set_reading, key=self.key,
                                                               status=status, note=note))
        self.assert_status(404, lambda: self.research_write(s.set_reading, key=self.key, status='', note=''))
        self.assert_status(404, lambda: s.get_citation(self.key))
        self.assertFalse((self.root / 'config/research.json').exists())

    def test_research_bad_json_and_symlink_are_never_overwritten(self):
        self.local_copy()
        path = self.root / 'config/research.json'
        for raw in ('{bad', '[]', '{"papers":[]}', '{"papers":{"TS/2026/a":{"note":3}}}'):
            path.write_text(raw)
            self.assert_status(500, self.server.get_research)
            self.assert_status(500, lambda: self.server.set_reading(
                dict(key=self.key, status='', note='', revision='bad')))
            self.assertEqual(path.read_text(), raw)
        path.unlink()
        target = self.root / 'keep.json'
        target.write_text('{"papers":{}}')
        path.symlink_to(target)
        self.assert_status(400, self.server.get_research)
        self.assert_status(400, lambda: self.server.set_reading(
            dict(key=self.key, status='', note='', revision='bad')))
        self.assertEqual(target.read_text(), '{"papers":{}}')

    def test_research_atomic_failure_and_concurrent_revision(self):
        self.local_copy()
        s = self.server
        saved = self.research_write(s.set_reading, key=self.key, status='reading', note='old')
        path = self.root / 'config/research.json'
        before = path.read_bytes()
        with patch.object(s.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.research_write(s.set_reading, key=self.key, status='read', note='new')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob('.research.json*')), [])
        def write(note):
            try:
                s.set_reading(dict(key=self.key, status='read', note=note, revision=saved['revision']))
                return 200
            except HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(write, ['one', 'two'])), [200, 409])

    def test_citation_escaping_explicit_empty_and_no_get_writes(self):
        self.seed()
        s = self.server
        before = (self.paper / 'meta.json').read_bytes()
        saved = self.research_write(s.set_citation, key=self.key, fields={
            'entry_type': 'article', 'title': r'A & B_%$#~^ {x} \input{evil}',
            'authors': r'{Alice Smith} and Bob \input{evil}', 'year': '2025',
            'venue': 'A & B', 'doi': '10.1234/test_foo', 'arxiv': '2501.12345v2'})
        with patch.object(s, 'write_local_json', side_effect=AssertionError('GET writes')):
            citation = s.get_citation(self.key)
        self.assertTrue(citation['complete'])
        self.assertEqual(citation['fields']['authors'], r'{Alice Smith} and Bob \input{evil}')
        self.assertIn(r'\textbraceleft{}Alice Smith\textbraceright{}', citation['bibtex'])
        self.assertIn(r'\textbackslash{}input\textbraceleft{}evil\textbraceright{}', citation['bibtex'])
        self.assertIn(r'\&', citation['bibtex'])
        self.assertNotIn(r'\input{evil}', citation['bibtex'])
        self.assertIn('journal = ', citation['bibtex'])
        self.assertEqual(citation['cite'], '\\cite{' + citation['cite_key'] + '}')
        self.assertEqual(s.get_research()['revision'], saved['revision'])
        self.research_write(s.set_citation, key=self.key, fields={'title': '', 'authors': '', 'year': ''})
        citation = s.get_citation(self.key)
        self.assertFalse(citation['complete'])
        self.assertEqual(citation['fields']['title'], '')
        self.assertNotIn('title = ', citation['bibtex'])
        self.assertEqual(len([w for w in citation['warnings'] if '缺少' in w]), 3)
        self.assertEqual((self.paper / 'meta.json').read_bytes(), before)

    def test_bibtex_unmatched_literal_braces_do_not_break_field_balance(self):
        self.seed()
        s = self.server
        self.research_write(s.set_citation, key=self.key, fields={'title': 'A } unmatched { { title'})
        bib = s.get_citation(self.key)['bibtex']
        depth = 0
        for char in bib:
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
            self.assertGreaterEqual(depth, 0)
        self.assertEqual(depth, 0)
        self.assertIn(r'\textbraceright{}', bib)

    def test_citation_missing_authors_and_remote_fallback(self):
        self.local_copy()
        s = self.server
        s.write_local_json(s.titles_file(), {self.key: {'title': 'Cached'}})
        with patch.object(s, 'remote_exists', side_effect=HTTPException(502, 'offline')):
            citation = s.get_citation(self.key)
        self.assertEqual(citation['fields']['title'], 'Cached')
        self.assertEqual(citation['fields']['year'], '2026')
        self.assertEqual(citation['fields']['entry_type'], 'misc')
        self.assertFalse(citation['complete'])
        self.assertTrue(any('元数据未核对' in w for w in citation['warnings']))
        self.assertNotIn('author = ', citation['bibtex'])
        self.assertFalse((self.root / 'config/research.json').exists())
        with patch.object(s, 'read_meta', side_effect=AssertionError('nonexistent meta')):
            self.assertEqual(s.get_citation(self.key)['fields']['title'], 'Cached')

    def test_citation_validates_fields_and_remote_values(self):
        meta = self.seed()
        s = self.server
        for fields in (None, [], {'unknown': ''}, {'authors': []}, {'title': 'x' * 2001},
                       {'year': '0000'}, {'year': 2026}, {'entry_type': 'book'},
                       {'doi': r'10.1234/\input'}, {'arxiv': 'https://arxiv.org/abs/2501.12345'}):
            self.assert_status(400, lambda fields=fields: self.research_write(
                s.set_citation, key=self.key, fields=fields))
        meta.update(authors=['Alice Smith', 'Bob Jones'], year=2025, doi=r'10.1234/\input')
        (self.paper / 'meta.json').write_text(json.dumps(meta))
        citation = s.get_citation(self.key)
        self.assertEqual(citation['fields']['authors'], 'Alice Smith and Bob Jones')
        self.assertEqual(citation['fields']['year'], '2025')
        self.assertEqual(citation['fields']['doi'], '')
        self.assertTrue(any('doi' in w for w in citation['warnings']))

    def test_research_rename_preserves_fields_and_updates_cite_key(self):
        meta = self.seed()
        s = self.server
        self.assertEqual(s.get_citation(self.key)['cite_key'], self.slug)
        self.research_write(s.set_reading, key=self.key, status='read', note='keep note')
        self.research_write(s.set_citation, key=self.key, fields={'title': 'Override'})
        other = 'TS/2026/other'
        (self.local / other).mkdir(parents=True)
        linked = self.research_write(s.link_versions, key=self.key, other_key=other,
                                     label='arxiv', other_label='journal')
        result = self.research_write(s.edit_paper, old=meta, meta=dict(meta, slug='2026-ICML-Li-New'))
        self.assertEqual(s.get_research()['items'][result['key']]['version_group'],
                         linked['items'][other]['version_group'])
        record = s.get_research()['items'][result['key']]
        self.assertNotIn(self.key, s.get_research()['items'])
        self.assertEqual(record['note'], 'keep note')
        self.assertEqual(record['bibliography']['title'], 'Override')
        self.assertEqual(s.get_citation(result['key'])['cite_key'], '2026-ICML-Li-New')
        self.assertNotIn('cite_key', record)

    def test_citation_export_uses_slug_keys_and_disambiguates(self):
        meta = self.seed()
        s = self.server
        renamed = self.research_write(s.edit_paper, old=meta,
                                      meta=dict(meta, slug='2026-ICML-Li-B'))['key']
        self.assertEqual(s.get_citation(renamed)['cite_key'], '2026-ICML-Li-B')
        self.seed()
        moved = self.research_write(s.edit_paper, old=meta,
                                    meta=dict(meta, slug='2026-ICML-Li-C'))['key']
        self.assertEqual(s.get_citation(moved)['cite_key'], '2026-ICML-Li-C')
        twin = 'Other/2026/2026-ICML-Li-B'
        (self.remote / twin).mkdir(parents=True)
        (self.remote / twin / 'meta.json').write_text(json.dumps(
            dict(meta, topic='Other', slug='2026-ICML-Li-B')))
        self.project(refs=[{'path': renamed}, {'path': moved}, {'path': twin}])
        before = s.get_research()
        with patch.object(s, 'write_local_json', side_effect=AssertionError('export writes')):
            exported = s.project_bibliography('demo')
        self.assertEqual(exported['count'], 3)
        keys = [line.split('{', 1)[1].rstrip(',')
                for line in exported['bibtex'].splitlines() if line.startswith('@')]
        self.assertEqual(keys, ['2026-ICML-Li-B-TS', '2026-ICML-Li-C', '2026-ICML-Li-B-Other'])
        self.assertEqual(len(set(keys)), 3)
        self.assertEqual(s.get_research(), before)

    def test_citation_key_uses_slug_and_suffixes_on_collision(self):
        s = self.server
        slug, peer = self.slug, 'Other/2025/' + self.slug
        self.assertEqual(s.citation_key(self.key, set()), slug)
        self.assertEqual(s.citation_key(self.key, {self.key}), slug)
        self.assertEqual(s.citation_key(self.key, {self.key, peer}), slug + '-TS')
        self.assertEqual(s.citation_key(peer, {self.key, peer}), slug + '-Other')
        same_topic = 'TS/2025/' + slug
        self.assertEqual(s.citation_key(self.key, {self.key, same_topic}), slug + '-TS-2026')
        self.assertEqual(s.citation_key(same_topic, {self.key, same_topic}), slug + '-TS-2025')

    def test_legacy_stored_cite_key_is_ignored_and_cleaned(self):
        self.seed()
        s = self.server
        records = {self.key: {'cite_key': 'SavedCustomKey', 'note': 'keep'}}
        (self.root / 'config/research.json').write_text(json.dumps({'papers': records}))
        self.assertEqual(s.get_research()['items'][self.key]['cite_key'], 'SavedCustomKey')
        self.assertEqual(s.get_citation(self.key)['cite_key'], self.slug)
        saved = self.research_write(s.set_reading, key=self.key, status='read', note='keep')
        self.assertNotIn('cite_key', saved['items'][self.key])
        self.assertEqual(saved['items'][self.key]['note'], 'keep')

    def test_citation_key_follows_slug_not_stored_reservations(self):
        meta = self.seed()
        s = self.server
        renamed = self.research_write(s.edit_paper, old=meta,
                                      meta=dict(meta, slug='2026-ICML-Li-B'))['key']
        self.seed()
        saved = self.research_write(s.set_reading, key=self.key, status='reading', note='stable')
        self.assertNotIn('cite_key', saved['items'][self.key])
        saved['items'].pop(renamed)
        s.save_research(saved)
        self.assertEqual(s.get_citation(self.key)['cite_key'], self.slug)

    def test_citation_batch_migration_moves_records_without_stored_keys(self):
        s = self.server
        owner, moved_owner, moved = 'TS/2026/b', 'NewTS/2026/b', 'NewTS/2026/a'
        records = {owner: {'cite_key': 'LegacyKeyA'}, self.key: {'note': 'legacy'}}
        path = self.root / 'config/research.json'
        path.write_text(json.dumps({'papers': records}))
        before = path.read_bytes()
        for mapping in ({owner: moved_owner, self.key: moved},
                        {self.key: moved, owner: moved_owner}):
            state = s.prepare_research_migration(mapping, {'revision': s.get_research()['revision']})
            self.assertEqual(state['items'][moved]['note'], 'legacy')
            self.assertEqual(state['items'][moved_owner]['cite_key'], 'LegacyKeyA')
            self.assertEqual(s.citation_key(moved_owner, state['items']), 'b')
            self.assertEqual(s.citation_key(moved, state['items']), 'a')
            self.assertEqual(path.read_bytes(), before)

    def test_research_rename_conflict_precedes_any_moves(self):
        meta = self.seed()
        s = self.server
        target = 'TS/2026/2026-ICML-Li-New'
        (self.local / target).mkdir(parents=True)
        self.research_write(s.set_reading, key=target, status='read', note='keep')
        (self.local / target).rmdir()
        before = s.get_research()
        with patch.object(s, 'stage_remote', side_effect=AssertionError('must preflight')):
            self.assert_status(409, lambda: self.research_write(
                s.edit_paper, old=meta, meta=dict(meta, slug=target.split('/')[-1])))
        self.assertTrue(self.paper.exists())
        self.assertEqual(s.get_research(), before)

    def test_research_topic_migration_includes_orphans_and_slug_cite_keys(self):
        self.seed()
        s = self.server
        orphan = 'TS/2025/2025-ICML-Li-Orphan'
        (self.local / orphan).mkdir(parents=True)
        self.research_write(s.set_reading, key=orphan, status='reproduce', note='orphan')
        (self.local / orphan).rmdir()
        self.research_write(s.rename_topic, topic='TS', new='NewTS')
        items = s.get_research()['items']
        for old in (self.key, orphan):
            self.assertNotIn(old, items)
            new_key = 'NewTS' + old[2:]
            self.assertNotIn('cite_key', items[new_key])
            self.assertEqual(s.citation_key(new_key, items), old.rsplit('/', 1)[-1])
        self.assertEqual(items['NewTS' + orphan[2:]]['note'], 'orphan')

    def test_research_topic_conflict_and_stale_revision_preflight(self):
        self.seed()
        self.local_copy()
        s = self.server
        new_key = 'NewTS/' + self.year + '/' + self.slug
        state = s.get_research()
        state['items'][new_key] = {'note': 'existing orphan'}
        with s.mutation_lock():
            s.save_research(state)
        self.assert_status(409, lambda: self.research_write(s.rename_topic, topic='TS', new='NewTS'))
        self.assert_status(409, lambda: s.rename_topic({'topic': 'TS', 'new': 'Else', 'revision': 'stale'}))
        self.assertTrue(self.paper.exists())
        self.assertTrue((self.local / self.key).exists())
        self.assertFalse((self.remote / 'NewTS').exists())

    def test_research_migration_failure_is_reported_without_erasing_records(self):
        meta = self.seed()
        s = self.server
        self.research_write(s.set_reading, key=self.key, status='read', note='retained')
        with patch.object(s, 'save_research', side_effect=OSError('disk full')):
            result = self.research_write(s.edit_paper, old=meta, meta=dict(meta, slug='2026-ICML-Li-New'))
        self.assertTrue(result['ok'])
        self.assertTrue(any('研究记录迁移失败' in w for w in result['warnings']))
        self.assertEqual(s.get_research()['items'][self.key]['note'], 'retained')

    def test_versions_link_unlink_preserves_other_members_and_status(self):
        self.local_copy()
        s = self.server
        keys = [self.key, 'TS/2026/b', 'TS/2026/c']
        for key in keys[1:]:
            (self.local / key).mkdir(parents=True)
        self.research_write(s.set_reading, key=self.key, status='reading', note='keep')
        self.research_write(s.link_versions, key=keys[0], other_key=keys[1], label='arxiv', other_label='conference')
        linked = self.research_write(s.link_versions, key=keys[0], other_key=keys[2], label='arxiv', other_label='journal')
        self.assertEqual(len({r['version_group'] for r in linked['items'].values()}), 1)
        self.assertEqual(linked['items'][keys[1]]['version_label'], 'conference')
        self.assertEqual(linked['items'][keys[0]]['note'], 'keep')
        unlinked = self.research_write(s.unlink_versions, key=keys[0])['items']
        self.assertNotIn('version_group', unlinked[keys[0]])
        self.assertEqual(unlinked[keys[0]]['status'], 'reading')
        self.assertTrue(unlinked[keys[1]]['version_group'])
        unlinked = self.research_write(s.unlink_versions, key=keys[1])['items']
        self.assertNotIn('version_group', unlinked[keys[2]])
        self.assertEqual(unlinked[keys[2]]['version_label'], 'journal')

    def test_versions_reject_different_groups_invalid_labels_and_missing(self):
        s = self.server
        keys = ['TS/2026/' + name for name in ('a', 'b', 'c', 'd')]
        for key in keys:
            (self.local / key).mkdir(parents=True)
        for a, b in (keys[:2], keys[2:]):
            self.research_write(s.link_versions, key=a, other_key=b, label='', other_label='other')
        before = s.get_research()
        self.assert_status(409, lambda: self.research_write(
            s.link_versions, key=keys[0], other_key=keys[2], label='', other_label=''))
        self.assertEqual(s.get_research(), before)
        for other, label, code in ((keys[0], '', 400), (keys[1], 'bad', 400), ('TS/2026/missing', '', 404)):
            self.assert_status(code, lambda: self.research_write(
                s.link_versions, key=keys[0], other_key=other, label=label, other_label=''))
        self.assert_status(409, lambda: s.unlink_versions({'key': keys[0], 'revision': 'stale'}))

    def test_research_delete_preserves_records_and_does_not_scan_on_get(self):
        self.seed()
        s = self.server
        self.research_write(s.set_reading, key=self.key, status='read', note='recoverable')
        before = s.get_research()['items']
        s.delete_paper('both', self.topic, self.year, self.slug, self.slug)
        with patch.object(s, 'scan_remote', side_effect=AssertionError('GET scanned')):
            self.assertEqual(s.get_research()['items'], before)
        self.assertFalse(self.paper.exists())

    def test_project_bibliography_registered_deduplicates_and_skips_missing(self):
        self.seed()
        s = self.server
        missing = 'TS/2026/missing'
        path = self.project(refs=[{'path': self.key}, {'path': self.key}, {'path': missing}])
        before = path.read_bytes()
        self.assert_status(403, lambda: s.project_bibliography('unregistered'))
        with patch.object(s, 'scan_remote', wraps=self.scan_remote) as scan, \
                patch.object(s, 'read_meta', side_effect=AssertionError('batch meta sufficient')):
            result = s.project_bibliography('demo')
        scan.assert_called_once_with(cache_titles=False)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['filename'], 'references.bib')
        self.assertTrue(any(missing in w and '跳过' in w for w in result['warnings']))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / 'config/research.json').exists())
        self.assertFalse((self.root / 'config/titles.json').exists())
        self.assertFalse((path.parent / 'references.bib').exists())

    def test_citation_batch_scan_retains_full_meta_without_cache_write(self):
        s = self.server
        meta = dict(title='Batch', authors=['Full Name'], year=2025, doi='10.1234/test',
                    added_at='2026-01-02T03:04:05+00:00')
        listing = f'{self.key}\td\n{self.key}/meta.json\tf\nTS/2026/not-a-directory\tf\n'
        output = f'__META__{self.key}/meta.json\n{json.dumps(meta)}\n__END__\n'
        with patch.object(s, 'ssh_need', side_effect=[
                subprocess.CompletedProcess([], 0, listing, ''),
                subprocess.CompletedProcess([], 0, output, '')]) as runner, \
                patch.object(s, 'save_title_cache', side_effect=AssertionError('GET cache write')):
            papers = self.actual_scan_remote(cache_titles=False)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(papers[self.key]['_citation_meta'], meta)
        self.assertEqual(papers[self.key]['added_at'], '2026-01-02T03:04:05+00:00')
        self.assertNotIn('TS/2026/not-a-directory', papers)
        self.assertFalse(s.titles_file().exists())

    def test_research_external_revision_change_is_not_overwritten(self):
        self.local_copy()
        s = self.server
        self.research_write(s.set_reading, key=self.key, status='read', note='first')
        state = s.get_research()
        self.research_write(s.set_reading, key=self.key, status='reading', note='external')
        state['items'][self.key]['note'] = 'stale write'
        with s.mutation_lock():
            self.assert_status(409, lambda: s.save_research(state))
        self.assertEqual(s.get_research()['items'][self.key]['note'], 'external')

    def test_project_bibliography_incomplete_scan_and_offline_warnings(self):
        self.seed()
        self.local_copy()
        s = self.server
        self.project(refs=[{'path': self.key}])
        with patch.object(s, 'scan_remote', return_value={self.key: {'has_meta': True}}), \
                patch.object(s, 'read_meta', wraps=s.read_meta) as reader:
            result = s.project_bibliography('demo')
        reader.assert_called_once_with(str(self.paper))
        self.assertIn('Original', result['bibtex'])
        self.research_write(s.set_citation, key=self.key, fields={'title': 'Offline saved'})
        with patch.object(s, 'scan_remote', side_effect=HTTPException(502, 'offline')):
            result = s.project_bibliography('demo')
        self.assertEqual(result['count'], 1)
        self.assertIn('Offline saved', result['bibtex'])
        self.assertTrue(any('元数据未核对' in w for w in result['warnings']))

    def test_pdf_and_source_commit_preserves_metadata_and_recovery(self):
        self.seed()
        result = self.save(source=self.bundle())
        self.assertEqual((self.paper / 'source/main.tex').read_text(), 'new source')
        self.assertEqual((self.paper / (self.slug + '.pdf')).read_bytes(), b'%PDF-1.4 new')
        self.assertEqual(result['meta']['custom'], 'preserve')
        backup = Path(result['recovery_path'])
        self.assertEqual((backup / 'source/main.tex').read_text(), 'old source')
        self.assertEqual((backup / (self.slug + '.pdf')).read_bytes(), b'%PDF-1.4 old')
        self.assertEqual((self.local / self.key / (self.slug + '.pdf')).read_bytes(), b'%PDF-1.4 new')
        self.assertFalse(result['warnings'])

    def test_source_only_preserves_pdf(self):
        self.seed()
        self.save(pdf=None, source=self.bundle())
        self.assertEqual((self.paper / (self.slug + '.pdf')).read_bytes(), b'%PDF-1.4 old')
        self.assertFalse(self.local.exists())

    def test_user_filename_cannot_control_temporary_destination(self):
        self.seed()
        destination = self.root / 'unexpected.zip'
        self.save(pdf=None, source=self.bundle(), filename=str(destination))
        self.assertFalse(destination.exists())
        self.assertEqual((self.paper / 'source/main.tex').read_text(), 'new source')

    def test_invalid_archive_does_not_replace_existing_pdf_or_source(self):
        self.seed()
        before = {p.relative_to(self.paper): p.read_bytes() for p in self.paper.rglob('*') if p.is_file()}
        self.assert_status(400, lambda: self.save(source=self.bundle('../escape.tex')))
        after = {p.relative_to(self.paper): p.read_bytes() for p in self.paper.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(self.local.exists())

    def test_transfer_failure_preserves_original(self):
        self.seed()
        with patch.object(self.server, 'copy_to_remote', side_effect=HTTPException(502, 'transfer failed')):
            self.assert_status(502, lambda: self.save(source=self.bundle()))
        self.assertEqual((self.paper / 'source/main.tex').read_text(), 'old source')
        self.assertEqual((self.paper / (self.slug + '.pdf')).read_bytes(), b'%PDF-1.4 old')
        self.assertFalse(self.local.exists())

    def test_extract_failure_preserves_original(self):
        self.seed()
        shell = self.shell
        def fail_extract(command, timeout=60, stdin=None, binary=False):
            if 'tar -xzf' in command:
                return subprocess.CompletedProcess([], 2, '', 'tar failed')
            return shell(command, timeout, stdin, binary)
        with patch.object(self.server, 'ssh_run', fail_extract):
            self.assert_status(502, lambda: self.save(source=self.bundle()))
        self.assertEqual((self.paper / 'source/main.tex').read_text(), 'old source')

    def test_publish_failure_rolls_back_original(self):
        self.seed()
        missing = str(self.remote) + '.staging/not-created'
        self.assert_status(502, lambda: self.server.publish_remote(missing, str(self.paper), str(self.paper)))
        self.assertEqual((self.paper / 'source/main.tex').read_text(), 'old source')

    def test_bad_metadata_is_not_replaced(self):
        self.seed()
        (self.paper / 'meta.json').write_text('{broken')
        self.assert_status(500, lambda: self.save())
        self.assertEqual((self.paper / 'meta.json').read_text(), '{broken')
        self.assertEqual((self.paper / (self.slug + '.pdf')).read_bytes(), b'%PDF-1.4 old')

    def test_local_commit_failure_is_reported_as_partial_success(self):
        self.seed()
        with patch.object(self.server, 'publish_local_pdf', side_effect=OSError('disk full')):
            result = self.save()
        self.assertTrue(result['ok'])
        self.assertTrue(result['warnings'])
        self.assertEqual((self.paper / (self.slug + '.pdf')).read_bytes(), b'%PDF-1.4 new')

    def test_symlink_in_local_path_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.local.mkdir()
        (self.local / self.topic).symlink_to(outside, target_is_directory=True)
        self.assert_status(400, lambda: self.server.local_pdf_path(self.topic, self.year, self.slug))
        self.assert_status(400, lambda: self.server.delete_paper('local', self.topic, self.year, self.slug, self.slug))
        self.assertTrue(outside.exists())

    def test_remote_symlink_is_rejected(self):
        self.remote.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        (self.remote / self.topic).symlink_to(outside, target_is_directory=True)
        self.assert_status(502, lambda: self.server.read_meta(str(self.paper)))
        self.assertEqual(list(outside.iterdir()), [])

    def test_newline_and_absolute_reference_paths_are_rejected(self):
        for value in ('abc\n', '..', '/root', 'a/b'):
            self.assert_status(400, lambda value=value: self.server.seg(value))
        for value in ('../a.tex', '/a.tex', 'a\\b.tex', 'a\nb.tex'):
            self.assert_status(400, lambda value=value: self.server.relative_file(value))

    def test_connection_failure_is_not_an_empty_reference_list(self):
        file = self.project(refs=[{'path': self.key, 'pdf': self.slug + '.pdf'}])
        before = file.read_bytes()
        with patch.object(self.server, 'ssh_run', return_value=subprocess.CompletedProcess([], 255, '', 'offline')):
            self.assert_status(502, lambda: self.server.read_refs_doc('demo'))
            self.assert_status(502, lambda: self.server.add_project({'project': 'demo'}))
        self.assertEqual(file.read_bytes(), before)

    def test_corrupt_refs_are_not_overwritten(self):
        file = self.project()
        for invalid in ('', '{broken', '{"references": null}', '{"references":[null]}'):
            file.write_text(invalid)
            self.assert_status(500, lambda: self.server.read_refs_doc('demo'))
            self.assertEqual(file.read_text(), invalid)

    def test_refs_compare_and_swap_preserves_external_edit(self):
        file = self.project()
        doc, _ = self.server.read_refs_doc('demo')
        external = {'library': str(self.remote), 'references': [], 'external': True}
        file.write_text(json.dumps(external))
        doc['references'].append({'path': self.key, 'pdf': self.slug + '.pdf'})
        self.assert_status(409, lambda: self.server.write_refs_doc('demo', doc))
        self.assertEqual(json.loads(file.read_text()), external)
        self.assertEqual(list(file.parent.glob('*.tmp')), [])

    def test_crlf_reference_revision_hashes_original_bytes(self):
        file = self.project()
        file.write_bytes(b'{\r\n  "references": []\r\n}\r\n')
        doc, _ = self.server.read_refs_doc('demo')
        self.server.write_refs_doc('demo', doc)
        self.assertEqual(json.loads(file.read_bytes())['references'], [])

    def test_null_documents_are_invalid_not_missing(self):
        file = self.project()
        file.write_text('null')
        self.assert_status(500, lambda: self.server.add_project({'project': 'demo'}))
        self.assertEqual(file.read_text(), 'null')
        self.seed()
        meta = self.paper / 'meta.json'
        meta.write_text('null')
        self.assert_status(500, lambda: self.save())
        self.assertEqual(meta.read_text(), 'null')

    def test_source_update_repairs_empty_pdf_metadata_without_dropping_reference(self):
        meta = self.seed()
        meta['pdf'] = ''
        (self.paper / 'meta.json').write_text(json.dumps(meta))
        self.project(refs=[{'path': self.key, 'pdf': self.slug + '.pdf'}])
        result = self.save(pdf=None, source=self.bundle())
        self.assertEqual(result['meta']['pdf'], self.slug + '.pdf')
        doc, _ = self.server.read_refs_doc('demo')
        self.assertEqual(doc['references'][0]['pdf'], self.slug + '.pdf')

    def test_source_update_keeps_legacy_pdf_filename(self):
        meta = self.seed()
        (self.paper / (self.slug + '.pdf')).rename(self.paper / 'paper.pdf')
        meta.pop('pdf')
        (self.paper / 'meta.json').write_text(json.dumps(meta))
        result = self.save(pdf=None, source=self.bundle())
        self.assertEqual(result['meta']['pdf'], 'paper.pdf')
        self.assertTrue((self.paper / 'paper.pdf').exists())

    def test_put_rejects_stale_revision(self):
        self.project()
        self.assert_status(409, lambda: self.server.put_refs({'project': 'demo', 'references': [], 'revision': 'stale'}))

    def test_put_quotes_shell_characters_without_execution(self):
        self.seed()
        marker = self.root / 'executed'
        filename = 'source/$(touch executed).tex'
        (self.paper / filename).write_text('safe')
        self.project()
        doc, _ = self.server.read_refs_doc('demo')
        result = self.server.put_refs({'project': 'demo', 'revision': doc.revision,
                                      'references': [{'path': self.key, 'entry': filename}]})
        self.assertTrue(result['ok'])
        self.assertFalse(marker.exists())

    def test_project_refs_sync_code_link_and_drop_it_when_cleared(self):
        meta = self.seed()
        s = self.server
        link = 'https://github.com/u/repo'
        s.edit_paper({'old': meta, 'meta': dict(meta, code=link),
                      'revision': s.get_research()['revision']})
        self.project()
        added = s.add_ref({'project': 'demo', 'path': self.key})
        self.assertEqual(added['ref']['code'], link)
        current = s.get_refs('demo')['references'][0]
        self.assertEqual(current['current']['code'], link)
        self.assertFalse(current['stale'])
        new_link = 'https://gitlab.com/u/repo'
        s.edit_paper({'old': meta, 'meta': dict(meta, code=new_link),
                      'revision': s.get_research()['revision']})
        saved, _ = s.read_refs_doc('demo')
        self.assertEqual(saved['references'][0]['code'], new_link)
        # Simulate an external meta.json change: sync must follow the library.
        stored = json.loads((self.paper / 'meta.json').read_text())
        (self.paper / 'meta.json').write_text(json.dumps(dict(stored, code='')))
        self.assertTrue(s.get_refs('demo')['references'][0]['stale'])
        result = s.sync_refs({'project': 'demo', 'path': self.key})
        self.assertEqual(result['updated'][0]['changes']['code'], '')
        saved, _ = s.read_refs_doc('demo')
        self.assertNotIn('code', saved['references'][0])
        self.assertFalse(s.get_refs('demo')['references'][0]['stale'])

    def test_put_refs_rejects_invalid_code_links(self):
        self.seed()
        self.project()
        doc, _ = self.server.read_refs_doc('demo')
        for bad in ('javascript:alert(1)', 'ftp://example.com/x', 'https://x y'):
            self.assert_status(400, lambda bad=bad: self.server.put_refs({
                'project': 'demo', 'revision': doc.revision,
                'references': [{'path': self.key, 'pdf': self.slug + '.pdf', 'code': bad}]}))

    def test_put_preserves_missing_existing_reference_for_note_edit(self):
        self.project(refs=[{'path': self.key, 'pdf': self.slug + '.pdf'}])
        doc, _ = self.server.read_refs_doc('demo')
        ref = dict(doc['references'][0], note='new note')
        self.server.put_refs({'project': 'demo', 'revision': doc.revision, 'references': [ref]})
        saved, _ = self.server.read_refs_doc('demo')
        self.assertEqual(saved['references'][0]['note'], 'new note')

    def test_missing_project_dir_requires_confirmation_before_registering(self):
        s = self.server
        with self.assertRaises(HTTPException) as error:
            s.add_project({'project': 'new/SRNet'})
        self.assertEqual(error.exception.status_code, 404)
        self.assertTrue(error.exception.detail['missing_dir'])
        self.assertEqual(s.load_projects(), [])
        self.assertFalse((self.workshop / 'new/SRNet').exists())
        result = s.add_project({'project': 'new/SRNet', 'allow_missing': True})
        self.assertEqual(result['project'], 'new/SRNet')
        self.assertTrue(result['missing'])
        self.assertFalse(result['created'])
        self.assertEqual(s.load_projects(), ['new/SRNet'])
        self.assertFalse((self.workshop / 'new/SRNet').exists())
        listed = s.projects()['items'][0]
        self.assertEqual((listed['exists'], listed['has_refs'], listed['count']), (False, False, 0))

    def test_first_reference_creates_confirmed_missing_project_dir(self):
        self.seed()
        s = self.server
        s.save_projects(['demo'])
        with self.assertRaises(HTTPException) as error:
            s.add_ref({'project': 'demo', 'path': self.key})
        self.assertEqual(error.exception.status_code, 409)
        self.assertTrue(error.exception.detail['missing_dir'])
        self.assertFalse((self.workshop / 'demo').exists())
        result = s.add_ref({'project': 'demo', 'path': self.key, 'create_missing': True})
        self.assertTrue(result['created_dir'])
        doc, found = s.read_refs_doc('demo')
        self.assertTrue(found)
        self.assertEqual(doc['references'][0]['path'], self.key)
        self.assertEqual(doc['references'][0]['pdf'], self.slug + '.pdf')
        self.assertEqual(result['created_dir'], True)
        self.assertFalse(s.add_ref({'project': 'demo', 'path': self.key,
                                    'update': True})['created_dir'])

    def test_local_projects_register_existing_absolute_dirs_only(self):
        s = self.server
        self.assert_status(400, lambda: s.add_project({'project': 'rel/path', 'source': 'local'}))
        missing = self.root / 'local-projects' / 'missing'
        with self.assertRaises(HTTPException) as error:
            s.add_project({'project': str(missing), 'source': 'local'})
        self.assertEqual(error.exception.status_code, 404)
        self.assertTrue(error.exception.detail['missing_dir'])
        self.assertEqual(s.load_projects('local'), [])
        self.assertFalse(missing.exists())
        folder = self.root / 'local-projects' / 'alpha'
        folder.mkdir(parents=True)
        result = s.add_project({'project': str(folder), 'source': 'local'})
        self.assertEqual((result['created'], result['missing']), (True, False))
        self.assertEqual(s.load_projects('local'), [str(folder)])
        self.assertEqual(s.load_projects(), [])
        data = json.loads((folder / 'reference-tex.json').read_text())
        self.assertEqual(data['library'], str(self.local))
        self.assertEqual(data['read_order'], ['entry', 'pdf'])
        listed = s.projects('local')
        self.assertEqual(listed['source'], 'local')
        self.assertEqual(listed['items'][0]['project'], str(folder))
        self.assertTrue(listed['items'][0]['exists'])
        self.assertEqual(s.projects()['items'], [])

    def test_local_project_refs_use_local_pdf_only(self):
        self.seed()
        self.seed_local()
        s = self.server
        folder = self.local_project()
        result = s.add_ref({'project': str(folder), 'source': 'local', 'path': self.key})
        self.assertEqual(result['source'], 'local')
        self.assertFalse(result['created_dir'])
        doc, found = s.read_refs_doc(str(folder), 'local')
        self.assertTrue(found)
        ref = doc['references'][0]
        self.assertEqual(ref['pdf'], self.slug + '.pdf')
        self.assertNotIn('entry', ref)
        self.assertNotIn('code', ref)
        self.assertEqual(doc['library'], str(self.local))
        self.assertEqual(json.loads((folder / 'reference-tex.json').read_text())['library'],
                         str(self.local))
        data = s.get_refs(str(folder), 'local')
        self.assertEqual(data['library'], str(self.local))
        self.assertEqual(data['file'], str(folder / 'reference-tex.json'))
        self.assertFalse(data['references'][0]['stale'])
        self.assertFalse(data['references'][0]['missing'])
        self.assert_status(409, lambda: s.add_ref({
            'project': str(folder), 'source': 'local', 'path': self.key}))
        shutil.rmtree(folder)
        with self.assertRaises(HTTPException) as error:
            s.add_ref({'project': str(folder), 'source': 'local', 'path': self.key})
        self.assertEqual(error.exception.status_code, 409)
        self.assertTrue(error.exception.detail['missing_dir'])
        self.assertFalse(folder.exists())

    def test_local_project_rejects_refs_without_local_pdf(self):
        self.seed()
        s = self.server
        folder = self.local_project()
        with self.assertRaises(HTTPException) as error:
            s.add_ref({'project': str(folder), 'source': 'local', 'path': self.key})
        self.assertEqual(error.exception.status_code, 404)
        self.assertIn('pdf', str(error.exception.detail))

    def test_local_project_file_listing_preview_and_symlink_fence(self):
        s = self.server
        folder = self.local_project('files')
        (folder / 'paper.pdf').write_bytes(b'%PDF-1.7')
        (folder / 'figure.png').write_bytes(b'\x89PNG')
        (folder / '.hidden').write_text('x')
        (folder / 'src').mkdir()
        (folder / 'src' / 'main.tex').write_text('x')
        os.symlink(self.local, folder / 'link')
        os.symlink(folder / 'paper.pdf', folder / 'paper-link.pdf')
        listing = s.project_files(str(folder), '', 'pdf,png', 'local')
        names = [entry['name'] for entry in listing['entries']]
        self.assertIn('paper.pdf', names)
        self.assertIn('src', names)
        self.assertNotIn('link', names)
        self.assertNotIn('paper-link.pdf', names)
        self.assertNotIn('.hidden', names)
        recent = s.project_recent(str(folder), 'pdf', 10, 'local')
        self.assertEqual([item['path'] for item in recent['files']], ['paper.pdf'])
        response = s.project_file(str(folder), 'paper.pdf', 'local')
        self.assertEqual(Path(response.path), folder / 'paper.pdf')
        self.assertEqual(response.media_type, 'application/pdf')
        self.assert_status(400, lambda: s.project_file(str(folder), 'paper-link.pdf', 'local'))
        self.assert_status(400, lambda: s.project_file(str(folder), '../outside.pdf', 'local'))

    def test_local_refs_follow_paper_and_knowledge_updates(self):
        self.seed()
        self.seed_local()
        s = self.server
        folder = self.local_project()
        s.add_ref({'project': str(folder), 'source': 'local', 'path': self.key})
        meta = dict(topic=self.topic, year=int(self.year), slug=self.slug,
                    title='Updated title', pdf=self.slug + '.pdf',
                    has_pdf=True, has_source=True, entry='source/main.tex',
                    code='https://example.com/code')
        touched, errors = s.sync_projects_for_paper(self.key, meta)
        self.assertEqual(errors, [])
        self.assertIn(str(folder), touched)
        doc, _ = s.read_refs_doc(str(folder), 'local')
        ref = doc['references'][0]
        self.assertEqual(ref['title'], 'Updated title')
        self.assertNotIn('entry', ref)
        self.assertNotIn('code', ref)
        item_id = 'a' * 32
        doc['references'].append({'kind': 'knowledge', 'path': f'_knowledge/{item_id}',
                                  'entry': 'note.md'})
        s.write_refs_doc(str(folder), doc, 'local')
        touched, errors = s.sync_projects_for_knowledge(
            item_id, {'title': 'My note', 'kind': 'note', 'url': ''})
        self.assertEqual(errors, [])
        self.assertIn(str(folder), touched)
        saved, _ = s.read_refs_doc(str(folder), 'local')
        self.assertEqual(saved['references'][-1]['title'], 'My note')
        self.assertEqual(saved['references'][-1]['entry'], 'note.md')

    def test_read_order_is_written_and_legacy_files_get_it_on_save(self):
        self.project(refs=[{'path': self.key, 'pdf': self.slug + '.pdf'}])
        path = self.workshop / 'demo' / 'reference-tex.json'
        legacy = json.loads(path.read_text())
        self.assertNotIn('read_order', legacy)
        doc, _ = self.server.read_refs_doc('demo')
        self.assertEqual(doc['read_order'], ['entry', 'pdf'])
        self.assertEqual(list(doc)[:3], ['library', 'read_order', 'references'])
        self.server.put_refs({'project': 'demo', 'revision': doc.revision,
                              'references': doc['references']})
        saved = json.loads(path.read_text())
        self.assertEqual(saved['read_order'], ['entry', 'pdf'])
        self.assertEqual(list(saved)[:3], ['library', 'read_order', 'references'])

    def test_corrupt_local_state_does_not_get_silently_cleared(self):
        self.server.STARS_FILE.write_text('{broken')
        self.assert_status(500, lambda: self.server.set_star({'key': self.key, 'starred': True}))
        self.assertEqual(self.server.STARS_FILE.read_text(), '{broken')

    def test_parallel_star_updates_are_not_lost(self):
        keys = [f'TS/2026/2026-ICML-Li-P{i}' for i in range(20)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda key: self.server.set_star({'key': key, 'starred': True}), keys))
        self.assertEqual(set(self.server.load_stars()), set(keys))

    def test_local_atomic_failure_preserves_state(self):
        self.server.save_stars([self.key])
        with patch.object(self.server.os, 'replace', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                self.server.save_stars([])
        self.assertEqual(self.server.load_stars(), [self.key])
        self.assertEqual(list((self.root / 'config').glob('.stars.json*')), [])

    def test_sync_failure_is_returned_not_swallowed(self):
        meta = self.seed()
        self.project(refs=[{'path': self.key, 'pdf': self.slug + '.pdf', 'title': 'old'}])
        with patch.object(self.server, 'write_refs_doc', side_effect=HTTPException(502, 'offline')):
            updated, errors = self.server.sync_projects_for_paper(self.key, meta)
        self.assertEqual(updated, [])
        self.assertEqual(len(errors), 1)
        self.assertIn('demo', errors[0])

    def test_delete_moves_to_recovery_and_preserves_refs(self):
        self.seed()
        file = self.project(refs=[{'path': self.key, 'pdf': self.slug + '.pdf'}])
        before = file.read_bytes()
        local = self.local / self.key
        local.mkdir(parents=True)
        (local / (self.slug + '.pdf')).write_bytes(b'%PDF-1.4 local')
        result = self.server.delete_paper('both', self.topic, self.year, self.slug, self.slug)
        self.assertFalse(self.paper.exists())
        self.assertFalse(local.exists())
        self.assertTrue((Path(result['recovery']['remote']) / 'source/main.tex').exists())
        self.assertTrue((Path(result['recovery']['local']) / (self.slug + '.pdf')).exists())
        self.assertEqual(file.read_bytes(), before)

    def test_local_scan_shows_cached_remote_titles(self):
        self.seed()
        local = self.local / self.key
        local.mkdir(parents=True)
        (local / (self.slug + '.pdf')).write_bytes(b'%PDF-1.4 local')
        self.assertEqual(self.server.scan_local()[self.key]['title'], '')
        self.server.save_title_cache({self.key: {'title': 'Cached Title', 'venue': 'ICML'}})
        item = self.server.scan_local()[self.key]
        self.assertEqual((item['title'], item['venue']), ('Cached Title', 'ICML'))
        self.assertFalse(item['has_meta'])
        self.assertEqual(item['pdf'], self.slug + '.pdf')

    def test_library_exposes_added_at_from_meta_and_local_mtime(self):
        meta = self.seed()
        meta['added_at'] = '2026-01-02T03:04:05+00:00'
        (self.paper / 'meta.json').write_text(json.dumps(meta))
        remote = self.server.library('remote')['items']
        self.assertEqual(remote[0]['added_at'], '2026-01-02T03:04:05+00:00')
        local = self.local / self.key
        local.mkdir(parents=True)
        pdf = local / (self.slug + '.pdf')
        pdf.write_bytes(b'%PDF-1.4 local')
        os.utime(pdf, (1700000000, 1700000000))
        self.assertEqual(self.server.library('local')['items'][0]['added_at'],
                         '2023-11-14T22:13:20+00:00')

    def test_local_scan_survives_broken_title_cache(self):
        self.seed()
        local = self.local / self.key
        local.mkdir(parents=True)
        (local / (self.slug + '.pdf')).write_bytes(b'%PDF-1.4 local')
        path = self.server.titles_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{broken')
        item = self.server.scan_local()[self.key]
        self.assertTrue(item['has_pdf'])
        self.assertEqual(item['title'], '')

    def test_edit_renames_with_recovery_and_updates_refs(self):
        meta = self.seed()
        self.project(refs=[{'path': self.key, 'pdf': self.slug + '.pdf', 'note': 'keep'}])
        new_slug = '2026-ICML-Li-Renamed'
        result = self.server.edit_paper({'old': meta, 'meta': dict(meta, slug=new_slug),
                                         'revision': self.server.get_research()['revision']})
        new_path = self.remote / self.topic / self.year / new_slug
        self.assertFalse(self.paper.exists())
        self.assertTrue((new_path / (new_slug + '.pdf')).exists())
        self.assertTrue(Path(result['recovery_path']).exists())
        refs, _ = self.server.read_refs_doc('demo')
        self.assertEqual(refs['references'][0]['path'], result['key'])
        self.assertEqual(refs['references'][0]['note'], 'keep')
        self.assertFalse(result['warnings'])

    def test_edit_stores_validated_code_link_on_remote(self):
        meta = self.seed()
        s = self.server
        revision = s.get_research()['revision']
        s.edit_paper({'old': meta, 'meta': dict(meta, code='https://github.com/u/repo'),
                      'revision': revision})
        stored = json.loads((self.paper / 'meta.json').read_text())
        self.assertEqual(stored['code'], 'https://github.com/u/repo')
        self.assertEqual(stored['custom'], 'preserve')
        for bad in ('javascript:alert(1)', 'ftp://example.com/x', 'https://x y', 'x' * 501):
            with self.assertRaises(HTTPException) as error:
                s.edit_paper({'old': meta, 'meta': dict(meta, code=bad),
                              'revision': s.get_research()['revision']})
            self.assertEqual(error.exception.status_code, 400)
        s.edit_paper({'old': meta, 'meta': dict(meta, code=''),
                      'revision': s.get_research()['revision']})
        self.assertEqual(json.loads((self.paper / 'meta.json').read_text())['code'], '')

    def local_copy(self):
        local = self.local / self.key
        local.mkdir(parents=True)
        (local / (self.slug + '.pdf')).write_bytes(b'%PDF-1.4 local')
        return local

    def test_topic_rename_moves_both_sides_and_syncs_state(self):
        self.seed()
        local = self.local_copy()
        self.project(refs=[{'path': self.key, 'pdf': self.slug + '.pdf', 'note': 'keep'}])
        self.server.save_stars([self.key])
        result = self.server.rename_topic({'topic': self.topic, 'new': 'NewTS',
                                           'revision': self.server.get_research()['revision']})
        self.assertFalse((self.remote / self.topic).exists())
        new_paper = self.remote / 'NewTS' / self.year / self.slug
        meta = json.loads((new_paper / 'meta.json').read_text())
        self.assertEqual(meta['topic'], 'NewTS')
        self.assertEqual(meta['custom'], 'preserve')
        self.assertTrue((new_paper / (self.slug + '.pdf')).exists())
        self.assertFalse(local.exists())
        self.assertTrue((self.local / 'NewTS' / self.year / self.slug / (self.slug + '.pdf')).exists())
        self.assertEqual(self.server.load_stars(), ['NewTS/' + self.year + '/' + self.slug])
        refs, _ = self.server.read_refs_doc('demo')
        self.assertEqual(refs['references'][0]['path'], 'NewTS/' + self.year + '/' + self.slug)
        self.assertEqual(refs['references'][0]['note'], 'keep')
        self.assertEqual(result['topic'], 'NewTS')
        self.assertEqual(result['papers'], 1)
        self.assertEqual(result['meta_updated'], 1)
        self.assertTrue(result['local_moved'])
        self.assertEqual(result['projects_updated'], ['demo'])
        self.assertFalse(result['warnings'])

    def test_topic_rename_conflict_leaves_both_sides_untouched(self):
        self.seed()
        self.local_copy()
        conflict = self.remote / 'Other' / self.year / self.slug
        conflict.mkdir(parents=True)
        marker = conflict / 'keep.txt'
        marker.write_text('keep')
        self.assert_status(409, lambda: self.server.rename_topic(
            {'topic': self.topic, 'new': 'Other'}))
        self.assertTrue(self.paper.exists())
        self.assertTrue((self.local / self.key).exists())
        self.assertTrue(marker.exists())
        self.assertEqual(json.loads((self.paper / 'meta.json').read_text())['topic'], self.topic)
        self.assertFalse((self.local / 'Other').exists())

    def test_topic_operations_reject_invalid_names_and_confirmation(self):
        self.seed()
        self.assert_status(400, lambda: self.server.rename_topic(
            {'topic': self.topic, 'new': '../escape'}))
        self.assert_status(400, lambda: self.server.rename_topic(
            {'topic': self.topic, 'new': self.topic}))
        self.assert_status(400, lambda: self.server.delete_topic(self.topic, 'wrong'))
        self.assertTrue(self.paper.exists())

    def test_topic_rename_rejects_nested_symlink(self):
        self.seed()
        (self.paper / 'link').symlink_to(self.root / 'outside')
        self.assert_status(400, lambda: self.server.rename_topic(
            {'topic': self.topic, 'new': 'NewTS'}))
        self.assertTrue(self.paper.exists())
        self.assertFalse((self.remote / 'NewTS').exists())

    def test_topic_delete_moves_to_recovery_and_keeps_refs(self):
        self.seed()
        local = self.local_copy()
        file = self.project(refs=[{'path': self.key, 'pdf': self.slug + '.pdf', 'note': 'keep'}])
        before = file.read_bytes()
        self.server.save_stars([self.key, 'Other/2026/other'])
        result = self.server.delete_topic(self.topic, self.topic)
        self.assertFalse((self.remote / self.topic).exists())
        self.assertFalse(local.exists())
        self.assertTrue((Path(result['recovery']['remote']) / self.year / self.slug
                         / 'source/main.tex').exists())
        self.assertTrue((Path(result['recovery']['local']) / self.year / self.slug
                         / (self.slug + '.pdf')).exists())
        self.assertEqual(self.server.load_stars(), ['Other/2026/other'])
        self.assertEqual(file.read_bytes(), before)
        self.assertIn('回收区', result['notice'])
        self.assertFalse(result['warnings'])

    def request(self, method, path, body=b'', headers=None, limit=1024):
        from request_security import LocalRequestGuard
        route, _, query = path.partition('?')
        scope = dict(type='http', asgi={'version': '3.0'}, http_version='1.1',
                     method=method, scheme='http', path=route, raw_path=route.encode(),
                     query_string=query.encode(), root_path='', server=('127.0.0.1', 8817),
                     client=('127.0.0.1', 40000), headers=[(b'host', b'127.0.0.1:8817')] + (headers or []))
        messages = []
        chunks = [dict(type='http.request', body=body[:1], more_body=True),
                  dict(type='http.request', body=body[1:], more_body=False)]
        async def receive():
            return chunks.pop(0) if chunks else {'type': 'http.disconnect'}
        async def send(message):
            messages.append(message)
        asyncio.run(LocalRequestGuard(self.server.app, port=8817, max_body_bytes=limit)(scope, receive, send))
        status = next(m['status'] for m in messages if m['type'] == 'http.response.start')
        return status, b''.join(m.get('body', b'') for m in messages)

    def test_research_http_routes_shapes_and_write_guard(self):
        self.local_copy()
        other = 'TS/2026/other'
        (self.local / other).mkdir(parents=True)
        headers = [(b'content-type', b'application/json'), (b'x-papertex-request', b'1')]
        for route, payload in (
                ('/api/reading', dict(key=self.key, status='read', note='HTTP')),
                ('/api/citation', dict(key=self.key, fields={'title': 'HTTP title'})),
                ('/api/versions/link', dict(key=self.key, other_key=other, label='', other_label='other')),
                ('/api/versions/unlink', dict(key=self.key))):
            payload['revision'] = self.server.get_research()['revision']
            body = json.dumps(payload).encode()
            status, _ = self.request('POST', route, body, headers[:1])
            self.assertEqual(status, 403, route)
            status, response = self.request('POST', route, body, headers)
            self.assertEqual(status, 200, response)
            self.assertEqual(set(json.loads(response)), {'items', 'revision', 'ok'}
                             if route == '/api/reading' else {'items', 'revision'})
            if route != '/api/versions/link':
                status, _ = self.request('POST', route, body, headers)
                self.assertEqual(status, 409, route)
        status, response = self.request('GET', '/api/research')
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(response)), {'items', 'revision'})
        status, response = self.request('GET', '/api/citation?key=' + self.key)
        self.assertEqual(status, 200, response)
        self.assertEqual(set(json.loads(response)),
                         {'key', 'cite_key', 'cite', 'bibtex', 'fields', 'warnings', 'complete'})
        self.project(refs=[{'path': self.key}])
        status, response = self.request('GET', '/api/project/bibliography?project=demo')
        self.assertEqual(status, 200, response)
        self.assertEqual(set(json.loads(response)), {'bibtex', 'filename', 'warnings', 'count'})

    def test_citation_saved_only_fallback_but_confirmed_missing_is_404(self):
        self.seed()
        s = self.server
        self.research_write(s.set_citation, key=self.key, fields={'title': 'Saved only'})
        with patch.object(s, 'remote_exists', side_effect=HTTPException(502, 'offline')):
            citation = s.get_citation(self.key)
        self.assertEqual(citation['fields']['title'], 'Saved only')
        self.assertTrue(any('是否仍存在未核对' in w for w in citation['warnings']))
        shutil.rmtree(self.paper)
        self.assert_status(404, lambda: s.get_citation(self.key))

    def test_citation_key_follows_rename_without_record(self):
        meta = self.seed()
        s = self.server
        self.assertEqual(s.get_citation(self.key)['cite_key'], self.slug)
        self.assertEqual(s.get_research()['items'], {})
        result = self.research_write(s.edit_paper, old=meta, meta=dict(meta, slug='2026-ICML-Li-New'))
        self.assertEqual(s.get_citation(result['key'])['cite_key'], '2026-ICML-Li-New')

    def test_research_topic_delete_retains_version_records(self):
        self.seed()
        self.local_copy()
        s = self.server
        other = 'TS/2026/other'
        (self.local / other).mkdir(parents=True)
        self.research_write(s.link_versions, key=self.key, other_key=other,
                             label='conference', other_label='journal')
        before = s.get_research()
        s.delete_topic('TS', 'TS')
        self.assertEqual(s.get_research(), before)
        self.research_write(s.unlink_versions, key=self.key)
        self.assertNotIn('version_group', s.get_research()['items'][other])

    def test_bodyless_write_endpoints_drain_before_side_effects(self):
        self.seed()
        self.project()
        paths = [('DELETE', '/api/projects?project=demo'),
                 ('DELETE', f'/api/paper?source=remote&topic={self.topic}&year={self.year}&slug={self.slug}&confirm={self.slug}'),
                 ('DELETE', f'/api/topic?topic={self.topic}&confirm={self.topic}'),
                 ('POST', f'/api/open?topic={self.topic}&year={self.year}&slug={self.slug}')]
        with patch.object(self.server.subprocess, 'Popen') as opened:
            for method, path in paths:
                status, _ = self.request(method, path, b'oversized', [(b'x-papertex-request', b'1')], limit=2)
                self.assertEqual(status, 413)
            opened.assert_not_called()
        self.assertTrue(self.paper.exists())
        self.assertEqual(self.server.load_projects(), ['demo'])

    def test_topic_endpoints_route_and_require_write_header(self):
        self.seed()
        self.local_copy()
        self.project(refs=[{'path': self.key, 'pdf': self.slug + '.pdf'}])
        self.server.save_stars([self.key])
        body = json.dumps({'topic': self.topic, 'new': 'NewTS',
                           'revision': self.server.get_research()['revision']}).encode()
        headers = [(b'content-type', b'application/json')]
        status, _ = self.request('POST', '/api/topic/rename', body, headers)
        self.assertEqual(status, 403)
        self.assertTrue(self.paper.exists())
        headers.append((b'x-papertex-request', b'1'))
        status, response = self.request('POST', '/api/topic/rename', body, headers)
        self.assertEqual(status, 200, response)
        self.assertTrue((self.remote / 'NewTS' / self.year / self.slug).exists())
        self.assertFalse((self.remote / self.topic).exists())
        refs, _ = self.server.read_refs_doc('demo')
        self.assertEqual(refs['references'][0]['path'], 'NewTS/' + self.year + '/' + self.slug)
        status, response = self.request(
            'DELETE', '/api/topic?topic=NewTS&confirm=NewTS', b'',
            [(b'x-papertex-request', b'1')])
        self.assertEqual(status, 200, response)
        self.assertFalse((self.remote / 'NewTS').exists())
        self.assertFalse((self.local / 'NewTS').exists())
        self.assertEqual(self.server.load_stars(), [])

    def test_write_api_requires_token_and_accepts_same_origin_json(self):
        body = json.dumps({'key': self.key, 'starred': True}).encode()
        headers = [(b'content-type', b'application/json')]
        status, _ = self.request('POST', '/api/star', body, headers)
        self.assertEqual(status, 403)
        self.assertEqual(self.server.load_stars(), [])
        headers.extend([(b'x-papertex-request', b'1'), (b'origin', b'http://127.0.0.1:8817')])
        status, _ = self.request('POST', '/api/star', body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(self.server.load_stars(), [self.key])

    def multipart(self, archive):
        parts = []
        for name, value in dict(topic=self.topic, year=self.year, slug=self.slug).items():
            parts.append(f'--papertex-test\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(b'--papertex-test\r\nContent-Disposition: form-data; name="source_archive"; filename="source.zip"\r\nContent-Type: application/zip\r\n\r\n' + archive + b'\r\n--papertex-test--\r\n')
        return b''.join(parts)

    def test_multipart_supplement_limits_before_mutation(self):
        self.seed()
        body = self.multipart(self.bundle())
        headers = [(b'x-papertex-request', b'1'),
                   (b'content-type', b'multipart/form-data; boundary=papertex-test')]
        status, _ = self.request('POST', '/api/supplement', body, headers, limit=len(body) - 1)
        self.assertEqual(status, 413)
        self.assertEqual((self.paper / 'source/main.tex').read_text(), 'old source')
        status, response = self.request('POST', '/api/supplement', body, headers, limit=len(body))
        self.assertEqual(status, 200, response)
        self.assertEqual((self.paper / 'source/main.tex').read_text(), 'new source')

    def test_multipart_rejects_traversal_without_losing_original(self):
        self.seed()
        headers = [(b'x-papertex-request', b'1'),
                   (b'content-type', b'multipart/form-data; boundary=papertex-test')]
        status, _ = self.request('POST', '/api/supplement', self.multipart(self.bundle('../escape.tex')), headers)
        self.assertEqual(status, 400)
        self.assertEqual((self.paper / 'source/main.tex').read_text(), 'old source')

    def test_multipart_progress_records_success_and_failure(self):
        self.seed()
        headers = [(b'x-papertex-request', b'1'),
                   (b'content-type', b'multipart/form-data; boundary=papertex-test')]
        for filename, expected in [('main.tex', 200), ('../bad.tex', 400)]:
            operation = uuid4().hex
            status, _ = self.request('POST', '/api/supplement?operation_id=' + operation,
                                     self.multipart(self.bundle(filename)), headers)
            self.assertEqual(status, expected)
            status, raw = self.request('GET', '/api/operations/' + operation)
            self.assertEqual(status, 200)
            progress = json.loads(raw)
            self.assertEqual(progress['status'], 'completed' if expected == 200 else 'failed')
            self.assertEqual(progress['details'], {})

    def test_progress_id_validation_happens_before_storage_mutation(self):
        self.seed()
        headers = [(b'x-papertex-request', b'1'),
                   (b'content-type', b'multipart/form-data; boundary=papertex-test')]
        status, _ = self.request('POST', '/api/supplement?operation_id=bad',
                                 self.multipart(self.bundle()), headers)
        self.assertEqual(status, 400)
        self.assertEqual((self.paper / 'source/main.tex').read_text(), 'old source')

    def test_progress_is_readable_while_operation_waits_for_write_lock(self):
        self.seed()
        self.project()
        operation = uuid4().hex
        queued = threading.Event()
        update = self.server.update_progress
        def notify(message, **details):
            update(message, **details)
            if message == '等待其他写操作完成':
                queued.set()
        with patch.object(self.server, 'update_progress', notify), ThreadPoolExecutor(max_workers=1) as pool:
            with self.server.mutation_lock():
                future = pool.submit(self.server.add_ref, {'project': 'demo', 'path': self.key}, operation_id=operation)
                self.assertTrue(queued.wait(2))
                status, raw = self.request('GET', '/api/operations/' + operation)
                self.assertEqual(status, 200)
                progress = json.loads(raw)
                self.assertEqual(progress['status'], 'running')
                self.assertEqual(progress['message'], '等待其他写操作完成')
            self.assertTrue(future.result(timeout=3)['ok'])

    def test_metadata_write_failure_preserves_live_paper(self):
        self.seed()
        with patch.object(self.server, 'write_meta', side_effect=HTTPException(502, 'write failed')):
            self.assert_status(502, lambda: self.save(source=self.bundle()))
        self.assertEqual((self.paper / 'source/main.tex').read_text(), 'old source')
        self.assertEqual((self.paper / (self.slug + '.pdf')).read_bytes(), b'%PDF-1.4 old')

    def test_edit_local_recovery_failure_reports_partial_success(self):
        meta = self.seed()
        local = self.local / self.key
        local.mkdir(parents=True)
        (local / (self.slug + '.pdf')).write_bytes(b'%PDF-1.4 local')
        with patch.object(self.server, 'local_recovery_path', side_effect=HTTPException(400, 'symlink')):
            result = self.server.edit_paper({'old': meta, 'meta': dict(meta, slug='2026-ICML-Li-New'),
                                             'revision': self.server.get_research()['revision']})
        self.assertTrue(result['ok'])
        self.assertTrue(result['warnings'])
        self.assertTrue(local.exists())
        self.assertTrue((self.remote / result['key']).exists())

    def test_parallel_reference_additions_preserve_both_updates(self):
        self.seed()
        self.project()
        second = self.remote / self.topic / self.year / '2026-ICML-Li-Second'
        shutil.copytree(self.paper, second)
        keys = [self.key, str(second.relative_to(self.remote))]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda key: self.server.add_ref({'project': 'demo', 'path': key}), keys))
        doc, _ = self.server.read_refs_doc('demo')
        self.assertEqual({r['path'] for r in doc['references']}, set(keys))

    def upload_with_arxiv(self, *, fetch_source, pdf=True, source=None):
        s, sent = self.server, {}

        def fake_save(topic, year, slug, pdf_data, source_data, source_name, copy_local, meta):
            sent.update(pdf=pdf_data, source=source_data, source_name=source_name, meta=meta)
            return {'meta': {'topic': topic, 'year': year, 'slug': slug},
                    'remote_path': 'remote', 'entry': '', 'warnings': []}

        def upload_file(data, filename):
            return UploadFile(file=io.BytesIO(data), filename=filename)

        with patch.object(s, 'arxiv_lookup', return_value={}), \
                patch.object(s, 'arxiv_pdf_bytes', return_value=b'%PDF-1.4 arxiv') as pdf_fetch, \
                patch.object(s, 'arxiv_source_bytes', return_value=(b'PK source', 'source.zip')) as src_fetch, \
                patch.object(s, 'save_paper', side_effect=fake_save):
            result = s.upload(
                topic=self.topic, year=self.year, slug=self.slug, title='', venue='',
                arxiv='', arxiv_url='https://arxiv.org/abs/2401.12345', force=True,
                copy_local=False, fetch_source=fetch_source, code='', bibtex='',
                openreview_url='', ieee_url='',
                pdf=upload_file(b'%PDF-1.4 local', 'local.pdf') if pdf else None,
                source_archive=upload_file(source, 'source.zip') if source else None,
            )
        return result, sent, pdf_fetch, src_fetch

    def test_pdf_upload_skips_arxiv_source_unless_switch_is_on(self):
        result, sent, pdf_fetch, src_fetch = self.upload_with_arxiv(fetch_source=False)
        self.assertEqual(pdf_fetch.call_count, 0)
        self.assertEqual(src_fetch.call_count, 0)
        self.assertEqual(sent['pdf'], b'%PDF-1.4 local')
        self.assertIsNone(sent['source'])
        self.assertFalse(result['pdf_fetched'])
        self.assertFalse(result['source_fetched'])

    def test_pdf_upload_fetches_arxiv_source_when_switch_is_on(self):
        result, sent, _, src_fetch = self.upload_with_arxiv(fetch_source=True)
        self.assertEqual(src_fetch.call_count, 1)
        self.assertEqual(sent['source'], b'PK source')
        self.assertEqual(sent['source_name'], 'source.zip')
        self.assertTrue(result['source_fetched'])

    def test_arxiv_only_import_still_fetches_pdf_and_source(self):
        result, sent, pdf_fetch, src_fetch = self.upload_with_arxiv(fetch_source=False, pdf=False)
        self.assertEqual(pdf_fetch.call_count, 1)
        self.assertEqual(src_fetch.call_count, 1)
        self.assertEqual(sent['pdf'], b'%PDF-1.4 arxiv')
        self.assertEqual(sent['source'], b'PK source')
        self.assertTrue(result['pdf_fetched'])
        self.assertTrue(result['source_fetched'])

    def test_attached_source_archive_wins_over_arxiv_fetch(self):
        result, sent, _, src_fetch = self.upload_with_arxiv(
            fetch_source=True, source=b'local archive')
        self.assertEqual(src_fetch.call_count, 0)
        self.assertEqual(sent['source'], b'local archive')
        self.assertEqual(sent['source_name'], 'source.zip')
        self.assertFalse(result['source_fetched'])

    def test_parse_bibtex_cleans_braces_and_accents(self):
        fields = self.server.parse_bibtex("""@inproceedings{smith2026,
          title = {A {BERT} Study of $x$},
          author = {Alice Smith and Jos{\\'e} Garc{\\'i}a},
          year = {2026},
          booktitle = "Proc. of ICML",
          doi = {10.1000/xyz}
        }""")
        self.assertEqual(fields, {
            'entry_type': 'inproceedings', 'title': 'A BERT Study of $x$',
            'authors': 'Alice Smith and José García', 'year': '2026',
            'venue': 'Proc. of ICML', 'doi': '10.1000/xyz',
        })

    def test_upload_stores_code_and_pasted_bibliography(self):
        s, sent = self.server, {}

        def fake_save(topic, year, slug, pdf_data, source_data, source_name, copy_local, meta):
            sent.update(meta)
            return {'meta': {'topic': topic, 'year': year, 'slug': slug}, 'remote_path': 'remote',
                    'entry': '', 'warnings': []}

        with patch.object(s, 'save_paper', side_effect=fake_save):
            result = s.upload(topic=self.topic, year=self.year, slug=self.slug, title='', venue='',
                              arxiv='', arxiv_url='', openreview_url='', ieee_url='',
                              code='https://github.com/example/repo',
                              bibtex='@article{k, title={Pasted Title}, author={A and B}, '
                                     'year={2026}, journal={TSP}}',
                              force=True, copy_local=False,
                              pdf=UploadFile(file=io.BytesIO(b'%PDF-1.4 local'), filename='p.pdf'),
                              source_archive=None)
        self.assertEqual(sent['code'], 'https://github.com/example/repo')
        self.assertEqual(sent['title'], 'Pasted Title')
        self.assertEqual(result['warnings'], [])
        record = s.get_research()['items'][self.key]
        self.assertEqual(record['bibliography'], {
            'entry_type': 'article', 'title': 'Pasted Title',
            'authors': 'A and B', 'year': '2026', 'venue': 'TSP'})

    def test_upload_keeps_existing_bibliography_values(self):
        self.seed()
        s = self.server
        s.set_citation({'key': self.key, 'fields': {'title': 'Manual title'},
                        'revision': s.get_research()['revision']})
        with patch.object(s, 'save_paper', return_value={'meta': {}, 'remote_path': 'remote',
                                                         'entry': '', 'warnings': []}):
            result = s.upload(topic=self.topic, year=self.year, slug=self.slug, title='', venue='',
                              arxiv='', arxiv_url='', openreview_url='', ieee_url='', code='', force=True,
                              copy_local=False,
                              bibtex='@article{k, title={Pasted Title}, author={A and B}}',
                              pdf=UploadFile(file=io.BytesIO(b'%PDF-1.4 local'), filename='p.pdf'),
                              source_archive=None)
        self.assertEqual(result['warnings'], [])
        self.assertEqual(s.get_research()['items'][self.key]['bibliography'],
                         {'title': 'Manual title', 'entry_type': 'article', 'authors': 'A and B'})

    def test_upload_warns_on_bad_bibtex_and_rejects_bad_code(self):
        s = self.server
        with self.assertRaises(HTTPException) as ctx:
            s.upload(topic=self.topic, year=self.year, slug=self.slug, title='', venue='',
                     arxiv='', arxiv_url='', openreview_url='', ieee_url='', code='javascript:alert(1)', force=True,
                     copy_local=False, pdf=None, source_archive=None)
        self.assertEqual(ctx.exception.status_code, 400)
        with patch.object(s, 'save_paper', return_value={'meta': {}, 'remote_path': 'remote',
                                                         'entry': '', 'warnings': []}):
            result = s.upload(topic=self.topic, year=self.year, slug=self.slug, title='', venue='',
                              arxiv='', arxiv_url='', openreview_url='', ieee_url='', code='',
                              bibtex='not a bibtex entry',
                              force=True, copy_local=False,
                              pdf=UploadFile(file=io.BytesIO(b'%PDF-1.4 local'), filename='p.pdf'),
                              source_archive=None)
        self.assertTrue(any('BibTeX' in w for w in result['warnings']))
        self.assertNotIn(self.key, s.get_research()['items'])

    def test_upload_from_openreview_fetches_pdf_and_saves_link(self):
        s, sent = self.server, {}

        def fake_save(topic, year, slug, pdf_data, source_data, source_name, copy_local, meta):
            sent.update(meta)
            sent['pdf'] = pdf_data
            return {'meta': {'topic': topic, 'year': year, 'slug': slug}, 'remote_path': 'remote',
                    'entry': '', 'warnings': []}

        lookup = {
            'openreview_id': 'VKGTGGcwl6',
            'openreview_url': 'https://openreview.net/forum?id=VKGTGGcwl6',
            'title': 'Paper Title', 'venue': 'ICLR', 'year': 2026, 'author': 'Lovelace',
            'keyword': 'Paper', 'arxiv_id': '', 'bibtex': '', 'abstract': '',
        }
        with patch.object(s, 'openreview_lookup', return_value=lookup), \
                patch.object(s.orx, 'pdf_bytes', return_value=b'%PDF-1.7 orx') as pdf_fetch, \
                patch.object(s, 'save_paper', side_effect=fake_save):
            result = s.upload(topic=self.topic, year=self.year, slug=self.slug, title='', venue='',
                              arxiv='', arxiv_url='', openreview_url='VKGTGGcwl6', ieee_url='', code='',
                              bibtex='', force=True, copy_local=False, fetch_source=False,
                              pdf=None, source_archive=None)
        self.assertEqual(sent['openreview'], 'https://openreview.net/forum?id=VKGTGGcwl6')
        self.assertEqual(sent['title'], 'Paper Title')
        self.assertEqual(sent['venue'], 'ICLR')
        self.assertEqual(sent['pdf'], b'%PDF-1.7 orx')
        pdf_fetch.assert_called_once_with('VKGTGGcwl6')
        self.assertTrue(result['pdf_fetched'])

    def test_duplicate_report_matches_openreview_link(self):
        other = self.remote / 'TS/2025/2025-ICLR-Doe-Other'
        other.mkdir(parents=True)
        (other / 'meta.json').write_text(json.dumps({
            'title': 'Other paper',
            'openreview': 'https://openreview.net/forum?id=VKGTGGcwl6',
        }))
        report = self.server.duplicate_report(openreview='VKGTGGcwl6')
        self.assertEqual([hit['key'] for hit in report['duplicates']],
                         ['TS/2025/2025-ICLR-Doe-Other'])
        self.assertEqual(report['duplicates'][0]['reasons'], ['openreview'])

    def test_upload_rejects_unsafe_openreview_link(self):
        with self.assertRaises(HTTPException) as ctx:
            self.server.upload(topic=self.topic, year=self.year, slug=self.slug, title='', venue='',
                               arxiv='', arxiv_url='', openreview_url='https://evil.example/forum?id=VKGTGGcwl6', ieee_url='',
                               code='', bibtex='', force=True, copy_local=False,
                               pdf=None, source_archive=None)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_duplicate_report_matches_doi_case_insensitively(self):
        other = self.remote / 'TS/2025/2025-TDSC-Doe-Other'
        other.mkdir(parents=True)
        (other / 'meta.json').write_text(json.dumps({
            'title': 'Other paper',
            'doi': '10.1109/TDSC.2022.3143493',
        }))
        report = self.server.duplicate_report(doi='10.1109/tdsc.2022.3143493')
        self.assertEqual([hit['key'] for hit in report['duplicates']],
                         ['TS/2025/2025-TDSC-Doe-Other'])
        self.assertEqual(report['duplicates'][0]['reasons'], ['doi'])

    def test_upload_from_ieee_fetches_pdf_and_saves_doi(self):
        s, sent = self.server, {}

        def fake_save(topic, year, slug, pdf_data, source_data, source_name, copy_local, meta):
            sent.update(meta)
            sent['pdf'] = pdf_data
            return {'meta': {'topic': topic, 'year': year, 'slug': slug}, 'remote_path': 'remote',
                    'entry': '', 'warnings': []}

        lookup = {
            'doi': '10.1109/TDSC.2022.3143493', 'arnumber': '9684703',
            'title': 'IEEE Paper', 'authors': ['Ada Lovelace'], 'year': 2023,
            'venue': 'TDSC', 'venue_raw': 'IEEE Transactions on Dependable and Secure Computing',
            'abstract': '', 'early_access': False, 'published': '',
        }
        with patch.object(s.ieee_xplore, 'lookup', return_value=lookup), \
                patch.object(s.ieee_xplore, 'pdf_bytes',
                             return_value=b'%PDF-1.7 ieee') as pdf_fetch, \
                patch.object(s, 'save_paper', side_effect=fake_save):
            result = s.upload(topic=self.topic, year=self.year, slug=self.slug, title='', venue='',
                              arxiv='', arxiv_url='', openreview_url='',
                              ieee_url='https://ieeexplore.ieee.org/document/9684703/',
                              code='', bibtex='', force=True, copy_local=False,
                              pdf=None, source_archive=None)
        self.assertEqual(sent['doi'], '10.1109/TDSC.2022.3143493')
        self.assertEqual(sent['title'], 'IEEE Paper')
        self.assertEqual(sent['venue'], 'TDSC')
        self.assertEqual(sent['pdf'], b'%PDF-1.7 ieee')
        self.assertTrue(result['pdf_fetched'])
        pdf_fetch.assert_called_once()

    def test_upload_rejects_non_ieee_link(self):
        with self.assertRaises(HTTPException) as ctx:
            self.server.upload(topic=self.topic, year=self.year, slug=self.slug, title='', venue='',
                               arxiv='', arxiv_url='', openreview_url='',
                               ieee_url='https://evil.example/document/9684703',
                               code='', bibtex='', force=True, copy_local=False,
                               pdf=None, source_archive=None)
        self.assertEqual(ctx.exception.status_code, 400)

    def project_files_setup(self):
        rel = 'demo/proj'
        folder = self.workshop / rel
        (folder / 'paper' / 'figures').mkdir(parents=True)
        (folder / 'references').mkdir()
        (folder / 'results').mkdir()
        (folder / 'paper' / 'main.pdf').write_bytes(b'%PDF-1.4 main')
        (folder / 'paper' / 'figures' / 'fig.png').write_bytes(b'\x89PNG\r\n\x1a\nfig')
        (folder / 'paper' / 'old.jpg').write_bytes(b'\xff\xd8old')
        (folder / 'notes.txt').write_text('x')
        (folder / 'references' / 'ref.pdf').write_bytes(b'%PDF ref')
        self.server.save_projects([rel])
        return rel, folder

    def test_project_entry_parser_filters_types_and_sorts(self):
        def entry(kind, size, mtime, name):
            return b'\0'.join([kind, str(size).encode(), str(mtime).encode(),
                               name.encode(), b''])
        payload = b''.join([
            entry(b'd', 4096, 1, 'zdir'),
            entry(b'd', 4096, 1, 'adir'),
            entry(b'f', 10, 3, 'a.pdf'),
            entry(b'f', 20, 5, 'b.png'),
            entry(b'f', 30, 4, 'c.txt'),
            entry(b'f', 40, 2, 'd.jpeg'),
            entry(b'f', 50, 9, '.hidden.pdf'),
        ])
        entries, truncated = self.server.parse_project_entries(
            payload, {'pdf', 'png', 'jpg'})
        self.assertEqual([item['name'] for item in entries],
                         ['adir', 'zdir', 'b.png', 'a.pdf', 'd.jpeg'])
        self.assertEqual(entries[-1]['type'], 'jpg')
        self.assertFalse(truncated)

    def test_project_entry_parser_marks_incomplete_tail(self):
        payload = b'\0'.join([b'd', b'1', b'1', b'adir'])
        entries, truncated = self.server.parse_project_entries(payload, set())
        self.assertEqual([item['name'] for item in entries], ['adir'])
        self.assertTrue(truncated)

    def test_project_recent_parser_filters_and_skips_references(self):
        payload = b''.join([
            b'5\t10\tpaper/main.pdf\0',
            b'9\t20\tpaper/fig.png\0',
            b'99\t30\treferences/ref.pdf\0',
            b'100\t40\t../escape.pdf\0',
            b'101\t50\tpaper/bad.txt\0',
            b'102\t60\t.hidden/x.pdf\0',
            b'bad\t70\tpaper/x.pdf\0',
        ])
        self.assertEqual([item['path'] for item in self.server.parse_project_recent(
            payload, {'pdf'})], ['paper/main.pdf'])
        self.assertEqual([item['path'] for item in self.server.parse_project_recent(
            payload, {'pdf', 'png'})], ['paper/main.pdf', 'paper/fig.png'])

    def test_project_files_endpoint_validates_and_returns_listing(self):
        rel, folder = self.project_files_setup()

        def entry(kind, size, mtime, name):
            return b'\0'.join([kind, size, mtime, name, b''])

        payload = entry(b'd', b'4096', b'1.0', b'figures') + \
            entry(b'f', b'10', b'3.0', b'main.pdf')
        calls = []

        def fake_run(command, timeout=60, stdin=None, binary=False):
            calls.append(command)
            return subprocess.CompletedProcess([], 0, stdout=payload, stderr=b'')

        with patch.object(self.server, 'ssh_run', side_effect=fake_run):
            data = self.server.project_files(project=rel, path='paper', types='pdf')
        self.assertEqual(data['project'], rel)
        self.assertEqual([item['name'] for item in data['entries']], ['figures', 'main.pdf'])
        self.assertIn('find', calls[0])
        self.assertIn(str(folder / 'paper'), calls[0])

    def test_project_files_endpoint_rejects_unregistered_paths_and_types(self):
        rel, _ = self.project_files_setup()
        self.assert_status(403, lambda: self.server.project_files(
            project='other/proj', types='pdf'))
        self.assert_status(400, lambda: self.server.project_files(
            project=rel, path='../outside', types='pdf'))
        self.assert_status(400, lambda: self.server.project_files(
            project=rel, path='paper/', types='pdf'))
        self.assert_status(400, lambda: self.server.project_files(
            project=rel, path='paper', types='pdf,exe'))
        self.assert_status(400, lambda: self.server.project_file(
            project=rel, path='notes.txt'))

    def test_project_files_endpoint_maps_remote_errors(self):
        rel, _ = self.project_files_setup()
        for code, status in ((44, 404), (73, 400), (1, 502)):
            with self.subTest(code=code), patch.object(
                    self.server, 'ssh_run',
                    return_value=subprocess.CompletedProcess([], code, stdout=b'', stderr=b'x')):
                self.assert_status(status, lambda: self.server.project_files(
                    project=rel, types='pdf'))
        for code, status in ((44, 404), (45, 413), (73, 400), (1, 502)):
            with self.subTest(file_code=code), patch.object(
                    self.server, 'ssh_stream',
                    side_effect=lambda command, target, timeout=120:
                    subprocess.CompletedProcess([], code, stderr=b'x')):
                self.assert_status(status, lambda: self.server.project_file(
                    project=rel, path='paper/main.pdf'))

    def test_project_file_endpoint_serves_allowed_media(self):
        rel, _ = self.project_files_setup()

        def fake_stream(command, target, timeout=120):
            target.write(b'%PDF-1.4 main')
            return subprocess.CompletedProcess([], 0, stderr=b'')

        with patch.object(self.server, 'ssh_stream', side_effect=fake_stream):
            response = self.server.project_file(project=rel, path='paper/main.pdf')
        try:
            self.assertEqual(response.media_type, 'application/pdf')
            self.assertEqual(Path(response.path).read_bytes(), b'%PDF-1.4 main')
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        finally:
            Path(response.path).unlink(missing_ok=True)

    def test_project_recent_endpoint_uses_requested_types_and_prunes_references(self):
        rel, _ = self.project_files_setup()
        calls = []

        def fake_run(command, timeout=60, stdin=None, binary=False):
            calls.append(command)
            return subprocess.CompletedProcess(
                [], 0, stdout=b'5\t10\tpaper/main.pdf\0', stderr=b'')

        with patch.object(self.server, 'ssh_run', side_effect=fake_run):
            data = self.server.project_recent(project=rel, types='pdf', limit=5)
        self.assertEqual([item['path'] for item in data['files']], ['paper/main.pdf'])
        self.assertIn('references', calls[0])
        self.assertIn('*.pdf', calls[0])
        self.assertNotIn('*.png', calls[0])
        with patch.object(self.server, 'ssh_run', side_effect=AssertionError('no ssh')):
            self.assertEqual(self.server.project_recent(project=rel, types='')['files'], [])

    def add_paper(self, slug, title, arxiv='', pdf=b'%PDF-1.4 same', source=None):
        folder = self.remote / self.topic / self.year / slug
        folder.mkdir(parents=True)
        (folder / (slug + '.pdf')).write_bytes(pdf)
        if source is not None:
            (folder / 'source').mkdir()
            (folder / 'source/main.tex').write_text(source)
        meta = dict(topic=self.topic, year=int(self.year), slug=slug, title=title,
                    arxiv=arxiv, entry='source/main.tex' if source else '',
                    pdf=slug + '.pdf', has_pdf=True, has_source=source is not None)
        (folder / 'meta.json').write_text(json.dumps(meta))
        return f'{self.topic}/{self.year}/{slug}'

    def test_mirror_plan_pulls_missing_local_pdf(self):
        self.seed()
        plan = self.server.mirror_plan()
        self.assertEqual([item['key'] for item in plan['pull']], [self.key])
        self.assertEqual(plan['pull_bytes'], len(b'%PDF-1.4 old'))
        self.assertEqual(plan['push'], [])
        result = self.server.mirror_apply({'direction': 'pull', 'keys': [self.key]})
        self.assertEqual([item['key'] for item in result['done']], [self.key])
        target = self.local / self.key / (self.slug + '.pdf')
        self.assertEqual(target.read_bytes(), b'%PDF-1.4 old')
        self.assertEqual(self.server.mirror_plan()['pull'], [])

    def test_mirror_pull_skips_when_local_pdf_exists(self):
        self.seed()
        local = self.local / self.key
        local.mkdir(parents=True)
        (local / (self.slug + '.pdf')).write_bytes(b'%PDF-1.4 mine')
        result = self.server.mirror_apply({'direction': 'pull', 'keys': [self.key]})
        self.assertTrue(result['skipped'])
        self.assertFalse(result['done'])
        self.assertEqual((local / (self.slug + '.pdf')).read_bytes(), b'%PDF-1.4 mine')

    def test_mirror_pull_rejects_non_pdf_payload(self):
        folder = self.remote / self.key
        folder.mkdir(parents=True)
        (folder / (self.slug + '.pdf')).write_bytes(b'not a pdf')
        (folder / 'meta.json').write_text(json.dumps(
            dict(topic=self.topic, year=int(self.year), slug=self.slug, has_pdf=True,
                 has_source=False, pdf=self.slug + '.pdf')))
        result = self.server.mirror_apply({'direction': 'pull', 'keys': [self.key]})
        self.assertFalse(result['done'])
        self.assertTrue(result['warnings'])
        self.assertFalse((self.local / self.key / (self.slug + '.pdf')).exists())

    def test_mirror_push_updates_existing_meta_and_keeps_source(self):
        self.seed()
        (self.paper / (self.slug + '.pdf')).unlink()
        meta = json.loads((self.paper / 'meta.json').read_text())
        meta.update(has_pdf=False, pdf='')
        (self.paper / 'meta.json').write_text(json.dumps(meta))
        local = self.local / self.key
        local.mkdir(parents=True)
        (local / (self.slug + '.pdf')).write_bytes(b'%PDF-1.4 pushed')
        plan = self.server.mirror_plan()
        self.assertEqual([item['key'] for item in plan['push']], [self.key])
        self.assertTrue(plan['push'][0]['remote_has_source'])
        result = self.server.mirror_apply({'direction': 'push', 'keys': [self.key]})
        self.assertEqual([item['key'] for item in result['done']], [self.key])
        self.assertEqual((self.paper / (self.slug + '.pdf')).read_bytes(), b'%PDF-1.4 pushed')
        updated = json.loads((self.paper / 'meta.json').read_text())
        self.assertTrue(updated['has_pdf'])
        self.assertEqual(updated['entry'], 'source/main.tex')
        self.assertEqual(updated['custom'], 'preserve')

    def test_mirror_push_creates_remote_paper_and_meta(self):
        local = self.local / self.key
        local.mkdir(parents=True)
        (local / (self.slug + '.pdf')).write_bytes(b'%PDF-1.4 only local')
        result = self.server.mirror_apply({'direction': 'push', 'keys': [self.key]})
        self.assertEqual([item['key'] for item in result['done']], [self.key])
        meta = json.loads((self.paper / 'meta.json').read_text())
        self.assertEqual((meta['slug'], meta['year'], meta['topic']), (self.slug, 2026, self.topic))
        self.assertTrue(meta['has_pdf'])
        self.assertFalse(meta['has_source'])
        self.assertEqual(meta['pdf'], self.slug + '.pdf')

    def test_mirror_apply_validates_direction_and_keys(self):
        self.assert_status(400, lambda: self.server.mirror_apply({'direction': 'sideways', 'keys': [self.key]}))
        self.assert_status(400, lambda: self.server.mirror_apply({'direction': 'pull', 'keys': []}))
        self.assert_status(400, lambda: self.server.mirror_apply({'direction': 'pull', 'keys': ['../etc/passwd']}))

    def test_duplicate_scan_groups_by_title_and_content(self):
        first = self.add_paper('2026-ICML-Li-A', 'A Study of Long Titles', source='tex')
        second = self.add_paper('2026-ICML-Li-B', 'A Study of Long Titles', source='tex')
        self.add_paper('2026-ICML-Li-C', 'Unrelated Paper Title',
                       pdf=b'%PDF-1.4 unrelated')
        groups = self.server.duplicates_scan()['groups']
        self.assertEqual(len(groups), 1)
        self.assertEqual({item['key'] for item in groups[0]['items']}, {first, second})
        self.assertEqual(groups[0]['reasons'], ['md5', 'title'])
        self.assertIn(groups[0]['suggested'], {first, second})

    def test_duplicate_scan_matches_same_arxiv_id(self):
        first = self.add_paper('2026-ICML-Li-A', 'First Different Title Here', arxiv='2401.12345')
        second = self.add_paper('2026-ICML-Li-B', 'Second Different Title Here', arxiv='2401.12345')
        groups = self.server.duplicates_scan()['groups']
        self.assertEqual(len(groups), 1)
        self.assertEqual({item['key'] for item in groups[0]['items']}, {first, second})
        self.assertIn('arxiv', groups[0]['reasons'])

    def test_merge_duplicates_moves_drops_to_recovery_and_remaps_refs(self):
        keep = self.add_paper('2026-ICML-Li-Keep', 'Merged Paper Title', source='keep source')
        drop = self.add_paper('2026-ICML-Li-Drop', 'Merged Paper Title', source='drop source')
        meta = json.loads((self.remote / keep / 'meta.json').read_text())
        meta.update(custom='keep-custom')
        (self.remote / keep / 'meta.json').write_text(json.dumps(meta))
        self.project('demo', [{'path': drop, 'note': 'from drop'}])
        drop_local = self.local / drop
        drop_local.mkdir(parents=True)
        (drop_local / '2026-ICML-Li-Drop.pdf').write_bytes(b'%PDF-1.4 drop local')

        self.research_write(self.server.set_reading, key=keep, status='reading', note='keep notes')
        self.research_write(self.server.set_reading, key=drop, status='reproduce', note='drop notes')
        self.research_write(self.server.link_versions, key=keep, other_key=drop,
                             label='arxiv', other_label='journal')
        research_before = self.server.get_research()
        result = self.server.merge_duplicates({'keep': keep, 'drops': [drop]})
        self.assertEqual(self.server.get_research(), research_before)
        self.assertTrue(any('研究记录' in warning for warning in result['warnings']))
        self.assertEqual(result['keep'], keep)
        self.assertFalse((self.remote / drop).exists())
        recovery_root = Path(str(self.remote) + '.recovery')
        backups = list(recovery_root.glob('*/2026-ICML-Li-Drop'))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / 'source/main.tex').is_file())
        kept = json.loads((self.remote / keep / 'meta.json').read_text())
        self.assertEqual(kept['custom'], 'keep-custom')
        self.assertTrue(kept['has_pdf'])
        self.assertTrue(kept['has_source'])
        drop_pdf = self.local / keep / '2026-ICML-Li-Keep.pdf'
        self.assertEqual(drop_pdf.read_bytes(), b'%PDF-1.4 drop local')
        self.assertFalse(drop_local.exists())
        local_backups = list(Path(str(self.local.resolve()) + '.recovery').glob('*/2026-ICML-Li-Drop'))
        self.assertEqual(len(local_backups), 1)
        doc, _ = self.server.read_refs_doc('demo')
        self.assertEqual([ref['path'] for ref in doc['references']], [keep])
        self.assertEqual(doc['references'][0]['note'], 'from drop')

    def test_merge_duplicates_rejects_pair_outside_group(self):
        keep = self.add_paper('2026-ICML-Li-Keep', 'One Duplicate Title Here')
        self.add_paper('2026-ICML-Li-Drop', 'One Duplicate Title Here')
        other = self.add_paper('2026-ICML-Li-Other', 'Completely Different Words',
                               pdf=b'%PDF-1.4 distinct')
        self.assert_status(409, lambda: self.server.merge_duplicates(
            {'keep': keep, 'drops': [other]}))
        self.assertTrue((self.remote / other).is_dir())

    def test_recovery_list_and_restore_local_paper(self):
        backup_dir = Path(str(self.local.resolve()) + '.recovery') / uuid4().hex / self.slug
        backup_dir.mkdir(parents=True)
        (backup_dir / (self.slug + '.pdf')).write_bytes(b'%PDF-1.4 backup')
        (backup_dir / 'meta.json').write_text(json.dumps(
            dict(topic=self.topic, year=int(self.year), slug=self.slug, title='Backup')))
        listing = self.server.recovery_list()
        local_items = [item for item in listing['items'] if item['source'] == 'local']
        self.assertEqual(len(local_items), 1)
        self.assertEqual(local_items[0]['kind'], 'paper')
        self.assertEqual(local_items[0]['target'], self.key)
        self.assertTrue(local_items[0]['restorable'])
        self.server.restore_recovery({'source': 'local', 'id': local_items[0]['id']})
        self.assertEqual((self.local / self.key / (self.slug + '.pdf')).read_bytes(),
                         b'%PDF-1.4 backup')
        self.assertFalse(backup_dir.exists())

    def test_recovery_restore_remote_paper_and_topic(self):
        self.seed()
        rid = uuid4().hex
        entry = Path(str(self.remote) + '.recovery') / rid
        shutil.move(str(self.paper), str(entry / self.slug))
        listing = self.server.recovery_list()
        item = [i for i in listing['items'] if i['id'] == rid][0]
        self.assertEqual((item['kind'], item['target']), ('paper', self.key))
        self.server.restore_recovery({'source': 'remote', 'id': rid})
        self.assertTrue((self.paper / 'source/main.tex').is_file())

        topic_id = uuid4().hex
        topic_backup = Path(str(self.remote) + '.recovery') / topic_id / 'CV'
        shutil.copytree(self.remote / self.topic, topic_backup)
        listing = self.server.recovery_list()
        item = [i for i in listing['items'] if i['id'] == topic_id][0]
        self.assertEqual((item['kind'], item['target']), ('topic', 'CV'))
        self.server.restore_recovery({'source': 'remote', 'id': topic_id})
        self.assertTrue((self.remote / 'CV/2026' / self.slug).is_dir())

    def test_recovery_restore_local_pdf_only_backup_uses_unique_remote_slug(self):
        self.seed()
        rid = uuid4().hex
        backup = Path(str(self.local.resolve()) + '.recovery') / rid / 'paper'
        backup.mkdir(parents=True)
        (backup / (self.slug + '.pdf')).write_bytes(b'%PDF-1.4 backup')
        listing = self.server.recovery_list()
        item = [i for i in listing['items'] if i['id'] == rid][0]
        self.assertEqual((item['kind'], item['target']), ('paper', self.key))
        self.assertTrue(item['restorable'])
        self.server.restore_recovery({'source': 'local', 'id': rid})
        self.assertEqual((self.local / self.key / (self.slug + '.pdf')).read_bytes(),
                         b'%PDF-1.4 backup')

    def test_recovery_ignores_ds_store_markers(self):
        self.seed()
        rid = uuid4().hex
        backup = Path(str(self.local.resolve()) + '.recovery') / rid / 'paper'
        backup.mkdir(parents=True)
        (backup / (self.slug + '.pdf')).write_bytes(b'%PDF-1.4 backup')
        (backup.parent / '.DS_Store').write_bytes(b'\x00\x00\x00\x01')
        listing = self.server.recovery_list()
        item = [i for i in listing['items'] if i['id'] == rid][0]
        self.assertEqual((item['kind'], item['target']), ('paper', self.key))
        self.assertTrue(item['restorable'])

        remote_id = uuid4().hex
        remote_entry = Path(str(self.remote) + '.recovery') / remote_id
        remote_entry.mkdir(parents=True)
        shutil.copytree(self.paper, remote_entry / self.slug)
        (remote_entry / '.DS_Store').write_bytes(b'\x00\x00\x00\x01')
        (remote_entry / self.slug / '.DS_Store').write_bytes(b'\x00\x00\x00\x01')
        shutil.rmtree(self.remote / self.key)
        listing = self.server.recovery_list()
        item = [i for i in listing['items'] if i['id'] == remote_id][0]
        self.assertEqual((item['kind'], item['target']), ('paper', self.key))
        self.server.restore_recovery({'source': 'remote', 'id': remote_id})
        self.assertTrue((self.paper / 'source/main.tex').is_file())

    def test_recovery_list_labels_empty_remote_backup(self):
        rid = uuid4().hex
        (Path(str(self.remote) + '.recovery') / rid).mkdir(parents=True)
        listing = self.server.recovery_list()
        item = [i for i in listing['items'] if i['id'] == rid][0]
        self.assertEqual(item['label'], '空备份（可清理）')
        self.assertFalse(item['restorable'])

    def test_recovery_restore_refuses_existing_target(self):
        self.seed()
        rid = uuid4().hex
        backup = Path(str(self.remote) + '.recovery') / rid / self.slug
        shutil.copytree(self.paper, backup)
        self.assert_status(409, lambda: self.server.restore_recovery(
            {'source': 'remote', 'id': rid}))
        self.assertTrue(backup.is_dir())

    def test_recovery_purge_requires_typed_confirm(self):
        rid = uuid4().hex
        backup = Path(str(self.local.resolve()) + '.recovery') / rid / 'paper'
        backup.mkdir(parents=True)
        (backup / 'meta.json').write_text('{}')
        self.assert_status(400, lambda: self.server.purge_recovery(
            {'source': 'local', 'ids': [rid]}))
        self.assert_status(400, lambda: self.server.purge_recovery(
            {'source': 'local', 'ids': [rid], 'confirm': '删除'}))
        self.assert_status(400, lambda: self.server.purge_recovery(
            {'source': 'local', 'ids': ['../escape'], 'confirm': '清理'}))
        self.assertTrue(backup.is_dir())
        result = self.server.purge_recovery(
            {'source': 'both', 'ids': [rid], 'confirm': '清理'})
        self.assertEqual(result['purged'], [{'source': 'local', 'id': rid}])
        self.assertFalse(backup.exists())

    def test_settings_save_is_private_and_requires_restart(self):
        target = self.root / 'config' / 'settings.json'
        payload = {
            'host': '127.0.0.1', 'port': 8899,
            'local_library': '~/PaperLibrary', 'remote_name': 'Lab',
            'ssh_alias': 'lab', 'remote_library': '/srv/lib',
            'remote_workshop': '/srv/proj',
        }
        with patch.object(self.server, 'CONFIG_FILE', target):
            result = self.server.save_settings(payload)
        self.assertTrue(result['restart_required'])
        saved = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(saved['ssh_alias'], 'lab')
        self.assertEqual(saved['remote_name'], 'Lab')
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.server.CFG['port'], 8899)

    def test_settings_save_rejects_incomplete_remote(self):
        with self.assertRaises(HTTPException) as ctx:
            self.server.save_settings({
                'ssh_alias': 'lab', 'remote_library': '/srv/lib',
                'remote_workshop': '',
            })
        self.assertEqual(ctx.exception.status_code, 400)

    def test_status_reports_remote_name_and_configuration(self):
        ok = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(self.server, 'ssh_run', return_value=ok):
            data = self.server.status()
        self.assertEqual(data['remote_name'], self.server.CFG['remote_name'])
        self.assertTrue(data['configured'])
        self.assertEqual(data['ssh_target'], self.server.CFG['ssh_alias'])

    def test_recovery_purge_refuses_symlink_backup(self):
        rid = uuid4().hex
        entry = Path(str(self.remote) + '.recovery') / rid
        entry.mkdir(parents=True)
        (entry / 'link').symlink_to(str(self.paper.parent))
        result = self.server.purge_recovery(
            {'source': 'remote', 'ids': [rid], 'confirm': '清理'})
        self.assertFalse(result['purged'])
        self.assertTrue(result['warnings'])
        self.assertTrue(entry.is_dir())

    def test_recovery_list_reports_remote_read_failure(self):
        with patch.object(self.server, 'ssh_run', side_effect=HTTPException(502, 'down')):
            data = self.server.recovery_list()
        self.assertEqual(data['items'], [])
        self.assertTrue(data['errors'])


if __name__ == '__main__':
    unittest.main()
