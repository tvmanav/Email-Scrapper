"""
Fills in missing decision-maker emails by opening each person's LinkedIn
profile in a dedicated, project-local Chrome profile (so Apollo/Kendo/SalesQL
stay installed and logged in between runs) and reading whatever email the
extension reveals. Tries Apollo first, then SalesQL, then Kendo.

Selectors for each extension are best-effort guesses: this was written
without live access to the extensions' DOM. When an extraction fails, the
page HTML/screenshot is dumped to apollo_enrich_debug/ - send that over so
the selectors can be corrected.

Setup (one time):
  APOLLO_SETUP=1 python3 apollo_linkedin_enrich.py
  A browser window opens on a fresh profile. Install the Apollo, Kendo and
  SalesQL Chrome extensions, log into each of them, and log into LinkedIn.
  Then press Enter in the terminal. The profile is saved in .apollo_profile/
  and reused by every later run - no re-login needed.

Normal run:
  python3 apollo_linkedin_enrich.py

Env vars:
  APOLLO_PROFILE_DIR     profile directory (default: .apollo_profile)
  APOLLO_MAX_PER_RUN     stop after this many profiles (0 = no limit, default 0)
  APOLLO_PACE_SECONDS    delay between profiles, in seconds (default 4)
"""
import csv
import logging
import os
import re
from pathlib import Path
from typing import Optional

import pandas as pd
from playwright.sync_api import Page, sync_playwright

