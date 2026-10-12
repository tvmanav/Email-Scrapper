import pytest

import company_email_finder as m

BASE = "https://zakelijkeregister.nl"


def profile_html(name="Solert IT", website="https://solert-it.nl/", email=("info", "solert-it.nl"),
                 linkedin="https://www.linkedin.com/company/solert-it/", extra=""):
    email_html = (f'<span class="js-email-link" data-email-user="{email[0]}" data-email-domain="{email[1]}"></span>'
                  if email else "")
    website_html = f'<a class="link-truncate" href="{website}">{website}</a>' if website else ""
    social = f'<div class="k">Sociale media</div><div class="social-links"><a href="{linkedin}">LinkedIn</a></div>' if linkedin else ""
    return f"""<html><body><h1>{name}</h1><div class="kv">
      <div class="k">Bedrijfsnaam</div><div>{name}</div>
      <div class="k">E-mail</div><div>{email_html}</div>
      <div class="k">Website</div><div>{website_html}</div>{social}</div>
      <div class="related"><a href="https://www.linkedin.com/company/unrelated-neighbour">x</a></div>
      </body></html>"""


def results_html(*items):
    lis = "".join(f'<li class="search-result-item"><a href="{href}" class="search-result-link">'
                  f'<span class="search-result-name">{name}</span></a></li>' for name, href in items)
    return f'<html><body><ul class="search-results-list">{lis}</ul></body></html>'


class TestParseZrProfile:
    def test_full_profile(self):
        c = m.parse_zr_profile("Solert IT", profile_html())
        assert (c.name, c.url, c.linkedin, c.directory_email) == (
            "Solert IT", "https://solert-it.nl", "https://www.linkedin.com/company/solert-it", "info@solert-it.nl")

    def test_ignores_linkedin_outside_gegevens_block(self):
        assert m.parse_zr_profile("X", profile_html(linkedin="")).linkedin == ""

    def test_personal_linkedin_not_used_as_company(self):
        assert m.parse_zr_profile("X", profile_html(linkedin="https://www.linkedin.com/in/someone")).linkedin == ""

    def test_missing_website_gives_none(self):
        assert m.parse_zr_profile("X", profile_html(website="")) is None

    @pytest.mark.parametrize("site", ["https://www.facebook.com/acme", "https://instagram.com/acme",
                                      "https://nl.linkedin.com/company/acme"])
    def test_social_page_is_not_a_website(self, site):
        assert m.parse_zr_profile("X", profile_html(website=site)) is None

    def test_email_on_other_domain_dropped(self):
        assert m.parse_zr_profile("X", profile_html(email=("jan", "gmail.com"))).directory_email == ""

    def test_lookalike_email_domain_dropped(self):
        assert m.parse_zr_profile("X", profile_html(email=("jan", "evilsolert-it.nl"))).directory_email == ""

    def test_subdomain_email_kept(self):
        assert m.parse_zr_profile("X", profile_html(email=("jan", "mail.solert-it.nl"))).directory_email == "jan@mail.solert-it.nl"

    def test_no_email_is_fine(self):
        assert m.parse_zr_profile("X", profile_html(email=None)).directory_email == ""

    def test_www_prefixed_website(self):
        assert m.parse_zr_profile("X", profile_html(website="http://www.solert-it.nl/", email=("a", "solert-it.nl"))).directory_email

    def test_label_without_value_cell_is_skipped(self):
        html = '<div class="kv"><div class="k">Website</div></div>'
        assert m.parse_zr_profile("X", html) is None

    def test_page_without_details_block(self):
        assert m.parse_zr_profile("X", "<html><body><h1>Nope</h1></body></html>") is None
        assert m.parse_zr_profile("X", "") is None


