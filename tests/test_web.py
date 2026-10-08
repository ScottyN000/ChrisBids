import hashlib
import shutil
import unittest
import urllib.error
from unittest import mock

from pipeline import web
from tests.pdfgen import write_pdf
from tests.webfake import Resp, Sites

NAMED = frozenset({"paintdocs.com", "hilti.com"})


def fetcher(pages, **kw):
    sites = Sites(pages)
    return web.Fetcher(NAMED, opener=sites, clock=lambda: "2026-10-08", resolve=False, **kw), sites


class AllowedCase(unittest.TestCase):
    def test_gov_us_and_named_domains_pass(self):
        for url in ("https://www.osha.gov/x", "https://labor.maryland.gov/a", "http://www.leg.state.fl.us/s",
                    "https://paintdocs.com/p", "https://www.paintdocs.com/p", "https://files-ask.hilti.com/x.pdf",
                    "https://OSHA.GOV/x", "https://osha.gov./x"):
            with self.subTest(url=url):
                self.assertEqual(web.allowed(url, NAMED), "")

    def test_other_hosts_are_refused(self):
        for url, why in (
            ("https://example.com/x", "example.com is not on the allowlist"),
            ("https://notpaintdocs.com/x", "notpaintdocs.com is not on the allowlist"),
            ("https://paintdocs.com.evil.io/x", "paintdocs.com.evil.io is not on the allowlist"),
            ("https://gov.example.com/x", "gov.example.com is not on the allowlist"),
            ("file:///etc/passwd", "only http(s) URLs are fetched, not 'file'"),
            ("http://127.0.0.1/x", "127.0.0.1 is a non-public address"),
            ("https://user:pw@osha.gov/x", "a URL carrying credentials is not fetched"),
        ):
            with self.subTest(url=url):
                self.assertEqual(web.allowed(url, NAMED), why)

    def test_host_of_drops_www_and_case(self):
        self.assertEqual(web.host_of("https://WWW.Hilti.com/a"), "hilti.com")
        self.assertEqual(web.host_of("https://www2.hilti.com/a"), "www2.hilti.com")
        self.assertEqual(web.host_of("nonsense"), "")


class TextCase(unittest.TestCase):
    def test_html_text_keeps_the_words_and_drops_scripts(self):
        markup = ("<html><head><title>T</title><style>p{}</style></head><body><nav>Menu</nav>"
                  "<script>var x = 'no';</script><p>Permits   will be issued&nbsp;2-4 weeks.</p>"
                  "<div>Second <b>block</b></div><ul><li>one</li><li>two</li></ul><svg><text>no</text></svg>"
                  "<noscript>no</noscript>after</body></html>")
        self.assertEqual(web.html_text(markup),
                         "Menu\nPermits will be issued 2-4 weeks.\nSecond block\none\ntwo\nafter")

    def test_an_unclosed_skip_tag_does_not_hide_the_rest_forever(self):
        self.assertEqual(web.html_text("</script>a<br>b"), "a\nb")

    def test_normalize_folds_quotes_dashes_space_and_case(self):
        self.assertEqual(web.normalize("  “Don’t” –  STOP—now‐ok‑x­z\n"), "\"don't\" - stop-now-ok-xz")
        self.assertEqual(web.normalize(None), "")
        self.assertEqual(web.normalize("ﬁre"), "fire")     # NFKC
        self.assertEqual(web.normalize("½″ to 2′ 3″"), '1/2" to 2\' 3"')   # inch, foot and fraction marks

    def test_quote_in_needs_every_passage_in_order(self):
        text = "The new HIT-HY 270 is available.\nSome other text.  HIT-HY 70 has been set to phase-out status."
        self.assertTrue(web.quote_in("the new HIT-HY 270 is available.", text))
        self.assertTrue(web.quote_in("HIT-HY 270 is available. ... has been set to phase-out status.", text))
        self.assertTrue(web.quote_in("HIT-HY 270 is available. … phase-out", text))
        self.assertFalse(web.quote_in("phase-out status. ... The new HIT-HY 270", text))
        self.assertFalse(web.quote_in("HIT-HY 370", text))
        self.assertFalse(web.quote_in("", text))
        self.assertFalse(web.quote_in(" ... ", text))
        self.assertTrue(web.quote_in("other text. HIT-HY", text))

    def test_quote_in_takes_each_passage_after_the_last(self):
        self.assertTrue(web.quote_in("a b ... a b", "a b c a b"))
        self.assertFalse(web.quote_in("a b ... a b", "a b c"))