from company_email_finder import (
    GENERIC_EMAIL_PREFIXES,
    OUTPUT_COLUMNS,
    OUTPUT_FILE,
    extract_emails,
    join_name,
    sanitize_csv_field,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("apollo_enrich")

CHROME_PROFILE_DIR = os.getenv("APOLLO_PROFILE_DIR", ".apollo_profile")
DONE_FILE = "apollo_enrich_done.txt"

# Playwright disables extensions by default (for clean, reproducible
# automation); this profile exists specifically to run Apollo/Kendo/SalesQL,
# so that default has to be turned off for every launch that uses it.
# --use-mock-keychain/--password-store=basic are also Playwright defaults -
# on macOS, Chrome's cookie encryption is tied to the real Keychain, so a
# mock one makes an existing profile's already-encrypted cookies (sessions
# included) silently undecryptable, i.e. the profile looks logged out.
EXTENSION_IGNORE_ARGS = [
    "--disable-extensions", "--disable-component-extensions-with-background-pages",
    "--use-mock-keychain", "--password-store=basic",
]


def launch_extension_profile(p, headless: bool):
    # LinkedIn/Google often CAPTCHA-wall or block sign-in from a browser that
    # announces itself as automated (navigator.webdriver, the "Chrome is
    # being controlled by automated test software" banner) - same issue the
    # Clutch flow hits with Cloudflare, fixed there with this same arg.
    return p.chromium.launch_persistent_context(
        CHROME_PROFILE_DIR, channel="chrome", headless=headless, viewport=None,
        ignore_default_args=EXTENSION_IGNORE_ARGS,
        args=["--disable-blink-features=AutomationControlled"],
    )
DEBUG_DIR = Path("apollo_enrich_debug")

MAX_PER_RUN = int(os.getenv("APOLLO_MAX_PER_RUN", "0"))
PACE_SECONDS = float(os.getenv("APOLLO_PACE_SECONDS", "4"))
NAV_TIMEOUT_MS = 20_000
WAIT_AFTER_LOAD_MS = 4_000

EXTENSION_ORDER = ("apollo", "salesql", "kendo")


class CreditsExhausted(Exception):
    """Raised by an extractor when its extension reports no credits left."""


def read_done() -> set:
    if not os.path.exists(DONE_FILE):
        return set()
    with open(DONE_FILE) as f:
        return {line.strip() for line in f if line.strip()}


def mark_done(linkedin_url: str) -> None:
    with open(DONE_FILE, "a") as f:
        f.write(linkedin_url + "\n")


def build_queue(rows: list) -> list:
    """(row_index, person_name, linkedin_url) for every decision-maker row
    with a LinkedIn URL but no email yet - one row is one person now, so no
    more parsing a packed field to find them."""
    queue = []
    for index, row in enumerate(rows):
        if not row.get("first_name") and not row.get("last_name"):
            continue
        if row.get("decision_maker_email") or not row.get("contact_linkedin"):
            continue
        queue.append((index, join_name(row.get("first_name", ""), row.get("last_name", "")), row["contact_linkedin"]))
    return queue


def dump_debug(page: Page, label: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", label)[:80]
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{safe}.png"), full_page=True)
        (DEBUG_DIR / f"{safe}.html").write_text(page.content())
    except Exception:
        logger.exception("Could not save debug dump for %s", label)


# A revealed contact sometimes has both a corporate and a personal address
# (e.g. SalesQL showing "ritesh@simform.com" alongside "sritesh81@gmail.com"
# for the same person) - the corporate one is the useful one for outreach.
FREE_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com"}


def rank_email(email: str) -> int:
    return 1 if email.split("@")[-1].lower() in FREE_EMAIL_DOMAINS else 0


def best_email(text: str) -> Optional[str]:
    candidates = extract_emails(text)
    return min(candidates, key=rank_email) if candidates else None


def new_email(before_text: str, after_text: str) -> Optional[str]:
    """An email revealed by clicking "reveal" must be new, not already on
    the page beforehand - every one of these extensions shows the logged-in
    account's own email somewhere in its persistent UI chrome (Kendo's
    footer, Apollo's account menu, ...), and that must never be mistaken
    for the contact's email. Among newly-revealed candidates, a corporate
    address is preferred over a personal one (see FREE_EMAIL_DOMAINS)."""
    before = set(extract_emails(before_text))
    candidates = [e for e in extract_emails(after_text) if e not in before]
    if candidates:
        return min(candidates, key=rank_email)
    return None


# ---------------------------------------------------------------------
# Extension extractors. Each returns the email string, or None if the
# extension didn't surface one on this profile. Raises CreditsExhausted
# when the extension's own "out of credits" message is detected.
# ---------------------------------------------------------------------

# A bare "upgrade" matches permanent nav-menu links ("Upgrade" in SalesQL's
# header is always present, regardless of credit balance) - require it to
# be talking about credits/plan specifically, not just appear anywhere.
CREDITS_EXHAUSTED_RE = re.compile(
    r"no credits left|out of credits|insufficient credits|"
    r"upgrade (your plan|to (continue|reveal|unlock))", re.I,
)

def try_apollo(page: Page, baseline_before: Optional[str] = None) -> Optional[str]:
    # Apollo's toolbar icon (data-cy is Apollo's own QA hook, stabler than
    # its hashed CSS classes) is collapsed by default; clicking it opens
    # the actual contact panel elsewhere in the DOM.
    icon = page.locator("#apollo-everywhere-overlay-icon")
    if icon.count() == 0:
        return None
    before = baseline_before if baseline_before is not None else page.locator("body").inner_text(timeout=5000)
    try:
        # force=True: Kendo's panel renders at the maximum possible z-index
        # (2147483647) and visually covers Apollo's icon, so a normal click
        # fails actionability checks and silently does nothing.
        icon.click(timeout=5000, force=True)
        page.wait_for_timeout(2500)
    except Exception:
        logger.debug("apollo icon click failed", exc_info=True)

    reveal = page.get_by_text(re.compile(r"access email|reveal email|get email|show email", re.I)).first
    if reveal.count():
        try:
            reveal.click(timeout=5000)
            page.wait_for_timeout(2000)
        except Exception:
            pass

    after = page.locator("body").inner_text(timeout=5000)
    if CREDITS_EXHAUSTED_RE.search(after):
        raise CreditsExhausted("apollo")
    return new_email(before, after)


def try_kendo(page: Page, baseline_before: Optional[str] = None) -> Optional[str]:
    panel = page.locator("#kendo_elem")
    if panel.count() == 0:
        return None

    before = panel.inner_text(timeout=5000)
    if "go to a profile" in before.lower():
        # Kendo sometimes misses a hard page.goto() navigation and stays in
        # its idle state even though we're already on a profile page; a
        # reload lets its content script re-detect the current URL.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        panel = page.locator("#kendo_elem")
        if panel.count() == 0:
            return None
        before = panel.inner_text(timeout=5000)

    after = before
    reveal = panel.get_by_role("button", name=re.compile(r"show info|find email|reveal email|show email|start sourcing", re.I)).first
    if reveal.count():
        try:
            reveal.click(timeout=5000)
            page.wait_for_timeout(2000)
            after = panel.inner_text(timeout=5000)
        except Exception:
            pass

    if CREDITS_EXHAUSTED_RE.search(after):
        raise CreditsExhausted("kendo")
    return new_email(before, after)


def try_salesql(page: Page, baseline_before: Optional[str] = None) -> Optional[str]:
    # SalesQL's floating badge is identified via its alt text (its own CSS
    # classes are build-hashed and change between versions); clicking the
    # collapsed badge only expands a summary ("5 emails found") - the actual
    # address stays hidden until "Reveal Info & Add" is clicked too.
    badge = page.locator("img[alt='SalesQL']").first
    if badge.count() == 0:
        return None
    before = baseline_before if baseline_before is not None else page.locator("body").inner_text(timeout=5000)
    try:
        badge.click(timeout=5000)
        page.wait_for_timeout(2000)
    except Exception:
        pass

    reveal = page.get_by_text(re.compile(r"reveal info|reveal email|show email|get email", re.I)).first
    if reveal.count():
        try:
            reveal.click(timeout=5000)
            page.wait_for_timeout(2500)
        except Exception:
            pass

    after = page.locator("body").inner_text(timeout=5000)
    if CREDITS_EXHAUSTED_RE.search(after):
        raise CreditsExhausted("salesql")
    return new_email(before, after)


EXTRACTORS = {"apollo": try_apollo, "kendo": try_kendo, "salesql": try_salesql}
EXHAUSTED = {name: False for name in EXTENSION_ORDER}


DEBUG_STEPS = os.getenv("APOLLO_DEBUG_STEPS") == "1"


def find_email(page: Page, debug_label: str = "") -> tuple:
    """Returns (email_or_None, source_extension_or_None). A revealed address
    that's a shared department mailbox (info@, hr@, sales@, ...) is not this
    person's own email, even though the extension offered it as their
    contact - skip it and keep trying the remaining extensions, rather than
    writing a company-wide inbox into decision_maker_email."""
    try:
        # Captured once, before any extension acts, and reused as every
        # extractor's "before" snapshot: otherwise an email one extension
        # reveals in the page body becomes the NEXT extension's own
        # before-snapshot baseline, making it look already-present there
        # too and hiding a genuinely new email forever.
        baseline_before = page.locator("body").inner_text(timeout=5000)
    except Exception:
        baseline_before = None

    for name in EXTENSION_ORDER:
        if EXHAUSTED[name]:
            continue
        try:
            email = EXTRACTORS[name](page, baseline_before)
        except CreditsExhausted:
            logger.warning("%s reports no credits left; skipping it for the rest of this run.", name)
            EXHAUSTED[name] = True
            continue
        except Exception:
            logger.exception("%s extractor errored", name)
            continue
        if DEBUG_STEPS and debug_label:
            dump_debug(page, f"{debug_label}_{name}_{'HIT' if email else 'miss'}")
        if email and email.startswith(GENERIC_EMAIL_PREFIXES):
            logger.info("%s revealed a shared mailbox (%s), not a personal email; trying the next extension", name, email)
            continue
        if email:
            return email, name
    return None, None


def save_rows(rows: list) -> None:
    sanitized = [{col: sanitize_csv_field(row.get(col, "")) for col in OUTPUT_COLUMNS} for row in rows]
    pd.DataFrame(sanitized, columns=OUTPUT_COLUMNS).to_csv(OUTPUT_FILE, index=False, quoting=csv.QUOTE_ALL)


def apply_email(row: dict, email: str) -> None:
    row["decision_maker_email"] = sanitize_csv_field(email)


SETUP_WAIT_SECONDS = int(os.getenv("APOLLO_SETUP_WAIT_SECONDS", "900"))


def run_setup() -> None:
    """Opens a visible browser on a fresh, dedicated profile and holds it
    open for SETUP_WAIT_SECONDS so a person can install Apollo/Kendo/SalesQL
    and log into each of them and into LinkedIn. The profile (cookies,
    installed extensions) is then reused by every later run - no re-login
    needed. There's no way to detect "done" generically here (unlike the
    Clutch flow, which polls for a page element), so this just waits out a
    fixed window instead of blocking on terminal input - the script may be
    launched headlessly from an automation context with no attached stdin."""
    logger.info(
        "Opening a browser window on %s - install Apollo, Kendo and SalesQL there, "
        "then log into each of them and into LinkedIn. You have %d minutes.",
        CHROME_PROFILE_DIR, SETUP_WAIT_SECONDS // 60,
    )
    with sync_playwright() as p:
        context = launch_extension_profile(p, headless=False)
        context.new_page().goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        elapsed = 0
        while elapsed < SETUP_WAIT_SECONDS:
            step = min(60, SETUP_WAIT_SECONDS - elapsed)
            context.pages[0].wait_for_timeout(step * 1000)
            elapsed += step
            logger.info("%d minute(s) left to finish setup...", (SETUP_WAIT_SECONDS - elapsed) // 60)

        context.close()
    logger.info("Setup window closed. Profile saved in %s - future runs reuse it automatically.", CHROME_PROFILE_DIR)


def main() -> None:
    if os.getenv("APOLLO_SETUP") == "1":
        run_setup()
        return

    if not os.path.isdir(CHROME_PROFILE_DIR):
        raise RuntimeError(
            f"No profile at {CHROME_PROFILE_DIR!r} yet - run the one-time setup first: "
            "APOLLO_SETUP=1 python3 apollo_linkedin_enrich.py"
        )

    rows = pd.read_csv(OUTPUT_FILE, dtype=str, keep_default_na=False).to_dict("records")

    queue = build_queue(rows)
    done = read_done()
    remaining = [item for item in queue if item[2] not in done]
    todo = remaining[:MAX_PER_RUN] if MAX_PER_RUN > 0 else remaining

    logger.info(
        "%d profiles queued, %d already attempted, %d to run this time",
        len(queue), len(queue) - len(remaining), len(todo),
    )
    if not todo:
        logger.info("Nothing to do.")
        return

    found_count = 0
    with sync_playwright() as p:
        try:
            context = launch_extension_profile(p, headless=False)
        except Exception as e:
            raise RuntimeError(
                f"Could not open the profile at {CHROME_PROFILE_DIR!r} - make sure no other "
                f"Chrome window is already using it. ({e})"
            ) from e

        page = context.new_page()

        for i, (row_index, person_name, linkedin_url) in enumerate(todo, 1):
            company_url = rows[row_index]["company_url"]
            logger.info("[%d/%d] %s - %s", i, len(todo), person_name, linkedin_url)
            try:
                page.goto(linkedin_url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
            except Exception as e:
                logger.warning("Could not open %s: %s", linkedin_url, e)
                mark_done(linkedin_url)
                continue

            if page.get_by_text(re.compile(r"sign in to linkedin|join linkedin", re.I)).first.count():
                raise RuntimeError("This Chrome profile isn't logged into LinkedIn - log in once, then re-run.")

            email, source = find_email(page, debug_label=f"{person_name}_{company_url}")
            mark_done(linkedin_url)

            if email:
                apply_email(rows[row_index], email)
                found_count += 1
                save_rows(rows)
                logger.info("  -> found via %s: %s", source, email)
            else:
                dump_debug(page, f"{person_name}_{company_url}")
                logger.info("  -> not found; debug dump saved")

            if all(EXHAUSTED.values()):
                logger.warning("All three extensions report no credits left; stopping.")
                break

            page.wait_for_timeout(int(PACE_SECONDS * 1000))

        context.close()

    logger.info("Done. %d/%d profiles got an email this run.", found_count, len(todo))


if __name__ == "__main__":
    main()
