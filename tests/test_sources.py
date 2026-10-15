import pytest

import company_email_finder as m

# Captured before the autouse isolate fixture (tests/conftest.py) stubs
# ensure_clutch_access to a no-op for every other test - TestEnsureClutchAccess
# below needs the real implementation to exercise it directly.
_real_ensure_clutch_access = m.ensure_clutch_access

CLUTCH_ROW = ('<div class="provider-row"><a class="provider__title-link">{name}</a>'
              '<a class="website-link__item" href="{href}">Visit</a></div>')


class TestExtractClutchCompanies:
    def install(self, monkeypatch, renders, redirects):
        state = {"n": 0}
        def render(url):
            html = renders[min(state["n"], len(renders) - 1)]
            state["n"] += 1
            return html
        monkeypatch.setattr(m, "render_clutch_listing_html", render)
        monkeypatch.setattr(m, "resolve_clutch_redirect", lambda href: redirects.get(href))
        return state

    def rows(self, *items):
        return "<html><body>" + "".join(CLUTCH_ROW.format(name=n, href=h) for n, h in items) + "</body></html>"

    def test_extracts_and_resolves(self, monkeypatch):
        self.install(monkeypatch, [self.rows(("Acme", "r1"), ("Beta", "r2"))],
                     {"r1": "https://acme.com", "r2": "https://beta.io"})
        assert [(c.name, c.url) for c in m.extract_clutch_companies("u")] == [("Acme", "https://acme.com"), ("Beta", "https://beta.io")]

    def test_dedupes_domains_and_skips_clutch_and_unresolved(self, monkeypatch):
        self.install(monkeypatch, [self.rows(("A", "r1"), ("A again", "r2"), ("Self", "r3"), ("Dead", "r4"))],
                     {"r1": "https://acme.com", "r2": "https://www.acme.com/x", "r3": "https://clutch.co/profile/x"})
        assert [c.name for c in m.extract_clutch_companies("u")] == ["A"]

    def test_rows_without_name_or_link_skipped(self, monkeypatch):
        html = ('<div class="provider-row"><a class="website-link__item" href="r1">v</a></div>'
                '<div class="provider-row"><a class="provider__title-link">NoLink</a></div>')
        self.install(monkeypatch, [html], {"r1": "https://acme.com"})
        assert m.extract_clutch_companies("u") == []

    def test_retries_when_no_rows_then_succeeds(self, monkeypatch):
        state = self.install(monkeypatch, ["<html>Just a moment...</html>", self.rows(("Acme", "r1"))],
                             {"r1": "https://acme.com"})
        assert len(m.extract_clutch_companies("u")) == 1 and state["n"] == 2

    def test_raises_after_all_attempts_fail(self, monkeypatch):
        state = self.install(monkeypatch, ["<html></html>"], {})
        with pytest.raises(RuntimeError, match="No provider rows"):
            m.extract_clutch_companies("u")
        assert state["n"] == m.CLUTCH_RENDER_MAX_ATTEMPTS

    def test_render_timeout_is_retried_then_succeeds(self, monkeypatch):
        outcomes = iter([TimeoutError("goto timeout"), self.rows(("Acme", "r1"))])
        def render(url):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(m, "render_clutch_listing_html", render)
        monkeypatch.setattr(m, "resolve_clutch_redirect", lambda href: "https://acme.com")
        assert [c.name for c in m.extract_clutch_companies("u")] == ["Acme"]

    def test_persistent_render_failure_gives_clear_error(self, monkeypatch):
        calls = []
        def render(url):
            calls.append(url)
            raise TimeoutError("goto timeout")
        monkeypatch.setattr(m, "render_clutch_listing_html", render)
        with pytest.raises(RuntimeError, match="Could not load the Clutch page"):
            m.extract_clutch_companies("u")
        assert len(calls) == m.CLUTCH_RENDER_MAX_ATTEMPTS

    def test_mixed_failures_timeout_then_empty_page_then_success(self, monkeypatch):
        outcomes = iter([TimeoutError("t"), "<html>Just a moment</html>", self.rows(("Acme", "r1"))])
        def render(url):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(m, "render_clutch_listing_html", render)
        monkeypatch.setattr(m, "resolve_clutch_redirect", lambda href: "https://acme.com")
        assert len(m.extract_clutch_companies("u")) == 1

    def test_falls_back_to_heading_for_name(self, monkeypatch):
        html = '<div class="provider-row"><h3>Heading Co</h3><a class="website-link__item" href="r1">v</a></div>'
        self.install(monkeypatch, [html], {"r1": "https://h.com"})
        assert m.extract_clutch_companies("u")[0].name == "Heading Co"


