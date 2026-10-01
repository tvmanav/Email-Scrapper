import os
import re
import csv
import json
import time
import logging
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin, urlparse, urldefrag, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
import pandas as pd
import tldextract

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

CLUTCH_URL = "https://clutch.co/se/developers/healthcare-industry"

# Groq exposes an OpenAI-compatible endpoint, so the official `openai`
# SDK is reused as the client, just pointed at Groq instead of OpenAI.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")

OUTPUT_FILE = "company_emails.csv"

MAX_COMPANIES = 0
# 0 = process all companies
# Set to e.g. 5 while testing

MAX_PAGES_PER_COMPANY = 15

REQUEST_TIMEOUT = 20

CRAWL_DELAY = 0.5

# Clutch sits behind a Cloudflare JS challenge, so the listing page
# must be rendered with a real browser engine (Playwright), not requests.
CLUTCH_PAGE_TIMEOUT_MS = 30000

CLUTCH_RENDER_WAIT_MS = 8000

CLUTCH_MAX_SHOW_MORE_CLICKS = 25

USER_AGENT = (
    "Mozilla/5.0 "
    "(Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/150.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# OPENAI
# ============================================================

client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url=GROQ_BASE_URL,
)


# ============================================================
# LLM OUTPUT SCHEMA
# ============================================================

class DecisionMaker(BaseModel):
    name: str
    title: str
    email: str


class LLMDecision(BaseModel):
    decision_makers: List[DecisionMaker] = Field(default_factory=list)

    contact_emails: List[str] = Field(default_factory=list)


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class Company:
    name: str
    url: str


@dataclass
class PageData:
    url: str
    text: str
    emails: List[str]


# ============================================================
# SESSION
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# EMAIL EXTRACTION
# ============================================================

EMAIL_REGEX = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+"
    r"@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)


def normalize_email(email: str) -> str:
    email = email.strip().lower()

    # Remove trailing punctuation
    email = email.rstrip(".,;:)]}>")

    return email


def decode_cloudflare_email(hex_string: str) -> Optional[str]:
    """
    Cloudflare's "email protection" plugin replaces a visible email
    with a placeholder and stashes the real address, XOR-"encrypted",
    in a data-cfemail attribute. It renders fine in a browser (a JS
    snippet decodes it) but is invisible to a plain HTTP scraper
    unless decoded here.
    """

    try:

        raw = bytes.fromhex(hex_string)

    except ValueError:

        return None

    if len(raw) < 2:
        return None

    key = raw[0]

    decoded = bytes(byte ^ key for byte in raw[1:])

    try:

        return decoded.decode("utf-8")

    except UnicodeDecodeError:

        return None


def extract_emails(text: str) -> List[str]:

    if not text:
        return []

    emails = EMAIL_REGEX.findall(text)

    result = set()

    for email in emails:

        email = normalize_email(email)

        if "example.com" in email:
            continue

        if email.endswith(".png"):
            continue

        if email.endswith(".jpg"):
            continue

        result.add(email)

    return sorted(result)


# ============================================================
# DOMAIN HELPERS
# ============================================================

def normalize_url(url: str) -> str:

    if not url:
        return ""

    url = url.strip()

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    url, _ = urldefrag(url)

    return url.rstrip("/")


def get_domain(url: str) -> str:

    parsed = urlparse(url)

    return parsed.netloc.lower().replace("www.", "")


def same_domain(url1: str, url2: str) -> bool:

    return get_domain(url1) == get_domain(url2)


# ============================================================
# ROBOTS
# ============================================================

robots_cache = {}


def can_crawl(url: str) -> bool:

    domain = get_domain(url)

    if domain not in robots_cache:

        robots_url = f"https://{domain}/robots.txt"

        rp = RobotFileParser()
        rp.set_url(robots_url)

        try:
            rp.read()
        except Exception:
            # robots.txt couldn't be reached (timeout, DNS failure, etc.)
            # Unlike an explicit disallow, this isn't a signal the site
            # doesn't want to be crawled, so default to allowing rather
            # than silently dropping the whole domain from the run.
            robots_cache[domain] = True
            return True

        robots_cache[domain] = rp

    rp = robots_cache[domain]

    if rp is True:
        return True

    return rp.can_fetch(USER_AGENT, url)


