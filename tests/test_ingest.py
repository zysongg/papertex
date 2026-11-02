import gzip
import io
import json
import os
import stat
import struct
import sys
import tarfile
import tempfile
import unittest
import warnings
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import ingest


TEX = b"\\documentclass{article}\n\\begin{document}Hello\\end{document}\n"


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.target = self.base / "extract"

    def assert_status(self, status, function, *args, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status_code, status, caught.exception.detail)
        return caught.exception

    def zip(self, entries):
        path = self.base / "input.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in entries:
                    archive.writestr(name, data)
        return path

    def tar(self, entries, mode="w", format=tarfile.PAX_FORMAT):
        path = self.base / "input.tar"
        with tarfile.open(path, mode, format=format) as archive:
            for name, data in entries:
                info = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
                if info.isreg():
                    info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return path


class ArchiveTests(TemporaryTest):
    def test_zip_single_root_filter_and_entry(self):
        path = self.zip([
            ("__MACOSX/._paper", b"ignored"), ("README.md", b"ignored"),
            ("paper/main.tex", TEX), ("paper/notes.MD", b"ignored"),
            ("paper/.DS_Store", b"ignored"), ("paper/._main.tex", b"ignored"),
            ("paper/assets/figure.txt", b"figure"),
            ("paper/ignored.md/nested.tex", TEX),
        ])
        root = ingest.extract_archive(path, self.target)
        self.assertEqual(root, self.target / "paper")
        self.assertEqual(ingest.find_entry(root), "main.tex")
        self.assertEqual((root / "main.tex").read_bytes(), TEX)
        self.assertEqual(sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()),
                         ["assets/figure.txt", "main.tex"])

    def test_tar_plain_gzip_and_dot_prefix(self):
        for mode in ("w", "w:gz"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=self.base) as directory:
                root_dir = tarfile.TarInfo("./")
                root_dir.type = tarfile.DIRTYPE
                path = self.tar([(root_dir, b""), ("./main.tex", TEX), ("./a.txt", b"a")], mode)
                target = Path(directory) / "out"
                root = ingest.extract_archive(path, target)
                self.assertEqual(root, target)
                self.assertEqual(ingest.find_entry(root), "main.tex")

    def test_tar_strips_only_one_root_and_accepts_late_directory(self):
        folder = tarfile.TarInfo("paper/")
        folder.type = tarfile.DIRTYPE
        path = self.tar([("paper/nested/main.tex", TEX), (folder, b"")])
        root = ingest.extract_archive(path, self.target)
        self.assertEqual(root, self.target / "paper")
        self.assertEqual(ingest.find_entry(root), "nested/main.tex")

    def test_unicode_pax_and_gnu_long_names(self):
        for format, name in ((tarfile.PAX_FORMAT, "paper/论文.tex"),
                             (tarfile.GNU_FORMAT, "paper/" + "a" * 110 + ".tex")):
            with self.subTest(format=format), tempfile.TemporaryDirectory(dir=self.base) as directory:
                path = self.tar([(name, TEX)], format=format)
                root = ingest.extract_archive(path, Path(directory) / "out")
                self.assertEqual((root / Path(name).name).read_bytes(), TEX)

    def test_path_traversal_and_unsafe_names_before_any_write(self):
        bad_names = ["../escape.tex", "/absolute.tex", "a/../../escape.tex", "a\\b.tex",
                     "C:/drive.tex", "x:y.tex", "a//b.tex", "a/./b.tex", "bad\nname.tex",
                     "bad\x7fname.tex", "bad\u202ename.tex"]
        for archive_type in (self.zip, self.tar):
            for name in bad_names:
                with self.subTest(type=archive_type.__name__, name=name):
                    path = archive_type([("good.tex", TEX), (name, TEX)])
                    self.assert_status(400, ingest.extract_archive, path, self.target)
                    self.assertFalse(self.target.exists())
                    self.assertFalse((self.base / "escape.tex").exists())

    def test_zip_embedded_null_original_name(self):
        path = self.zip([("badXname.tex", TEX)])
        path.write_bytes(path.read_bytes().replace(b"badXname.tex", b"bad\0name.tex"))
        self.assert_status(400, ingest.extract_archive, path, self.target)

    def test_tar_symlink_hardlink_devices_fifo_sparse(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE,
                     tarfile.FIFOTYPE, tarfile.GNUTYPE_SPARSE):
            with self.subTest(kind=kind):
                info = tarfile.TarInfo("bad")
                info.type = kind
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    info.linkname = "../outside"
                path = self.tar([("main.tex", TEX), (info, b"")])
                self.assert_status(400, ingest.extract_archive, path, self.target)
                self.assertFalse(self.target.exists())

    def test_zip_special_file_modes(self):
        for kind in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK):
            with self.subTest(kind=kind):
                info = zipfile.ZipInfo("link")
                info.create_system = 3
                info.external_attr = (kind | 0o777) << 16
                path = self.zip([("main.tex", TEX), (info, b"../outside")])
                self.assert_status(400, ingest.extract_archive, path, self.target)

    def test_duplicates_aliases_and_conflicting_paths(self):
        pairs = [("main.tex", "main.tex"), ("./main.tex", "main.tex"),
                 ("Main.tex", "main.tex"), ("e\u0301.tex", "é.tex"),
                 ("file", "file/main.tex"), ("file/main.tex", "file"),
                 ("Folder/a.tex", "folder/b.tex")]
        for archive_type in (self.zip, self.tar):
            for first, second in pairs:
                with self.subTest(type=archive_type.__name__, pair=(first, second)):
                    path = archive_type([(first, TEX), (second, TEX)])
                    self.assert_status(400, ingest.extract_archive, path, self.target)
                    self.assertFalse(self.target.exists())

    def test_filtered_members_are_still_validated(self):
        for archive_type in (self.zip, self.tar):
            with self.subTest(type=archive_type.__name__):
                path = archive_type([("__MACOSX/../escape.md", b"x")])
                self.assert_status(400, ingest.extract_archive, path, self.target)
                path = archive_type([("ignored.md", b"a"), ("ignored.md", b"b")])
                self.assert_status(400, ingest.extract_archive, path, self.target)

    def test_member_total_count_and_upload_limits(self):
        cases = [("MAX_MEMBER_BYTES", 8, [("main.tex", b"x" * 9)]),
                 ("MAX_EXTRACT_BYTES", 8, [("one.tex", b"12345"), ("two.tex", b"12345")]),
                 ("MAX_ARCHIVE_FILES", 1, [("one.tex", b"a"), ("ignored.md", b"b")]),
                 ("MAX_UPLOAD_BYTES", 8, [("main.tex", TEX)])]
        for archive_type in (self.zip, self.tar):
            for constant, limit, entries in cases:
                with self.subTest(type=archive_type.__name__, constant=constant):
                    path = archive_type(entries)
                    with patch.object(ingest, constant, limit):
                        self.assert_status(413, ingest.extract_archive, path, self.target)
                    self.assertFalse(self.target.exists())

    def test_oversized_tar_header_rejected_without_reading_body(self):
        info = tarfile.TarInfo("huge.tex")
        info.size = ingest.MAX_MEMBER_BYTES + 1
        path = self.base / "huge.tar"
        path.write_bytes(info.tobuf())
        self.assert_status(413, ingest.extract_archive, path, self.target)

    def test_oversized_metadata_and_sparse_pax_rejected(self):
        info = tarfile.TarInfo("pax")
        info.type = tarfile.XHDTYPE
        info.size = ingest._MAX_METADATA + 1
        path = self.base / "pax.tar"
        path.write_bytes(info.tobuf())
        self.assert_status(413, ingest.extract_archive, path, self.target)
        info = tarfile.TarInfo("main.tex")
        info.pax_headers = {"GNU.sparse.map": "0,12"}
        path = self.tar([(info, TEX)])
        self.assert_status(400, ingest.extract_archive, path, self.target)

    def test_pax_path_override_cannot_traverse(self):
        info = tarfile.TarInfo("safe.tex")
        info.pax_headers = {"path": "../outside.tex"}
        path = self.tar([(info, TEX)])
        self.assert_status(400, ingest.extract_archive, path, self.target)

    def test_corrupt_and_truncated_archives(self):
        for data in (b"not a tar", b"PK\x03\x04bad", b"\x1f\x8bbad"):
            with self.subTest(data=data):
                path = self.base / "bad.archive"
                path.write_bytes(data)
                self.assert_status(400, ingest.extract_archive, path, self.target)
        path = self.tar([("main.tex", TEX)])
        path.write_bytes(path.read_bytes()[:520])
        self.assert_status(400, ingest.extract_archive, path, self.target)
        self.assertFalse(self.target.exists())

    def test_tar_trailing_hidden_members_and_expansion_limit(self):
        path = self.tar([("main.tex", TEX)])
        path.write_bytes(path.read_bytes() + b"hidden")
        self.assert_status(400, ingest.extract_archive, path, self.target)
        path.write_bytes(gzip.compress(bytes(20000)))
        with patch.object(ingest, "MAX_EXTRACT_BYTES", 8), patch.object(ingest, "MAX_ARCHIVE_FILES", 1):
            self.assert_status(413, ingest.extract_archive, path, self.target)

    def test_zip_count_is_checked_before_zipfile_allocation(self):
        path = self.zip([("one.tex", TEX), ("two.tex", TEX)])
        with patch.object(ingest, "MAX_ARCHIVE_FILES", 1), patch.object(ingest.zipfile, "ZipFile") as opener:
            self.assert_status(413, ingest.extract_archive, path, self.target)
            opener.assert_not_called()
        data = bytearray(path.read_bytes())
        end = data.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", data, end + 8, 1, 1)
        path.write_bytes(data)
        with patch.object(ingest, "MAX_ARCHIVE_FILES", 1), patch.object(ingest.zipfile, "ZipFile") as opener:
            self.assert_status(413, ingest.extract_archive, path, self.target)
            opener.assert_not_called()

    def test_zip_actual_stream_size_checked(self):
        archive = Mock()
        archive.open.return_value = io.BytesIO(b"x" * 9)
        info = zipfile.ZipInfo("main.tex")
        info.file_size = 8
        self.target.mkdir()
        with patch.object(ingest, "MAX_MEMBER_BYTES", 8):
            self.assert_status(413, ingest._extract_zip, archive, [(info, ("main.tex",))], self.target)

    def test_zip_crc_failure_cleans_staging(self):
        path = self.zip([("main.tex", TEX)])
        data = bytearray(path.read_bytes())
        end = data.find(b"PK\x01\x02")
        data[end + 16:end + 20] = bytes(4)
        path.write_bytes(data)
        self.assert_status(400, ingest.extract_archive, path, self.target)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_existing_data_and_symlink_destination_preserved(self):
        path = self.zip([("main.tex", TEX)])
        self.target.mkdir()
        keep = self.target / "keep.txt"
        keep.write_text("keep")
        self.assert_status(400, ingest.extract_archive, path, self.target)
        self.assertEqual(keep.read_text(), "keep")
        link = self.base / "link"
        link.symlink_to(self.target, target_is_directory=True)
        self.assert_status(400, ingest.extract_archive, path, link)
        archive_link = self.base / "archive-link"
        archive_link.symlink_to(path)
        self.assert_status(400, ingest.extract_archive, archive_link, self.base / "other")


class EntryAndPackagingTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.target.mkdir()

    def readme(self, name):
        (self.target / "00README.json").write_text(json.dumps({
            "sources": [{"usage": "toplevel", "filename": name}]
        }))

    def test_readme_real_tex_priority(self):
        (self.target / "main.tex").write_bytes(TEX)
        (self.target / "nested").mkdir()
        (self.target / "nested" / "chosen.tex").write_bytes(TEX)
        self.readme("./nested/chosen.tex")
        self.assertEqual(ingest.find_entry(self.target), "nested/chosen.tex")

    def test_readme_cannot_choose_non_tex_directory_or_symlink(self):
        outside = self.base / "outside.tex"
        outside.write_bytes(TEX)
        (self.target / "link.tex").symlink_to(outside)
        (self.target / "folder.tex").mkdir()
        (self.target / "binary.pdf").write_bytes(b"%PDF")
        for name in ("link.tex", "folder.tex", "binary.pdf", "missing.tex"):
            with self.subTest(name=name):
                self.readme(name)
                self.assertEqual(ingest.find_entry(self.target), "")

    def test_readme_traversal_rejected(self):
        for name in ("../outside.tex", "/etc/passwd", "C:/outside.tex", "x\\outside.tex"):
            with self.subTest(name=name):
                self.readme(name)
                self.assert_status(400, ingest.find_entry, self.target)

    def test_symlink_parent_never_traversed(self):
        external = self.base / "external"
        external.mkdir()
        (external / "main.tex").write_bytes(TEX)
        (self.target / "nested").symlink_to(external, target_is_directory=True)
        self.readme("nested/main.tex")
        self.assertEqual(ingest.find_entry(self.target), "")
        self.assert_status(400, ingest.find_entry, self.target / "nested")

    def test_fallback_priorities_and_invalid_readme(self):
        (self.target / "00README.json").write_text("not json")
        (self.target / "a.tex").write_bytes(b"fragment")
        (self.target / "z.tex").write_bytes(TEX)
        self.assertEqual(ingest.find_entry(self.target), "z.tex")
        (self.target / "paper.tex").write_bytes(TEX)
        self.assertEqual(ingest.find_entry(self.target), "paper.tex")
        (self.target / "main.tex").write_bytes(TEX)
        self.assertEqual(ingest.find_entry(self.target), "main.tex")

    def test_find_entry_nested_and_empty(self):
        self.assertEqual(ingest.find_entry(self.target), "")
        (self.target / "nested").mkdir()
        (self.target / "nested" / "paper.tex").write_bytes(TEX)
        self.assertEqual(ingest.find_entry(self.target), "nested/paper.tex")

    def test_readme_bounded(self):
        (self.target / "00README.json").write_bytes(b" " * (1024 * 1024 + 1))
        self.assert_status(413, ingest.find_entry, self.target)

    def test_package_no_links_no_hidden_metadata_and_roundtrip(self):
        external = self.base / "external"
        external.mkdir()
        (external / "secret.tex").write_bytes(b"secret")
        (self.target / "main.tex").write_bytes(TEX)
        (self.target / "nested").mkdir()
        (self.target / "nested" / "other.tex").write_bytes(TEX)
        (self.target / "inside.tex").symlink_to(self.target / "main.tex")
        (self.target / "outside.tex").symlink_to(external / "secret.tex")
        (self.target / "external").symlink_to(external, target_is_directory=True)
        (self.target / "dangling.tex").symlink_to(self.base / "missing")
        (self.target / "notes.md").write_text("ignored")
        (self.target / "__MACOSX").mkdir()
        (self.target / "__MACOSX" / "secret.tex").write_bytes(b"ignored")
        os.link(self.target / "main.tex", self.target / "hardcopy.tex")
        out = self.base / "source.tgz"
        ingest.make_tgz(self.target, out)
        with tarfile.open(out) as archive:
            members = archive.getmembers()
            self.assertEqual([m.name for m in members], ["hardcopy.tex", "main.tex", "nested/other.tex"])
            self.assertTrue(all(m.isreg() and not m.issym() and not m.islnk() for m in members))
        root = ingest.extract_archive(out, self.base / "roundtrip")
        self.assertEqual(ingest.find_entry(root), "main.tex")

    def test_package_cannot_overwrite_existing_file_or_symlink(self):
        (self.target / "main.tex").write_bytes(TEX)
        out = self.base / "existing"
        out.write_bytes(b"keep")
        self.assert_status(400, ingest.make_tgz, self.target, out)
        self.assertEqual(out.read_bytes(), b"keep")
        link = self.base / "output-link"
        link.symlink_to(out)
        self.assert_status(400, ingest.make_tgz, self.target, link)
        self.assertEqual(out.read_bytes(), b"keep")

    def test_package_limit_removes_partial_output(self):
        (self.target / "main.tex").write_bytes(TEX)
        out = self.base / "source.tgz"
        with patch.object(ingest, "MAX_MEMBER_BYTES", 8):
            self.assert_status(413, ingest.make_tgz, self.target, out)
        self.assertFalse(out.exists())


