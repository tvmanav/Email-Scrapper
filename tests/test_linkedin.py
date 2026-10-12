import pytest

import company_email_finder as m
from tests.conftest import page

CO = m.Company("Acme Software", "https://www.acmesoft.nl")


def link(kind, url, context=""):
    return m.LinkedInLink(kind=kind, url=url, context=context)


def hit(href, title, body=""):
    return {"href": href, "title": title, "body": body}


@pytest.fixture
def search(monkeypatch):
    """Replace search_web with canned results keyed by a substring of the query."""
    log = []
    def install(mapping):
        def fake(query, budget):
            log.append(query)
            for needle, results in mapping.items():
                if needle in query:
                    return results
            return []
        monkeypatch.setattr(m, "search_web", fake)
        return log
    return install


class TestSearchBudget:
    def test_counts_down(self):
        b = m.SearchBudget(2)
        assert b.take() and b.take() and not b.take() and b.remaining == 0

    def test_zero_budget(self):
        assert not m.SearchBudget(0).take()


class TestSearchWeb:
    def fake_ddgs(self, monkeypatch, behaviours):
        calls = []
        it = iter(behaviours)
        class D:
            def __init__(self, **kwargs):
                pass

            def text(self, query, max_results=None):
                calls.append(query)
                b = next(it)
                if isinstance(b, Exception):
                    raise b
                return b
        monkeypatch.setattr(m, "DDGS", D)
        return calls

    def test_returns_results(self, monkeypatch):
        self.fake_ddgs(monkeypatch, [[{"href": "x"}]])
        assert m.search_web("q", m.SearchBudget(3)) == [{"href": "x"}]

    def test_uses_configured_timeout_not_the_library_default(self, monkeypatch):
        seen = {}

        class D:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def text(self, query, max_results=None):
                return [{"href": "x"}]

        monkeypatch.setattr(m, "DDGS", D)
        m.search_web("q", m.SearchBudget(3))
        assert seen == {"timeout": m.LINKEDIN_SEARCH_TIMEOUT}

    def test_retries_then_succeeds(self, monkeypatch):
        calls = self.fake_ddgs(monkeypatch, [TimeoutError("t"), [{"href": "x"}]])
        assert m.search_web("q", m.SearchBudget(3)) == [{"href": "x"}]
        assert len(calls) == 2

    def test_gives_up_after_repeated_real_errors(self, monkeypatch):
        calls = self.fake_ddgs(monkeypatch, [TimeoutError("timed out")] * 5)
        assert m.search_web("q", m.SearchBudget(3)) == []
        assert len(calls) == m.LINKEDIN_SEARCH_MAX_ATTEMPTS

    def test_no_results_is_final_and_not_retried(self, monkeypatch):
        calls = self.fake_ddgs(monkeypatch, [RuntimeError("No results found."), [{"href": "x"}]])
        assert m.search_web("q", m.SearchBudget(3)) == []
        assert len(calls) == 1

    def test_searches_are_spaced_out(self, monkeypatch):
        import time as _time
        sleeps, clock = [], {"t": 100.0}
        monkeypatch.setattr(_time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(m, "last_search_at", 0.0)
        m.throttle_search()                      # first search never waits
        clock["t"] += 0.4
        m.throttle_search()                      # 0.4s later: must wait the rest of the interval
        assert sleeps == [pytest.approx(m.LINKEDIN_SEARCH_MIN_INTERVAL - 0.4)]

    def test_none_result_becomes_empty_list(self, monkeypatch):
        self.fake_ddgs(monkeypatch, [None])
        assert m.search_web("q", m.SearchBudget(1)) == []

    def test_budget_exhausted_makes_no_request(self, monkeypatch):
        calls = self.fake_ddgs(monkeypatch, [[{"href": "x"}]])
        assert m.search_web("q", m.SearchBudget(0)) == []
        assert calls == []

    def test_one_budget_unit_per_logical_search_even_with_retries(self, monkeypatch):
        self.fake_ddgs(monkeypatch, [TimeoutError("t"), [{"href": "x"}]])
        budget = m.SearchBudget(1)
        m.search_web("q", budget)
        assert budget.remaining == 0

    def test_disabled_flag(self, monkeypatch):
        calls = self.fake_ddgs(monkeypatch, [[{"href": "x"}]])
        monkeypatch.setattr(m, "ENABLE_LINKEDIN_SEARCH", False)
        assert m.search_web("q", m.SearchBudget(3)) == []
        assert calls == []


class TestResultHead:
    @pytest.mark.parametrize("title,expected", [
        ("Jane Doe - CEO at Acme | LinkedIn", "Jane Doe - CEO at Acme"),
        ("Jane Doe – CEO at Acme | LinkedInJohn Roe - CTO | LinkedIn", "Jane Doe – CEO at Acme"),
        ("Jane Doe - CEO | Acme ...John Roe - CTO at Other | LinkedIn", "Jane Doe - CEO | Acme"),
        ("Jane Doe - CEO at Acme…Other Guy - X", "Jane Doe - CEO at Acme"),
        ("", ""),
    ])
    def test_cases(self, title, expected):
        assert m.result_head({"title": title}) == expected

    def test_missing_title(self):
        assert m.result_head({}) == ""


class TestCompanyKeys:
    def test_keys_from_name_domain_and_first_word(self):
        assert m.company_keys(CO) == {"acmesoftware", "acmesoft", "acme"}

    def test_short_keys_dropped(self):
        assert m.company_keys(m.Company("IT", "https://it.nl")) == set()

    def test_empty_name(self):
        assert m.company_keys(m.Company("", "https://acmesoft.nl")) == {"acmesoft"}


class TestFindCompanyLinkedin:
    def test_directory_value_wins(self, search):
        search({})
        co = m.Company("Acme", "https://acme.nl", linkedin="https://www.linkedin.com/company/from-dir")
        assert m.find_company_linkedin(co, [], m.SearchBudget(3)) == "https://www.linkedin.com/company/from-dir"

    def test_prefers_link_matching_company_over_partner(self, search):
        search({})
        pages = [page("https://www.acmesoft.nl", links=[
            link("company", "https://www.linkedin.com/company/some-partner"),
            link("company", "https://www.linkedin.com/company/acme-software")])]
        assert m.find_company_linkedin(CO, pages, m.SearchBudget(3)).endswith("acme-software")

    def test_numeric_slug_falls_back_to_homepage_link(self, search):
        search({})
        pages = [page(links=[link("company", "https://www.linkedin.com/company/1349302")])]
        assert m.find_company_linkedin(CO, pages, m.SearchBudget(3)).endswith("1349302")

    def test_unmatched_link_on_inner_page_is_not_trusted(self, search):
        log = search({})
        pages = [page("https://www.acmesoft.nl"), page("https://www.acmesoft.nl/cases",
                 links=[link("company", "https://www.linkedin.com/company/big-client")])]
        assert m.find_company_linkedin(CO, pages, m.SearchBudget(3)) == ""
        assert log  # fell through to search

    def test_person_links_ignored(self, search):
        search({})
        pages = [page(links=[link("in", "https://www.linkedin.com/in/jane")])]
        assert m.find_company_linkedin(CO, pages, m.SearchBudget(3)) == ""

    def test_search_fallback_accepts_matching_slug(self, search):
        search({"linkedin.com/company": [hit("https://nl.linkedin.com/company/acme-software-bv?x=1", "Acme")]})
        assert m.find_company_linkedin(CO, [], m.SearchBudget(3)) == "https://www.linkedin.com/company/acme-software-bv"

    def test_search_fallback_rejects_unrelated_company_and_persons(self, search):
        search({"linkedin.com/company": [
            hit("https://www.linkedin.com/company/totally-other", "x"),
            hit("https://www.linkedin.com/in/acme-fan", "x")]})
        assert m.find_company_linkedin(CO, [], m.SearchBudget(3)) == ""

    def test_no_results(self, search):
        search({})
        assert m.find_company_linkedin(CO, [], m.SearchBudget(3)) == ""


class TestFindSiteLinkedin:
    def test_matches_by_slug(self):
        pages = [page(links=[link("in", "https://www.linkedin.com/in/jane-doe-123")])]
        assert m.find_site_linkedin(m.Person("Jane Doe", "CEO"), pages).endswith("jane-doe-123")

    def test_matches_by_context_when_slug_is_opaque(self):
        pages = [page(links=[link("in", "https://www.linkedin.com/in/jroe", "John Roe CTO")])]
        assert m.find_site_linkedin(m.Person("John Roe", "CTO"), pages).endswith("jroe")

    def test_other_people_do_not_match(self):
        pages = [page(links=[link("in", "https://www.linkedin.com/in/john-roe")])]
        assert m.find_site_linkedin(m.Person("Jane Doe", "CEO"), pages) == ""

    def test_single_name_is_never_matched(self):
        pages = [page(links=[link("in", "https://www.linkedin.com/in/bart")])]
        assert m.find_site_linkedin(m.Person("Bart", "Directeur"), pages) == ""

    def test_company_links_ignored(self):
        pages = [page(links=[link("company", "https://www.linkedin.com/company/jane-doe")])]
        assert m.find_site_linkedin(m.Person("Jane Doe", "CEO"), pages) == ""

    def test_accent_insensitive(self):
        pages = [page(links=[link("in", "https://www.linkedin.com/in/dragos-stefan-41")])]
        assert m.find_site_linkedin(m.Person("Dragoș Ștefan", "COO"), pages)


class TestFindSearchLinkedin:
    P = m.Person("Jane Doe", "CEO")

    def test_accepts_name_and_company_match(self, search):
        search({"Jane Doe": [hit("https://se.linkedin.com/in/jane-doe-9", "Jane Doe - CEO at Acme Software | LinkedIn")]})
        assert m.find_search_linkedin(self.P, CO, m.SearchBudget(3)) == "https://www.linkedin.com/in/jane-doe-9"

    def test_company_in_snippet_counts(self, search):
        search({"Jane Doe": [hit("https://www.linkedin.com/in/jane", "Jane Doe | LinkedIn", "CEO at Acme Software, Netherlands")]})
        assert m.find_search_linkedin(self.P, CO, m.SearchBudget(3))

    def test_rejects_wrong_person(self, search):
        search({"Jane Doe": [hit("https://www.linkedin.com/in/john-roe", "John Roe - CEO at Acme Software | LinkedIn")]})
        assert m.find_search_linkedin(self.P, CO, m.SearchBudget(3)) == ""

    def test_rejects_right_name_wrong_company(self, search):
        search({"Jane Doe": [hit("https://www.linkedin.com/in/jane-doe", "Jane Doe - CEO at Globex | LinkedIn", "Globex Inc")]})
        assert m.find_search_linkedin(self.P, CO, m.SearchBudget(3)) == ""

    def test_rejects_company_pages(self, search):
        search({"Jane Doe": [hit("https://www.linkedin.com/company/jane-doe", "Jane Doe Acme Software")]})
        assert m.find_search_linkedin(self.P, CO, m.SearchBudget(3)) == ""

    def test_skips_wrong_hit_and_takes_next(self, search):
        search({"Jane Doe": [hit("https://www.linkedin.com/in/other", "Other - CEO at Acme Software"),
                             hit("https://www.linkedin.com/in/jane-doe", "Jane Doe - CEO at Acme Software")]})
        assert m.find_search_linkedin(self.P, CO, m.SearchBudget(3)).endswith("jane-doe")

    def test_single_name_skips_search(self, search):
        log = search({})
        assert m.find_search_linkedin(m.Person("Bart", "Directeur"), CO, m.SearchBudget(3)) == ""
        assert log == []

    def test_accent_in_site_name_matches_ascii_result(self, search):
        search({"Ștefan": [hit("https://www.linkedin.com/in/dragos-stefan", "Dragos Stefan - COO at Acme Software | LinkedIn")]})
        assert m.find_search_linkedin(m.Person("Dragoș Ștefan", "COO"), CO, m.SearchBudget(3))


class TestFindPeopleByRole:
    def run(self, search, results):
        search({"linkedin.com/in": results})
        return m.find_people_by_role(CO, m.SearchBudget(6))

    def test_accepts_current_senior_role(self, search):
        people = self.run(search, [hit("https://www.linkedin.com/in/jane", "Jane Doe - CEO at Acme Software | LinkedIn")])
        assert [(p.name, p.title) for p in people] == [("Jane Doe", "CEO at Acme Software")]

    def test_accepts_dutch_bij(self, search):
        people = self.run(search, [hit("https://www.linkedin.com/in/jan", "Jan Smit - Eigenaar bij Acme Software | LinkedIn")])
        assert [p.name for p in people] == ["Jan Smit"]

    @pytest.mark.parametrize("title", [
        "Jane Doe - Marketing Manager at Acme Software | LinkedIn",       # not senior
        "Jane Doe - Associate Director at Acme Software | LinkedIn",       # junior
        "Jane Doe - Director of Sales at Acme Software | LinkedIn",        # tier 2 is excluded
        "Jane Doe - Ex CEO at Acme Software | LinkedIn",                   # former
        "Jane Doe - Former CEO at Acme Software | LinkedIn",
        "Jane Doe - CEO at Acme Software Holdings | LinkedIn",             # namesake
        "Jane Doe - CEO at Globex | LinkedIn",                             # other company
        "Jane Doe - CEO | Acme Software | Globex | LinkedIn",              # employer list, no "at"
        "Jane Doe - CEO | LinkedIn",                                       # no employer
        "CEO at Acme Software | LinkedIn",                                 # no name / one word
        "Jane Doe 3rd - CEO at Acme Software | LinkedIn",                  # digit in name
        "Executive Assistant to CEO at Acme Software | LinkedIn",
    ])
    def test_rejects(self, search, title):
        assert self.run(search, [hit("https://www.linkedin.com/in/x", title)]) == []

    def test_role_search_people_are_flagged_unverified(self, search):
        people = self.run(search, [hit("https://www.linkedin.com/in/j", "Jane Doe - CEO at Acme Software | LinkedIn")])
        assert people[0].unverified

    def test_allows_legal_suffix(self, search):
        people = self.run(search, [hit("https://www.linkedin.com/in/j", "Jane Doe - CEO at Acme Software Inc | LinkedIn")])
        assert len(people) == 1

    def test_strips_headline_tail_from_title(self, search):
        people = self.run(search, [hit("https://www.linkedin.com/in/j", "Jane Doe - CTO at Acme Software / Building teams | LinkedIn")])
        assert people[0].title == "CTO at Acme Software"

    def test_dedupes_same_profile_and_sorts_by_seniority(self, search):
        people = self.run(search, [
            hit("https://www.linkedin.com/in/b", "Bob Ray - COO at Acme Software | LinkedIn"),
            hit("https://www.linkedin.com/in/a", "Ann Lee - CEO at Acme Software | LinkedIn"),
            hit("https://nl.linkedin.com/in/a", "Ann Lee - CEO at Acme Software | LinkedIn")])
        assert [p.name for p in people] == ["Ann Lee", "Bob Ray"]

    def test_skips_non_profile_results(self, search):
        assert self.run(search, [hit("https://www.linkedin.com/company/acme", "Acme Software - CEO at Acme Software")]) == []

    def test_no_results(self, search):
        assert self.run(search, []) == []

    def test_respects_max_role_searches(self, search, monkeypatch):
        monkeypatch.setattr(m, "LINKEDIN_MAX_ROLE_SEARCHES", 1)
        log = search({})
        m.find_people_by_role(CO, m.SearchBudget(6))
        assert len(log) == 1


class TestEnrichLinkedin:
    def vd(self, *people):
        return m.LLMDecision(decision_makers=list(people))

    def test_site_link_used_before_any_search(self, search):
        log = search({})
        pages = [page(links=[link("company", "https://www.linkedin.com/company/acme-software"),
                             link("in", "https://www.linkedin.com/in/jane-doe")])]
        company, people = m.enrich_linkedin(CO, pages, self.vd(m.DecisionMaker(name="Jane Doe", title="CEO", email="")))
        assert company.endswith("acme-software") and people[0].linkedin_url.endswith("jane-doe")
        assert not people[0].unverified  # named on the company's own site
        assert log == []

    def test_searches_for_missing_profile(self, search):
        search({"Jane Doe": [hit("https://www.linkedin.com/in/jd", "Jane Doe - CEO at Acme Software | LinkedIn")]})
        _, people = m.enrich_linkedin(CO, [page()], self.vd(m.DecisionMaker(name="Jane Doe", title="CEO", email="")))
        assert people[0].linkedin_url.endswith("/jd")

    def test_unfindable_person_kept_without_link(self, search):
        search({})
        _, people = m.enrich_linkedin(CO, [page()], self.vd(m.DecisionMaker(name="Jane Doe", title="CEO", email="j@x.nl")))
        assert (people[0].name, people[0].email, people[0].linkedin_url) == ("Jane Doe", "j@x.nl", "")

    def test_role_search_only_when_site_names_nobody(self, search):
        log = search({"linkedin.com/in": [hit("https://www.linkedin.com/in/j", "Jane Doe - CEO at Acme Software | LinkedIn")]})
        _, people = m.enrich_linkedin(CO, [page()], self.vd())
        assert [p.name for p in people] == ["Jane Doe"] and log

    def test_total_search_budget_enforced(self, monkeypatch):
        used = []
        def fake(query, budget):
            if budget.take():
                used.append(query)
            return []
        monkeypatch.setattr(m, "search_web", fake)
        dms = [m.DecisionMaker(name=f"Pers{i} Name{i}", title="CEO", email="") for i in range(5)]
        m.enrich_linkedin(CO, [page()], self.vd(*dms))
        assert len(used) <= m.LINKEDIN_MAX_SEARCHES_PER_COMPANY
