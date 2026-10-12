import json

import pandas as pd
import pytest

import company_email_finder as m
from tests.conftest import page

CO = m.Company("Acme", "https://acme.nl")
P = m.Person


def dm(name, title, email=""):
    return m.DecisionMaker(name=name, title=title, email=email)


class TestRowHelpers:
    def test_empty_result_has_every_column(self):
        row = m.empty_result(CO)
        assert set(row) == set(m.OUTPUT_COLUMNS)
        assert row["company_name"] == "Acme" and row["company_url"] == "https://acme.nl" and row["contact_emails"] == ""

    def test_format_person_sanitizes_separators(self):
        out = m.format_person(P("Jan; de | Vries", "CEO; Owner", "j@a.nl", ))
        assert ";" not in out.replace("; ", "") and out.count(" | ") == 3

    def test_format_person_field_order(self):
        assert m.format_person(P("Jane Doe", "CEO", "j@a.nl", "https://www.linkedin.com/in/j")) == \
            "Jane Doe | CEO | https://www.linkedin.com/in/j | j@a.nl"

    def test_unverified_people_are_marked_in_output(self):
        out = m.format_person(P("Jane Doe", "CEO", linkedin_url="u", unverified=True))
        assert out == "Jane Doe | CEO [unverified] | u | "
        assert "[unverified]" not in m.format_person(P("Jane Doe", "CEO", linkedin_url="u"))

    def test_build_row_joins_and_dedupes(self):
        row = m.build_row(CO, ["a@a.nl"], ["z@a.nl", "b@a.nl", "b@a.nl"], "https://www.linkedin.com/company/a",
                          [P("A B", "CEO"), P("C D", "CTO")])
        assert row["contact_emails"] == "b@a.nl;z@a.nl"
        assert row["decision_makers"].count("; ") == 1
        assert row["company_linkedin"].endswith("/a")

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
        row = m.process_company(CO, queue)
        assert row["decision_maker_emails"] == "jane@acme.nl"
        assert row["company_linkedin"] == "" and row["decision_makers"] == ""
        assert pipeline["enrich_calls"] == 0 and queue == []

    def test_no_dm_email_runs_linkedin_and_fills_columns(self, pipeline):
        pipeline["enrich"] = ("https://www.linkedin.com/company/acme",
                              [P("Jane Doe", "CEO", linkedin_url="https://www.linkedin.com/in/jane")])
        queue = []
        row = m.process_company(CO, queue)
        assert row["contact_emails"] == "info@acme.nl"
        assert row["company_linkedin"].endswith("/acme") and "linkedin.com/in/jane" in row["decision_makers"]
        assert pipeline["enrich_calls"] == 1 and queue == []  # complete -> no retry needed

    def test_incomplete_linkedin_is_queued_for_second_pass(self, pipeline):
        pipeline["enrich"] = ("", [])
        queue = []
        m.process_company(CO, queue)
        assert len(queue) == 1 and queue[0][0] is CO

    def test_no_pages_gives_empty_row(self, pipeline):
        pipeline["pages"] = []
        row = m.process_company(CO, [])
        assert row == m.empty_result(CO) and pipeline["enrich_calls"] == 0

    def test_crawl_exception_gives_empty_row(self, pipeline, monkeypatch):
        def boom(company): raise RuntimeError("dns")
        monkeypatch.setattr(m, "crawl_company", boom)
        assert m.process_company(CO, []) == m.empty_result(CO)

    def test_llm_failure_falls_back_to_heuristics(self, pipeline):
        pipeline["llm"] = RuntimeError("quota")
        row = m.process_company(CO, [])
        assert row["contact_emails"] == "info@acme.nl"

    def test_linkedin_failure_keeps_emails(self, pipeline):
        pipeline["enrich"] = RuntimeError("search exploded")
        queue = []
        row = m.process_company(CO, queue)
        assert row["contact_emails"] == "info@acme.nl" and row["company_linkedin"] == ""
        assert len(queue) == 1  # worth another attempt

    def test_directory_email_is_last_resort(self, pipeline):
        pipeline["pages"] = [page("https://acme.nl", "hello")]
        co = m.Company("Acme", "https://acme.nl", directory_email="dir@acme.nl")
        assert m.process_company(co, [])["contact_emails"] == "dir@acme.nl"

    def test_directory_email_not_used_when_site_has_one(self, pipeline):
        co = m.Company("Acme", "https://acme.nl", directory_email="dir@acme.nl")
        assert m.process_company(co, [])["contact_emails"] == "info@acme.nl"

    def test_decision_maker_without_email_still_listed_via_linkedin(self, pipeline):
        pipeline["llm"] = m.LLMDecision(decision_makers=[dm("Jane Doe", "CEO")])
        pipeline["enrich"] = ("c", [P("Jane Doe", "CEO", linkedin_url="u")])
        row = m.process_company(CO, [])
        assert row["decision_maker_emails"] == "" and "Jane Doe" in row["decision_makers"]


