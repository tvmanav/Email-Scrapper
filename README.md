# Email Scrapper

Finds decision-maker and company contact emails for software development
companies listed on [Clutch.co](https://clutch.co), then optionally
enriches missing emails via Apollo/Kendo/SalesQL LinkedIn browser
extensions. Everything is written to a single CSV: `company_emails.csv`.

## Setup (new machine)

Clone the repo first, on any OS:

```bash
git clone <this-repo-url>
cd Email-Scrapper
```

Then follow the block for your OS. In every case, `pip install` alone does
**not** download the actual browser — `playwright install chromium` is a
separate, required step.

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
echo "GROQ_API_KEY=your-groq-key-here" > .env
```

### Windows (PowerShell)

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
"GROQ_API_KEY=your-groq-key-here" | Out-File -Encoding utf8 .env
```

If `Activate.ps1` is blocked by the execution policy, run PowerShell as
administrator once and allow it: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

### Windows (Command Prompt)

```bat
py -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
playwright install chromium
echo GROQ_API_KEY=your-groq-key-here > .env
```

`.env` is gitignored and never committed — you need your own Groq API key
(free tier works; see the quota note below). `GROQ_API_KEY` is required
unless you run with `USE_LLM=0`.

Every command further down this README (`python3 company_email_finder.py`,
etc.) is the same on all three platforms once the virtual environment is
activated, with one difference: Windows ships its interpreter as `python`,
not `python3` — use whichever one your shell resolves (run `python3
company_email_finder.py` on macOS/Linux, `python company_email_finder.py`
on Windows).

## Running the scraper

```bash
python3 company_email_finder.py
```

With no environment variables, this:
1. Probes Clutch access headlessly. If Cloudflare blocks it (a brand-new
   machine, or an expired session), it opens a **visible** browser window
   and waits up to 5 minutes for you to complete the check there — just
   click the "Verify you are human" checkbox when it appears. This only
   needs to happen once; the passing session is cached in `.clutch_profile/`
   and reused by every later headless run.
2. Walks every page of the general Clutch developers listing
   (`https://clutch.co/developers`), crawls each company's site, and
   writes/merges results into `company_emails.csv` as it goes (checkpointed
   chunk by chunk, so it's safe to stop and resume).

### Modes (`SOURCE` env var)

| `SOURCE` | What it does |
|---|---|
| `clutch_all` (default) | Walks every listing page (`?page=0..CLUTCH_ALL_PAGES-1`) of `CLUTCH_URL`, checkpointed in `clutch_companies.jsonl` / `clutch_pages_done.txt` so a long run can be stopped and resumed. |
| `clutch` | A single discovery pass over `CLUTCH_URL` as given (no pagination walk). |
| `clutch_countries` | Walks one country at a time (`clutch.co/<cc>/<category>`) until Clutch returns an empty page, then moves to the next. `CLUTCH_COUNTRIES="se,no,dk"` fixes the list/order; leave unset to auto-discover every country Clutch lists. |
| `clutch_verify` | Only runs the interactive Cloudflare check and exits — useful to pre-verify before a long unattended run. Not usually needed by hand anymore; `clutch`/`clutch_all`/`clutch_countries` now do this automatically. |
| `clutch_sitemap` | Alternative discovery that never needs Cloudflare clearance at all: reads every profile URL from Clutch's own sitemap (not challenged, unlike every actual content page) and each profile's last Wayback Machine snapshot (also not challenged) instead of the live page. Covers Clutch's *entire* directory, not just one category — `CLUTCH_SITEMAP_SERVICE_KEYWORD` (e.g. `developer`) approximates a category filter against each profile's listed service lines, since sitemap entries carry no category of their own. Archived snapshots can lag behind a company's current real site. Checkpointed in `clutch_sitemap_profiles.txt` / `clutch_sitemap_done.txt` / `clutch_sitemap_companies.jsonl`, so a long run can be stopped and resumed. |
| `rerun_generic` | Doesn't crawl new companies — gives every existing company that still has no decision-maker email a fresh LLM pass, until the daily Groq quota runs out. Resumable via `rerun_done.txt`. |

### Useful env vars

| Variable | Default | Purpose |
|---|---|---|
| `CLUTCH_URL` | `https://clutch.co/developers` | Listing URL to crawl (for `clutch` / `clutch_all`). |
| `CLUTCH_ALL_PAGES` | `1122` | Upper bound on listing pages for `clutch_all` — the real listing is far smaller in practice for most categories, but this caps a runaway walk. Lower it (e.g. `CLUTCH_ALL_PAGES=25`) to bound a single run. |
| `MAX_COMPANIES` | `0` (all) | Cap the number of companies actually processed — handy while testing. For `clutch_sitemap` this also caps how many profiles are resolved via Wayback in that run, since resolving is itself a network round-trip per profile. |
| `CLUTCH_SITEMAP_SERVICE_KEYWORD` | `""` (none) | For `clutch_sitemap`: case-insensitive substring match against each profile's listed service lines (e.g. `developer`). Empty means no filtering — every profile is kept. |
| `CLUTCH_SITEMAP_RESOLVE_BATCH_SIZE` | `15` | For `clutch_sitemap`: how many profiles to resolve via Wayback before crawling and saving that batch. Kept small since resolving is a slow, rate-limited round trip per profile — raising it means fewer but larger gaps before results land in `company_emails.csv`. |
| `USE_LLM` | `1` | Set to `0` to skip every Groq call entirely; emails/people then come from heuristics only (no API key needed). |
| `WORKERS` | `4` | Companies processed in parallel. |
| `CLUTCH_HEADED` | `0` | Set to `1` to render every Clutch page in a visible window (debugging). |
| `RERUN_MAX` | `0` (until quota runs out) | Caps how many companies `rerun_generic` re-analyses in one run. |

Full list of tunables is at the top of `company_email_finder.py`.

### Output: `company_emails.csv`

One row per named contact (plus one placeholder row for a company with no
named contact at all). Never committed to git — it's your live data file,
regenerated/merged in place on every run; re-running never loses or
duplicates a company already on file (matched by domain).

| Column | Meaning |
|---|---|
| `company_name`, `company_url` | The company and the source page it was found on. |
| `company_email` | The company's own generic mailbox(es) (`info@`, `hello@`, ...), joined with `; ` if there's more than one. Repeated on every row for that company. |
| `company_linkedin` | The company's LinkedIn page. |
| `first_name`, `last_name`, `contact_title` | The named contact. A title ending in ` [unverified]` means this person was found only via a LinkedIn search guess, not confirmed on the company's own site. |
| `decision_maker_email` | This person's own email, if found. Never a shared mailbox. |
| `contact_linkedin` | This person's LinkedIn profile. |

## Enriching missing emails (Apollo / SalesQL / Kendo)

A separate, optional pass that opens each contact's LinkedIn profile in a
real Chrome window and reads whatever email the Apollo, SalesQL or Kendo
browser extension reveals (in that order).

**One-time setup:**
```bash
APOLLO_SETUP=1 python3 apollo_linkedin_enrich.py
```
A browser window opens — install the Apollo, Kendo and SalesQL Chrome
extensions, log into each of them and into LinkedIn. The profile is saved
in `.apollo_profile/` and reused by every later run; no re-login needed.

**Normal run:**
```bash
python3 apollo_linkedin_enrich.py
```
Processes everyone with a `contact_linkedin` but no `decision_maker_email`
yet, skipping anyone already attempted (tracked in `apollo_enrich_done.txt`
— delete it to retry people who previously came up empty).

| Variable | Default | Purpose |
|---|---|---|
| `APOLLO_MAX_PER_RUN` | `0` (no limit) | Stop after this many profiles. |
| `APOLLO_PACE_MIN_SECONDS` / `APOLLO_PACE_MAX_SECONDS` | `25` / `55` | Randomized delay range between profiles. A fixed delay is itself a bot signature; lowering this risks LinkedIn temporarily blocking the account (there's no technical fix for that if it happens — only LinkedIn's own recovery flow). |
| `APOLLO_BREAK_EVERY` / `APOLLO_BREAK_SECONDS` | `8` / `300` | Take a longer break (default 5 min) after this many profiles. |

**Never run `company_email_finder.py` and `apollo_linkedin_enrich.py` at
the same time** — the Apollo script loads the whole CSV into memory once
and overwrites the file on every save; a concurrent crawler run adding new
companies would get those new rows wiped out.

## Notes

- **No guessed emails, ever.** Every email in the output was actually found
  on a page, by the LLM cross-checked against the page's own text, or
  revealed by a LinkedIn enrichment extension — never a pattern guess like
  `firstname.lastname@domain.com`.
- **Groq's free tier has a small daily token cap** — a `clutch_all` run
  typically exhausts it a couple of minutes in, after which crawling
  continues on heuristics only (no LLM). Run `rerun_generic` on a later day
  to give the companies that missed out an LLM pass once quota resets.
- Checkpoint files (`clutch_companies.jsonl`, `clutch_pages_done.txt`,
  `clutch_countries_done.txt`, `rerun_done.txt`, `apollo_enrich_done.txt`)
  are all plain text/JSONL and safe to delete individually to force that
  one phase to start over.

## Tests

```bash
pytest
```
