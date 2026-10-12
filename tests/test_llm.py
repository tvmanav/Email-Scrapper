import httpx2 as httpx
import pytest
from openai import RateLimitError

import company_email_finder as m
from tests.conftest import page

CO = m.Company("Acme", "https://acme.com")
SIGNAL_PAGE = page(emails=["info@acme.com"])  # has something for the model to extract


def rate_limit(message):
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com"))
    return RateLimitError(message, response=response, body=None)


class FakeCompletion:
    def __init__(self, parsed):
        self.choices = [type("C", (), {"message": type("M", (), {"parsed": parsed})()})()]


class TestLeadershipPage:
    def test_team_and_about_are_leadership(self):
        for path in ("team", "about-us", "over-ons", "directie", "leadership"):
            assert m.is_leadership_page(page(f"https://acme.com/{path}"))

    def test_contact_and_legal_pages_are_not_leadership(self):
        for path in ("contact", "contact-us", "privacy", "impressum", "kontakt", "algemene-voorwaarden"):
            assert not m.is_leadership_page(page(f"https://acme.com/{path}")), path

    def test_homepage_is_not(self):
        assert not m.is_leadership_page(page("https://acme.com"))


class TestPrepareLlmContext:
    def test_includes_company_and_emails(self):
        ctx = m.prepare_llm_context(CO, [page("https://acme.com", "Welcome", ["info@acme.com"])])
        assert "Acme" in ctx and "https://acme.com" in ctx and "info@acme.com" in ctx and "Welcome" in ctx

    def test_team_page_without_email_is_included(self):
        pages = [page("https://acme.com", "home"), page("https://acme.com/team", "Jane Doe CEO")]
        assert "Jane Doe CEO" in m.prepare_llm_context(CO, pages)

    def test_leadership_pages_come_before_other_email_pages(self):
        pages = [page("https://acme.com", "HOMETEXT", ["a@acme.com"]),
                 page("https://acme.com/contact", "CONTACTTEXT", ["c@acme.com"]),
                 page("https://acme.com/team", "TEAMTEXT")]
        ctx = m.prepare_llm_context(CO, pages)
        assert ctx.index("TEAMTEXT") < ctx.index("CONTACTTEXT")

    def test_irrelevant_page_without_email_is_dropped(self):
        pages = [page("https://acme.com", "home"), page("https://acme.com/blog/x", "BLOGTEXT")]
        assert "BLOGTEXT" not in m.prepare_llm_context(CO, pages)

    def test_homepage_without_email_still_included(self):
        assert "HOMETEXT" in m.prepare_llm_context(CO, [page("https://acme.com", "HOMETEXT")])

    def test_text_truncated_per_page_limit(self):
        long = "x" * 20000
        ctx = m.prepare_llm_context(CO, [page("https://acme.com", long, ["a@acme.com"])])
        assert ctx.count("x") == m.LLM_NO_SIGNAL_CHAR_LIMIT  # no email/title in the text -> short head only

    def test_total_budget_respected(self):
        pages = [page(f"https://acme.com/team/{i}", "y" * 3000) for i in range(30)]
        ctx = m.prepare_llm_context(CO, pages)
        included = ctx.count("URL:")
        assert 0 < included < 30  # the cap really cut pages off
        assert len(ctx) <= m.LLM_CONTEXT_CHAR_BUDGET + 500  # budget + the fixed header

    def test_single_page_list(self):
        assert m.prepare_llm_context(CO, [page()])


class TestAnalyzeWithLlm:
    def install(self, monkeypatch, side_effects):
        calls = []
        effects = iter(side_effects)
        def parse(**kwargs):
            calls.append(kwargs)
            effect = next(effects)
            if isinstance(effect, Exception):
                raise effect
            return effect
        monkeypatch.setattr(m.client.chat.completions, "parse", parse)
        return calls

    def test_returns_parsed_result(self, monkeypatch):
        expected = m.LLMDecision(contact_emails=["info@acme.com"])
        calls = self.install(monkeypatch, [FakeCompletion(expected)])
        assert m.analyze_with_llm(CO, [SIGNAL_PAGE]) == expected
        assert calls[0]["model"] == m.LLM_MODEL and calls[0]["response_format"] is m.LLMDecision
        assert calls[0]["messages"][0]["role"] == "system"

    def test_none_parsed_becomes_empty_decision(self, monkeypatch):
        self.install(monkeypatch, [FakeCompletion(None)])
        assert m.analyze_with_llm(CO, [SIGNAL_PAGE]) == m.LLMDecision()

    def test_short_rate_limit_waits_and_retries(self, monkeypatch):
        expected = m.LLMDecision(contact_emails=["a@acme.com"])
        calls = self.install(monkeypatch, [rate_limit("try again in 5s"), FakeCompletion(expected)])
        assert m.analyze_with_llm(CO, [SIGNAL_PAGE]) == expected
        assert len(calls) == 2

    def test_long_rate_limit_fails_fast(self, monkeypatch):
        calls = self.install(monkeypatch, [rate_limit("try again in 20m0.5s")])
        with pytest.raises(RateLimitError):
            m.analyze_with_llm(CO, [SIGNAL_PAGE])
        assert len(calls) == 1

    def test_rate_limit_without_timing_info_raises(self, monkeypatch):
        self.install(monkeypatch, [rate_limit("slow down")])
        with pytest.raises(RateLimitError):
            m.analyze_with_llm(CO, [SIGNAL_PAGE])

    def test_gives_up_after_max_retries(self, monkeypatch):
        calls = self.install(monkeypatch, [rate_limit("try again in 1s")] * 10)
        with pytest.raises(RateLimitError):
            m.analyze_with_llm(CO, [SIGNAL_PAGE])
        assert len(calls) == m.RATE_LIMIT_MAX_RETRIES + 1

    def test_other_errors_propagate(self, monkeypatch):
        self.install(monkeypatch, [ValueError("bad json")])
        with pytest.raises(ValueError):
            m.analyze_with_llm(CO, [SIGNAL_PAGE])


