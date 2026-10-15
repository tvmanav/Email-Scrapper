import os
import socket
import sys
import time
from pathlib import Path

# Must be set before the module is imported: it builds its OpenAI client and
# reads MAX_COMPANIES / SOURCE at import time, and load_dotenv() never
# overrides variables that are already set - so tests ignore the real .env.
os.environ["GROQ_API_KEY"] = "test-key"
os.environ["MAX_COMPANIES"] = "0"
os.environ["SOURCE"] = "clutch"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import company_email_finder as m


class FakeResponse:
    def __init__(self, text="", status_code=200, content_type="text/html; charset=utf-8", url="https://x.test/"):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.url = url


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    """No real network, search or sleeping in any test."""
    m.robots_cache.clear()
    m._existing_groups_cache = None
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    # A real browser must never start from a test; tests of the fallback
    # switch it on explicitly together with a fake Playwright.
    monkeypatch.setattr(m, "ENABLE_BROWSER_FALLBACK", False)

    def no_network(*args, **kwargs):
        raise AssertionError(f"unexpected network call: {args}")

    monkeypatch.setattr(m.session, "get", no_network)

    # is_safe_url() resolves DNS directly (not through session.get). Fake
    # hosts like "acme.com" must resolve to *some* address without a real
    # lookup, so default every test to a public one; tests of is_safe_url
    # itself override this to exercise private/unresolvable addresses.
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 0))])

    class NoSearch:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            raise AssertionError("unexpected web search")

    monkeypatch.setattr(m, "DDGS", NoSearch)


@pytest.fixture
def fake_site(monkeypatch):
    """Serve pages from a dict through session.get; records every request URL."""
    calls = []

    def install(pages, robots=None):
        def get(url, **kwargs):
            calls.append(url)
            if url.endswith("/robots.txt"):
                if robots is None:
                    return FakeResponse("", 404, "text/plain", url)
                return FakeResponse(robots, 200, "text/plain", url)
            if url in pages:
                value = pages[url]
                return value if hasattr(value, "status_code") else FakeResponse(value, url=url)
            return FakeResponse("not found", 404, url=url)

        monkeypatch.setattr(m.session, "get", get)
        return calls

    return install


def page(url="https://acme.com", text="", emails=(), links=()):
    return m.PageData(url=url, text=text, emails=list(emails), linkedin_links=list(links))
