from bs4 import BeautifulSoup

import company_email_finder as m
from tests.test_helpers import encode_cfemail


class TestParsePage:
    def test_text_and_visible_emails(self):
        pg = m.parse_page("https://acme.com", "<html><body><p>Mail <b>info@acme.com</b></p></body></html>")
        assert "Mail info@acme.com" in pg.text
        assert pg.emails == ["info@acme.com"]
        assert pg.url == "https://acme.com"

    def test_scripts_styles_noscript_svg_are_ignored(self):
        html = """<html><head><style>.x{content:'css@evil.com'}</style>
        <script>var a='track@analytics.com'</script></head><body>
        <noscript>ns@acme.com</noscript><svg><text>svg@acme.com</text></svg>
        <p>real@acme.com</p></body></html>"""
        pg = m.parse_page("https://acme.com", html)
        assert pg.emails == ["real@acme.com"]
        assert "track" not in pg.text and "css@" not in pg.text

    def test_mailto_with_query_and_case(self):
        pg = m.parse_page("u", '<a href="MAILTO:Sales@Acme.com?subject=Hi&body=x">mail</a>')
        assert pg.emails == ["sales@acme.com"]

    def test_empty_mailto_is_ignored(self):
        assert m.parse_page("u", '<a href="mailto:">x</a>').emails == []

    def test_cloudflare_protected_email_decoded_into_text_and_emails(self):
        enc = encode_cfemail("jan@acme.nl")
        html = f'<p>Contact <a class="__cf_email__" data-cfemail="{enc}">[email&#160;protected]</a></p>'
        pg = m.parse_page("u", html)
        assert pg.emails == ["jan@acme.nl"]
        assert "jan@acme.nl" in pg.text

    def test_undecodable_cfemail_left_alone(self):
        pg = m.parse_page("u", '<a data-cfemail="zz">[email protected]</a>')
        assert pg.emails == []

    def test_form_placeholders_and_values_are_not_emails(self):
        html = '<input placeholder="name@company.com"><textarea placeholder="you@mail.com">x</textarea><input value="v@v.com">'
        assert m.parse_page("u", html).emails == []

    def test_emails_in_attribute_other_than_mailto_are_found(self):
        pg = m.parse_page("u", '<div data-contact="boss@acme.com"></div>')
        assert pg.emails == ["boss@acme.com"]

    def test_empty_and_garbage_html(self):
        assert m.parse_page("u", "").emails == []
        assert m.parse_page("u", "<<<not html>>>").emails == []
        assert m.parse_page("u", "<div><p>unclosed").text == "unclosed"

    def test_linkedin_links_collected(self):
        html = '<a href="https://www.linkedin.com/company/acme">in</a><a href="https://nl.linkedin.com/in/jan-x?a=1">Jan</a>'
        pg = m.parse_page("u", html)
        assert [(l.kind, l.url) for l in pg.linkedin_links] == [
            ("company", "https://www.linkedin.com/company/acme"),
            ("in", "https://www.linkedin.com/in/jan-x"),
        ]


class TestEmailsInsideScripts:
    def test_own_domain_email_in_next_data_script_is_found(self):
        html = '<script id="__NEXT_DATA__" type="application/json">{"props":{"email":"info@slashdev.io"}}</script><p>hi</p>'
        assert m.parse_page("https://slashdev.io", html).emails == ["info@slashdev.io"]

    def test_json_ld_email_found(self):
        html = '<script type="application/ld+json">{"@type":"Organization","email":"hello@acme.nl"}</script>'
        assert m.parse_page("https://www.acme.nl/", html).emails == ["hello@acme.nl"]

    def test_unicode_escaped_at_sign_decoded(self):
        html = '<script>var d = {"mail":"info\\u0040acme.nl"}</script>'
        assert m.parse_page("https://acme.nl", html).emails == ["info@acme.nl"]

    def test_third_party_tracking_emails_in_scripts_still_ignored(self):
        html = '<script>track("owner@analytics-vendor.com"); x("noreply@google.com")</script>'
        assert m.parse_page("https://acme.nl", html).emails == []

    def test_lookalike_domain_in_script_ignored(self):
        html = '<script>{"email":"info@evilacme.nl"}</script>'
        assert m.parse_page("https://acme.nl", html).emails == []

    def test_subdomain_email_in_script_accepted(self):
        assert m.parse_page("https://acme.nl", '<script>{"e":"x@mail.acme.nl"}</script>').emails == ["x@mail.acme.nl"]

    def test_script_text_does_not_leak_into_page_text(self):
        assert "info@acme.nl" not in m.parse_page("https://acme.nl", '<script>{"e":"info@acme.nl"}</script><p>Hi</p>').text