# ============================================================
# HTTP FETCH
# ============================================================

def fetch(url: str) -> Optional[str]:

    url = normalize_url(url)

    if not url:
        return None

    if not can_crawl(url):

        logger.warning(
            "Blocked by robots.txt: %s",
            url
        )

        return None

    try:

        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )

        if response.status_code != 200:

            logger.warning(
                "HTTP %s: %s",
                response.status_code,
                url
            )

            return None

        content_type = response.headers.get(
            "content-type",
            ""
        ).lower()

        if "text/html" not in content_type:

            return None

        time.sleep(CRAWL_DELAY)

        return response.text

    except requests.RequestException as e:

        logger.warning(
            "Request failed: %s | %s",
            url,
            e
        )

        return None


# ============================================================
# HTML PARSING
# ============================================================

def parse_page(
    url: str,
    html: str
) -> PageData:

    soup = BeautifulSoup(
        html,
        "lxml"
    )

    # Remove things that don't provide useful contact context
    for tag in soup([
        "script",
        "style",
        "noscript",
        "svg"
    ]):

        tag.decompose()

    # Form placeholder/value attributes commonly contain fake example
    # addresses (e.g. placeholder="name@company.com" on a Contact
    # Form 7 field) - strip them so they can't be mistaken for a real
    # contact email.
    for tag in soup.find_all(["input", "textarea"]):

        if tag.has_attr("placeholder"):
            del tag["placeholder"]

        if tag.has_attr("value"):
            del tag["value"]

    # Cloudflare's email-protection plugin hides real addresses behind
    # a data-cfemail attribute and a "[email protected]" placeholder,
    # decoded client-side by JS a plain scraper never runs. Decode it
    # in place so both the visible text (for name/title context) and
    # the regex-based extraction below see the real address.
    for tag in soup.find_all(attrs={"data-cfemail": True}):

        decoded = decode_cloudflare_email(
            tag["data-cfemail"]
        )

        if decoded:

            tag.string = decoded

    text = soup.get_text(
        separator=" ",
        strip=True
    )

    # Scan the cleaned markup (script/style/svg already stripped above)
    # rather than the raw HTML, so analytics/tracking JS can't inject
    # false-positive "emails" into the extracted set.
    emails = set(
        extract_emails(str(soup))
    )

    # Also inspect mailto links
    for a in soup.find_all("a"):

        href = a.get("href", "")

        if href.lower().startswith("mailto:"):

            email = href[7:].split("?")[0]

            if email:

                emails.add(
                    normalize_email(email)
                )

    return PageData(
        url=url,
        text=text,
        emails=sorted(emails)
    )


# ============================================================
# CLUTCH EXTRACTION
# ============================================================

