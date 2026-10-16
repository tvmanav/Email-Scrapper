import pytest

import company_email_finder as m


def encode_cfemail(email: str, key: int = 0x42) -> str:
    return bytes([key] + [ord(c) ^ key for c in email]).hex()


# ---------------------------------------------------------------- emails

class TestNormalizeEmail:
    @pytest.mark.parametrize("raw,expected", [
        ("INFO@Acme.COM", "info@acme.com"),
        ("  info@acme.com  ", "info@acme.com"),
        ("info@acme.com.", "info@acme.com"),
        ("info@acme.com);", "info@acme.com"),
        ("<info@acme.com>", "<info@acme.com"),  # only trailing junk is stripped
    ])
    def test_cases(self, raw, expected):
        assert m.normalize_email(raw) == expected


class TestDecodeCloudflareEmail:
    def test_roundtrip(self):
        assert m.decode_cloudflare_email(encode_cfemail("jan@acme.nl")) == "jan@acme.nl"

    def test_different_keys(self):
        for key in (0x00, 0x01, 0x7F, 0xAB):
            assert m.decode_cloudflare_email(encode_cfemail("a@b.co", key)) == "a@b.co"

    @pytest.mark.parametrize("bad", ["", "zz", "4", "42", "not hex at all"])
    def test_invalid_returns_none(self, bad):
        assert m.decode_cloudflare_email(bad) is None

    def test_undecodable_bytes_return_none(self):
        # key 0x00 leaves bytes untouched; 0xff alone is invalid UTF-8
        assert m.decode_cloudflare_email("00ff") is None


class TestExtractEmails:
    def test_empty_inputs(self):
        assert m.extract_emails("") == []
        assert m.extract_emails(None) == []
        assert m.extract_emails("no emails here") == []

    def test_finds_sorted_deduped_lowercase(self):
        text = "Write to Zed@Acme.com or abe@acme.com; again ZED@acme.com"
        assert m.extract_emails(text) == ["abe@acme.com", "zed@acme.com"]

    def test_plus_dots_subdomains(self):
        assert m.extract_emails("a.b+tag@mail.sub.acme.co.uk") == ["a.b+tag@mail.sub.acme.co.uk"]

    def test_sentence_punctuation_not_included(self):
        assert m.extract_emails("Mail info@acme.com.") == ["info@acme.com"]
        assert m.extract_emails("(info@acme.com)") == ["info@acme.com"]

    @pytest.mark.parametrize("junk", [
        "logo@2x.png", "hero@2x.jpg", "photo@3x.jpeg", "icon@2x.gif",
        "banner@2x.webp", "sprite@2x.svg", "fav@2x.ico",
    ])
    def test_image_filenames_excluded(self, junk):
        assert m.extract_emails(f"<img src='{junk}'>") == []

    @pytest.mark.parametrize("junk", [
        "name@example.com", "name@example.org", "user@sentry.io",
        "abc123@sentry.wixpress.com", "x@sentry-next.wixpress.com",
    ])
    def test_placeholder_and_tracking_excluded(self, junk):
        assert m.extract_emails(junk) == []

    def test_real_email_next_to_junk(self):
        assert m.extract_emails("logo@2x.png info@acme.com") == ["info@acme.com"]


# ---------------------------------------------------------------- urls / domains

class TestNormalizeUrl:
    @pytest.mark.parametrize("raw,expected", [
        ("", ""),
        (None, ""),
        ("acme.com", "https://acme.com"),
        ("  acme.com  ", "https://acme.com"),
        ("http://acme.com/", "http://acme.com"),
        ("https://acme.com/about/", "https://acme.com/about"),
        ("https://acme.com/about#team", "https://acme.com/about"),
        ("HTTPS://Acme.com/x", "HTTPS://Acme.com/x"),  # scheme is recognised, not doubled
        ("Http://acme.com", "Http://acme.com"),
    ])
    def test_cases(self, raw, expected):
        assert m.normalize_url(raw) == expected

    def test_keeps_query_string(self):
        assert m.normalize_url("https://acme.com/p?a=1") == "https://acme.com/p?a=1"