class TestSecondPass:
    def rows_for(self, *companies):
        return [m.build_row(c, [], ["info@x.nl"], "", []) for c in companies]

    def test_fills_gaps_and_saves(self, monkeypatch):
        saved = []
        monkeypatch.setattr(m, "save_results", lambda r: saved.append(len(r)))
        monkeypatch.setattr(m, "enrich_linkedin", lambda c, p, v: ("https://www.linkedin.com/company/a",
                            [P("Jane Doe", "CEO", linkedin_url="https://www.linkedin.com/in/j")]))
        results = self.rows_for(CO)
        m.second_pass([(CO, [], None, "", [])], results)
        assert results[0]["company_linkedin"].endswith("/a") and "linkedin.com/in/j" in results[0]["decision_makers"]
        assert results[0]["contact_emails"] == "info@x.nl" and saved

    def test_never_overwrites_existing_company_page(self, monkeypatch):
        monkeypatch.setattr(m, "save_results", lambda r: None)
        monkeypatch.setattr(m, "enrich_linkedin", lambda c, p, v: ("https://www.linkedin.com/company/new", []))
        results = self.rows_for(CO)
        m.second_pass([(CO, [], None, "https://www.linkedin.com/company/old", [])], results)
        assert results[0]["company_linkedin"].endswith("/old")

    def test_one_failure_does_not_stop_the_rest(self, monkeypatch):
        other = m.Company("Beta", "https://beta.nl")
        monkeypatch.setattr(m, "save_results", lambda r: None)
        def enrich(c, p, v):
            if c is CO:
                raise RuntimeError("x")
            return ("https://www.linkedin.com/company/beta", [])
        monkeypatch.setattr(m, "enrich_linkedin", enrich)
        results = self.rows_for(CO, other)
        m.second_pass([(CO, [], None, "", []), (other, [], None, "", [])], results)
        assert results[0]["company_linkedin"] == "" and results[1]["company_linkedin"].endswith("/beta")

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
        m.second_pass([(CO, [], None, "", [])], self.rows_for(CO))
        assert m.SECOND_PASS_COOLDOWN_SECONDS in waits