class TestExtractLinkedInLinks:
    def soup(self, html):
        return BeautifulSoup(html, "lxml")

    def test_dedupes_same_url(self):
        links = m.extract_linkedin_links(self.soup(
            '<a href="https://linkedin.com/in/x">a</a><a href="https://www.linkedin.com/in/x/">b</a>'))
        assert len(links) == 1

    def test_non_linkedin_and_share_links_ignored(self):
        links = m.extract_linkedin_links(self.soup(
            '<a href="https://twitter.com/x">t</a><a href="https://www.linkedin.com/sharing/share-offsite/?url=x">s</a><a>no href</a>'))
        assert links == []

    def test_context_uses_alt_aria_and_title(self):
        links = m.extract_linkedin_links(self.soup(
            '<a href="https://linkedin.com/in/jr" aria-label="John Roe" title="CTO"><img alt="J photo"></a>'))
        assert "John Roe" in links[0].context and "CTO" in links[0].context and "J photo" in links[0].context

    def test_parent_text_used_only_when_one_profile_in_parent(self):
        card = '<div><h3>Jane Doe</h3><a href="https://linkedin.com/in/j">LinkedIn</a></div>'
        grid = ('<div><h3>Jane Doe</h3><a href="https://linkedin.com/in/j">in</a>'
                '<h3>John Roe</h3><a href="https://linkedin.com/in/r">in</a></div>')
        assert "Jane Doe" in m.extract_linkedin_links(self.soup(card))[0].context
        # two profiles share a parent: neither may inherit the other's name
        ctx = [l.context for l in m.extract_linkedin_links(self.soup(grid))]
        assert all("Jane" not in c and "John" not in c for c in ctx)

    def test_long_parent_text_not_used(self):
        long_text = "word " * 60
        links = m.extract_linkedin_links(self.soup(
            f'<div><p>{long_text}</p><a href="https://linkedin.com/in/j">in</a></div>'))
        assert "word" not in links[0].context


class TestScoreLink:
    def test_exact_path_segment_beats_many_weak_matches(self):
        strong = m.score_link("x", "https://acme.com/contact")
        weak = m.score_link("about team leadership company people", "https://acme.com/blog/post")
        assert strong > weak

    def test_service_page_with_management_substring_is_weak(self):
        assert m.score_link("", "https://acme.com/cloud-management-outsourcing/") < 1000

    def test_no_keywords_scores_zero(self):
        assert m.score_link("Pricing", "https://acme.com/pricing") == 0

    def test_dutch_and_legal_pages_are_strong(self):
        for path in ("over-ons", "privacy", "impressum", "kontakt", "directie", "algemene-voorwaarden"):
            assert m.score_link("", f"https://acme.com/{path}") >= 1000, path


class TestDiscoverPages:
    HOME = "https://acme.com"

    def links(self, *hrefs):
        return "".join(f'<a href="{h}">{t}</a>' for h, t in hrefs)

    def test_ranks_contact_before_blog(self):
        html = self.links(("/blog/post", "News"), ("/contact", "Contact"), ("/about-us", "About"))
        result = m.discover_pages(self.HOME, html, 10)
        assert result[0].endswith(("/contact", "/about-us"))
        assert not any("blog" in u for u in result[:2])

    def test_excludes_other_domains_and_non_http_links(self):
        html = self.links(("https://other.com/contact", "c"), ("mailto:a@acme.com", "m"),
                          ("tel:+3112345", "t"), ("javascript:void(0)", "j"), ("/team", "Team"))
        assert m.discover_pages(self.HOME, html, 10) == ["https://acme.com/team"]

    def test_strips_fragments_and_dedupes_trailing_slash(self):
        html = self.links(("/contact", "a"), ("/contact/", "b"), ("/contact#form", "c"))
        assert len(m.discover_pages(self.HOME, html, 10)) == 1

    def test_respects_max_pages(self):
        html = self.links(*[(f"/team-{i}", "team") for i in range(10)])
        assert len(m.discover_pages(self.HOME, html, 3)) == 3

    def test_zero_max_pages(self):
        assert m.discover_pages(self.HOME, self.links(("/team", "t")), 0) == []

    def test_relative_links_resolved_against_homepage(self):
        assert m.discover_pages("https://acme.com/en", self.links(("team", "Team")), 5) == ["https://acme.com/team"]

    def test_links_without_href_or_keywords_skipped(self):
        assert m.discover_pages(self.HOME, '<a>team</a><a href="/pricing">Pricing</a>', 5) == []

    def test_www_and_bare_domain_are_the_same_site(self):
        html = self.links(("https://www.acme.com/contact", "c"))
        assert m.discover_pages("https://acme.com", html, 5) == ["https://www.acme.com/contact"]
