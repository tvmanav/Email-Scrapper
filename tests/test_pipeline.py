import json

import pandas as pd
import pytest

import company_email_finder as m
from tests.conftest import page

CO = m.Company("Acme", "https://acme.nl")
P = m.Person


def dm(name, title, email=""):
    return m.DecisionMaker(name=name, title=title, email=email)


class TestSplitName:
    def test_two_words(self):
        assert m.split_name("Jane Doe") == ("Jane", "Doe")

    def test_middle_name_kept_in_last_name(self):
        assert m.split_name("Mohammed Faleh Al-Qahtani") == ("Mohammed", "Faleh Al-Qahtani")

    def test_single_word(self):
        assert m.split_name("Madonna") == ("Madonna", "")

    def test_empty(self):
        assert m.split_name("") == ("", "")
        assert m.split_name(None) == ("", "")

    def test_non_breaking_space_still_splits(self):
        # A name scraped off a page can be joined by \xa0 instead of a plain
        # space; partition(" ") alone would leave it stuck in first_name.
        assert m.split_name("Alex\xa0Melchenko") == ("Alex", "Melchenko")

    def test_join_is_the_inverse(self):
        for name in ("Jane Doe", "Mohammed Faleh Al-Qahtani", "Madonna"):
            assert m.join_name(*m.split_name(name)) == name


class TestCompanyEmailHelpers:
    def test_join_dedupes_and_sorts(self):
        assert m.join_company_emails(["b@a.nl", "a@a.nl", "a@a.nl"]) == "a@a.nl; b@a.nl"

    def test_join_empty(self):
        assert m.join_company_emails([]) == ""

    def test_split_is_the_inverse(self):
        assert m.split_company_email("a@a.nl; b@a.nl") == ["a@a.nl", "b@a.nl"]
        assert m.split_company_email("") == []
        assert m.split_company_email(None) == []


class TestRowHelpers:
    def test_empty_result_has_every_column(self):
        row = m.empty_result(CO)
        assert set(row) == set(m.OUTPUT_COLUMNS)
        assert row["company_name"] == "Acme" and row["company_url"] == "https://acme.nl"
        assert row["decision_maker_email"] == "" and row["company_email"] == ""

    def test_build_rows_one_row_per_contact(self):
        rows = m.build_rows(
            CO, [P("A B", "CEO", "a@a.nl"), P("C D", "CTO")], ["z@a.nl", "b@a.nl", "b@a.nl"],
            "https://www.linkedin.com/company/a",
        )
        # One row per named contact - the generic addresses are deduped,
        # joined, and repeated on every row (like company_linkedin), not
        # spun out into their own separate rows.
        assert len(rows) == 2
        assert [(r["first_name"], r["last_name"]) for r in rows] == [("A", "B"), ("C", "D")]
        assert rows[0]["decision_maker_email"] == "a@a.nl" and rows[1]["decision_maker_email"] == ""
        assert all(r["company_email"] == "b@a.nl; z@a.nl" for r in rows)
        assert all(row["company_linkedin"].endswith("/a") for row in rows)

    def test_build_rows_generic_emails_without_contacts_get_one_row(self):
        rows = m.build_rows(CO, [], ["b@a.nl", "a@a.nl"], "")
        assert len(rows) == 1
        assert rows[0]["company_email"] == "a@a.nl; b@a.nl"
        assert rows[0]["first_name"] == ""

    def test_build_rows_unverified_person_marked(self):
        rows = m.build_rows(CO, [P("Jane Doe", "CEO", linkedin_url="u", unverified=True)], [], "")
        assert rows[0]["contact_title"] == "CEO [unverified]"
        assert "[unverified]" not in m.build_rows(CO, [P("Jane Doe", "CEO", linkedin_url="u")], [], "")[0]["contact_title"]

    def test_build_rows_no_contact_gives_one_placeholder_row(self):
        rows = m.build_rows(CO, [], [], "")
        assert len(rows) == 1
        assert rows[0]["first_name"] == "" and rows[0]["decision_maker_email"] == "" and rows[0]["company_email"] == ""
        assert rows[0]["company_name"] == "Acme" and rows[0]["company_url"] == "https://acme.nl"

    def test_linkedin_incomplete(self):
        done = [P("A B", "CEO", linkedin_url="u")]
        assert not m.linkedin_incomplete("c", done)
        assert m.linkedin_incomplete("", done)
        assert m.linkedin_incomplete("c", [])
        assert m.linkedin_incomplete("c", done + [P("C D", "CTO")])


class TestMergePeople:
    def test_fills_missing_links_only(self):
        old = [P("Jane Doe", "CEO"), P("Ann Lee", "CTO", linkedin_url="old")]
        new = [P("Jane Doe", "CEO", linkedin_url="jane"), P("Ann Lee", "CTO", linkedin_url="new")]
        merged = m.merge_people(old, new)
        assert [(p.name, p.linkedin_url) for p in merged] == [("Jane Doe", "jane"), ("Ann Lee", "old")]

    def test_adopts_new_list_when_old_empty(self):
        assert m.merge_people([], [P("A B", "CEO", linkedin_url="x")])[0].name == "A B"

    def test_unmatched_new_people_not_mixed_in(self):
        merged = m.merge_people([P("Jane Doe", "CEO")], [P("Other Guy", "CEO", linkedin_url="x")])
        assert [p.name for p in merged] == ["Jane Doe"] and merged[0].linkedin_url == ""

    def test_accent_insensitive_name_match(self):
        merged = m.merge_people([P("Dragoș Ștefan", "COO")], [P("Dragos Stefan", "COO", linkedin_url="d")])
        assert merged[0].linkedin_url == "d"

    def test_both_empty(self):
        assert m.merge_people([], []) == []