class ReadTests(TemporaryTest):
    def test_constants_and_exact_upload_limit(self):
        self.assertEqual(ingest.MAX_UPLOAD_BYTES, 300 * 1024 * 1024)
        self.assertEqual(ingest.MAX_MEMBER_BYTES, 100 * 1024 * 1024)
        self.assertEqual(ingest.MAX_EXTRACT_BYTES, 1024 * 1024 * 1024)
        self.assertEqual(ingest.MAX_ARCHIVE_FILES, 10000)
        self.assertEqual(ingest.read_limited(io.BytesIO(b"1234"), 4), b"1234")
        self.assertEqual(ingest.read_limited(io.BytesIO(), 0), b"")
        self.assert_status(413, ingest.read_limited, io.BytesIO(b"12345"), 4)
        self.assert_status(413, ingest.read_limited, io.BytesIO(b"1"), 0)

    def test_read_is_chunked_and_handles_short_reads(self):
        class ShortStream(io.BytesIO):
            sizes = []

            def read(self, size=-1):
                self.sizes.append(size)
                return super().read(min(size, 3))

        stream = ShortStream(b"1234567")
        self.assertEqual(ingest.read_limited(stream, 7), b"1234567")
        self.assertTrue(all(0 < size <= ingest._CHUNK for size in stream.sizes))
        self.assert_status(400, ingest.read_limited, io.StringIO("text"), 4)
        self.assert_status(400, ingest.read_limited, io.BytesIO(), -1)

    def test_unreadable_stream_returns_400(self):
        stream = io.BytesIO(b"closed")
        stream.close()
        self.assert_status(400, ingest.read_limited, stream)
        broken = Mock()
        broken.read.side_effect = OSError("read failed")
        self.assert_status(400, ingest.read_limited, broken)