class Hop:
    def __init__(self, status=302, location=None):
        self.status_code, self.headers = status, ({"location": location} if location else {})


class TestResolveClutchRedirect:
    def chain(self, monkeypatch, hops):
        """hops: {url: Hop | Exception}; records every URL requested."""
        requested = []
        def get(url, **kwargs):
            requested.append((url, kwargs.get("allow_redirects")))
            outcome = hops[url]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(m.session, "get", get)
        return requested

    def test_returns_first_non_clutch_address_without_requesting_it(self, monkeypatch):
        requested = self.chain(monkeypatch, {
            "https://r.clutch.co/r": Hop(302, "https://acme.com/en?utm_source=clutch&utm_medium=x#frag")})
        assert m.resolve_clutch_redirect("https://r.clutch.co/r") == "https://acme.com/en"
        assert [u for u, _ in requested] == ["https://r.clutch.co/r"]  # the company's own server is never contacted

    def test_never_follows_redirects_automatically(self, monkeypatch):
        requested = self.chain(monkeypatch, {"https://r.clutch.co/r": Hop(302, "https://acme.com")})
        m.resolve_clutch_redirect("https://r.clutch.co/r")
        assert all(flag is False for _, flag in requested)

    def test_follows_multiple_clutch_hops(self, monkeypatch):
        requested = self.chain(monkeypatch, {
            "https://r.clutch.co/r": Hop(302, "https://ppc.clutch.co/ppc/click?s=abc"),
            "https://ppc.clutch.co/ppc/click?s=abc": Hop(301, "https://beta.io/?utm_source=clutch")})
        assert m.resolve_clutch_redirect("https://r.clutch.co/r") == "https://beta.io"
        assert len(requested) == 2

    def test_relative_location_is_resolved(self, monkeypatch):
        self.chain(monkeypatch, {
            "https://r.clutch.co/r": Hop(302, "/second"),
            "https://r.clutch.co/second": Hop(302, "https://acme.com")})
        assert m.resolve_clutch_redirect("https://r.clutch.co/r") == "https://acme.com"

    def test_slow_company_site_cannot_drop_the_company(self, monkeypatch):
        import requests
        # the company's server would time out - but we must never ask it
        requested = self.chain(monkeypatch, {
            "https://r.clutch.co/r": Hop(302, "https://slow-site.com/"),
            "https://slow-site.com/": requests.ReadTimeout("slow")})
        assert m.resolve_clutch_redirect("https://r.clutch.co/r") == "https://slow-site.com"

    def test_hop_failure_is_retried_once(self, monkeypatch):
        import requests
        state = {"n": 0}
        def get(url, **kwargs):
            state["n"] += 1
            if state["n"] == 1:
                raise requests.ConnectTimeout("t")
            return Hop(302, "https://acme.com")
        monkeypatch.setattr(m.session, "get", get)
        assert m.resolve_clutch_redirect("https://r.clutch.co/r") == "https://acme.com"

    def test_persistent_failure_returns_none(self, monkeypatch):
        import requests
        self.chain(monkeypatch, {"https://r.clutch.co/r": requests.ConnectionError("x")})
        assert m.resolve_clutch_redirect("https://r.clutch.co/r") is None

    def test_page_that_does_not_redirect_returns_none(self, monkeypatch):
        self.chain(monkeypatch, {"https://r.clutch.co/r": Hop(200)})
        assert m.resolve_clutch_redirect("https://r.clutch.co/r") is None

    def test_redirect_without_location_returns_none(self, monkeypatch):
        self.chain(monkeypatch, {"https://r.clutch.co/r": Hop(302, None)})
        assert m.resolve_clutch_redirect("https://r.clutch.co/r") is None

    def test_redirect_loop_is_cut_off(self, monkeypatch):
        requested = self.chain(monkeypatch, {"https://r.clutch.co/r": Hop(302, "https://r.clutch.co/r")})
        assert m.resolve_clutch_redirect("https://r.clutch.co/r") is None
        assert len(requested) == m.CLUTCH_REDIRECT_MAX_HOPS

    def test_redirect_to_internal_address_is_refused_without_requesting_it(self, monkeypatch):
        requested = self.chain(monkeypatch, {
            "https://r.clutch.co/r": Hop(302, "https://internal.acme.com/"),
            "https://internal.acme.com/": Hop(200),
        })
        monkeypatch.setattr(m, "is_safe_url", lambda url: "internal" not in url)
        assert m.resolve_clutch_redirect("https://r.clutch.co/r") is None
        assert [u for u, _ in requested] == ["https://r.clutch.co/r"]  # the unsafe hop is never requested

    def test_mid_chain_unsafe_hop_is_refused_without_requesting_it(self, monkeypatch):
        requested = self.chain(monkeypatch, {
            "https://r.clutch.co/r": Hop(302, "https://internal.clutch.co/x"),
            "https://internal.clutch.co/x": Hop(302, "https://acme.com"),
        })
        monkeypatch.setattr(m, "is_safe_url", lambda url: "internal" not in url)
        assert m.resolve_clutch_redirect("https://r.clutch.co/r") is None
        assert [u for u, _ in requested] == ["https://r.clutch.co/r"]  # the unsafe clutch hop is never requested

    @pytest.mark.parametrize("url,expected", [
        ("https://clutch.co/x", True), ("https://r.clutch.co/x", True), ("https://ppc.clutch.co/x", True),
        ("https://acme.com", False), ("https://notclutch.co/x", False), ("https://clutch.co.evil.com/", False), ("", False)])
    def test_is_clutch_host(self, url, expected):
        assert m.is_clutch_host(url) is expected


