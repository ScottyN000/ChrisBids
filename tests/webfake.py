"""Stand-ins for urllib's opener, so fetch tests need no network."""
from __future__ import annotations

from email.message import Message


class Resp:
    def __init__(self, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8", location: str = ""):
        self.body, self.status = body, status
        self.headers = Message()
        if ctype:
            self.headers["Content-Type"] = ctype
        if location:
            self.headers["Location"] = location

    def getcode(self):
        return self.status

    def read(self, n=-1):
        return self.body if n < 0 else self.body[:n]


class Sites:
    """Answers each URL from a dict of URL -> Resp (or an exception to raise), and records the requests."""

    def __init__(self, pages: dict):
        self.pages, self.requests = pages, []

    def open(self, req, timeout=None):
        self.requests.append((req.full_url, dict(req.header_items()), timeout))
        got = self.pages[req.full_url]
        if isinstance(got, Exception):
            raise got
        return got


class OneHTML(Sites):
    """Every URL answers with the same HTML page."""

    def __init__(self, markup: str):
        super().__init__({})
        self.markup = markup

    def open(self, req, timeout=None):
        self.requests.append((req.full_url, dict(req.header_items()), timeout))
        return Resp(self.markup.encode())
