import pytest

import company_email_finder as m
from tests.conftest import page

SHELL = '<html><body><div id="root"></div></body></html>'


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakeBrowserPage:
    def __init__(self, site, visited):
        self.site, self.visited, self.current = site, visited, ""

    def goto(self, url, **kwargs):
        self.visited.append(url)
        outcome = self.site.get(m.normalize_url(url))
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return None
        status, html = outcome
        self.current = html
        return FakeResponse(status)

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return self.current


class FakePlaywright:
    def __init__(self, site, launches, visited, fail_launch=False):
        self.site, self.launches, self.visited, self.fail_launch = site, launches, visited, fail_launch
        self.closed = 0

    def __enter__(self):
        outer = self
        class Context:
            def new_page(self_inner): return FakeBrowserPage(outer.site, outer.visited)
        class Browser:
            def new_context(self_inner, **kwargs): return Context()
            def close(self_inner): outer.closed += 1
        class P:
            class chromium:
                @staticmethod
                def launch(**kwargs):
                    outer.launches.append(kwargs)
                    if outer.fail_launch:
                        raise RuntimeError("chromium missing")
                    return Browser()
        return P

    def __exit__(self, *args):
        return False


@pytest.fixture
def browser(monkeypatch):
    """Install a fake Playwright serving `site` ({normalized_url: (status, html)})."""
    from tests.conftest import FakeResponse as HttpResponse
    monkeypatch.setattr(m, "ENABLE_BROWSER_FALLBACK", True)
    # default: no robots.txt anywhere (tests needing rules install their own via fake_site)
    monkeypatch.setattr(m.session, "get", lambda url, **k: HttpResponse("", 404, "text/plain", url))
    state = {"launches": [], "visited": []}
    def install(site, fail_launch=False):
        fake = FakePlaywright(site, state["launches"], state["visited"], fail_launch)
        monkeypatch.setattr(m, "sync_playwright", lambda: fake)
        state["fake"] = fake
        return state
    return install


class TestProbeCommonPages:
    def install_fetch(self, monkeypatch, site):
        calls = []
        def fake_fetch(url):
            calls.append(m.normalize_url(url))
            return site.get(m.normalize_url(url))
        monkeypatch.setattr(m, "fetch", fake_fetch)
        return calls

    def test_stops_at_first_page_with_an_email(self, monkeypatch):
        calls = self.install_fetch(monkeypatch, {"https://acme.nl/contact": "<p>info@acme.nl</p>"})
        found = m.probe_common_pages("https://acme.nl", [page("https://acme.nl")])
        assert [p.url for p in found] == ["https://acme.nl/contact"] and calls == ["https://acme.nl/contact"]

    def test_collects_pages_without_email_and_keeps_going(self, monkeypatch):
        self.install_fetch(monkeypatch, {"https://acme.nl/contact-us": "<p>Get in touch</p>", "https://acme.nl/kontakt": "<p>x@acme.nl</p>"})
        found = m.probe_common_pages("https://acme.nl", [page("https://acme.nl")])
        assert [p.url for p in found] == ["https://acme.nl/contact-us", "https://acme.nl/kontakt"]

    def test_skips_pages_already_crawled(self, monkeypatch):
        calls = self.install_fetch(monkeypatch, {})
        m.probe_common_pages("https://acme.nl", [page("https://acme.nl"), page("https://acme.nl/contact/")])
        assert "https://acme.nl/contact" not in calls

    def test_probe_count_is_capped(self, monkeypatch):
        calls = self.install_fetch(monkeypatch, {})
        m.probe_common_pages("https://acme.nl", [page("https://acme.nl")])
        assert len(calls) == m.MAX_CONTACT_PROBES

    def test_uses_site_origin_not_deep_path(self, monkeypatch):
        calls = self.install_fetch(monkeypatch, {})
        m.probe_common_pages("https://acme.nl/en/old", [page("https://acme.nl/en/old")])
        assert all(c.startswith("https://acme.nl/") and "/en/" not in c for c in calls)

    def test_nothing_found(self, monkeypatch):
        self.install_fetch(monkeypatch, {})
        assert m.probe_common_pages("https://acme.nl", [page("https://acme.nl")]) == []