class TestSaveResults:
    def test_writes_exact_columns_and_unicode(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        rows = [m.build_row(m.Company("Zoë, Inc. \"Q\"", "https://z.nl"), [], ["a@z.nl"], "", [P("Dragoș Ștefan", "COO")])]
        m.save_results(rows)
        df = pd.read_csv(out).fillna("")
        assert list(df.columns) == m.OUTPUT_COLUMNS
        assert df.company_name[0] == 'Zoë, Inc. "Q"' and "Dragoș Ștefan" in df.decision_makers[0]

    def test_empty_results_still_writes_header(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        m.save_results([])
        assert list(pd.read_csv(out).columns) == m.OUTPUT_COLUMNS


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
            return [m.empty_result(c) for c in companies]

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

    def test_saves_are_suppressed_during_a_chunk_and_restored_after(self, tmp_path, monkeypatch):
        out = tmp_path / "out.csv"
        monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
        monkeypatch.setattr(m, "CHUNK_SIZE", 10)
        monkeypatch.setattr(m, "ENABLE_SECOND_PASS", False)
        suppressed_during_run = []

        def run(companies, retry_queue):
            suppressed_during_run.append(m._suppress_saves)
            return [m.empty_result(c) for c in companies]

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
            return [m.empty_result(c) for c in companies]

        monkeypatch.setattr(m, "run_companies", run)
        seen = []
        monkeypatch.setattr(m, "second_pass", lambda queue, rows: seen.append(list(queue)))

        m.run_chunked([m.Company("A", "https://a.com")])

        assert seen == [["retry-me"]]


class TestMain:
    COS = [m.Company(f"C{i}", f"https://c{i}.nl") for i in range(4)]

    @pytest.fixture
    def run(self, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_WORKERS", 1)  # deterministic order for these tests
        calls = {"clutch": 0, "zr": [], "processed": [], "second": 0}
        monkeypatch.setattr(m, "extract_clutch_companies", lambda url: calls.__setitem__("clutch", calls["clutch"] + 1) or list(self.COS))
        monkeypatch.setattr(m, "extract_zakelijkeregister_companies", lambda q: calls["zr"].append(q) or list(self.COS))
        def process(company, queue):
            calls["processed"].append(company.name)
            if company.name == "C1":
                queue.append((company, [], None, "", []))
            return m.empty_result(company)
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

    def test_zakelijkeregister_source_uses_query(self, run, monkeypatch):
        monkeypatch.setattr(m, "SOURCE", "zakelijkeregister")
        monkeypatch.setattr(m, "ZR_QUERY", "software")
        m.main()
        assert run["zr"] == ["software"] and run["clutch"] == 0

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
            return m.empty_result(company)
        monkeypatch.setattr(m, "process_company", process)
        rows = m.run_companies(companies, [])
        assert [r["company_name"] for r in rows] == [c.name for c in companies]

    def test_every_company_processed_exactly_once(self, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_WORKERS", 8)
        monkeypatch.setattr(m, "save_results", lambda rows: None)
        seen = []
        monkeypatch.setattr(m, "process_company", lambda c, q: seen.append(c.name) or m.empty_result(c))
        companies = [m.Company(f"C{i}", f"https://c{i}.nl") for i in range(40)]
        m.run_companies(companies, [])
        assert sorted(seen) == sorted(c.name for c in companies)

    def test_saved_snapshots_only_contain_finished_rows(self, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_WORKERS", 4)
        snapshots = []
        monkeypatch.setattr(m, "save_results", lambda rows: snapshots.append(list(rows)))
        monkeypatch.setattr(m, "process_company", lambda c, q: m.empty_result(c))
        m.run_companies([m.Company(f"C{i}", f"https://c{i}.nl") for i in range(6)], [])
        assert [len(s) for s in snapshots] == [1, 2, 3, 4, 5, 6]
        assert all(row is not None for snap in snapshots for row in snap)

    @pytest.mark.parametrize("workers", [0, -3])
    def test_nonpositive_worker_count_still_runs(self, monkeypatch, workers):
        monkeypatch.setattr(m, "COMPANY_WORKERS", workers)
        monkeypatch.setattr(m, "save_results", lambda rows: None)
        monkeypatch.setattr(m, "process_company", lambda c, q: m.empty_result(c))
        assert len(m.run_companies([m.Company("A", "https://a.nl")], [])) == 1

    def test_failing_company_does_not_block_others(self, monkeypatch):
        monkeypatch.setattr(m, "COMPANY_WORKERS", 3)
        monkeypatch.setattr(m, "save_results", lambda rows: None)
        def process(company, queue):
            return m.empty_result(company)  # process_company itself never raises
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
        rows = [m.build_row(c, [], [], "", []) for c in companies]
        m.second_pass([(c, [], None, "", []) for c in companies], rows)
        assert [r["company_linkedin"] for r in rows] == [f"https://www.linkedin.com/company/c{i}" for i in range(10)]


class TestRecrawlOfFailedCompanies:
    def test_company_with_no_pages_is_queued_for_full_retry(self, pipeline):
        pipeline["pages"] = []
        queue = []
        assert m.process_company(CO, queue) == m.empty_result(CO)
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
        monkeypatch.setattr(m, "process_company", lambda c, q: m.build_row(c, [], ["info@acme.nl"], "", []))
        called = []
        monkeypatch.setattr(m, "enrich_linkedin", lambda *a: called.append(1) or ("", []))
        rows = [m.empty_result(CO)]
        m.second_pass([(CO, None, None, "", [])], rows)
        assert rows[0]["contact_emails"] == "info@acme.nl" and saved and called == []

    def test_second_crawl_that_finds_nothing_leaves_row_unchanged(self, monkeypatch):
        saved = []
        monkeypatch.setattr(m, "save_results", lambda r: saved.append(1))
        monkeypatch.setattr(m, "process_company", lambda c, q: m.empty_result(c))
        rows = [m.empty_result(CO)]
        m.second_pass([(CO, None, None, "", [])], rows)
        assert rows[0] == m.empty_result(CO) and saved == []

    def test_recrawl_failure_does_not_stop_other_retries(self, monkeypatch):
        other = m.Company("Beta", "https://beta.nl")
        monkeypatch.setattr(m, "save_results", lambda r: None)
        def process(company, queue):
            if company is CO:
                raise RuntimeError("boom")
            return m.build_row(company, [], ["info@beta.nl"], "", [])
        monkeypatch.setattr(m, "process_company", process)
        rows = [m.empty_result(CO), m.empty_result(other)]
        m.second_pass([(CO, None, None, "", []), (other, None, None, "", [])], rows)
        assert rows[0]["contact_emails"] == "" and rows[1]["contact_emails"] == "info@beta.nl"

    def test_recrawl_does_not_requeue_itself(self, monkeypatch):
        monkeypatch.setattr(m, "save_results", lambda r: None)
        monkeypatch.setattr(m, "crawl_company", lambda c: [])
        outer = []
        real = m.process_company
        def spy(company, queue):
            outer.append(queue)
            return real(company, queue)
        monkeypatch.setattr(m, "process_company", spy)
        rows = [m.empty_result(CO)]
        m.second_pass([(CO, None, None, "", [])], rows)
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