class FetchCase(unittest.TestCase):
    def test_an_html_page(self):
        body = b"<html><body><p>Permits will be issued.</p></body></html>"
        f, sites = fetcher({"https://osha.gov/a": Resp(body)})
        page = f.fetch("https://osha.gov/a")
        self.assertTrue(page.ok)
        self.assertEqual((page.url, page.final_url, page.retrieved, page.status, page.content_type, page.text),
                         ("https://osha.gov/a", "https://osha.gov/a", "2026-10-08", 200, "text/html",
                          "Permits will be issued."))
        self.assertEqual(page.sha256, hashlib.sha256(body).hexdigest())
        url, headers, timeout = sites.requests[0]
        self.assertEqual((url, headers["User-agent"], headers["Accept"], timeout),
                         ("https://osha.gov/a", web.USER_AGENT, "*/*", web.TIMEOUT))

    def test_html_without_a_content_type_and_other_charsets(self):
        f, _ = fetcher({"https://osha.gov/a": Resp(b"<HTML><p>caf\xe9</p></HTML>", ctype="text/html; charset=latin-1"),
                        "https://osha.gov/b": Resp(b"<html><p>x</p></html>", ctype="")})
        self.assertEqual(f.fetch("https://osha.gov/a").text, "café")
        self.assertEqual(f.fetch("https://osha.gov/b").text, "x")

    def test_plain_text(self):
        f, _ = fetcher({"https://osha.gov/t": Resp(b"Line one\nLine two", ctype="text/plain")})
        self.assertEqual(f.fetch("https://osha.gov/t").text, "Line one\nLine two")

    @unittest.skipUnless(shutil.which("pdftotext"), "needs poppler-utils")
    def test_a_pdf(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.pdf"
            write_pdf(path, ["Spread rate 350 sq ft per gallon"])
            data = path.read_bytes()
        f, _ = fetcher({"https://paintdocs.com/p": Resp(data, ctype="application/octet-stream"),
                        "https://paintdocs.com/q": Resp(data, ctype="application/pdf")})
        for url in ("https://paintdocs.com/p", "https://paintdocs.com/q"):
            page = f.fetch(url)
            self.assertTrue(page.ok, page.error)
            self.assertIn("Spread rate 350 sq ft per gallon", page.text)

    def test_a_pdf_poppler_cannot_read(self):
        f, _ = fetcher({"https://paintdocs.com/p": Resp(b"%PDF-1.4 broken", ctype="application/pdf")})
        with mock.patch.object(web.subprocess, "run", side_effect=web.subprocess.CalledProcessError(1, "pdftotext")):
            page = f.fetch("https://paintdocs.com/p")
        self.assertTrue(page.error.startswith("could not extract text: "), page.error)

    def test_pdftotext_is_called_safely(self):
        calls = []

        def fake(args, **kw):
            calls.append((args, kw))
            return mock.Mock(stdout="Text from the PDF".encode())

        with mock.patch.object(web.subprocess, "run", fake):
            self.assertEqual(web.pdf_text(b"%PDF-"), "Text from the PDF")
        args, kw = calls[0]
        self.assertEqual(args[:3], ["pdftotext", "-enc", "UTF-8"])
        self.assertTrue(args[3].endswith("page.pdf"))
        self.assertEqual(args[4], "-")
        self.assertEqual(kw, {"capture_output": True, "timeout": 120, "check": True})

    def test_errors_become_a_page_with_no_text(self):
        f, _ = fetcher({
            "https://osha.gov/404": Resp(b"gone", status=404),
            "https://osha.gov/big": Resp(b"x" * (web.MAX_BYTES + 1), ctype="text/plain"),
            "https://osha.gov/img": Resp(b"\x89PNG", ctype="image/png"),
            "https://osha.gov/blank": Resp(b"<html><script>x</script></html>"),
            "https://osha.gov/down": urllib.error.URLError("timed out"),
            "https://osha.gov/reset": ConnectionResetError("reset"),
            "https://osha.gov/none": Resp(b"\x00\x01", ctype=""),
        })
        for url, why in (("https://osha.gov/404", "HTTP 404"),
                         ("https://osha.gov/big", f"larger than {web.MAX_BYTES} bytes"),
                         ("https://osha.gov/img", "cannot read image/png"),
                         ("https://osha.gov/blank", "no text on the page (a scan or a script-only page)"),
                         ("https://osha.gov/down", "did not open: timed out"),
                         ("https://osha.gov/reset", "did not open: reset"),
                         ("https://osha.gov/none", "cannot read unknown content")):
            with self.subTest(url=url):
                page = f.fetch(url)
                self.assertEqual(page.error, why)
                self.assertFalse(page.ok)
                self.assertEqual(page.text, "")
                # The hash is of bytes that were read in full, even when they held no text.
                self.assertEqual(bool(page.sha256), url.endswith(("img", "blank", "none")))

    def test_an_http_error_response_is_read_not_raised(self):
        err = urllib.error.HTTPError("https://osha.gov/x", 403, "Forbidden", {}, None)
        err.read = lambda n=-1: b""
        err.getcode = lambda: 403
        f, _ = fetcher({"https://osha.gov/x": Resp(b"")})
        f.opener = mock.Mock(open=mock.Mock(side_effect=err))
        page = f.fetch("https://osha.gov/x")
        self.assertEqual((page.error, page.status), ("HTTP 403", 403))

    def test_a_refused_url_is_never_opened(self):
        f, sites = fetcher({})
        page = f.fetch("https://example.com/x")
        self.assertEqual(page.error, "not fetched: example.com is not on the allowlist")
        self.assertEqual(sites.requests, [])

    def test_redirects_are_followed_only_to_allowed_hosts(self):
        f, sites = fetcher({
            "https://osha.gov/a": Resp(b"", status=301, location="/b"),
            "https://osha.gov/b": Resp(b"", status=302, location="https://paintdocs.com/c"),
            "https://paintdocs.com/c": Resp(b"<p>Here</p>"),
            "https://osha.gov/evil": Resp(b"", status=307, location="https://example.com/x"),
            "https://osha.gov/loop": Resp(b"", status=308, location="https://osha.gov/loop"),
            "https://osha.gov/noloc": Resp(b"moved", status=303, location=""),
        })
        page = f.fetch("https://osha.gov/a")
        self.assertEqual((page.text, page.final_url, page.url), ("Here", "https://paintdocs.com/c",
                                                                 "https://osha.gov/a"))
        self.assertEqual(f.fetch("https://osha.gov/evil").error, "not fetched: example.com is not on the allowlist")
        sites.requests.clear()
        self.assertEqual(f.fetch("https://osha.gov/loop").error, f"more than {web.MAX_REDIRECTS} redirects")
        self.assertEqual(len(sites.requests), web.MAX_REDIRECTS + 1)
        self.assertEqual(f.fetch("https://osha.gov/noloc").error, "HTTP 303")

    def test_each_hop_is_resolved_when_asked(self):
        f, sites = fetcher({"https://osha.gov/a": Resp(b"<p>x</p>")})
        f.resolve = True
        with mock.patch.object(web.guard, "public_url", side_effect=web.guard.UnsafeInput("resolves to 10.0.0.1")):
            self.assertEqual(f.fetch("https://osha.gov/a").error, "not fetched: resolves to 10.0.0.1")
        self.assertEqual(sites.requests, [])
        with mock.patch.object(web.guard, "public_url", return_value="ok") as checked:
            self.assertTrue(f.fetch("https://osha.gov/a").ok)
        self.assertEqual(checked.call_args_list[-1], mock.call("https://osha.gov/a"))

    def test_defaults(self):
        f = web.Fetcher({"a.com"})
        self.assertEqual(f.named, frozenset({"a.com"}))
        self.assertTrue(f.resolve)
        self.assertRegex(f.clock(), r"^\d{4}-\d{2}-\d{2}$")
        self.assertIsNone(web._NoRedirect().redirect_request(None, None, 301, "", {}, "x"))
        self.assertEqual(web.Page("u").ok, True)
        self.assertEqual(web.Page("u", error="x").ok, False)


if __name__ == "__main__":
    unittest.main()