class FakeCurl:
    def __init__(self, response=b"", error=b"", code=0):
        self.stdout = self.pipe(response)
        self.stderr = self.pipe(error)
        self.code = code
        self.killed = False

    @staticmethod
    def pipe(data):
        reader, writer = os.pipe()
        try:
            if data:
                os.write(writer, data)
        finally:
            os.close(writer)
        return os.fdopen(reader, "rb", buffering=0)

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.killed = True


class NetworkTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.popen = patch.object(ingest.subprocess, "Popen").start()
        self.download_proxy = patch.object(ingest, "_download_proxy",
                                           return_value=(None, False)).start()
        self.addCleanup(patch.stopall)
        self.popen.side_effect = AssertionError("真实网络调用被禁止")

    def responses(self, *responses):
        processes = [FakeCurl(*r) if isinstance(r, tuple) else FakeCurl(r) for r in responses]
        for process in processes:
            self.addCleanup(process.stdout.close)
            self.addCleanup(process.stderr.close)
        self.popen.side_effect = processes
        return processes

    def test_id_success(self):
        cases = {"2401.12345": "2401.12345", "0704.0001v2": "0704.0001v2",
                 "https://arxiv.org/abs/2401.12345v3": "2401.12345v3",
                 "https://export.arxiv.org/pdf/2401.12345.pdf": "2401.12345",
                 "https://arxiv.org:443/pdf/2401.12345v12": "2401.12345v12"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(ingest.arxiv_id(value), expected)
        self.popen.assert_not_called()

    def test_id_and_url_rejections_never_start_network(self):
        urls = ["http://arxiv.org/abs/2401.12345", "https://evil.example/abs/2401.12345",
                "https://arxiv.org.evil.example/abs/2401.12345", "https://arxiv.org@evil.example/abs/2401.12345",
                "https://user@arxiv.org/abs/2401.12345", "https://user:pass@arxiv.org/abs/2401.12345",
                "https://arxiv.org:444/abs/2401.12345", "https://arxiv.org:/abs/2401.12345",
                "https://arxiv.org:bad/abs/2401.12345", "https://arxiv.org./abs/2401.12345",
                "https://arxiv.org\\@evil.example/abs/2401.12345", "file:///2401.12345",
                "https://arxiv.org/abs/2401.12345\n", "https://[bad/abs/2401.12345"]
        for url in urls:
            with self.subTest(url=url):
                self.assert_status(400, ingest.arxiv_id, url)
                self.assert_status(400, ingest.arxiv_http, url)
        for value in ("prefix 2401.12345 suffix", "x2401.12345", "2401.12345.pdf", "2413.12345",
                      "2401.12345v0", "hep-th/9901001", "https://arxiv.org/other/2401.12345",
                      "https://arxiv.org/abs/2401.12345?x=1", "https://arxiv.org/abs/2401.12345#x",
                      "https://arxiv.org/abs/2401.12345.pdf", "https://arxiv.org/pdf/%32%3401.12345", None):
            with self.subTest(value=value):
                self.assert_status(400, ingest.arxiv_id, value)
        self.popen.assert_not_called()

    def test_download_success_curl_flags(self):
        self.responses(b"HTTP/2 200\r\nContent-Length: 4\r\n\r\ndata")
        self.assertEqual(ingest.arxiv_http("https://arxiv.org/pdf/2401.12345", limit=4), b"data")
        args = self.popen.call_args.args[0]
        self.assertEqual(args[:2], ["curl", "-q"])
        self.assertIn("--no-location", args)
        self.assertIn("--globoff", args)
        self.assertIn("--max-time", args)
        self.assertIn("--suppress-connect-headers", args)
        self.assertNotIn("--proxy", args)
        self.assertNotIn("-L", args)
        self.assertNotIn("--location", args)
        self.assertNotIn("env", self.popen.call_args.kwargs)

    def test_reachable_proxy_is_passed_through_child_environment(self):
        self.download_proxy.return_value = ("http://127.0.0.1:49162", False)
        self.responses(b"HTTP/2 200\r\nContent-Length: 4\r\n\r\ndata")
        self.assertEqual(ingest.arxiv_http("https://arxiv.org/pdf/2401.12345", limit=4), b"data")
        env = self.popen.call_args.kwargs["env"]
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:49162")
        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:49162")
        self.assertEqual(env["ALL_PROXY"], "http://127.0.0.1:49162")
        self.assertNotIn("NO_PROXY", env)

    def test_unreachable_proxy_is_bypassed_without_inheriting_env(self):
        self.download_proxy.return_value = (None, True)
        self.responses(b"HTTP/2 200\r\nContent-Length: 4\r\n\r\ndata")
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9",
                                     "https_proxy": "http://127.0.0.1:9",
                                     "NO_PROXY": "arxiv.org"}, clear=False):
            self.assertEqual(ingest.arxiv_http("https://arxiv.org/pdf/2401.12345", limit=4), b"data")
        env = self.popen.call_args.kwargs["env"]
        for key in ingest._PROXY_ENV:
            self.assertNotIn(key, env)

    def test_download_reports_host_and_actual_received_bytes(self):
        self.responses(b"HTTP/2 200\r\nContent-Length: 4\r\n\r\ndata")
        updates = []
        self.assertEqual(ingest.arxiv_http("https://arxiv.org/pdf/2401.12345", limit=4,
                                         progress=lambda **data: updates.append(data)), b"data")
        self.assertEqual(updates[0]['host'], 'arxiv.org')
        self.assertEqual(updates[0]['received_bytes'], 0)
        self.assertEqual(updates[-1]['received_bytes'], 4)
        self.assertEqual(updates[-1]['total_bytes'], 4)

    def test_download_without_content_length_does_not_invent_total(self):
        self.responses(b"HTTP/2 200\r\n\r\ndata")
        updates = []
        ingest.arxiv_http("https://arxiv.org/a", progress=lambda **data: updates.append(data))
        self.assertEqual(updates[-1], {'received_bytes': 4, 'total_bytes': None})

    def test_redirect_allowed_relative_and_cross_host(self):
        self.responses(b"HTTP/1.1 302 Found\r\nLocation: /next\r\n\r\n",
                       b"HTTP/2 307\r\nLocation: https://export.arxiv.org/final\r\n\r\n",
                       b"HTTP/2 200\r\n\r\nok")
        self.assertEqual(ingest.arxiv_http("https://arxiv.org/start"), b"ok")
        urls = [call.args[0][-1] for call in self.popen.call_args_list]
        self.assertEqual(urls, ["https://arxiv.org/start", "https://arxiv.org/next",
                                "https://export.arxiv.org/final"])

    def test_redirect_to_http_export_api_is_upgraded_to_https(self):
        self.responses(b"HTTP/2 302\r\nLocation: http://export.arxiv.org/api/query?id_list=2405.00946\r\n\r\n",
                       b"HTTP/2 200\r\n\r\nok")
        self.assertEqual(ingest.arxiv_http("https://arxiv.org/api/query?id_list=2405.00946"), b"ok")
        urls = [call.args[0][-1] for call in self.popen.call_args_list]
        self.assertEqual(urls, ["https://arxiv.org/api/query?id_list=2405.00946",
                                "https://export.arxiv.org/api/query?id_list=2405.00946"])

    def test_redirect_unsafe_domains_protocol_ports_userinfo(self):
        locations = ["https://evil.example/a", "//evil.example/a",
                     "file:///etc/passwd", "https://arxiv.org:444/a", "https://user@arxiv.org/a",
                     "http://evil.example/a", "http://arxiv.org:80@evil.example/a",
                     "https://arxiv.org\\@evil.example/a", "/safe\tbad", "https://[broken/a"]
        for location in locations:
            with self.subTest(location=location):
                self.popen.reset_mock()
                self.responses(f"HTTP/1.1 302 Found\r\nLocation: {location}\r\n\r\n".encode())
                self.assert_status(400, ingest.arxiv_http, "https://arxiv.org/start")
                self.assertEqual(self.popen.call_count, 1)

    def test_every_redirect_is_checked(self):
        self.responses(b"HTTP/2 302\r\nLocation: /safe\r\n\r\n",
                       b"HTTP/2 302\r\nLocation: https://evil.example/x\r\n\r\n")
        self.assert_status(400, ingest.arxiv_http, "https://arxiv.org/start")
        self.assertEqual(self.popen.call_count, 2)

    def test_redirect_loop_missing_location_and_hop_limit(self):
        self.responses(b"HTTP/2 302\r\nLocation: /start\r\n\r\n")
        self.assert_status(502, ingest.arxiv_http, "https://arxiv.org/start")
        self.responses(b"HTTP/2 302\r\n\r\n")
        self.assert_status(502, ingest.arxiv_http, "https://arxiv.org/start")
        self.popen.reset_mock()
        self.responses(*(f"HTTP/2 302\r\nLocation: /{i}\r\n\r\n".encode() for i in range(6)))
        self.assert_status(502, ingest.arxiv_http, "https://arxiv.org/start")
        self.assertEqual(self.popen.call_count, 6)

    def test_download_declared_and_actual_limits(self):
        for response in (b"HTTP/2 200\r\nContent-Length: 100\r\n\r\n",
                         b"HTTP/2 200\r\n\r\n12345",
                         b"HTTP/2 200\r\nContent-Length: 3\r\n\r\n12345"):
            with self.subTest(response=response):
                self.responses(response)
                self.assert_status(413, ingest.arxiv_http, "https://arxiv.org/a", limit=4)

    def test_headers_bounded_and_informational_response(self):
        self.responses(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/2 200\r\n\r\nok")
        self.assertEqual(ingest.arxiv_http("https://arxiv.org/a"), b"ok")
        self.responses(b"HTTP/2 200\r\nX-Long: " + b"a" * 200)
        with patch.object(ingest, "_MAX_HEADERS", 100):
            self.assert_status(502, ingest.arxiv_http, "https://arxiv.org/a")

    def test_bad_headers_and_incomplete_downloads(self):
        for response in (b"garbage\r\n\r\n", b"HTTP/2 200\r\nBad\r\n\r\n",
                         b"HTTP/2 200\r\nContent-Length: -1\r\n\r\n",
                         b"HTTP/2 200\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n",
                         b"HTTP/2 200\r\nContent-Length: 4\r\n\r\na",
                         b"HTTP/2 200\r\nContent-Encoding: gzip\r\n\r\n"):
            with self.subTest(response=response):
                self.responses(response)
                self.assert_status(502, ingest.arxiv_http, "https://arxiv.org/a")

    def test_http_and_curl_errors(self):
        for code, expected in ((404, 404), (403, 502), (429, 503), (500, 502), (503, 502), (504, 504)):
            with self.subTest(code=code):
                self.responses(f"HTTP/2 {code}\r\n\r\n".encode())
                self.assert_status(expected, ingest.arxiv_http, "https://arxiv.org/a")
        for code, expected in ((28, 504), (60, 502), (6, 502), (18, 502)):
            with self.subTest(curl=code):
                self.responses((b"", b"TLS or network failed", code))
                self.assert_status(expected, ingest.arxiv_http, "https://arxiv.org/a")
        self.popen.side_effect = FileNotFoundError("curl")
        self.assert_status(502, ingest.arxiv_http, "https://arxiv.org/a")

    def test_total_deadline_shared_across_redirects(self):
        with patch.object(ingest.time, "monotonic", side_effect=[0, 0, 0, 121]):
            self.responses(b"HTTP/2 302\r\nLocation: /next\r\n\r\n")
            self.assert_status(504, ingest.arxiv_http, "https://arxiv.org/start", timeout=120)
        self.assertEqual(self.popen.call_count, 1)

    def test_download_limit_stops_process(self):
        process = self.responses(b"HTTP/2 200\r\n\r\n12345")[0]
        process.poll = Mock(return_value=None)
        self.assert_status(413, ingest.arxiv_http, "https://arxiv.org/a", limit=4)
        self.assertTrue(process.killed)
        self.assertTrue(process.stdout.closed)

    def test_invalid_download_options_do_not_start_curl(self):
        for options in ({"timeout": 0}, {"timeout": float("nan")}, {"timeout": float("inf")},
                        {"limit": -1}, {"limit": "1"}):
            with self.subTest(options=options):
                self.assert_status(400, ingest.arxiv_http, "https://arxiv.org/a", **options)
        self.popen.assert_not_called()

    def test_stalled_stream_is_killed_and_closed(self):
        process = self.responses(b"")[0]
        process.poll = Mock(return_value=None)
        with patch.object(ingest.selectors, "DefaultSelector") as factory:
            selector = factory.return_value.__enter__.return_value
            selector.get_map.return_value = {1: object()}
            selector.select.return_value = []
            self.assert_status(504, ingest.arxiv_http, "https://arxiv.org/a")
        self.assertTrue(process.killed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)


class SourceTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.http = self.stack.enter_context(patch.object(ingest, "arxiv_http"))
        self.stack.enter_context(patch.object(ingest.subprocess, "Popen", side_effect=AssertionError("禁止联网")))

    def test_zip_tar_and_gzip_tar_success_without_traversal(self):
        fixtures = [(self.zip([("main.tex", TEX)]).read_bytes(), "source.zip"),
                    (self.tar([("main.tex", TEX)]).read_bytes(), "source.tar"),
                    (self.tar([("main.tex", TEX)], "w:gz").read_bytes(), "source.tar.gz")]
        for payload, filename in fixtures:
            with self.subTest(filename=filename):
                self.http.return_value = payload
                with patch.object(ingest.tarfile, "open", side_effect=AssertionError("不应遍历 tar")):
                    self.assertEqual(ingest.arxiv_source_bytes("2401.12345"), (payload, filename))

    def test_tar_probe_only_reads_first_header(self):
        header = tarfile.TarInfo("main.tex")
        header.size = 10**12
        payload = gzip.compress(header.tobuf() + bytes(200000))
        self.http.return_value = payload
        with patch.object(ingest, "MAX_MEMBER_BYTES", 8):
            self.assertEqual(ingest.arxiv_source_bytes("2401.12345"), (payload, "source.tar.gz"))

    def test_single_tex_plain_and_gzip_are_wrapped(self):
        for payload in (TEX, gzip.compress(TEX)):
            with self.subTest(compressed=payload != TEX):
                self.http.return_value = payload
                data, name = ingest.arxiv_source_bytes("2401.12345")
                self.assertEqual(name, "source.tar")
                with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                    self.assertEqual(archive.getnames(), ["main.tex"])
                    self.assertEqual(archive.extractfile("main.tex").read(), TEX)

    def test_gzip_single_tex_size_limit(self):
        self.http.return_value = gzip.compress(TEX + b"x" * 10000)
        with patch.object(ingest, "MAX_MEMBER_BYTES", 1024):
            self.assert_status(413, ingest.arxiv_source_bytes, "2401.12345")
        self.assertEqual(self.http.call_count, 1)

    def test_html_and_non_tex_are_not_source(self):
        for raw in (b"<html><body>not found</body></html>", b"<div>" + TEX + b"</div>",
                    b"<!DOCTYPE html><pre>" + TEX + b"</pre>", b"random text", b""):
            for payload in (raw, gzip.compress(raw)):
                with self.subTest(raw=raw, compressed=payload != raw):
                    self.http.return_value = payload
                    self.assert_status(502, ingest.arxiv_source_bytes, "2401.12345")

    def test_corrupt_gzip_is_bad_input_not_missing(self):
        self.http.return_value = b"\x1f\x8binvalid gzip"
        self.assert_status(400, ingest.arxiv_source_bytes, "2401.12345")
        self.assertEqual(self.http.call_count, 1)

    def test_only_two_explicit_404s_return_empty(self):
        self.http.side_effect = [HTTPException(404, "missing"), HTTPException(404, "missing")]
        self.assertEqual(ingest.arxiv_source_bytes("2401.12345"), (b"", ""))
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual([call.args[0] for call in self.http.call_args_list],
                         ["https://export.arxiv.org/e-print/2401.12345", "https://arxiv.org/e-print/2401.12345"])

    def test_source_network_failure_is_not_missing(self):
        for statuses, expected in (((502, 404), 502), ((404, 502), 502),
                                   ((504, 404), 504), ((404, 504), 504), ((502, 502), 502)):
            with self.subTest(statuses=statuses):
                self.http.side_effect = [HTTPException(code, "network failure") for code in statuses]
                self.assert_status(expected, ingest.arxiv_source_bytes, "2401.12345")

    def test_source_failover_success(self):
        self.http.side_effect = [HTTPException(502, "TLS failed"), gzip.compress(TEX)]
        data, name = ingest.arxiv_source_bytes("2401.12345")
        self.assertEqual(name, "source.tar")
        self.assertTrue(data)

    def test_source_413_and_invalid_id_not_swallowed(self):
        self.http.side_effect = HTTPException(413, "too large")
        self.assert_status(413, ingest.arxiv_source_bytes, "2401.12345")
        self.assertEqual(self.http.call_count, 1)
        self.http.reset_mock()
        self.assert_status(400, ingest.arxiv_source_bytes, "prefix 2401.12345")
        self.http.assert_not_called()


class DownloadProxyTests(TemporaryTest):
    def test_uses_reachable_system_proxy(self):
        with patch.object(ingest.urllib.request, "getproxies",
                          return_value={"https": "http://127.0.0.1:49162"}), \
             patch.object(ingest.socket, "create_connection") as connect:
            self.assertEqual(ingest._download_proxy(),
                             ("http://127.0.0.1:49162", False))
        connect.assert_called_once_with(("127.0.0.1", 49162), timeout=0.5)

    def test_adds_http_scheme_to_bare_proxy(self):
        with patch.object(ingest.urllib.request, "getproxies",
                          return_value={"https": "127.0.0.1:49162"}), \
             patch.object(ingest.socket, "create_connection"):
            self.assertEqual(ingest._download_proxy(),
                             ("http://127.0.0.1:49162", False))

    def test_marks_closed_port_as_unavailable(self):
        with patch.object(ingest.urllib.request, "getproxies",
                          return_value={"https": "http://127.0.0.1:49162"}), \
             patch.object(ingest.socket, "create_connection",
                          side_effect=ConnectionRefusedError):
            self.assertEqual(ingest._download_proxy(), (None, True))

    def test_absent_proxy_does_not_probe_network(self):
        with patch.object(ingest.urllib.request, "getproxies", return_value={}), \
             patch.object(ingest.socket, "create_connection") as connect:
            self.assertEqual(ingest._download_proxy(), (None, False))
        connect.assert_not_called()

    def test_rejects_non_proxy_scheme(self):
        with patch.object(ingest.urllib.request, "getproxies",
                          return_value={"https": "file:///tmp/socket"}), \
             patch.object(ingest.socket, "create_connection") as connect:
            self.assertEqual(ingest._download_proxy(), (None, True))
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