class TestGetDomain:
    @pytest.mark.parametrize("url,expected", [
        ("https://www.Acme.com/x", "acme.com"),
        ("http://acme.com", "acme.com"),
        ("https://acme.com:8080/a", "acme.com"),
        ("https://user:pw@acme.com/a", "acme.com"),
        ("https://awww.com", "awww.com"),          # "www." inside a name must survive
        ("https://newwww.acme.com", "newwww.acme.com"),
        ("https://shop.www.acme.com", "shop.www.acme.com"),
        ("https://acme.com.", "acme.com."),
        ("", ""),
    ])
    def test_cases(self, url, expected):
        assert m.get_domain(url) == expected

    def test_same_domain(self):
        assert m.same_domain("https://www.acme.com/a", "http://acme.com/b")
        assert not m.same_domain("https://acme.com", "https://acme.nl")
        assert not m.same_domain("https://acme.com", "https://blog.acme.com")


class TestDomainMatches:
    @pytest.mark.parametrize("email_domain,site,expected", [
        ("acme.nl", "acme.nl", True),
        ("mail.acme.nl", "acme.nl", True),
        ("ACME.nl", "acme.nl", True),
        ("evilacme.nl", "acme.nl", False),   # no dot boundary -> not the same company
        ("acme.nl.evil.com", "acme.nl", False),
        ("gmail.com", "acme.nl", False),
        ("acme.nl", "", False),
    ])
    def test_cases(self, email_domain, site, expected):
        assert m.domain_matches(email_domain, site) is expected


class TestIsSafeUrl:
    def resolves_to(self, monkeypatch, *ips):
        monkeypatch.setattr(m.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, (ip, 0)) for ip in ips])

    def test_public_address_is_safe(self, monkeypatch):
        self.resolves_to(monkeypatch, "93.184.216.34")
        assert m.is_safe_url("https://acme.com/about")

    @pytest.mark.parametrize("ip", [
        "127.0.0.1",        # loopback
        "169.254.169.254",  # link-local / cloud metadata
        "10.0.0.5",         # private
        "192.168.1.1",      # private
        "::1",               # loopback (IPv6)
        "fe80::1",           # link-local (IPv6)
    ])
    def test_internal_address_is_unsafe(self, monkeypatch, ip):
        self.resolves_to(monkeypatch, ip)
        assert not m.is_safe_url("https://acme.com/about")

    def test_any_internal_address_among_multiple_makes_it_unsafe(self, monkeypatch):
        self.resolves_to(monkeypatch, "93.184.216.34", "127.0.0.1")
        assert not m.is_safe_url("https://acme.com/about")

    def test_unresolvable_host_is_not_blocked(self, monkeypatch):
        # Nothing to attack if it doesn't resolve; a transient DNS failure must not drop a company.
        def fail(*a, **k):
            raise m.socket.gaierror("no such host")
        monkeypatch.setattr(m.socket, "getaddrinfo", fail)
        assert m.is_safe_url("https://nowhere.invalid/about")

    def test_missing_host_is_unsafe(self):
        assert not m.is_safe_url("not-a-url")


# ---------------------------------------------------------------- text helpers

class TestAlnum:
    def test_folds_case_accents_and_punctuation(self):
        assert m.alnum("Dragoș Ștefan") == "dragosstefan"
        assert m.alnum("Jane  O'Neil-Smith!") == "janeoneilsmith"
        assert m.alnum("A_B c") == "abc"

    def test_empty(self):
        assert m.alnum("") == ""
        assert m.alnum(None) == ""

    def test_ascii_and_accented_match(self):
        assert m.alnum("Åsén") == m.alnum("Asen")


class TestCleanTitle:
    @pytest.mark.parametrize("raw,expected", [
        ("CEO", "CEO"),
        ("CTO at Selleo / Building teams", "CTO at Selleo"),
        ("Field CTO @ Simform | Executive Leadership", "Field CTO @ Simform"),
        ("Founder · Investor", "Founder"),
        ("  Director  ", "Director"),
        ("", ""),
        (None, ""),
    ])
    def test_cases(self, raw, expected):
        assert m.clean_title(raw) == expected