class TestRenderCompanyPages:
    def test_renders_js_site_and_extracts_email(self, browser):
        state = browser({"https://acme.nl": (200, '<html><body><p>Mail info@acme.nl</p></body></html>')})
        pages = m.render_company_pages("https://acme.nl")
        assert pages[0].emails == ["info@acme.nl"] and state["fake"].closed == 1
        assert state["launches"][0]["headless"] is True

    def test_follows_contact_links_found_after_rendering(self, browser):
        state = browser({
            "https://acme.nl": (200, '<a href="/contact">Contact</a>'),
            "https://acme.nl/contact": (200, "<p>sales@acme.nl</p>")})
        pages = m.render_company_pages("https://acme.nl")
        assert [p.url for p in pages] == ["https://acme.nl", "https://acme.nl/contact"]
        assert pages[1].emails == ["sales@acme.nl"]

    @pytest.mark.parametrize("status", [403, 418, 429, 500])
    def test_blocked_or_error_pages_are_skipped_not_circumvented(self, browser, status):
        state = browser({"https://acme.nl": (status, "<html>Attention Required! | Cloudflare</html>")})
        assert m.render_company_pages("https://acme.nl") == []
        assert state["visited"] == ["https://acme.nl"]  # asked once, did not retry or try tricks

    def test_robots_disallow_prevents_any_browser_launch(self, browser, fake_site):
        state = browser({"https://acme.nl": (200, "<p>x@acme.nl</p>")})
        fake_site({}, robots="User-agent: *\nDisallow: /\n")
        assert m.render_company_pages("https://acme.nl") == []
        assert state["launches"] == []

    def test_robots_checked_for_followed_links_too(self, browser, fake_site):
        state = browser({"https://acme.nl": (200, '<a href="/contact">c</a>'), "https://acme.nl/contact": (200, "<p>x@acme.nl</p>")})
        fake_site({}, robots="User-agent: *\nDisallow: /contact\n")
        assert [p.url for p in m.render_company_pages("https://acme.nl")] == ["https://acme.nl"]

    def test_disabled_flag(self, browser, monkeypatch):
        state = browser({"https://acme.nl": (200, "<p>x@acme.nl</p>")})
        monkeypatch.setattr(m, "ENABLE_BROWSER_FALLBACK", False)
        assert m.render_company_pages("https://acme.nl") == [] and state["launches"] == []

    def test_navigation_timeout_is_survived(self, browser):
        browser({"https://acme.nl": TimeoutError("nav timeout")})
        assert m.render_company_pages("https://acme.nl") == []

    def test_missing_browser_binary_is_survived(self, browser):
        browser({}, fail_launch=True)
        assert m.render_company_pages("https://acme.nl") == []

    def test_page_budget_respected(self, browser, monkeypatch):
        monkeypatch.setattr(m, "BROWSER_FALLBACK_MAX_PAGES", 2)
        links = "".join(f'<a href="/team-{i}">team</a>' for i in range(6))
        site = {"https://acme.nl": (200, links), **{f"https://acme.nl/team-{i}": (200, "<p>x</p>") for i in range(6)}}
        state = browser(site)
        assert len(m.render_company_pages("https://acme.nl")) == 2

    def test_response_none_is_skipped(self, browser):
        browser({})  # unknown URL -> goto returns None
        assert m.render_company_pages("https://acme.nl") == []


class TestCrawlFallbackIntegration:
    def patch_fetch(self, monkeypatch, site):
        monkeypatch.setattr(m, "fetch", lambda url: site.get(m.normalize_url(url)))

    def test_no_extra_work_when_emails_already_found(self, monkeypatch, browser):
        state = browser({"https://acme.nl": (200, "<p>nope</p>")})
        self.patch_fetch(monkeypatch, {"https://acme.nl": "<p>info@acme.nl</p>"})
        pages = m.crawl_company(m.Company("Acme", "https://acme.nl"))
        assert len(pages) == 1 and state["launches"] == []

    def test_probe_finds_email_without_launching_browser(self, monkeypatch, browser):
        state = browser({})
        self.patch_fetch(monkeypatch, {"https://acme.nl": "<p>welcome</p>", "https://acme.nl/kontakt": "<p>kontakt@acme.nl</p>"})
        pages = m.crawl_company(m.Company("Acme", "https://acme.nl"))
        assert any("kontakt@acme.nl" in p.emails for p in pages) and state["launches"] == []

    def test_js_shell_gets_rendered_and_replaces_homepage(self, monkeypatch, browser):
        state = browser({"https://acme.nl": (200, "<html><body><h1>Acme</h1><p>Jane Doe, CEO. Mail hello@acme.nl</p></body></html>")})
        self.patch_fetch(monkeypatch, {"https://acme.nl": SHELL})
        pages = m.crawl_company(m.Company("Acme", "https://acme.nl"))
        assert pages[0].url == "https://acme.nl" and "Jane Doe" in pages[0].text
        assert pages[0].emails == ["hello@acme.nl"] and len(state["launches"]) == 1

    def test_blocked_site_stays_empty_and_no_browser_attempt(self, monkeypatch, browser):
        state = browser({"https://acme.nl": (200, "<p>x@acme.nl</p>")})
        self.patch_fetch(monkeypatch, {})  # homepage fetch fails (403/418 etc.)
        assert m.crawl_company(m.Company("Acme", "https://acme.nl")) == []
        assert state["launches"] == []

    def test_rendered_extra_pages_are_appended(self, monkeypatch, browser):
        browser({"https://acme.nl": (200, '<a href="/contact">Contact</a>'),
                 "https://acme.nl/contact": (200, "<p>sales@acme.nl</p>")})
        self.patch_fetch(monkeypatch, {"https://acme.nl": SHELL})
        pages = m.crawl_company(m.Company("Acme", "https://acme.nl"))
        assert "sales@acme.nl" in {e for p in pages for e in p.emails}

    def test_shell_kept_when_render_yields_nothing(self, monkeypatch, browser):
        browser({"https://acme.nl": (403, "blocked")})
        self.patch_fetch(monkeypatch, {"https://acme.nl": SHELL})
        pages = m.crawl_company(m.Company("Acme", "https://acme.nl"))
        assert len(pages) == 1 and pages[0].emails == []


def test_browser_concurrency_is_limited():
    assert m.BROWSER_WORKERS >= 1