def render_clutch_listing_html(clutch_url: str) -> str:
    """
    Clutch serves a Cloudflare "Just a moment..." JS challenge to
    plain HTTP clients, so the listing has to be rendered with a
    real browser engine. Headless Chromium is fingerprinted and
    blocked by that challenge unless automation markers are hidden.
    """

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )

        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            """
        )

        page = context.new_page()

        page.goto(
            clutch_url,
            wait_until="domcontentloaded",
            timeout=CLUTCH_PAGE_TIMEOUT_MS,
        )

        page.wait_for_timeout(CLUTCH_RENDER_WAIT_MS)

        # Clutch lazy-loads extra providers behind a "See more
        # providers" button. Click it until it disappears/disables
        # so the full list for this page is present in the DOM.
        for _ in range(CLUTCH_MAX_SHOW_MORE_CLICKS):

            button = page.query_selector(
                "#show-more-providers-button"
            )

            if not button:
                break

            if not button.is_visible():
                break

            if button.get_attribute("data-show-enabled") == "false":
                break

            button.click()

            page.wait_for_timeout(1500)

        html = page.content()

        browser.close()

        return html


def resolve_clutch_redirect(redirect_href: str) -> Optional[str]:
    """
    Clutch "Visit Website" links point at r.clutch.co click-tracking
    redirects, not the company's real site. Follow the redirect chain
    with a normal HTTP client (no challenge on this subdomain) and
    strip Clutch's utm_* tracking params from the final URL.
    """

    try:

        response = session.get(
            redirect_href,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

    except requests.RequestException as e:

        logger.warning(
            "Failed to resolve Clutch redirect: %s | %s",
            redirect_href,
            e
        )

        return None

    final_url = response.url

    parts = urlsplit(final_url)

    clean_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "", "")
    )

    return normalize_url(clean_url)


CLUTCH_RENDER_MAX_ATTEMPTS = 3


def extract_clutch_companies(
    clutch_url: str
) -> List[Company]:

    logger.info(
        "Rendering Clutch listing: %s",
        clutch_url
    )

    rows = []

    soup = None

    for attempt in range(1, CLUTCH_RENDER_MAX_ATTEMPTS + 1):

        html = render_clutch_listing_html(clutch_url)

        soup = BeautifulSoup(
            html,
            "lxml"
        )

        rows = soup.select("div.provider-row")

        if rows:
            break

        # The Cloudflare challenge occasionally hasn't fully resolved
        # within the fixed wait time - a fresh attempt usually clears
        # it rather than this being a real structure change.
        logger.warning(
            "Attempt %d/%d: no provider rows rendered, retrying.",
            attempt,
            CLUTCH_RENDER_MAX_ATTEMPTS
        )

    if not rows:
        raise RuntimeError(
            "No provider rows found on the Clutch page. "
            "Clutch may have changed its page structure."
        )

    companies = []

    seen = set()

    for row in rows:

        name_el = (
            row.select_one(".provider__title-link")
            or row.find(["h2", "h3", "h4"])
        )

        name = (
            name_el.get_text(" ", strip=True)
            if name_el else None
        )

        link_el = row.select_one("a.website-link__item")

        if not name or not link_el:
            continue

        redirect_href = link_el.get("href")

        if not redirect_href:
            continue

        website = resolve_clutch_redirect(redirect_href)

        if not website:
            continue

        domain = get_domain(website)

        if not domain or domain == "clutch.co":
            continue

        if domain in seen:
            continue

        seen.add(domain)

        companies.append(
            Company(
                name=name,
                url=website
            )
        )

    logger.info(
        "Found %d companies",
        len(companies)
    )

    return companies


# ============================================================
# IMPORTANT PAGES
# ============================================================

KEYWORDS = [
    "about",
    "team",
    "leadership",
    "management",
    "contact",
    "company",
    "people",
    "founder",
    "executive",
    "ceo",
    "cto",
    "director",
    "owner",
    "principal",
]

# Exact URL path segments that reliably indicate an about/team/contact
# page. Checked as whole segments (not substrings), so a service page
# like "/cloud-management-outsourcing/" doesn't collide with this -
# only "/about-us/" or "/contact/" style paths do.
HIGH_VALUE_PATH_SEGMENTS = {
    "about",
    "about-us",
    "aboutus",
    "who-we-are",
    "company",
    "team",
    "our-team",
    "leadership",
    "management-team",
    "people",
    "founders",
    "executive-team",
    "contact",
    "contact-us",
    "contactus",
    "get-in-touch",
}


def score_link(
    text: str,
    href: str
) -> int:

    combined = (
        f"{text} {href}"
    ).lower()

    weak_score = 0

    for keyword in KEYWORDS:

        if keyword in combined:

            weak_score += 1

    path_segments = {
        segment
        for segment in urlparse(href).path.lower().strip("/").split("/")
        if segment
    }

    strong_score = len(
        path_segments & HIGH_VALUE_PATH_SEGMENTS
    )

    # An exact path-segment match (e.g. "/contact/") is a much
    # stronger signal than an incidental keyword substring hit, so it
    # must always sort above any number of weak matches.
    return strong_score * 1000 + weak_score


def discover_pages(
    homepage: str,
    html: str,
    max_pages: int
) -> List[str]:

    soup = BeautifulSoup(
        html,
        "lxml"
    )

    candidates = []

    for a in soup.find_all("a"):

        href = a.get("href")

        if not href:
            continue

        absolute = urljoin(
            homepage,
            href
        )

        absolute, _ = urldefrag(
            absolute
        )

        if not same_domain(
            homepage,
            absolute
        ):
            continue

        text = a.get_text(
            " ",
            strip=True
        )

        score = score_link(
            text,
            absolute
        )

        if score > 0:

            candidates.append(
                (score, absolute)
            )

    # Highest priority pages first
    candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )

    result = []
    seen = set()

    for _, url in candidates:

        # Dedupe on the normalized form so "/contact" and "/contact/"
        # don't both consume a slot in the crawl budget.
        key = normalize_url(url)

        if key not in seen:

            seen.add(key)

            result.append(url)

        if len(result) >= max_pages:

            break

    return result


# ============================================================
# CRAWL COMPANY
# ============================================================

def crawl_company(
    company: Company
) -> List[PageData]:

    logger.info(
        "Crawling: %s | %s",
        company.name,
        company.url
    )

    start_url = company.url

    homepage_html = fetch(start_url)

    if not homepage_html:

        # Clutch's "visit website" link can point at a stale deep
        # path (e.g. a dropped /en locale prefix) even though the
        # site is live at its root domain. Retry there before giving
        # up on the company entirely.
        parts = urlparse(company.url)

        root_url = normalize_url(
            f"{parts.scheme}://{parts.netloc}"
        )

        if root_url != start_url:

            logger.info(
                "Homepage fetch failed, retrying at domain root: %s",
                root_url
            )

            start_url = root_url

            homepage_html = fetch(start_url)

    if not homepage_html:

        return []

    homepage = parse_page(
        start_url,
        homepage_html
    )

    pages = [
        homepage
    ]

    discovered = discover_pages(
        start_url,
        homepage_html,
        MAX_PAGES_PER_COMPANY - 1
    )

    for url in discovered:

        if url == start_url:
            continue

        html = fetch(url)

        if not html:
            continue

        page = parse_page(
            url,
            html
        )

        pages.append(page)

    return pages


# ============================================================
# PREPARE LLM INPUT
# ============================================================

LLM_CONTEXT_CHAR_BUDGET = 16000

LLM_EMAIL_PAGE_CHAR_LIMIT = 2500

LLM_NO_EMAIL_PAGE_CHAR_LIMIT = 800


def prepare_llm_context(
    company: Company,
    pages: List[PageData]
) -> str:

    # Groq's free-tier rate limit (8K tokens/minute on every available
    # model) can't absorb 15 pages x 12,000 chars of raw page text in
    # one request. A page that has no extracted email can never supply
    # a validated email on its own, so it isn't worth its token cost -
    # only the homepage is kept (for name/title context) when it has no
    # emails. Pages with emails are prioritized and capped, with an
    # overall budget as a last-resort guard against oversized sites.

    email_pages = [page for page in pages if page.emails]

    no_email_pages = [
        page for page in pages
        if not page.emails and page is pages[0]
    ]

    sections = []

    used_chars = 0

    for page in email_pages + no_email_pages:

        limit = (
            LLM_EMAIL_PAGE_CHAR_LIMIT
            if page.emails
            else LLM_NO_EMAIL_PAGE_CHAR_LIMIT
        )

        text = page.text[:limit]

        section = f"""
