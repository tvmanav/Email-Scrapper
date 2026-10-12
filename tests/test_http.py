import requests

import company_email_finder as m
from tests.conftest import FakeResponse


def timeout_error(*a, **k):
    raise requests.ConnectTimeout("boom")


class TestCanCrawl:
    def test_respects_disallow(self, fake_site):
        fake_site({}, robots="User-agent: *\nDisallow: /private\n")
        assert m.can_crawl("https://acme.com/about")
        assert not m.can_crawl("https://acme.com/private/x")

    def test_missing_robots_allows_everything(self, fake_site):
        fake_site({})  # robots.txt -> 404
        assert m.can_crawl("https://acme.com/anything")

    def test_forbidden_robots_does_not_block_site(self, monkeypatch):
        # urllib's RobotFileParser treats 403 as "disallow all"; RFC 9309 says no rules.
        monkeypatch.setattr(m.session, "get", lambda url, **k: FakeResponse("denied", 403, "text/html", url))
        assert m.can_crawl("https://acme.com/about")

    def test_server_error_robots_allows(self, monkeypatch):
        monkeypatch.setattr(m.session, "get", lambda url, **k: FakeResponse("", 503, "text/html", url))
        assert m.can_crawl("https://acme.com/about")

    def test_unreachable_robots_allows(self, monkeypatch):
        monkeypatch.setattr(m.session, "get", timeout_error)
        assert m.can_crawl("https://acme.com/about")

    def test_robots_fetched_once_per_origin(self, fake_site):
        calls = fake_site({}, robots="User-agent: *\nDisallow:\n")
        m.can_crawl("https://acme.com/a"); m.can_crawl("https://acme.com/b")
        assert sum(c.endswith("/robots.txt") for c in calls) == 1

    def test_http_and_https_and_subdomains_cached_separately(self, fake_site):
        calls = fake_site({}, robots="User-agent: *\nDisallow:\n")
        for url in ("https://acme.com/", "http://acme.com/", "https://www.acme.com/"):
            m.can_crawl(url)
        assert sum(c.endswith("/robots.txt") for c in calls) == 3

    def test_robots_with_html_error_page_has_no_rules(self, fake_site):
        fake_site({}, robots="<html><body>Welcome</body></html>")
        assert m.can_crawl("https://acme.com/anything")

    def test_agent_specific_rules_and_allow_override(self, fake_site):
        fake_site({}, robots="User-agent: *\nDisallow: /a/\nAllow: /a/public\n")
        assert not m.can_crawl("https://acme.com/a/secret")
        assert m.can_crawl("https://acme.com/a/public")

    def test_internal_address_is_blocked_without_fetching_robots(self, fake_site, monkeypatch):
        calls = fake_site({}, robots="User-agent: *\nDisallow:\n")
        monkeypatch.setattr(m, "is_safe_url", lambda url: False)
        assert not m.can_crawl("https://acme.com/about")
        assert calls == []  # robots.txt is never fetched for a blocked host


class TestFetch:
    def test_returns_html(self, fake_site):
        fake_site({"https://acme.com/about": "<p>hi</p>"})
        assert m.fetch("https://acme.com/about") == "<p>hi</p>"

    def test_normalizes_input_url(self, fake_site):
        calls = fake_site({"https://acme.com/about": "<p>hi</p>"})
        assert m.fetch("acme.com/about/") == "<p>hi</p>"
        assert "https://acme.com/about" in calls

    def test_empty_url(self):
        assert m.fetch("") is None
        assert m.fetch(None) is None

    def test_non_html_content_is_skipped(self, fake_site):
        fake_site({"https://acme.com/f.pdf": FakeResponse("%PDF", 200, "application/pdf")})
        assert m.fetch("https://acme.com/f.pdf") is None

    def test_xhtml_and_missing_content_type_accepted(self, fake_site):
        fake_site({
            "https://acme.com/x": FakeResponse("<p>x</p>", 200, "application/xhtml+xml"),
            "https://acme.com/n": FakeResponse("<p>n</p>", 200, ""),
        })
        assert m.fetch("https://acme.com/x") == "<p>x</p>"
        assert m.fetch("https://acme.com/n") == "<p>n</p>"

    def test_404_returns_none_without_retry(self, fake_site):
        calls = fake_site({})
        assert m.fetch("https://acme.com/missing") is None
        assert calls.count("https://acme.com/missing") == 1

    def test_blocked_by_robots_never_requests_page(self, fake_site):
        calls = fake_site({"https://acme.com/private": "secret"}, robots="User-agent: *\nDisallow: /private\n")
        assert m.fetch("https://acme.com/private") is None
        assert "https://acme.com/private" not in calls

    def test_transient_503_is_retried(self, monkeypatch):
        answers = iter([FakeResponse("", 503), FakeResponse("<p>ok</p>")])
        monkeypatch.setattr(m.session, "get", lambda url, **k: FakeResponse("", 404) if url.endswith("robots.txt") else next(answers))
        assert m.fetch("https://acme.com/") == "<p>ok</p>"

    def test_persistent_503_gives_up(self, monkeypatch):
        monkeypatch.setattr(m.session, "get", lambda url, **k: FakeResponse("", 404 if url.endswith("robots.txt") else 503))
        assert m.fetch("https://acme.com/") is None

    def test_timeout_retried_then_none(self, monkeypatch):
        attempts = []
        def get(url, **k):
            if url.endswith("robots.txt"):
                return FakeResponse("", 404)
            attempts.append(url)
            raise requests.ReadTimeout("slow")
        monkeypatch.setattr(m.session, "get", get)
        assert m.fetch("https://acme.com/") is None
        assert len(attempts) == m.FETCH_ATTEMPTS

    def test_timeout_then_success(self, monkeypatch):
        state = {"n": 0}
        def get(url, **k):
            if url.endswith("robots.txt"):
                return FakeResponse("", 404)
            state["n"] += 1
            if state["n"] == 1:
                raise requests.ConnectionError("reset")
            return FakeResponse("<p>ok</p>")
        monkeypatch.setattr(m.session, "get", get)
        assert m.fetch("https://acme.com/") == "<p>ok</p>"