class TestMergePeopleUnion:
    def test_each_side_fills_the_others_gaps(self):
        old = [P("Jane Doe", "CEO", linkedin_url="j")]
        new = [P("Jane Doe", "", email="jane@a.nl")]
        merged = m.merge_people_union(old, new)
        assert len(merged) == 1
        assert merged[0].title == "CEO" and merged[0].linkedin_url == "j" and merged[0].email == "jane@a.nl"

    def test_names_found_on_only_one_side_are_kept(self):
        merged = m.merge_people_union([P("Jane Doe", "CEO")], [P("Bob Ray", "CTO")])
        assert {p.name for p in merged} == {"Jane Doe", "Bob Ray"}

    def test_unverified_only_when_both_sides_are(self):
        merged = m.merge_people_union([P("Jane Doe", "CEO", unverified=True)], [P("Jane Doe", "CEO", unverified=False)])
        assert merged[0].unverified is False


@pytest.fixture
def pipeline(monkeypatch):
    """Stub crawl / LLM / LinkedIn so process_company can be exercised in isolation."""
    state = {"pages": [page("https://acme.nl", "Jane Doe CEO", ["info@acme.nl"])],
             "llm": m.LLMDecision(), "enrich": ("", []), "enrich_calls": 0}

    monkeypatch.setattr(m, "crawl_company", lambda company: state["pages"])

    def analyze(company, pages):
        if isinstance(state["llm"], Exception):
            raise state["llm"]
        return state["llm"]
    monkeypatch.setattr(m, "analyze_with_llm", analyze)

    def enrich(company, pages, validated):
        state["enrich_calls"] += 1
        if isinstance(state["enrich"], Exception):
            raise state["enrich"]
        return state["enrich"]
    monkeypatch.setattr(m, "enrich_linkedin", enrich)
    return state


class TestProcessCompany:
    def test_dm_email_found_skips_linkedin(self, pipeline):
        pipeline["pages"] = [page("https://acme.nl", "Jane Doe CEO", ["jane@acme.nl"])]
        pipeline["llm"] = m.LLMDecision(decision_makers=[dm("Jane Doe", "CEO", "jane@acme.nl")])
        queue = []
        rows = m.process_company(CO, queue)
        assert len(rows) == 1
        assert rows[0]["first_name"] == "Jane" and rows[0]["last_name"] == "Doe" and rows[0]["decision_maker_email"] == "jane@acme.nl"
        assert rows[0]["company_linkedin"] == ""
        assert pipeline["enrich_calls"] == 0 and queue == []

    def test_dm_email_found_still_finds_company_linkedin_for_free(self, pipeline):
        """find_company_linkedin() checks company.linkedin and the already-
        crawled pages before ever spending search budget, so skipping the
        per-person LinkedIn search must not also throw that away."""
        pipeline["pages"] = [page(
            "https://acme.nl", "Jane Doe CEO", ["jane@acme.nl"],
            links=[m.LinkedInLink(kind="company", url="https://www.linkedin.com/company/acme", context="Acme")],
        )]
        pipeline["llm"] = m.LLMDecision(decision_makers=[dm("Jane Doe", "CEO", "jane@acme.nl")])
        rows = m.process_company(CO, [])
        assert rows[0]["company_linkedin"] == "https://www.linkedin.com/company/acme"
        assert pipeline["enrich_calls"] == 0  # still no per-person search spent

    def test_no_dm_email_runs_linkedin_and_fills_columns(self, pipeline):
        pipeline["enrich"] = ("https://www.linkedin.com/company/acme",
                              [P("Jane Doe", "CEO", linkedin_url="https://www.linkedin.com/in/jane")])
        queue = []
        rows = m.process_company(CO, queue)
        dm_row = next(r for r in rows if r["first_name"])
        generic_row = next(r for r in rows if r["company_email"])
        assert generic_row["company_email"] == "info@acme.nl"
        assert dm_row["company_linkedin"].endswith("/acme")
        assert "linkedin.com/in/jane" in dm_row["contact_linkedin"]
        assert pipeline["enrich_calls"] == 1 and queue == []  # complete -> no retry needed

    def test_incomplete_linkedin_is_queued_for_second_pass(self, pipeline):
        pipeline["enrich"] = ("", [])
        queue = []
        m.process_company(CO, queue)
        assert len(queue) == 1 and queue[0][0] is CO

    def test_no_pages_gives_empty_row(self, pipeline):
        pipeline["pages"] = []
        rows = m.process_company(CO, [])
        assert rows == [m.empty_result(CO)] and pipeline["enrich_calls"] == 0

    def test_crawl_exception_gives_empty_row(self, pipeline, monkeypatch):
        def boom(company): raise RuntimeError("dns")
        monkeypatch.setattr(m, "crawl_company", boom)
        assert m.process_company(CO, []) == [m.empty_result(CO)]

    def test_llm_failure_falls_back_to_heuristics(self, pipeline):
        pipeline["llm"] = RuntimeError("quota")
        rows = m.process_company(CO, [])
        assert any(r["company_email"] == "info@acme.nl" for r in rows)

    def test_rate_limit_stops_instead_of_falling_back_to_heuristics(self, pipeline):
        """A RateLimitError means the daily quota is gone, not a burst (see
        analyze_with_llm's own retry loop) - every company from here on
        would be heuristics-only, so this flags the run to stop rather than
        quietly using the heuristics that worked for every other failure."""
        pipeline["llm"] = TestRerunGeneric.quota_error()
        rows = m.process_company(CO, [])
        assert rows == [m.empty_result(CO)]  # no heuristics-only row written for this company
        assert m._quota_exhausted.is_set()

    def test_linkedin_failure_keeps_emails(self, pipeline):
        pipeline["enrich"] = RuntimeError("search exploded")
        queue = []
        rows = m.process_company(CO, queue)
        assert any(r["company_email"] == "info@acme.nl" for r in rows)
        assert all(r["company_linkedin"] == "" for r in rows)
        assert len(queue) == 1  # worth another attempt

    def test_directory_email_is_last_resort(self, pipeline):
        pipeline["pages"] = [page("https://acme.nl", "hello")]
        co = m.Company("Acme", "https://acme.nl", directory_email="dir@acme.nl")
        rows = m.process_company(co, [])
        assert any(r["company_email"] == "dir@acme.nl" for r in rows)

    def test_directory_email_not_used_when_site_has_one(self, pipeline):
        co = m.Company("Acme", "https://acme.nl", directory_email="dir@acme.nl")
        rows = m.process_company(co, [])
        assert any(r["company_email"] == "info@acme.nl" for r in rows)
        assert not any(r["company_email"] == "dir@acme.nl" for r in rows)

    def test_decision_maker_without_email_still_listed_via_linkedin(self, pipeline):
        pipeline["llm"] = m.LLMDecision(decision_makers=[dm("Jane Doe", "CEO")])
        pipeline["enrich"] = ("c", [P("Jane Doe", "CEO", linkedin_url="u")])
        rows = m.process_company(CO, [])
        dm_row = next(r for r in rows if r["first_name"])
        assert dm_row["decision_maker_email"] == "" and (dm_row["first_name"], dm_row["last_name"]) == ("Jane", "Doe")