URL:
{page.url}

EXTRACTED EMAILS:
{json.dumps(page.emails)}

PAGE TEXT:
{text}
"""

        if used_chars + len(section) > LLM_CONTEXT_CHAR_BUDGET:
            break

        sections.append(section)

        used_chars += len(section)

    return f"""
COMPANY:
{company.name}

WEBSITE:
{company.url}

WEBSITE DATA:
{"".join(sections)}
"""


# ============================================================
# LLM DECISION
# ============================================================

SYSTEM_PROMPT = """
You are an information-extraction agent.

Your task is to identify publicly displayed business
email addresses from a company's own website.

You must NOT invent, infer, generate, guess, or construct
email addresses.

Only return an email if the exact email address appears
in the supplied website data.

Your main task is to identify decision-maker emails.

Decision makers include people with roles such as:

- Founder
- Co-Founder
- Owner
- CEO
- Chief Executive Officer
- Managing Director
- President
- Partner
- Executive Director
- CTO
- Chief Technology Officer
- COO
- Chief Operating Officer
- General Manager
- Director
- VP / Vice President
- Head of Department

Prioritize actual people over generic departments.

For every decision maker:

1. Verify that the person's name appears in the website data.
2. Verify that their title/role appears or is clearly associated
   with the person.
3. Verify that the EXACT email address appears in the website data.
4. Do not associate a generic company email with a person unless
   the website explicitly connects them.