class FakeButton:
    def __init__(self, page, visible=True, enabled="true"):
        self.page, self.visible, self.enabled = page, visible, enabled

    def is_visible(self):
        return self.visible

    def get_attribute(self, name):
        return self.enabled

    def click(self):
        self.page.clicks += 1
        if self.page.clicks >= self.page.clicks_until_gone:
            self.page.button = None


class FakePage:
    def __init__(self, button_factory, clicks_until_gone=3):
        self.clicks, self.clicks_until_gone = 0, clicks_until_gone
        self.button = button_factory(self)
        self.visited = None

    def goto(self, url, **kwargs):
        self.visited = url

    def wait_for_timeout(self, ms):
        pass

    def query_selector(self, selector):
        assert selector == "#show-more-providers-button"
        return self.button

    def content(self):
        return f"<html>clicked {self.clicks}</html>"


class FakePlaywright:
    def __init__(self, page):
        self.page, self.closed = page, False

    def __enter__(self):
        outer = self
        class Context:
            def add_init_script(self, script): pass
            def new_page(self): return outer.page
            def close(self): outer.closed = True
        class P:
            class chromium:
                @staticmethod
                def launch_persistent_context(profile_dir, **kwargs):
                    outer.launch_kwargs = {"profile_dir": profile_dir, **kwargs}
                    return Context()
        return P

    def __exit__(self, *args):
        return False


class TestRenderClutchListing:
    def render(self, monkeypatch, page):
        fake = FakePlaywright(page)
        monkeypatch.setattr(m, "sync_playwright", lambda: fake)
        return m.render_clutch_listing_html("https://clutch.co/x"), fake

    def test_clicks_show_more_until_button_disappears(self, monkeypatch):
        page = FakePage(FakeButton, clicks_until_gone=3)
        html, fake = self.render(monkeypatch, page)
        assert html == "<html>clicked 3</html>" and page.visited == "https://clutch.co/x" and fake.closed

    def test_stops_when_button_hidden(self, monkeypatch):
        page = FakePage(lambda p: FakeButton(p, visible=False))
        self.render(monkeypatch, page)
        assert page.clicks == 0

    def test_stops_when_button_disabled(self, monkeypatch):
        page = FakePage(lambda p: FakeButton(p, enabled="false"))
        self.render(monkeypatch, page)
        assert page.clicks == 0

    def test_no_button_at_all(self, monkeypatch):
        page = FakePage(lambda p: None)
        html, _ = self.render(monkeypatch, page)
        assert page.clicks == 0 and html

    def test_click_cap_prevents_endless_loop(self, monkeypatch):
        page = FakePage(FakeButton, clicks_until_gone=10**9)
        self.render(monkeypatch, page)
        assert page.clicks == m.CLUTCH_MAX_SHOW_MORE_CLICKS