class TestFetchZrSearchPage:
    def test_parses_results(self, monkeypatch):
        monkeypatch.setattr(m, "fetch", lambda url: results_html(("AFAS", f"{BASE}/bedrijf/afas-1")))
        assert m.fetch_zr_search_page("software", 1) == [("AFAS", f"{BASE}/bedrijf/afas-1")]

    def test_relative_links_made_absolute(self, monkeypatch):
        monkeypatch.setattr(m, "fetch", lambda url: results_html(("AFAS", "/bedrijf/afas-1")))
        assert m.fetch_zr_search_page("software", 1)[0][1] == f"{BASE}/bedrijf/afas-1"

    def test_query_and_page_are_url_encoded(self, monkeypatch):
        seen = []
        monkeypatch.setattr(m, "fetch", lambda url: seen.append(url) or results_html(("A", "/bedrijf/a")))
        m.fetch_zr_search_page("soft ware&x", 3)
        assert seen == [f"{BASE}/zoeken?q=soft+ware%26x&page=3"]

    def test_empty_answer_is_retried(self, monkeypatch):
        answers = iter([None, "<html></html>", results_html(("A", "/bedrijf/a"))])
        monkeypatch.setattr(m, "fetch", lambda url: next(answers))
        assert len(m.fetch_zr_search_page("software", 1)) == 1

    def test_gives_up_after_attempts(self, monkeypatch):
        calls = []
        monkeypatch.setattr(m, "fetch", lambda url: calls.append(url))
        assert m.fetch_zr_search_page("software", 9) == []
        assert len(calls) == m.ZR_PAGE_ATTEMPTS

    def test_items_without_name_skipped(self, monkeypatch):
        html = ('<li class="search-result-item"><a class="search-result-link" href="/bedrijf/x"></a></li>')
        monkeypatch.setattr(m, "fetch", lambda url: html)
        assert m.fetch_zr_search_page("s", 1) == []


class TestExtractZakelijkeregisterCompanies:
    def install(self, monkeypatch, pages, profiles):
        def fake_fetch(url):
            if "/zoeken" in url:
                page = int(url.rsplit("page=", 1)[1])
                return pages.get(page)
            return profiles.get(url)
        monkeypatch.setattr(m, "fetch", fake_fetch)

    def test_collects_across_pages_until_empty(self, monkeypatch):
        self.install(monkeypatch,
            {1: results_html(("A", f"{BASE}/bedrijf/a"), ("B", f"{BASE}/bedrijf/b")),
             2: results_html(("C", f"{BASE}/bedrijf/c"))},
            {f"{BASE}/bedrijf/a": profile_html("A", "https://a.nl", ("i", "a.nl"), ""),
             f"{BASE}/bedrijf/b": profile_html("B", "https://b.nl", None, ""),
             f"{BASE}/bedrijf/c": profile_html("C", "https://c.nl", None, "")})
        assert [c.name for c in m.extract_zakelijkeregister_companies("software")] == ["A", "B", "C"]

    def test_skips_companies_without_website_and_duplicates(self, monkeypatch):
        self.install(monkeypatch,
            {1: results_html(("A", f"{BASE}/bedrijf/a"), ("NoSite", f"{BASE}/bedrijf/n"), ("A2", f"{BASE}/bedrijf/a2"),
                             ("Dead", f"{BASE}/bedrijf/dead"))},
            {f"{BASE}/bedrijf/a": profile_html("A", "https://a.nl", None, ""),
             f"{BASE}/bedrijf/n": profile_html("NoSite", "", None, ""),
             f"{BASE}/bedrijf/a2": profile_html("A2", "https://www.a.nl/other", None, "")})  # same domain; /dead 404s
        assert [c.name for c in m.extract_zakelijkeregister_companies("software")] == ["A"]

    def test_respects_max_companies(self, monkeypatch):
        monkeypatch.setattr(m, "MAX_COMPANIES", 2)
        items = [(f"C{i}", f"{BASE}/bedrijf/c{i}") for i in range(5)]
        self.install(monkeypatch, {1: results_html(*items)},
                     {u: profile_html(n, f"https://{n.lower()}.nl", None, "") for n, u in items})
        assert len(m.extract_zakelijkeregister_companies("software")) == 2

    def test_stops_when_page_repeats_itself(self, monkeypatch):
        same = results_html(("A", f"{BASE}/bedrijf/a"))
        self.install(monkeypatch, {p: same for p in range(1, 200)},
                     {f"{BASE}/bedrijf/a": profile_html("A", "https://a.nl", None, "")})
        assert len(m.extract_zakelijkeregister_companies("software")) == 1

    def test_no_results_at_all(self, monkeypatch):
        self.install(monkeypatch, {}, {})
        assert m.extract_zakelijkeregister_companies("zzzz") == []

    def test_page_cap(self, monkeypatch):
        monkeypatch.setattr(m, "ZR_MAX_PAGES", 2)
        pages = {p: results_html((f"C{p}", f"{BASE}/bedrijf/c{p}")) for p in range(1, 10)}
        profiles = {f"{BASE}/bedrijf/c{p}": profile_html(f"C{p}", f"https://c{p}.nl", None, "") for p in range(1, 10)}
        self.install(monkeypatch, pages, profiles)
        assert len(m.extract_zakelijkeregister_companies("s")) == 2


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