class TestRoleRank:
    @pytest.mark.parametrize("title,rank", [
        # tier 0
        ("CEO", 0), ("Co-Founder & CEO", 0), ("Founder", 0), ("Owner", 0),
        ("Chief Executive Officer", 0), ("Managing Director", 0), ("President", 0),
        ("Eigenaar", 0), ("Oprichter", 0), ("Algemeen Directeur", 0),
        ("Directeur-Eigenaar", 0), ("Directeur / Eigenaar", 0), ("Directeur", 0),
        ("Bestuurder", 0), ("Zaakvoerder", 0), ("Geschäftsführer", 0),
        ("Inhaber", 0), ("Gründer", 0), ("VD", 0), ("Grundare", 0),
        # tier 1
        ("CTO", 1), ("Chief Technology Officer", 1), ("COO", 1), ("Chief Operating Officer", 1),
        ("General Manager", 1), ("Partner", 1), ("Technisch Directeur", 1),
        ("Financieel directeur", 1),
        # tier 2
        ("Director", 2), ("Director of Sales", 2), ("Directeur Verkoop", 2),
        # tier 3
        ("VP Engineering", 3), ("Vice President of Sales", 3), ("Vice-President", 3),
        # tier 4
        ("Head of Sales", 4), ("Hoofd Marketing", 4),
        # not decision makers
        ("Marketing Manager", 9), ("Software Engineer", 9), ("", 9), (None, 9),
        ("Executive Assistant to the CEO", 9), ("Assistant to the Director", 9),
    ])
    def test_ranks(self, title, rank):
        assert m.role_rank(title) == rank

    def test_vice_president_is_not_president(self):
        assert m.role_rank("Vice President") > m.role_rank("President")

    def test_ordering_puts_senior_first(self):
        titles = ["Head of Sales", "Director", "CTO", "CEO", "VP Sales"]
        assert sorted(titles, key=m.role_rank) == ["CEO", "CTO", "Director", "VP Sales", "Head of Sales"]


class TestRateLimitParsing:
    @pytest.mark.parametrize("message,expected", [
        ("Rate limit. Please try again in 7.5s.", 7.5),
        ("try again in 1m30.5s", 90.5),
        ("try again in 12s", 12.0),
        ("no timing info", None),
        ("", None),
        (None, None),
    ])
    def test_cases(self, message, expected):
        assert m.parse_rate_limit_wait_seconds(message) == expected


class TestLinkedInUrl:
    @pytest.mark.parametrize("url,expected", [
        ("https://www.linkedin.com/company/acme/", ("company", "https://www.linkedin.com/company/acme")),
        ("https://se.linkedin.com/in/jane-doe?trk=x", ("in", "https://www.linkedin.com/in/jane-doe")),
        ("http://linkedin.com/in/jane#frag", ("in", "https://www.linkedin.com/in/jane")),
        ("https://nl.linkedin.com/company/acme-bv/about", ("company", "https://www.linkedin.com/company/acme-bv")),
        ("HTTPS://WWW.LINKEDIN.COM/IN/Jane", ("in", "https://www.linkedin.com/in/Jane")),
        ("https://www.linkedin.com/company/1349302", ("company", "https://www.linkedin.com/company/1349302")),
    ])
    def test_valid(self, url, expected):
        assert m.normalize_linkedin_url(url) == expected

    @pytest.mark.parametrize("url", [
        "", None, "https://acme.com", "https://linkedin.com/", "https://www.linkedin.com/feed/",
        "https://www.linkedin.com/sharing/share-offsite/?url=x", "https://notlinkedin.com/in/x/",
        "https://www.linkedin.com/school/uni", "mailto:a@b.com",
    ])
    def test_invalid(self, url):
        assert m.normalize_linkedin_url(url) is None


class TestPlaceholderEmails:
    @pytest.mark.parametrize("text", [
        "you@company.com", "name@yourdomain.com", "you@acme.nl", "first.last@acme.nl", "email@acme.nl", "john.doe@email.com",
    ])
    def test_placeholders_are_dropped(self, text):
        assert m.extract_emails(f"write to {text} today") == []

    def test_real_addresses_survive(self, ):
        assert m.extract_emails("info@acme.nl and jane.smith@acme.nl") == ["info@acme.nl", "jane.smith@acme.nl"]


class TestBrandMatches:
    @pytest.mark.parametrize("email_domain,site,expected", [
        ("acme.com", "acme.com", True),
        ("acme.de", "acme.com", True),            # regional office of the same brand
        ("kingsmendv.com", "go.kingsmendv.com", True),
        ("acme.co.uk", "www.acme.com", True),
        ("sapientpro.com", "sapient.pro", True),   # same brand with a suffix
        ("bix-tech.com", "bixtech.ai", True),      # hyphen is not a different brand
        ("fulrum.rocks", "fulcrum.rocks", False),  # typo'd look-alike
        ("mightybyte.us", "appmakersla.com", False),
        ("gmail.com", "acme.com", False),
        ("ace.com", "acme.com", False),            # short labels need an exact match
    ])
    def test_cases(self, email_domain, site, expected):
        assert m.brand_matches(email_domain, site) is expected

    def test_label_handles_second_level_suffixes(self):
        assert m.brand_label("shop.acme.co.uk") == "acme" and m.brand_label("acme.com") == "acme"