class TestVerifyClutchAccess:
    def verify(self, monkeypatch, contents):
        page = FakePage(lambda p: None)
        shown = iter(contents)
        page.content = lambda: next(shown, "<html>challenge</html>")
        fake = FakePlaywright(page)
        monkeypatch.setattr(m, "sync_playwright", lambda: fake)
        return m.verify_clutch_access(), fake, page

    def test_succeeds_once_the_listing_shows_and_keeps_the_profile(self, monkeypatch):
        ok, fake, page = self.verify(monkeypatch, ["challenge", "challenge", '<div class="provider-row">'])
        assert ok and fake.closed
        assert fake.launch_kwargs["headless"] is False and fake.launch_kwargs["profile_dir"] == m.CLUTCH_PROFILE_DIR

    def test_a_page_mid_navigation_is_not_fatal(self, monkeypatch):
        page = FakePage(lambda p: None)
        answers = iter([RuntimeError("Page.content: page is navigating"), '<div class="provider-row">'])
        def content():
            value = next(answers)
            if isinstance(value, Exception):
                raise value
            return value
        page.content = content
        fake = FakePlaywright(page)
        monkeypatch.setattr(m, "sync_playwright", lambda: fake)
        assert m.verify_clutch_access() and fake.closed

    def test_gives_up_after_the_wait_and_still_closes(self, monkeypatch):
        monkeypatch.setattr(m, "CLUTCH_VERIFY_WAIT_SECONDS", 6)
        ok, fake, _ = self.verify(monkeypatch, [])
        assert not ok and fake.closed

    def test_headless_render_uses_the_same_profile(self, monkeypatch):
        fake = FakePlaywright(FakePage(lambda p: None))
        monkeypatch.setattr(m, "sync_playwright", lambda: fake)
        m.render_clutch_listing_html("https://clutch.co/x")
        assert fake.launch_kwargs["headless"] is True and fake.launch_kwargs["profile_dir"] == m.CLUTCH_PROFILE_DIR

    def test_main_verify_mode(self, monkeypatch):
        monkeypatch.setattr(m, "SOURCE", "clutch_verify")
        monkeypatch.setattr(m, "verify_clutch_access", lambda: True)
        m.main()
        monkeypatch.setattr(m, "verify_clutch_access", lambda: False)
        with pytest.raises(RuntimeError, match="not verified"):
            m.main()

    def test_headed_mode_renders_in_a_visible_window(self, monkeypatch):
        monkeypatch.setattr(m, "CLUTCH_HEADED", True)
        fake = FakePlaywright(FakePage(lambda p: None))
        monkeypatch.setattr(m, "sync_playwright", lambda: fake)
        m.render_clutch_listing_html("https://clutch.co/x")
        assert fake.launch_kwargs["headless"] is False


class TestEnsureClutchAccess:
    """A bare `python3 company_email_finder.py` must work on a machine that
    never ran SOURCE=clutch_verify by hand: probe headlessly first, and only
    fall back to the interactive check if that profile isn't cleared."""

    def test_already_verified_skips_the_interactive_check(self, monkeypatch):
        monkeypatch.setattr(m, "render_clutch_listing_html", lambda url: '<div class="provider-row">')
        called = []
        monkeypatch.setattr(m, "verify_clutch_access", lambda: called.append(1) or True)
        _real_ensure_clutch_access()
        assert called == []

    def test_cloudflare_challenge_triggers_the_interactive_check(self, monkeypatch):
        monkeypatch.setattr(m, "render_clutch_listing_html", lambda url: "<title>Just a moment...</title>")
        called = []
        monkeypatch.setattr(m, "verify_clutch_access", lambda: called.append(1) or True)
        _real_ensure_clutch_access()
        assert called == [1]

    def test_render_exception_also_triggers_the_interactive_check(self, monkeypatch):
        def boom(url):
            raise RuntimeError("navigation timeout")
        monkeypatch.setattr(m, "render_clutch_listing_html", boom)
        called = []
        monkeypatch.setattr(m, "verify_clutch_access", lambda: called.append(1) or True)
        _real_ensure_clutch_access()
        assert called == [1]

    def test_raises_when_the_interactive_check_is_never_completed(self, monkeypatch):
        monkeypatch.setattr(m, "render_clutch_listing_html", lambda url: "<title>Just a moment...</title>")
        monkeypatch.setattr(m, "verify_clutch_access", lambda: False)
        with pytest.raises(RuntimeError, match="not verified"):
            _real_ensure_clutch_access()

    def test_main_probes_access_for_every_clutch_based_source(self, monkeypatch):
        for source in ("clutch", "clutch_all", "clutch_countries"):
            calls = []
            monkeypatch.setattr(m, "SOURCE", source)
            monkeypatch.setattr(m, "ensure_clutch_access", lambda: calls.append(1))
            monkeypatch.setattr(m, "extract_clutch_companies", lambda url: [])
            monkeypatch.setattr(m, "collect_clutch_all", lambda url: [])
            monkeypatch.setattr(m, "run_chunked", lambda companies: None)
            monkeypatch.setattr(m, "run_countries", lambda countries: None)
            monkeypatch.setattr(m, "CLUTCH_COUNTRIES", ["se"])
            m.main()
            assert calls == [1], f"SOURCE={source!r} did not probe Clutch access"

    def test_main_does_not_probe_access_for_rerun_generic(self, monkeypatch):
        monkeypatch.setattr(m, "SOURCE", "rerun_generic")
        called = []
        monkeypatch.setattr(m, "ensure_clutch_access", lambda: called.append(1))
        monkeypatch.setattr(m, "rerun_generic", lambda: None)
        m.main()
        assert called == []