class TestSecondPass:
    def groups_for(self, *companies):
        return [m.build_rows(c, [], ["info@x.nl"], "") for c in companies]

    def test_fills_gaps_and_saves(self, monkeypatch):
        saved = []
        monkeypatch.setattr(m, "save_results", lambda r: saved.append(len(r)))
        monkeypatch.setattr(m, "enrich_linkedin", lambda c, p, v: ("https://www.linkedin.com/company/a",
                            [P("Jane Doe", "CEO", linkedin_url="https://www.linkedin.com/in/j")]))
        results = self.groups_for(CO)
        m.second_pass([(CO, [], None, "", [])], results)
        group = results[0]
        dm_row = next(r for r in group if r["first_name"])
        assert dm_row["company_linkedin"].endswith("/a")
        assert "linkedin.com/in/j" in dm_row["contact_linkedin"]
        assert any(r["company_email"] == "info@x.nl" for r in group)
        assert saved

    def test_never_overwrites_existing_company_page(self, monkeypatch):
        monkeypatch.setattr(m, "save_results", lambda r: None)
        monkeypatch.setattr(m, "enrich_linkedin", lambda c, p, v: ("https://www.linkedin.com/company/new", []))
        results = self.groups_for(CO)
        m.second_pass([(CO, [], None, "https://www.linkedin.com/company/old", [])], results)
        assert results[0][0]["company_linkedin"].endswith("/old")

    def test_one_failure_does_not_stop_the_rest(self, monkeypatch):
        other = m.Company("Beta", "https://beta.nl")
        monkeypatch.setattr(m, "save_results", lambda r: None)
        def enrich(c, p, v):
            if c is CO:
                raise RuntimeError("x")
            return ("https://www.linkedin.com/company/beta", [])
        monkeypatch.setattr(m, "enrich_linkedin", enrich)
        results = self.groups_for(CO, other)
        m.second_pass([(CO, [], None, "", []), (other, [], None, "", [])], results)
        assert results[0][0]["company_linkedin"] == "" and results[1][0]["company_linkedin"].endswith("/beta")

    def test_missing_row_is_skipped_not_crashed(self, monkeypatch):
        monkeypatch.setattr(m, "save_results", lambda r: None)
        monkeypatch.setattr(m, "enrich_linkedin", lambda c, p, v: ("x", []))
        m.second_pass([(CO, [], None, "", [])], [])

    def test_waits_for_cooldown(self, monkeypatch):
        import time
        waits = []
        monkeypatch.setattr(time, "sleep", lambda s: waits.append(s))
        monkeypatch.setattr(m, "enrich_linkedin", lambda c, p, v: ("", []))
        monkeypatch.setattr(m, "save_results", lambda r: None)
        m.second_pass([(CO, [], None, "", [])], self.groups_for(CO))
        assert m.SECOND_PASS_COOLDOWN_SECONDS in waits