def dm(name, title, email=""):
    return m.DecisionMaker(name=name, title=title, email=email)


class TestValidateResult:
    PAGES = [page("https://acme.com/team", "Jane Doe is our CEO. John Roe, CTO. Bob Smith leads Sales.",
                  ["jane@acme.com", "info@acme.com", "outsider@gmail.com"])]

    def test_keeps_person_with_real_email(self):
        out = m.validate_result(m.LLMDecision(decision_makers=[dm("Jane Doe", "CEO", "jane@acme.com")]), self.PAGES)
        assert [(p.name, p.email) for p in out.decision_makers] == [("Jane Doe", "jane@acme.com")]
        assert out.contact_emails == []

    def test_invented_email_is_blanked_but_person_kept(self):
        out = m.validate_result(m.LLMDecision(decision_makers=[dm("Jane Doe", "CEO", "made.up@acme.com")]), self.PAGES)
        assert out.decision_makers[0].email == ""

    def test_invented_person_dropped(self):
        out = m.validate_result(m.LLMDecision(decision_makers=[dm("Ghost Person", "CEO", "jane@acme.com")]), self.PAGES)
        assert out.decision_makers == []

    def test_email_case_and_whitespace_normalized(self):
        out = m.validate_result(m.LLMDecision(decision_makers=[dm(" Jane Doe ", " CEO ", " JANE@Acme.com ")]), self.PAGES)
        assert (out.decision_makers[0].name, out.decision_makers[0].title, out.decision_makers[0].email) == ("Jane Doe", "CEO", "jane@acme.com")

    def test_name_matching_ignores_case_accents_and_spacing(self):
        pages = [page(text="Dragoș   Ștefan\nCOO")]
        out = m.validate_result(m.LLMDecision(decision_makers=[dm("dragos stefan", "COO")]), pages)
        assert len(out.decision_makers) == 1

    def test_duplicates_collapsed(self):
        res = m.LLMDecision(decision_makers=[dm("Jane Doe", "CEO"), dm("jane doe", "Founder")])
        assert len(m.validate_result(res, self.PAGES).decision_makers) == 1

    def test_sorted_by_seniority_and_capped(self):
        names = [f"Person{i} Name{i}" for i in range(8)]
        pages = [page(text=" ".join(names))]
        res = m.LLMDecision(decision_makers=[dm(n, "Head of Sales") for n in names[:7]] + [dm(names[7], "CEO")])
        out = m.validate_result(res, pages)
        assert len(out.decision_makers) == m.MAX_DECISION_MAKERS
        assert out.decision_makers[0].title == "CEO"

    def test_contact_fallback_when_no_dm_email(self):
        res = m.LLMDecision(decision_makers=[dm("John Roe", "CTO")], contact_emails=["info@acme.com"])
        out = m.validate_result(res, self.PAGES)
        assert out.contact_emails == ["info@acme.com"] and out.decision_makers[0].name == "John Roe"

    def test_contact_emails_ignored_when_dm_has_email(self):
        res = m.LLMDecision(decision_makers=[dm("Jane Doe", "CEO", "jane@acme.com")], contact_emails=["info@acme.com"])
        assert m.validate_result(res, self.PAGES).contact_emails == []

    def test_llm_contact_email_must_exist_on_site(self):
        out = m.validate_result(m.LLMDecision(contact_emails=["fake@acme.com"]), self.PAGES)
        assert "fake@acme.com" not in out.contact_emails

    def test_generic_prefix_heuristic_when_llm_empty(self):
        assert m.validate_result(m.LLMDecision(), self.PAGES).contact_emails == ["info@acme.com"]

    def test_own_domain_fallback_when_no_generic_address(self):
        pages = [page(emails=["jan@acme.com", "x@gmail.com"])]
        assert m.validate_result(m.LLMDecision(), pages).contact_emails == ["jan@acme.com"]

    def test_lookalike_domain_not_accepted(self):
        pages = [page(emails=["jan@evilacme.com"])]
        assert m.validate_result(m.LLMDecision(), pages).contact_emails == []

    def test_third_party_only_gives_nothing(self):
        pages = [page(emails=["someone@gmail.com"])]
        assert m.validate_result(m.LLMDecision(), pages).contact_emails == []

    def test_no_pages(self):
        assert m.validate_result(m.LLMDecision(), []) == m.LLMDecision()

    def test_email_from_any_crawled_page_counts(self):
        pages = [page("https://acme.com", "Jane Doe CEO"), page("https://acme.com/contact", "", ["jane@acme.com"])]
        out = m.validate_result(m.LLMDecision(decision_makers=[dm("Jane Doe", "CEO", "jane@acme.com")]), pages)
        assert out.decision_makers[0].email == "jane@acme.com"