5. Do not infer email patterns such as first.last@company.com.

If multiple decision makers have publicly displayed emails,
return ALL of them.

If at least one valid decision-maker email exists:

- return those emails in decision_makers
- return an empty contact_emails list

If there are NO decision-maker emails:

Return publicly displayed generic/business contact emails
such as:

- info@
- contact@
- hello@
- office@
- enquiries@
- sales@
- business@
- support@
- admin@

Do not return personal emails belonging to people who cannot
reasonably be classified as decision makers.

Never invent information.
"""


RATE_LIMIT_MAX_RETRIES = 2

RATE_LIMIT_MAX_WAIT_SECONDS = 90

RATE_LIMIT_WAIT_RE = re.compile(
    r"try again in (?:(\d+)m)?([\d.]+)s"
)


def parse_rate_limit_wait_seconds(message: str) -> Optional[float]:

    match = RATE_LIMIT_WAIT_RE.search(message or "")

    if not match:
        return None

    minutes = int(match.group(1)) if match.group(1) else 0

    seconds = float(match.group(2))

    return minutes * 60 + seconds


def analyze_with_llm(
    company: Company,
    pages: List[PageData]
) -> LLMDecision:

    context = prepare_llm_context(
        company,
        pages
    )

    response = None

    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):

        try:

            response = client.chat.completions.parse(
                model=LLM_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": SYSTEM_PROMPT
                    },
                    {
                        "role": "user",
                        "content": context
                    }
                ],
                response_format=LLMDecision,
            )

            break

        except RateLimitError as e:

            wait_seconds = parse_rate_limit_wait_seconds(str(e))

            # Short per-minute bursts are worth waiting out. A long
            # wait (e.g. a daily token quota reset) isn't - blocking
            # for 20+ minutes on one company would stall the whole
            # run, so let it fail fast and move on instead.
            can_retry = (
                wait_seconds is not None
                and wait_seconds <= RATE_LIMIT_MAX_WAIT_SECONDS
                and attempt < RATE_LIMIT_MAX_RETRIES
            )

            if not can_retry:
                raise

            logger.warning(
                "Rate limited analyzing %s, waiting %.1fs "
                "before retry (%d/%d)",
                company.name,
                wait_seconds,
                attempt + 1,
                RATE_LIMIT_MAX_RETRIES
            )

            time.sleep(wait_seconds)

    result = response.choices[0].message.parsed

    if not result:

        return LLMDecision(
            decision_makers=[],
            contact_emails=[]
        )

    return result


# ============================================================
# HARD VALIDATION
# ============================================================

def validate_result(
    result: LLMDecision,
    pages: List[PageData]
) -> LLMDecision:

    # Emails that ACTUALLY appeared on the website
    website_emails = set()

    for page in pages:

        website_emails.update(
            page.emails
        )

    valid_decision_makers = []

    for person in result.decision_makers:

        email = normalize_email(
            person.email
        )

        if email in website_emails:

            valid_decision_makers.append(
                DecisionMaker(
                    name=person.name.strip(),
                    title=person.title.strip(),
                    email=email
                )
            )

    # Deduplicate
    seen = set()
    decision_makers = []

    for person in valid_decision_makers:

        if person.email not in seen:

            seen.add(person.email)

            decision_makers.append(
                person
            )

    # --------------------------------------------------------
    # IMPORTANT:
    # If decision-maker emails exist, ignore fallback emails.
    # --------------------------------------------------------

    if decision_makers:

        return LLMDecision(
            decision_makers=decision_makers,
            contact_emails=[]
        )

    # Otherwise fall back to contact emails. Trust the LLM's picks as
    # long as the exact email genuinely appears on the website - don't
    # second-guess its classification against a hardcoded prefix list,
    # since a legitimate general contact address doesn't have to be
    # named info@/contact@/etc.
    contact_emails = []

    for email in result.contact_emails:

        email = normalize_email(email)

        if email in website_emails:

            if email not in contact_emails:

                contact_emails.append(
                    email
                )

    # If the LLM found nothing, fall back to a generic-prefix heuristic
    # over the emails actually extracted from the site.
    if not contact_emails:

        generic_prefixes = (
            "info@",
            "contact@",
            "hello@",
            "office@",
            "enquiries@",
            "enquiry@",
            "sales@",
            "business@",
            "support@",
            "admin@",
        )

        for email in website_emails:

            if email.startswith(
                generic_prefixes
            ):

                contact_emails.append(
                    email
                )

    return LLMDecision(
        decision_makers=[],
        contact_emails=sorted(
            set(contact_emails)
        )
    )


# ============================================================
# PROCESS COMPANY
# ============================================================

def process_company(
    company: Company
):

    try:

        pages = crawl_company(
            company
        )

        if not pages:

            logger.warning(
                "No pages found: %s",
                company.name
            )

            return {
                "company_name": company.name,
                "company_url": company.url,
                "decision_maker_emails": "",
                "contact_emails": ""
            }

        # ----------------------------------------------------
        # LLM
        # ----------------------------------------------------

        llm_result = analyze_with_llm(
            company,
            pages
        )

        # ----------------------------------------------------
        # Deterministic validation
        # ----------------------------------------------------

        validated = validate_result(
            llm_result,
            pages
        )

        decision_emails = [
            person.email
            for person in validated.decision_makers
        ]

        return {
            "company_name": company.name,
            "company_url": company.url,
            "decision_maker_emails": ";".join(
                sorted(set(decision_emails))
            ),
            "contact_emails": ";".join(
                sorted(
                    set(
                        validated.contact_emails
                    )
                )
            )
        }

    except Exception as e:

        logger.exception(
            "Company failed: %s",
            company.name
        )

        return {
            "company_name": company.name,
            "company_url": company.url,
            "decision_maker_emails": "",
            "contact_emails": ""
        }


# ============================================================
# SAVE CSV
# ============================================================

def save_results(results):

    df = pd.DataFrame(
        results,
        columns=[
            "company_name",
            "company_url",
            "decision_maker_emails",
            "contact_emails",
        ]
    )

    df.to_csv(
        OUTPUT_FILE,
        index=False,
        quoting=csv.QUOTE_MINIMAL
    )

    logger.info(
        "Saved: %s",
        OUTPUT_FILE
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not os.getenv("GROQ_API_KEY"):

        raise RuntimeError(
            "GROQ_API_KEY is missing."
        )

    companies = extract_clutch_companies(
        CLUTCH_URL
    )

    if MAX_COMPANIES > 0:

        companies = companies[
            :MAX_COMPANIES
        ]

    logger.info(
        "Processing %d companies",
        len(companies)
    )

    results = []

    for index, company in enumerate(
        companies,
        start=1
    ):

        logger.info(
            "[%d/%d] %s",
            index,
            len(companies),
            company.name
        )

        result = process_company(
            company
        )

        results.append(
            result
        )

        # Save after every company
        # so progress isn't lost.
        save_results(
            results
        )

    logger.info(
        "Finished."
    )


if __name__ == "__main__":

    main()