class TestSaveResults:
    def test_writes_exact_columns_and_unicode(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        rows = m.build_rows(m.Company("Zoë, Inc. \"Q\"", "https://z.nl"), [P("Dragoș Ștefan", "COO")], ["a@z.nl"], "")
        m.save_results(rows)
        df = pd.read_csv(out).fillna("")
        assert list(df.columns) == m.OUTPUT_COLUMNS
        assert df.company_name[0] == 'Zoë, Inc. "Q"'
        assert "Dragoș" in set(df.first_name) and "Ștefan" in set(df.last_name)

    def test_empty_results_still_writes_header(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        m.save_results([])
        assert list(pd.read_csv(out).columns) == m.OUTPUT_COLUMNS

    def test_merges_into_existing_file_without_wiping_other_companies(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        m.save_results(m.build_rows(m.Company("Acme", "https://acme.com"), [], ["info@acme.com"], ""))
        m.save_results(m.build_rows(m.Company("Beta", "https://beta.com"), [], ["info@beta.com"], ""))
        df = pd.read_csv(out)
        assert set(df.company_name) == {"Acme", "Beta"}

    def test_rerun_of_same_company_updates_in_place_not_duplicates(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        co = m.Company("Acme", "https://www.acme.com")
        m.save_results(m.build_rows(co, [], ["info@acme.com"], ""))
        m.save_results(m.build_rows(m.Company("Acme", "https://acme.com"), [P("CEO Person", "CEO", "ceo@acme.com")], [], ""))
        df = pd.read_csv(out).fillna("")
        assert set(df.company_name) == {"Acme"}
        assert "ceo@acme.com" in set(df.decision_maker_email) and "info@acme.com" in set(df.company_email)


class TestOutputFileNaming:
    def test_clutch_keeps_legacy_name(self):
        assert m.OUTPUT_FILE == "company_emails.csv"


class TestCollectClutchAll:
    def test_walks_every_page_and_dedupes_by_domain(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_LIST_FILE", str(tmp_path / "companies.jsonl"))
        monkeypatch.setattr(m, "PAGES_DONE_FILE", str(tmp_path / "pages_done.txt"))
        monkeypatch.setattr(m, "CLUTCH_ALL_PAGES", 2)
        pages = {
            0: [m.Company("A", "https://a.com"), m.Company("B", "https://b.com")],
            1: [m.Company("B-dup", "https://b.com"), m.Company("C", "https://c.com")],
        }
        calls = []

        def fake_extract(url):
            n = int(url.rsplit("page=", 1)[1])
            calls.append(n)
            return pages[n]

        monkeypatch.setattr(m, "extract_clutch_companies", fake_extract)

        companies = m.collect_clutch_all("https://clutch.co/x")

        assert calls == [0, 1]
        assert [c.name for c in companies] == ["A", "B", "C"]  # "B-dup" dropped: same domain as B

    def test_resumes_from_checkpoint_without_rerunning_done_pages(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_LIST_FILE", str(tmp_path / "companies.jsonl"))
        pages_done = tmp_path / "pages_done.txt"
        pages_done.write_text("0\n")
        monkeypatch.setattr(m, "PAGES_DONE_FILE", str(pages_done))
        monkeypatch.setattr(m, "CLUTCH_ALL_PAGES", 2)
        # A previous run already recorded page 0's companies before crashing.
        (tmp_path / "companies.jsonl").write_text(json.dumps({"name": "A", "url": "https://a.com"}) + "\n")
        calls = []
        monkeypatch.setattr(m, "extract_clutch_companies", lambda url: calls.append(url) or [m.Company("B", "https://b.com")])

        companies = m.collect_clutch_all("https://clutch.co/x")

        assert len(calls) == 1  # page 0 is skipped; only page 1 is fetched
        assert [c.name for c in companies] == ["A", "B"]

    def test_failed_page_is_left_out_of_checkpoint_for_retry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_LIST_FILE", str(tmp_path / "companies.jsonl"))
        pages_done = tmp_path / "pages_done.txt"
        monkeypatch.setattr(m, "PAGES_DONE_FILE", str(pages_done))
        monkeypatch.setattr(m, "CLUTCH_ALL_PAGES", 1)

        def boom(url):
            raise RuntimeError("rendering failed")

        monkeypatch.setattr(m, "extract_clutch_companies", boom)

        companies = m.collect_clutch_all("https://clutch.co/x")

        assert companies == []
        assert not pages_done.exists()  # page 0 never got checkpointed, so a rerun retries it


class TestRunChunked:
    def fake_run_companies(self, monkeypatch, seen=None):
        def run(companies, retry_queue):
            if seen is not None:
                seen.append([c.name for c in companies])
            return [[m.empty_result(c)] for c in companies]

        monkeypatch.setattr(m, "run_companies", run)

    def test_resumes_by_skipping_domains_already_in_output(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        pd.DataFrame([m.empty_result(m.Company("Old", "https://done.com"))], columns=m.OUTPUT_COLUMNS).to_csv(out, index=False)
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        monkeypatch.setattr(m, "CHUNK_SIZE", 10)
        monkeypatch.setattr(m, "ENABLE_SECOND_PASS", False)
        processed = []
        self.fake_run_companies(monkeypatch, processed)

        m.run_chunked([m.Company("Done", "https://done.com"), m.Company("New", "https://new.com")])

        assert processed == [["New"]]
        assert set(pd.read_csv(out).fillna("").company_name) == {"Old", "New"}

    def test_processes_in_chunks_and_appends_each_chunk(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        monkeypatch.setattr(m, "CHUNK_SIZE", 2)
        monkeypatch.setattr(m, "ENABLE_SECOND_PASS", False)
        chunks_seen = []
        self.fake_run_companies(monkeypatch, chunks_seen)

        m.run_chunked([m.Company(f"C{i}", f"https://c{i}.com") for i in range(5)])

        assert chunks_seen == [["C0", "C1"], ["C2", "C3"], ["C4"]]
        assert list(pd.read_csv(out).fillna("").company_name) == ["C0", "C1", "C2", "C3", "C4"]

    def test_stops_after_the_chunk_where_quota_runs_out(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        monkeypatch.setattr(m, "CHUNK_SIZE", 2)
        monkeypatch.setattr(m, "ENABLE_SECOND_PASS", False)
        chunks_seen = []

        def run(companies, retry_queue):
            chunks_seen.append([c.name for c in companies])
            m._quota_exhausted.set()  # as process_company would on a RateLimitError
            return [[m.empty_result(c)] for c in companies]

        monkeypatch.setattr(m, "run_companies", run)

        m.run_chunked([m.Company(f"C{i}", f"https://c{i}.com") for i in range(5)])

        assert chunks_seen == [["C0", "C1"]]  # the remaining chunks are never started
        assert list(pd.read_csv(out).fillna("").company_name) == ["C0", "C1"]  # but this chunk's rows are still saved

    def test_saves_are_suppressed_during_a_chunk_and_restored_after(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        monkeypatch.setattr(m, "CHUNK_SIZE", 10)
        monkeypatch.setattr(m, "ENABLE_SECOND_PASS", False)
        suppressed_during_run = []

        def run(companies, retry_queue):
            suppressed_during_run.append(m._suppress_saves)
            return [[m.empty_result(c)] for c in companies]

        monkeypatch.setattr(m, "run_companies", run)

        m.run_chunked([m.Company("A", "https://a.com")])

        assert suppressed_during_run == [True]
        assert m._suppress_saves is False

    def test_second_pass_runs_per_chunk_when_enabled(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        monkeypatch.setattr(m, "CHUNK_SIZE", 10)
        monkeypatch.setattr(m, "ENABLE_SECOND_PASS", True)

        def run(companies, retry_queue):
            retry_queue.append("retry-me")
            return [[m.empty_result(c)] for c in companies]

        monkeypatch.setattr(m, "run_companies", run)
        seen = []
        monkeypatch.setattr(m, "second_pass", lambda queue, rows: seen.append(list(queue)))

        m.run_chunked([m.Company("A", "https://a.com")])

        assert seen == [["retry-me"]]

    def test_chunk_error_still_saves_what_was_scraped(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        monkeypatch.setattr(m, "CHUNK_SIZE", 10)
        monkeypatch.setattr(m, "ENABLE_SECOND_PASS", True)

        def run(companies, retry_queue):
            retry_queue.append("retry-me")
            return [[m.empty_result(c)] for c in companies]

        monkeypatch.setattr(m, "run_companies", run)
        monkeypatch.setattr(m, "second_pass", lambda queue, rows: (_ for _ in ()).throw(RuntimeError("boom")))

        m.run_chunked([m.Company("A", "https://a.com")])

        assert list(pd.read_csv(out).fillna("").company_name) == ["A"]
        assert m._suppress_saves is False


class TestMain:
    COS = [m.Company(f"C{i}", f"https://c{i}.nl") for i in range(4)]

    @pytest.fixture
    def run(self, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_WORKERS", 1)  # deterministic order for these tests
        calls = {"clutch": 0, "processed": [], "second": 0}
        monkeypatch.setattr(m, "extract_clutch_companies", lambda url: calls.__setitem__("clutch", calls["clutch"] + 1) or list(self.COS))
        def process(company, queue):
            calls["processed"].append(company.name)
            if company.name == "C1":
                queue.append((company, [], None, "", []))
            return [m.empty_result(company)]
        monkeypatch.setattr(m, "process_company", process)
        monkeypatch.setattr(m, "save_results", lambda r: None)
        monkeypatch.setattr(m, "second_pass", lambda q, r: calls.__setitem__("second", len(q)))
        return calls

    def test_missing_api_key(self, monkeypatch, run):
        monkeypatch.delenv("GROQ_API_KEY")
        with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
            m.main()

    def test_clutch_source_processes_everyone_and_runs_second_pass(self, run):
        m.main()
        assert run["clutch"] == 1 and run["processed"] == ["C0", "C1", "C2", "C3"] and run["second"] == 1

    def test_unknown_source(self, run, monkeypatch):
        monkeypatch.setattr(m, "SOURCE", "yellowpages")
        with pytest.raises(RuntimeError, match="Unknown SOURCE"):
            m.main()

    def test_max_companies_cap(self, run, monkeypatch):
        monkeypatch.setattr(m, "MAX_COMPANIES", 2)
        m.main()
        assert run["processed"] == ["C0", "C1"]

    def test_second_pass_can_be_disabled(self, run, monkeypatch):
        monkeypatch.setattr(m, "ENABLE_SECOND_PASS", False)
        m.main()
        assert run["second"] == 0

    def test_second_pass_skipped_when_nothing_to_retry(self, run, monkeypatch):
        monkeypatch.setattr(m, "MAX_COMPANIES", 1)  # only C0, which queues nothing
        m.main()
        assert run["second"] == 0

    def test_saves_after_every_company(self, run, monkeypatch):
        saves = []
        monkeypatch.setattr(m, "save_results", lambda r: saves.append(len(r)))
        m.main()
        assert saves == [1, 2, 3, 4]

    def test_clutch_all_source_collects_then_runs_chunked(self, run, monkeypatch):
        monkeypatch.setattr(m, "SOURCE", "clutch_all")
        seen = {}

        def collect(url):
            seen["collect"] = url
            return list(self.COS)

        monkeypatch.setattr(m, "collect_clutch_all", collect)
        monkeypatch.setattr(m, "run_chunked", lambda companies: seen.setdefault("chunked", [c.name for c in companies]))
        m.main()
        assert seen["collect"] == m.CLUTCH_URL
        assert seen["chunked"] == ["C0", "C1", "C2", "C3"]

    def test_clutch_all_respects_max_companies(self, run, monkeypatch):
        monkeypatch.setattr(m, "SOURCE", "clutch_all")
        monkeypatch.setattr(m, "MAX_COMPANIES", 2)
        monkeypatch.setattr(m, "collect_clutch_all", lambda url: list(self.COS))
        seen = {}
        monkeypatch.setattr(m, "run_chunked", lambda companies: seen.setdefault("chunked", [c.name for c in companies]))
        m.main()
        assert seen["chunked"] == ["C0", "C1"]


class TestParallelism:
    def test_rows_keep_input_order_with_many_workers(self, monkeypatch):
        import time as _time
        monkeypatch.setattr(m, "COMPANY_WORKERS", 4)
        monkeypatch.setattr(m, "save_results", lambda rows: None)
        companies = [m.Company(f"C{i}", f"https://c{i}.nl") for i in range(12)]
        def process(company, queue):
            _time.sleep(0)  # (patched no-op) - completion order is up to the scheduler
            return [m.empty_result(company)]
        monkeypatch.setattr(m, "process_company", process)
        groups = m.run_companies(companies, [])
        assert [group[0]["company_name"] for group in groups] == [c.name for c in companies]

    def test_every_company_processed_exactly_once(self, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_WORKERS", 8)
        monkeypatch.setattr(m, "save_results", lambda rows: None)
        seen = []
        monkeypatch.setattr(m, "process_company", lambda c, q: seen.append(c.name) or [m.empty_result(c)])
        companies = [m.Company(f"C{i}", f"https://c{i}.nl") for i in range(40)]
        m.run_companies(companies, [])
        assert sorted(seen) == sorted(c.name for c in companies)

    def test_saved_snapshots_only_contain_finished_rows(self, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_WORKERS", 4)
        snapshots = []
        monkeypatch.setattr(m, "save_results", lambda rows: snapshots.append(list(rows)))
        monkeypatch.setattr(m, "process_company", lambda c, q: [m.empty_result(c)])
        m.run_companies([m.Company(f"C{i}", f"https://c{i}.nl") for i in range(6)], [])
        assert [len(s) for s in snapshots] == [1, 2, 3, 4, 5, 6]
        assert all(row is not None for snap in snapshots for row in snap)

    @pytest.mark.parametrize("workers", [0, -3])
    def test_nonpositive_worker_count_still_runs(self, monkeypatch, workers):
        monkeypatch.setattr(m, "COMPANY_WORKERS", workers)
        monkeypatch.setattr(m, "save_results", lambda rows: None)
        monkeypatch.setattr(m, "process_company", lambda c, q: [m.empty_result(c)])
        assert len(m.run_companies([m.Company("A", "https://a.nl")], [])) == 1

    def test_failing_company_does_not_block_others(self, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_WORKERS", 3)
        monkeypatch.setattr(m, "save_results", lambda rows: None)
        def process(company, queue):
            return [m.empty_result(company)]  # process_company itself never raises
        monkeypatch.setattr(m, "process_company", process)
        assert len(m.run_companies([m.Company(f"C{i}", f"https://c{i}.nl") for i in range(5)], [])) == 5

    def test_empty_company_list(self, monkeypatch):
        monkeypatch.setattr(m, "save_results", lambda rows: None)
        assert m.run_companies([], []) == []

    def test_only_one_llm_call_runs_at_a_time(self, monkeypatch):
        import threading
        active, peak, guard = [0], [0], threading.Lock()
        def fake(company, pages):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            threading.Event().wait(0.01)
            with guard:
                active[0] -= 1
            return m.LLMDecision()
        monkeypatch.setattr(m, "_analyze_with_llm", fake)
        threads = [threading.Thread(target=m.analyze_with_llm, args=(m.Company("A", "https://a.nl"), [])) for _ in range(8)]
        [t.start() for t in threads]; [t.join() for t in threads]
        assert peak[0] == 1

    def test_second_pass_runs_all_retries_in_parallel_safely(self, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_WORKERS", 4)
        monkeypatch.setattr(m, "save_results", lambda r: None)
        monkeypatch.setattr(m, "enrich_linkedin", lambda c, p, v: (f"https://www.linkedin.com/company/{c.name}", []))
        companies = [m.Company(f"c{i}", f"https://c{i}.nl") for i in range(10)]
        groups = [m.build_rows(c, [], [], "") for c in companies]
        m.second_pass([(c, [], None, "", []) for c in companies], groups)
        assert [group[0]["company_linkedin"] for group in groups] == [f"https://www.linkedin.com/company/c{i}" for i in range(10)]


class TestRecrawlOfFailedCompanies:
    def test_company_with_no_pages_is_queued_for_full_retry(self, pipeline):
        pipeline["pages"] = []
        queue = []
        assert m.process_company(CO, queue) == [m.empty_result(CO)]
        assert len(queue) == 1 and queue[0][0] is CO and queue[0][1] is None

    def test_crawl_exception_is_not_queued_as_recrawl(self, pipeline, monkeypatch):
        def boom(company): raise RuntimeError("dns")
        monkeypatch.setattr(m, "crawl_company", boom)
        queue = []
        m.process_company(CO, queue)
        assert queue == []  # unexpected errors aren't assumed to be temporary

    def test_second_pass_recovers_company_on_second_crawl(self, monkeypatch):
        saved = []
        monkeypatch.setattr(m, "save_results", lambda r: saved.append(1))
        monkeypatch.setattr(m, "process_company", lambda c, q: m.build_rows(c, [], ["info@acme.nl"], ""))
        called = []
        monkeypatch.setattr(m, "enrich_linkedin", lambda *a: called.append(1) or ("", []))
        groups = [[m.empty_result(CO)]]
        m.second_pass([(CO, None, None, "", [])], groups)
        assert groups[0][0]["company_email"] == "info@acme.nl" and saved and called == []

    def test_second_crawl_that_finds_nothing_leaves_row_unchanged(self, monkeypatch):
        saved = []
        monkeypatch.setattr(m, "save_results", lambda r: saved.append(1))
        monkeypatch.setattr(m, "process_company", lambda c, q: [m.empty_result(c)])
        groups = [[m.empty_result(CO)]]
        m.second_pass([(CO, None, None, "", [])], groups)
        assert groups[0] == [m.empty_result(CO)] and saved == []

    def test_recrawl_failure_does_not_stop_other_retries(self, monkeypatch):
        other = m.Company("Beta", "https://beta.nl")
        monkeypatch.setattr(m, "save_results", lambda r: None)
        def process(company, queue):
            if company is CO:
                raise RuntimeError("boom")
            return m.build_rows(company, [], ["info@beta.nl"], "")
        monkeypatch.setattr(m, "process_company", process)
        groups = [[m.empty_result(CO)], [m.empty_result(other)]]
        m.second_pass([(CO, None, None, "", []), (other, None, None, "", [])], groups)
        assert groups[0][0]["company_email"] == "" and groups[1][0]["company_email"] == "info@beta.nl"

    def test_recrawl_does_not_requeue_itself(self, monkeypatch):
        monkeypatch.setattr(m, "save_results", lambda r: None)
        monkeypatch.setattr(m, "crawl_company", lambda c: [])
        outer = []
        real = m.process_company
        def spy(company, queue):
            outer.append(queue)
            return real(company, queue)
        monkeypatch.setattr(m, "process_company", spy)
        groups = [[m.empty_result(CO)]]
        m.second_pass([(CO, None, None, "", [])], groups)
        assert outer and all(q is not None for q in outer) and len(outer[0]) == 1  # its own throwaway list only


class TestSiteEmailMatching:
    def person(self, name):
        return m.Person(name=name, title="CEO")

    def test_matches_common_name_formats(self):
        emails = {"jane.smith@acme.com"}
        assert m.match_site_email(self.person("Jane Smith"), emails, "acme.com", ["jane"]) == "jane.smith@acme.com"
        assert m.match_site_email(self.person("Jane Smith"), {"jsmith@acme.com"}, "acme.com", ["jane"]) == "jsmith@acme.com"
        assert m.match_site_email(self.person("Jane Smith"), {"janes@acme.com"}, "acme.com", ["jane"]) == "janes@acme.com"

    def test_first_name_only_needs_a_unique_first_name(self):
        assert m.match_site_email(self.person("Jan de Vries"), {"jan@acme.nl"}, "acme.nl", ["jan"]) == "jan@acme.nl"
        assert m.match_site_email(self.person("Jan de Vries"), {"jan@acme.nl"}, "acme.nl", ["jan", "jan"]) == ""

    def test_never_matches_generic_or_foreign_domain_addresses(self):
        assert m.match_site_email(self.person("Info Person"), {"info@acme.com"}, "acme.com", ["info"]) == ""
        assert m.match_site_email(self.person("Jane Smith"), {"jane.smith@other.com"}, "acme.com", ["jane"]) == ""

    def test_attach_fills_only_missing_emails(self):
        people = [self.person("Jane Smith"), m.Person(name="Bob Ray", title="CTO", email="bob@acme.com")]
        pages = [m.PageData(url="https://acme.com", text="", emails=["jane.smith@acme.com", "bob.ray@acme.com"], linkedin_links=[])]
        added = m.attach_site_emails(people, pages, m.Company(name="Acme", url="https://acme.com"))
        assert added == ["jane.smith@acme.com"] and people[1].email == "bob@acme.com"


class TestClutchCountries:
    @pytest.fixture
    def files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_LIST_FILE", str(tmp_path / "companies.jsonl"))
        monkeypatch.setattr(m, "PAGES_DONE_FILE", str(tmp_path / "pages_done.txt"))
        monkeypatch.setattr(m, "COUNTRIES_DONE_FILE", str(tmp_path / "countries_done.txt"))
        return tmp_path

    def listing(self, monkeypatch, pages_by_country):
        """pages_by_country: {"se": [[Company,...], ...]}; a page past the end raises like Clutch's empty page."""
        asked = []

        def extract(url):
            cc = url.split("clutch.co/")[1].split("/")[0]
            page = int(url.rsplit("page=", 1)[1])
            asked.append((cc, page))
            pages = pages_by_country[cc]
            if page >= len(pages):
                raise RuntimeError("No provider rows found on the Clutch page.")
            return pages[page]

        monkeypatch.setattr(m, "extract_clutch_companies", extract)
        return asked

    def test_walks_pages_until_empty_and_tags_country(self, files, monkeypatch):
        asked = self.listing(monkeypatch, {"se": [[m.Company("A", "https://a.se")], [m.Company("B", "https://b.se")]]})
        assert m.collect_clutch_country("se") is True
        assert asked == [("se", 0), ("se", 1), ("se", 2)]
        assert [c.name for c in m.load_companies("se")] == ["A", "B"]
        assert m.load_companies("no") == []
        assert (files / "pages_done.txt").read_text().split() == ["se:0", "se:1"]

    def test_resumes_after_checkpointed_pages(self, files, monkeypatch):
        (files / "pages_done.txt").write_text("se:0\n")
        (files / "companies.jsonl").write_text(json.dumps({"name": "A", "url": "https://a.se", "country": "se"}) + "\n")
        asked = self.listing(monkeypatch, {"se": [[m.Company("A", "https://a.se")], [m.Company("B", "https://b.se")]]})
        m.collect_clutch_country("se")
        assert asked == [("se", 1), ("se", 2)]
        assert [c.name for c in m.load_companies("se")] == ["A", "B"]

    def test_unloadable_page_leaves_country_for_retry(self, files, monkeypatch):
        def extract(url):
            raise RuntimeError("Could not load the Clutch page after 4 attempts: timeout")
        monkeypatch.setattr(m, "extract_clutch_companies", extract)
        assert m.collect_clutch_country("se") is False
        assert not (files / "pages_done.txt").exists()

    def test_empty_first_page_is_a_failure_not_an_empty_country(self, files, monkeypatch):
        self.listing(monkeypatch, {"us": []})
        assert m.collect_clutch_country("us") is False

    def test_max_pages_cap(self, files, monkeypatch):
        monkeypatch.setattr(m, "CLUTCH_COUNTRY_MAX_PAGES", 2)
        asked = self.listing(monkeypatch, {"se": [[m.Company(f"C{i}", f"https://c{i}.se")] for i in range(5)]})
        assert m.collect_clutch_country("se") is True
        assert asked == [("se", 0), ("se", 1)]

    def test_run_countries_crawls_each_country_in_order_and_marks_done(self, files, monkeypatch):
        self.listing(monkeypatch, {
            "se": [[m.Company("A", "https://a.se")]],
            "no": [[m.Company("B", "https://b.no")]],
        })
        crawled = []
        monkeypatch.setattr(m, "run_chunked", lambda companies: crawled.append([c.name for c in companies]))
        m.run_countries(["se", "no"])
        assert crawled == [["A"], ["B"]]
        assert (files / "countries_done.txt").read_text().split() == ["se", "no"]

    def test_finished_countries_are_skipped_and_failed_ones_not_marked(self, files, monkeypatch):
        (files / "countries_done.txt").write_text("se\n")
        asked = self.listing(monkeypatch, {"no": [[m.Company("B", "https://b.no")]]})
        crawled = []
        monkeypatch.setattr(m, "run_chunked", lambda companies: crawled.append(len(companies)))
        monkeypatch.setattr(m, "collect_clutch_country", lambda cc: False)
        m.run_countries(["se", "no"])
        assert crawled == [] and asked == []
        assert (files / "countries_done.txt").read_text().split() == ["se"]

    def test_quota_exhaustion_stops_and_leaves_the_country_unmarked(self, files, monkeypatch):
        self.listing(monkeypatch, {
            "se": [[m.Company("A", "https://a.se")]],
            "no": [[m.Company("B", "https://b.no")]],
        })
        crawled = []

        def run_chunked(companies):
            crawled.append([c.name for c in companies])
            m._quota_exhausted.set()

        monkeypatch.setattr(m, "run_chunked", run_chunked)
        m.run_countries(["se", "no"])
        assert crawled == [["A"]]  # "no" is never even started
        assert not (files / "countries_done.txt").exists()  # "se" stays unmarked, resumed next run

    def test_discovers_countries_with_sweden_first(self, monkeypatch):
        html = '<a href="/no/developers">x</a><a href="https://clutch.co/se/developers/java">y</a><a href="/dk/developers">z</a>'
        monkeypatch.setattr(m, "render_clutch_listing_html", lambda url: html)
        assert m.discover_clutch_countries() == ["se", "dk", "no"]

    def test_discovery_gives_a_clear_error_when_blocked(self, monkeypatch):
        monkeypatch.setattr(m, "render_clutch_listing_html", lambda url: "<title>Just a moment...</title>")
        with pytest.raises(RuntimeError, match="CLUTCH_COUNTRIES"):
            m.discover_clutch_countries()

    def test_main_routes_to_countries(self, monkeypatch):
        monkeypatch.setattr(m, "SOURCE", "clutch_countries")
        monkeypatch.setattr(m, "CLUTCH_COUNTRIES", ["se", "no"])
        seen = []
        monkeypatch.setattr(m, "run_countries", lambda countries: seen.append(countries))
        m.main()
        assert seen == [["se", "no"]]


class TestRerunGeneric:
    HEAD = ["company_name", "company_url", "company_email", "company_linkedin",
            "first_name", "last_name", "contact_title", "decision_maker_email", "contact_linkedin"]

    @pytest.fixture
    def csv_file(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        monkeypatch.setattr(m, "RERUN_DONE_FILE", str(tmp_path / "rerun_done.txt"))
        rows = [
            ["A", "https://a.com", "", "https://www.linkedin.com/company/a",
             "Ann", "Lee", "Founder [unverified]", "", "https://www.linkedin.com/in/annlee"],
            ["A", "https://a.com", "info@a.com", "https://www.linkedin.com/company/a", "", "", "", "", ""],
            ["B", "https://b.com", "info@b.com", "", "", "", "", "", ""],
            ["C", "https://c.com", "", "", "Cy", "Doe", "CEO", "ceo@c.com", ""],
        ]
        pd.DataFrame(rows, columns=self.HEAD).to_csv(out, index=False, quoting=1)
        return out

    def run(self, monkeypatch, decisions, sites=None):
        """decisions: {company_name: LLMDecision | Exception}"""
        analysed = []

        def crawl(company):
            if sites is not None and company.name not in sites:
                return []
            return [page(company.url, f"Founder Ann Lee {company.name} ann@{company.url.split('//')[1]} info@{company.url.split('//')[1]}",
                         emails=[f"ann@{company.url.split('//')[1]}", f"info@{company.url.split('//')[1]}"])]

        def analyse(company, pages):
            analysed.append(company.name)
            outcome = decisions[company.name]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(m, "crawl_company", crawl)
        monkeypatch.setattr(m, "analyze_with_llm", analyse)
        m.rerun_generic()
        return analysed

    def test_finds_decision_maker_email_and_keeps_linkedin(self, csv_file, monkeypatch):
        decision = m.LLMDecision(decision_makers=[dm("Ann Lee", "Founder", "ann@a.com")])
        analysed = self.run(monkeypatch, {"A": decision, "B": m.LLMDecision()})
        assert analysed == ["A", "B"]                       # C already has a decision-maker email
        df = pd.read_csv(csv_file, dtype=str, keep_default_na=False)
        a_rows = df[df.company_name == "A"]
        dm_row = a_rows[a_rows.first_name == "Ann"].iloc[0]
        assert dm_row.decision_maker_email == "ann@a.com"
        assert dm_row.contact_linkedin == "https://www.linkedin.com/in/annlee"      # earlier LinkedIn link survives
        # the generic address is repeated onto the contact's own row now, not a separate blank row
        assert dm_row.company_email == "info@a.com"
        assert a_rows.iloc[0].company_url == "https://a.com" and a_rows.iloc[0].company_linkedin.endswith("/company/a")

    def test_untouched_rows_stay_as_they_were(self, csv_file, monkeypatch):
        self.run(monkeypatch, {"A": m.LLMDecision(), "B": m.LLMDecision()})
        df = pd.read_csv(csv_file, dtype=str, keep_default_na=False)
        c_row = df[df.company_name == "C"].iloc[0]
        assert c_row.first_name == "Cy" and c_row.last_name == "Doe" and c_row.decision_maker_email == "ceo@c.com"

    def test_stops_when_quota_runs_out_and_resumes_later(self, csv_file, tmp_path, monkeypatch):
        quota = TestRerunGeneric.quota_error()
        analysed = self.run(monkeypatch, {"A": m.LLMDecision(), "B": quota})
        assert analysed == ["A", "B"]
        assert (tmp_path / "rerun_done.txt").read_text().split() == ["https://a.com"]   # B is retried next time
        again = self.run(monkeypatch, {"B": m.LLMDecision()})
        assert again == ["B"]

    def test_failed_crawl_is_not_marked_done(self, csv_file, tmp_path, monkeypatch):
        self.run(monkeypatch, {"A": m.LLMDecision()}, sites={"A"})
        assert (tmp_path / "rerun_done.txt").read_text().split() == ["https://a.com"]

    def test_rerun_max_caps_a_run(self, csv_file, monkeypatch):
        monkeypatch.setattr(m, "RERUN_MAX", 1)
        assert self.run(monkeypatch, {"A": m.LLMDecision(), "B": m.LLMDecision()}) == ["A"]

    @staticmethod
    def quota_error():
        import httpx2 as httpx
        response = httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com"))
        return m.RateLimitError("Rate limit reached on tokens per day", response=response, body=None)

    def test_main_routes_and_requires_llm(self, monkeypatch):
        monkeypatch.setattr(m, "SOURCE", "rerun_generic")
        seen = []
        monkeypatch.setattr(m, "rerun_generic", lambda: seen.append(1))
        m.main()
        assert seen == [1]
        monkeypatch.setattr(m, "USE_LLM", False)
        with pytest.raises(RuntimeError, match="needs the LLM"):
            m.main()