class TestRelevantText:
    def test_keeps_window_around_email_and_title_drops_the_rest(self):
        text = "menu " * 300 + "Jane Doe, CEO of Acme, reach her at jane@acme.com today." + " footer" * 300
        out = m.relevant_text(text, ["jane@acme.com"], 2000)
        assert "Jane Doe" in out and "jane@acme.com" in out
        assert len(out) < len(text) / 3 and out.count("menu") < 120

    def test_overlapping_windows_are_merged_not_repeated(self):
        text = "Jane Doe CEO and John Roe CTO work here. " * 3
        out = m.relevant_text(text, [], 5000)
        assert " … " not in out  # one contiguous block

    def test_distant_hits_are_joined_with_ellipsis(self):
        text = "CEO Jane" + (" filler" * 400) + " CTO John"
        assert " … " in m.relevant_text(text, [], 5000)

    def test_no_signal_returns_short_head(self):
        assert m.relevant_text("lorem " * 1000, [], 5000) == ("lorem " * 1000)[:m.LLM_NO_SIGNAL_CHAR_LIMIT]

    def test_respects_limit(self):
        text = "CEO Jane Doe. " * 500
        assert len(m.relevant_text(text, [], 700)) <= 700

    def test_dutch_german_swedish_titles_count_as_signal(self):
        for word in ("Eigenaar", "Directeur", "Geschäftsführer", "Inhaber", "Gründer", "VD", "Grundare", "Oprichter"):
            assert m.ROLE_WORDS_RE.search(f"Jan Smit, {word}"), word

    def test_email_not_in_text_is_ignored_safely(self):
        assert m.relevant_text("hello world", ["x@y.nl"], 100) == "hello world"

    def test_empty_text(self):
        assert m.relevant_text("", [], 100) == ""


class TestLlmSignalShortcut:
    def test_signal_from_email_or_title(self):
        assert m.has_llm_signal([page(emails=["a@acme.com"])])
        assert m.has_llm_signal([page(text="Our Managing Director welcomes you")])

    def test_no_signal(self):
        assert not m.has_llm_signal([page(text="We build software for hospitals.")])
        assert not m.has_llm_signal([])

    def test_model_not_called_when_nothing_to_extract(self, monkeypatch):
        def explode(**kwargs):
            raise AssertionError("model must not be called")
        monkeypatch.setattr(m.client.chat.completions, "parse", explode)
        assert m.analyze_with_llm(CO, [page(text="We build software.")]) == m.LLMDecision()


class TestTokenBudget:
    def test_typical_team_page_context_is_small(self):
        menu = "Home About Services Blog Careers Contact " * 80
        team = "Jane Doe CEO. John Roe CTO. Reach us at info@acme.com. "
        pages = [page("https://acme.com", menu + team + menu, ["info@acme.com"]),
                 page("https://acme.com/team", menu + team + menu)]
        ctx = m.prepare_llm_context(CO, pages)
        assert len(ctx) <= m.LLM_CONTEXT_CHAR_BUDGET and "Jane Doe" in ctx and "info@acme.com" in ctx
        assert len(ctx) < 3000  # was ~16k chars of mostly menu text before


class TestHeuristicsOnlySwitch:
    def test_use_llm_off_skips_the_model(self, monkeypatch):
        monkeypatch.setattr(m, "USE_LLM", False)
        monkeypatch.setattr(m, "_analyze_with_llm", lambda *a: pytest.fail("model was called"))
        assert m.analyze_with_llm(m.Company(name="A", url="https://a.com"), []) == m.LLMDecision()


class TestForeignDomainContactEmails:
    def test_llm_pick_on_another_company_domain_is_dropped(self):
        pages = [page("https://acme.com", "Contact us", ["info@acme.com", "team@partnerco.us"])]
        result = m.validate_result(m.LLMDecision(contact_emails=["team@partnerco.us", "info@acme.com"]), pages)
        assert result.contact_emails == ["info@acme.com"]

    def test_regional_domain_of_the_same_brand_is_kept(self):
        pages = [page("https://acme.com", "Offices", ["info@acme.com", "info@acme.de"])]
        result = m.validate_result(m.LLMDecision(contact_emails=["info@acme.com", "info@acme.de"]), pages)
        assert result.contact_emails == ["info@acme.com", "info@acme.de"]
