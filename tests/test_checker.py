"""Tests for md-linkcheck. Run with: python -m unittest discover -s tests"""

import functools
import http.server
import os
import socketserver
import tempfile
import threading
import unittest

from md_linkcheck.checker import (
    Config,
    check_file,
    check_paths,
    extract_links,
    is_external,
    is_skippable,
    summarize,
)


class ExtractLinksTest(unittest.TestCase):
    def test_inline_image_and_autolink(self):
        md = (
            "[docs](https://example.com/docs)\n"
            "![alt](img/logo.png)\n"
            "<https://example.com/auto>\n"
            "visit https://example.com/bare today\n"
        )
        links = {(l.url, l.line, l.kind) for l in extract_links(md)}
        self.assertIn(("https://example.com/docs", 1, "link"), links)
        self.assertIn(("img/logo.png", 2, "image"), links)
        self.assertIn(("https://example.com/auto", 3, "autolink"), links)
        self.assertIn(("https://example.com/bare", 4, "bare"), links)

    def test_reference_links(self):
        md = "[read this][ref]\n\n[ref]: https://example.com/target\n"
        urls = [l.url for l in extract_links(md)]
        self.assertIn("https://example.com/target", urls)

    def test_code_blocks_are_ignored(self):
        md = "```\n[not a link](https://example.com/nope)\n```\n\n`[x](https://example.com/nope2)`\n\n[real](https://example.com/yes)\n"
        urls = [l.url for l in extract_links(md)]
        self.assertEqual(urls, ["https://example.com/yes"])

    def test_duplicate_link_reported_once(self):
        md = "[a](https://example.com/x) and [b](https://example.com/x)\n"
        links = extract_links(md)
        self.assertEqual(len(links), 1)

    def test_line_numbers_stay_correct_after_code_block(self):
        md = "```\ncode\n```\n[after](https://example.com/after)\n"
        links = extract_links(md)
        self.assertEqual([(l.url, l.line) for l in links], [("https://example.com/after", 4)])


class SkipLogicTest(unittest.TestCase):
    def test_skippable_schemes(self):
        for url in ["mailto:a@b.com", "tel:+123", "#section", "data:text/plain,hi"]:
            self.assertTrue(is_skippable(url), url)
        self.assertFalse(is_skippable("https://example.com"))

    def test_external_detection(self):
        self.assertTrue(is_external("https://example.com"))
        self.assertFalse(is_external("docs/page.md"))
        self.assertFalse(is_external("#anchor"))


class LocalCheckTest(unittest.TestCase):
    def test_existing_and_missing_local_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real.md")
            open(real, "w").write("x")
            doc = os.path.join(tmp, "index.md")
            open(doc, "w").write("[ok](real.md)\n[bad](missing.md)\n[anchor](#top)\n")
            results = check_file(doc, Config(check_external=False))
            by_url = {r.link.url: r.ok for r in results}
            self.assertTrue(by_url["real.md"])
            self.assertFalse(by_url["missing.md"])
            self.assertNotIn("#top", by_url)  # anchors are skipped


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self._respond()

    def do_GET(self):
        self._respond()

    def _respond(self):
        if self.path == "/ok":
            self.send_response(200)
        elif self.path == "/gone":
            self.send_response(404)
        elif self.path == "/nohead":
            if self.command == "HEAD":
                self.send_response(405)
            else:
                self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class HttpCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), QuietHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _doc_with(self, *paths):
        base = f"http://127.0.0.1:{self.port}"
        with tempfile.TemporaryDirectory() as tmp:
            doc = os.path.join(tmp, "index.md")
            with open(doc, "w") as fh:
                for i, path in enumerate(paths):
                    fh.write(f"[l{i}]({base}{path})\n")
            return check_file(doc, Config(check_local=False, timeout=5))

    def test_ok_and_broken(self):
        results = self._doc_with("/ok", "/gone")
        by_url = {r.link.url.split("/")[-1]: r.ok for r in results}
        self.assertTrue(by_url["ok"])
        self.assertFalse(by_url["gone"])

    def test_head_not_allowed_falls_back_to_get(self):
        results = self._doc_with("/nohead")
        self.assertTrue(results[0].ok)

    def test_exclude_pattern(self):
        results = self._doc_with("/gone")
        res = results[0]
        self.assertTrue(res.ok or res.note is None)  # sanity before exclusion
        with tempfile.TemporaryDirectory() as tmp:
            doc = os.path.join(tmp, "index.md")
            with open(doc, "w") as fh:
                fh.write(f"[x](http://127.0.0.1:{self.port}/gone)\n")
            excluded = check_file(doc, Config(check_local=False, timeout=5, excludes=(r"127\.0\.0\.1",)))
        self.assertEqual(excluded[0].note, "excluded")

    def test_unreachable_host_is_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = os.path.join(tmp, "index.md")
            with open(doc, "w") as fh:
                fh.write("[x](http://127.0.0.1:1/nope)\n")
            results = check_file(doc, Config(check_local=False, timeout=1, retries=0))
        self.assertFalse(results[0].ok)
        self.assertIsNotNone(results[0].error)


class CheckPathsTest(unittest.TestCase):
    def test_directory_scan_and_summarize(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "a.md"), "w").write("[ok](#x)\n")
            sub = os.path.join(tmp, "sub")
            os.mkdir(sub)
            open(os.path.join(sub, "b.markdown"), "w").write("[bad](nope.md)\n")
            open(os.path.join(tmp, "notes.txt"), "w").write("[bad](nope.md)\n")
            results = check_paths([tmp], Config(check_external=False))
            self.assertEqual(len(results), 2)  # only .md/.markdown files
            files, total, broken = summarize(results)
            self.assertEqual(files, 2)
            self.assertEqual(total, 1)  # "#x" anchor is skipped
            self.assertEqual(broken, 1)


if __name__ == "__main__":
    unittest.main()