class TestFetchRedirects:
    def serve(self, monkeypatch, hops):
        requested = []
        def get(url, **kwargs):
            requested.append(url)
            if url.endswith("/robots.txt"):
                return FakeResponse("", 404)
            assert kwargs.get("allow_redirects") is False
            return hops[url]
        monkeypatch.setattr(m.session, "get", get)
        return requested

    def redirect(self, location, status=302):
        r = FakeResponse("", status)
        r.headers["location"] = location
        return r

    def test_follows_redirects_to_a_public_page(self, monkeypatch):
        self.serve(monkeypatch, {
            "https://acme.com/old": self.redirect("/new"),
            "https://acme.com/new": FakeResponse("<p>ok</p>")})
        assert m.fetch("https://acme.com/old") == "<p>ok</p>"

    def test_redirect_to_internal_address_is_refused_unrequested(self, monkeypatch):
        requested = self.serve(monkeypatch, {
            "https://acme.com/old": self.redirect("http://169.254.169.254/latest/meta-data"),
            "http://169.254.169.254/latest/meta-data": FakeResponse("secret")})
        monkeypatch.setattr(m, "is_safe_url", lambda url: "169.254" not in url)
        assert m.fetch("https://acme.com/old") is None
        assert "http://169.254.169.254/latest/meta-data" not in requested

    def test_redirect_loop_gives_up(self, monkeypatch):
        self.serve(monkeypatch, {"https://acme.com/a": self.redirect("/a")})
        assert m.fetch("https://acme.com/a") is None


class TestCrawlCompany:
    def patch_fetch(self, monkeypatch, pages):
        calls = []
        def fake_fetch(url):
            url = m.normalize_url(url)
            calls.append(url)
            return pages.get(url)
        monkeypatch.setattr(m, "fetch", fake_fetch)
        return calls

    HOME = '<a href="/contact">Contact</a><a href="/about-us">About</a><a href="/">Home</a>'

    def test_crawls_homepage_and_discovered_pages(self, monkeypatch):
        calls = self.patch_fetch(monkeypatch, {
            "https://acme.com": self.HOME, "https://acme.com/contact": "<p>c@acme.com</p>",
            "https://acme.com/about-us": "<p>about</p>"})
        pages = m.crawl_company(m.Company("Acme", "https://acme.com"))
        assert [p.url for p in pages][0] == "https://acme.com"
        assert {p.url for p in pages} == {"https://acme.com", "https://acme.com/contact", "https://acme.com/about-us"}
        assert any(p.emails == ["c@acme.com"] for p in pages)

    def test_homepage_not_fetched_twice(self, monkeypatch):
        calls = self.patch_fetch(monkeypatch, {"https://acme.com": self.HOME + '<a href="https://acme.com/">team</a>'})
        m.crawl_company(m.Company("Acme", "https://acme.com"))
        assert calls.count("https://acme.com") == 1

    def test_failed_subpages_are_skipped(self, monkeypatch):
        self.patch_fetch(monkeypatch, {"https://acme.com": self.HOME})
        pages = m.crawl_company(m.Company("Acme", "https://acme.com"))
        assert [p.url for p in pages] == ["https://acme.com"]

    def test_falls_back_to_domain_root_when_deep_url_dead(self, monkeypatch):
        calls = self.patch_fetch(monkeypatch, {"https://acme.com": "<p>root</p>"})
        company = m.Company("Acme", "https://acme.com/en/old-landing")
        pages = m.crawl_company(company)
        assert pages and pages[0].url == "https://acme.com"
        assert calls[0] == "https://acme.com/en/old-landing"
        # The output CSV must record the address that actually answered, not
        # the dead deep link - otherwise every reader hits a 404 on it.
        assert company.url == "https://acme.com"

    def test_company_url_is_unchanged_when_the_deep_url_works(self, monkeypatch):
        self.patch_fetch(monkeypatch, {"https://acme.com/en": "<p>hi</p>"})
        company = m.Company("Acme", "https://acme.com/en")
        m.crawl_company(company)
        assert company.url == "https://acme.com/en"

    def test_no_fallback_when_already_at_root(self, monkeypatch):
        calls = self.patch_fetch(monkeypatch, {})
        assert m.crawl_company(m.Company("Acme", "https://acme.com")) == []
        assert calls == ["https://acme.com"]

    def test_returns_empty_when_site_dead(self, monkeypatch):
        self.patch_fetch(monkeypatch, {})
        assert m.crawl_company(m.Company("Acme", "https://acme.com/deep")) == []

    def test_page_budget_respected(self, monkeypatch):
        monkeypatch.setattr(m, "MAX_PAGES_PER_COMPANY", 3)
        links = "".join(f'<a href="/team-{i}">team</a>' for i in range(10))
        pages_map = {"https://acme.com": links, **{f"https://acme.com/team-{i}": "<p>x</p>" for i in range(10)}}
        self.patch_fetch(monkeypatch, pages_map)
        assert len(m.crawl_company(m.Company("Acme", "https://acme.com"))) <= 3
