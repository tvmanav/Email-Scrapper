import ipaddress
import os
import re
import csv
import json
import socket
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
import logging
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin, urlparse, urldefrag, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from ddgs import DDGS
from ddgs.exceptions import RatelimitException, TimeoutException as DDGSTimeoutException
from openai import OpenAI, RateLimitError
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

# ============================================================
# CONFIG
# ============================================================

load_dotenv()

CLUTCH_URL = os.getenv("CLUTCH_URL", "https://clutch.co/developers")

# SOURCE=clutch_all walks every listing page of CLUTCH_URL (?page=0..N-1),
# checkpointing the company list and appending results chunk by chunk, so a
# multi-day run over ~56k companies can be stopped and resumed at any point.
CLUTCH_ALL_PAGES = int(os.getenv("CLUTCH_ALL_PAGES", "1122"))
COMPANY_LIST_FILE = "clutch_companies.jsonl"
PAGES_DONE_FILE = "clutch_pages_done.txt"
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "200"))

# SOURCE=clutch_countries walks one country at a time (clutch.co/<cc>/<category>),
# every page until Clutch returns an empty one, then crawls that country's
# companies before moving to the next. CLUTCH_COUNTRIES="se,no,dk" fixes the
# list and order; leave it empty to discover the countries from Clutch itself.
CLUTCH_COUNTRIES = [c.strip().lower() for c in os.getenv("CLUTCH_COUNTRIES", "").split(",") if c.strip()]
CLUTCH_COUNTRY_CATEGORY = os.getenv("CLUTCH_COUNTRY_CATEGORY", "developers")
CLUTCH_COUNTRY_MAX_PAGES = int(os.getenv("CLUTCH_COUNTRY_MAX_PAGES", "100"))
COUNTRIES_DONE_FILE = "clutch_countries_done.txt"

# SOURCE=rerun_generic re-analyses rows that only have generic mailboxes with
# the LLM, as far as the daily token quota reaches, and resumes next day.
# RERUN_MAX caps one run (0 = until the quota runs out).
RERUN_DONE_FILE = "rerun_done.txt"
RERUN_MAX = int(os.getenv("RERUN_MAX", "0"))

# Groq exposes an OpenAI-compatible endpoint, so the official `openai`
# SDK is reused as the client, just pointed at Groq instead of OpenAI.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")

# USE_LLM=0 skips every model call: emails, LinkedIn links and people come
# from the validate_result heuristics only. No Groq key or quota is needed.
USE_LLM = os.getenv("USE_LLM", "1").lower() not in ("0", "false", "no", "off")

# Where the company list comes from: "clutch", "clutch_all", "clutch_countries",
# "clutch_verify" or "rerun_generic" - every Clutch-based mode. Defaults to
# the full walk-every-page crawl, so a bare `python3 company_email_finder.py`
# on a freshly cloned machine does the complete thing with no env vars.
SOURCE = os.getenv("SOURCE", "clutch_all").lower()
OUTPUT_FILE = "company_emails.csv"

MAX_COMPANIES = int(os.getenv("MAX_COMPANIES", "0"))  # 0 = process all companies, e.g. 5 while testing
MAX_PAGES_PER_COMPANY = int(os.getenv("MAX_PAGES_PER_COMPANY", "15"))
REQUEST_TIMEOUT = 20
FETCH_ATTEMPTS = 2  # one retry for timeouts / 429 / 5xx
CRAWL_DELAY = 0.25

# Companies are processed in parallel. Different sites never slow each other
# down; the two shared bottlenecks (web search and the Groq model, whose free
# tier allows ~8K tokens/minute) are serialized by the locks below instead.
COMPANY_WORKERS = int(os.getenv("WORKERS", "4"))
LOOKUP_WORKERS = 4  # directory-profile / redirect lookups

# Clutch sits behind a Cloudflare JS challenge, so the listing page
# must be rendered with a real browser engine (Playwright), not requests.
CLUTCH_PAGE_TIMEOUT_MS = 30000
CLUTCH_RENDER_WAIT_MS = 8000
CLUTCH_MAX_SHOW_MORE_CLICKS = 25
CLUTCH_RENDER_MAX_ATTEMPTS = 4
CLUTCH_RETRY_DELAY_SECONDS = 10
# Cloudflare's check is passed once by a human in a visible browser
# (SOURCE=clutch_verify); the cookies live in this profile, which every later
# headless Clutch render reuses.
CLUTCH_PROFILE_DIR = os.getenv("CLUTCH_PROFILE_DIR", ".clutch_profile")
CLUTCH_VERIFY_WAIT_SECONDS = int(os.getenv("CLUTCH_VERIFY_WAIT_SECONDS", "300"))
# CLUTCH_HEADED=1 renders Clutch in a visible window like the one a human
# verified in; Cloudflare keeps challenging the headless variant.
CLUTCH_HEADED = os.getenv("CLUTCH_HEADED", "0").lower() in ("1", "true", "yes", "on")

# A page with no extracted email can't supply a validated email on its
# own, so the LLM context prioritizes pages that have one and caps
# total size - Groq's free tier allows only 8K tokens/minute.
# On a paid tier raise these (e.g. 16000 / 2500 / 800) for fuller page context.
LLM_CONTEXT_CHAR_BUDGET = int(os.getenv("LLM_CONTEXT_CHAR_BUDGET", "8000"))
LLM_EMAIL_PAGE_CHAR_LIMIT = int(os.getenv("LLM_EMAIL_PAGE_CHAR_LIMIT", "1500"))
LLM_NO_EMAIL_PAGE_CHAR_LIMIT = int(os.getenv("LLM_NO_EMAIL_PAGE_CHAR_LIMIT", "600"))
LLM_LEADERSHIP_PAGE_CHAR_LIMIT = 2000
LLM_NO_SIGNAL_CHAR_LIMIT = 600  # page with no email and no job title in it
LLM_SNIPPET_WINDOW = 250  # chars kept on each side of an email / job title

# When the plain-HTTP crawl finds no email at all, try the usual contact URLs
# and then a real (non-stealth) browser, for sites that build pages in JS.
# Both fall back to nothing if a site answers with an error or a block page.
COMMON_CONTACT_PATHS = (
    "contact", "contact-us", "kontakt", "kontakta-oss", "about", "about-us",
    "om-oss", "over-ons", "team", "impressum", "privacy",
)
MAX_CONTACT_PROBES = 6
ENABLE_BROWSER_FALLBACK = True
BROWSER_FALLBACK_MAX_PAGES = 3
BROWSER_PAGE_TIMEOUT_MS = 20000
BROWSER_SETTLE_MS = 2500
BROWSER_WORKERS = 2

# LinkedIn lookup is free: links on the company's own site come first, and
# only gaps are filled through a keyless DuckDuckGo search (never LinkedIn
# itself, which blocks scraping and forbids it in its terms).
ENABLE_LINKEDIN_SEARCH = True
MAX_DECISION_MAKERS = 5
LINKEDIN_MAX_SEARCHES_PER_COMPANY = 6
LINKEDIN_MAX_ROLE_SEARCHES = 3
# The role-search fallback is the least reliable path, so it only keeps the
# top two tiers (Founder/CEO/MD/President, CTO/COO/Partner...) - see role_rank.
LINKEDIN_FALLBACK_MAX_ROLE_RANK = 1
# Free search fails at random (timeouts, rate limits), so companies whose
# LinkedIn data came back incomplete get one more attempt after the main run.
ENABLE_SECOND_PASS = True
SECOND_PASS_COOLDOWN_SECONDS = 30
COMPANY_NAME_SUFFIXES = ("inc", "ltd", "llc", "gmbh", "group", "corp", "co")
LINKEDIN_SEARCH_RESULTS = 8
LINKEDIN_SEARCH_DELAY = 2.0  # backoff after a real search error
LINKEDIN_SEARCH_MIN_INTERVAL = 1.0  # gap between any two searches, across all workers
LINKEDIN_SEARCH_MAX_ATTEMPTS = 2
# ddgs defaults to a 5s timeout, well under REQUEST_TIMEOUT and too short
# whenever its backend is slow - every attempt failed as a timeout rather
# than getting a chance to succeed.
LINKEDIN_SEARCH_TIMEOUT = int(os.getenv("LINKEDIN_SEARCH_TIMEOUT", str(REQUEST_TIMEOUT)))

RATE_LIMIT_MAX_RETRIES = 2
RATE_LIMIT_MAX_WAIT_SECONDS = 90

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ddgs logs every engine request at INFO, which would drown the run log.
for noisy_logger in ("ddgs", "primp", "httpx", "httpcore"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

client = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url=GROQ_BASE_URL)

session = requests.Session()
session.headers.update(HEADERS)

robots_cache = {}


# ============================================================
# SCHEMA / DATA STRUCTURES
# ============================================================

class DecisionMaker(BaseModel):
    name: str
    title: str
    email: str  # empty string when the site shows no email for this person


class LLMDecision(BaseModel):
    decision_makers: List[DecisionMaker] = Field(default_factory=list)
    contact_emails: List[str] = Field(default_factory=list)


@dataclass
class Company:
    name: str
    url: str
    linkedin: str = ""  # company LinkedIn page, when the source directory lists one
    directory_email: str = ""  # email shown by the source directory (same domain as the site)


@dataclass
class LinkedInLink:
    kind: str  # "company" or "in"
    url: str
    context: str  # anchor text / labels / slug, used to match a link to a person


@dataclass
class PageData:
    url: str
    text: str
    emails: List[str]
    linkedin_links: List[LinkedInLink]


@dataclass
class Person:
    name: str
    title: str
    email: str = ""
    linkedin_url: str = ""
    # True when the person came only from a LinkedIn search hit, not from the
    # company's own website - nothing independent confirms the role.
    unverified: bool = False


# ============================================================
# EMAIL EXTRACTION
# ============================================================

EMAIL_REGEX = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")


def normalize_email(email: str) -> str:
    return email.strip().lower().rstrip(".,;:)]}>")


def decode_cloudflare_email(hex_string: str) -> Optional[str]:
    """
    Cloudflare's "email protection" plugin replaces a visible email with a
    placeholder and stashes the real address, XOR-"encrypted", in a
    data-cfemail attribute. It renders fine in a browser (a JS snippet
    decodes it) but is invisible to a plain HTTP scraper unless decoded here.
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


# "logo@2x.png" matches the email regex; these are retina-image filenames.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif")

# Placeholder and error-tracking addresses that are never a real contact.
IGNORED_EMAIL_DOMAINS = (
    "example.com", "example.org", "example.net", "sentry.io", "wixpress.com",
    "company.com", "yourcompany.com", "yourdomain.com", "yoursite.com", "mydomain.com",
    "domain.com", "email.com", "sample.com", "test.com",
)

# Form-field hints ("you@", "name@") that appear in front of any domain.
PLACEHOLDER_EMAIL_LOCALS = {
    "you", "your", "yourname", "your.name", "name", "email", "user", "username",
    "firstname.lastname", "first.last", "john.doe", "jane.doe", "johndoe", "janedoe",
}


def extract_emails(text: str) -> List[str]:
    if not text:
        return []

    result = set()

    for email in EMAIL_REGEX.findall(text):
        email = normalize_email(email)
        domain = email.split("@")[-1]

        if email.endswith(IMAGE_EXTENSIONS) or email.split("@")[0] in PLACEHOLDER_EMAIL_LOCALS:
            continue
        if any(domain == ignored or domain.endswith("." + ignored) for ignored in IGNORED_EMAIL_DOMAINS):
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

    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "https://" + url

    url, _ = urldefrag(url)

    return url.rstrip("/")


def get_domain(url: str) -> str:
    # hostname drops credentials and the port; only a *leading* "www." is removed.
    host = urlparse(url).hostname or ""

    return host.removeprefix("www.")


def same_domain(url1: str, url2: str) -> bool:
    return get_domain(url1) == get_domain(url2)


def domain_matches(email_domain: str, site_domain: str) -> bool:
    """True for the site's own domain or a subdomain of it, never a look-alike suffix."""
    email_domain, site_domain = email_domain.lower(), site_domain.lower()

    return bool(site_domain) and (
        email_domain == site_domain or email_domain.endswith("." + site_domain)
    )


SECOND_LEVEL_SUFFIXES = {"co", "com", "org", "net", "gov", "ac", "edu"}


def brand_label(domain: str) -> str:
    """The company-name part of a domain: acme.co.uk, shop.acme.de and acme.com all give "acme"."""
    parts = domain.lower().strip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in SECOND_LEVEL_SUFFIXES:
        label = parts[-3]
    else:
        label = parts[-2] if len(parts) >= 2 else parts[0]

    return label.replace("-", "")


def brand_matches(email_domain: str, site_domain: str) -> bool:
    """
    Same company across regional/parent domains (euvic.de for euvic.com,
    kingsmendv.com for go.kingsmendv.com), but not a look-alike or another
    company's address (fulrum.rocks for fulcrum.rocks, an agency's mailbox).
    """
    a, b = brand_label(email_domain), brand_label(site_domain)

    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 5 and (a.startswith(b) or b.startswith(a))))


def is_safe_url(url: str) -> bool:
    """
    False when the URL's host resolves to a private, loopback, link-local or
    otherwise non-public address. Every URL fetched here comes from a
    scraped site or a redirect chain, so without this check a malicious or
    compromised page could redirect the crawler at internal infrastructure
    (e.g. a cloud metadata endpoint) - classic SSRF.
    """
    host = urlparse(url).hostname
    if not host:
        return False

    # socket.getaddrinfo() has no timeout parameter, unlike every other
    # network call in this module - run it on a side thread and bound how
    # long we wait, so a stuck DNS resolver stalls this check instead of the
    # calling worker forever.
    outcome = {}

    def resolve():
        try:
            outcome["addresses"] = {info[4][0] for info in socket.getaddrinfo(host, None)}
        except socket.gaierror:
            outcome["addresses"] = None

    resolver = threading.Thread(target=resolve, daemon=True)
    resolver.start()
    resolver.join(REQUEST_TIMEOUT)

    if resolver.is_alive() or outcome.get("addresses") is None:
        # Nothing to attack if the name doesn't resolve (or resolution is
        # stuck), and a transient DNS hiccup must not silently drop a
        # legitimate company from the run.
        return True

    addresses = outcome["addresses"]

    return all(
        not (ip.is_private or ip.is_loopback or ip.is_link_local
             or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
        for ip in (ipaddress.ip_address(address) for address in addresses)
    )


# ============================================================
# ROBOTS
# ============================================================

def load_robots(origin: str) -> Optional[RobotFileParser]:
    """
    Fetches robots.txt ourselves rather than via RobotFileParser.read():
    that has no timeout (a dead host can hang the run) and treats a 401/403
    answer as "disallow everything", which would silently skip every site
    behind a firewall. Per RFC 9309 a 4xx answer means "no rules".
    """
    try:
        response = session.get(f"{origin}/robots.txt", timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        # Unreachable robots.txt isn't a signal the site doesn't want to be
        # crawled, so allow rather than dropping the whole domain.
        return None

    if response.status_code != 200:
        return None

    parser = RobotFileParser()
    parser.parse(response.text.splitlines())

    return parser


robots_cache_lock = threading.Lock()
robots_locks = {}


def can_crawl(url: str) -> bool:
    if not is_safe_url(url):
        logger.warning("Refusing to crawl a non-public address: %s", url)
        return False

    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"

    if origin not in robots_cache:
        # Double-checked locking per origin: without it, concurrent workers
        # hitting the same new origin for the first time would each fetch
        # robots.txt themselves instead of sharing one result.
        with robots_cache_lock:
            origin_lock = robots_locks.setdefault(origin, threading.Lock())

        with origin_lock:
            if origin not in robots_cache:
                robots_cache[origin] = load_robots(origin)

    parser = robots_cache[origin]

    return True if parser is None else parser.can_fetch(USER_AGENT, url)


# ============================================================
# HTTP FETCH
# ============================================================

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
FETCH_MAX_REDIRECTS = 5


class UnsafeRedirect(Exception):
    pass


def get_following_redirects(url: str) -> requests.Response:
    """session.get that checks every redirect hop, since requests would follow one into an internal address unchecked."""
    for _ in range(FETCH_MAX_REDIRECTS + 1):
        response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        location = response.headers.get("location")

        if response.status_code not in REDIRECT_STATUS_CODES or not location:
            return response

        url = urljoin(url, location)
        if not is_safe_url(url):
            raise UnsafeRedirect(url)

    raise requests.TooManyRedirects(f"more than {FETCH_MAX_REDIRECTS} redirects")


def fetch(url: str) -> Optional[str]:
    url = normalize_url(url)

    if not url:
        return None

    if not can_crawl(url):
        logger.warning("Blocked by robots.txt: %s", url)
        return None

    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            response = get_following_redirects(url)
        except UnsafeRedirect as e:
            logger.warning("Redirect to a non-public address refused: %s -> %s", url, e)
            return None
        except requests.RequestException as e:
            logger.warning("Request failed (%d/%d): %s | %s", attempt, FETCH_ATTEMPTS, url, e)
            time.sleep(CRAWL_DELAY * attempt)
            continue

        if response.status_code in RETRYABLE_STATUS_CODES and attempt < FETCH_ATTEMPTS:
            time.sleep(CRAWL_DELAY * attempt)
            continue

        if response.status_code != 200:
            logger.warning("HTTP %s: %s", response.status_code, url)
            return None

        # "html" also covers application/xhtml+xml; a missing header is tolerated.
        content_type = response.headers.get("content-type", "").lower()
        if content_type and "html" not in content_type:
            return None

        time.sleep(CRAWL_DELAY)

        return response.text

    return None


# ============================================================
# LINKEDIN URL HELPERS
# ============================================================

LINKEDIN_URL_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(company|in)/([^/?#\s\"'<>]+)", re.IGNORECASE
)


def normalize_linkedin_url(url: str) -> Optional[tuple]:
    """
    Returns (kind, canonical_url) or None. Country subdomains (se.linkedin.com),
    query strings and trailing slashes all collapse to one canonical form.
    """
    match = LINKEDIN_URL_RE.search(url or "")
    if not match:
        return None

    kind, slug = match.group(1).lower(), match.group(2).rstrip("/")

    return kind, f"https://www.linkedin.com/{kind}/{slug}"


def alnum(text: str) -> str:
    # Fold accents so "Dragoș Ștefan" on a site matches "Dragos Stefan" on LinkedIn.
    folded = unicodedata.normalize("NFKD", (text or "").lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))

    return "".join(re.findall(r"[^\W_]+", folded))


def extract_linkedin_links(soup: BeautifulSoup) -> List[LinkedInLink]:
    links = []
    seen = set()

    for a in soup.find_all("a", href=True):
        normalized = normalize_linkedin_url(a["href"])
        if not normalized or normalized[1] in seen:
            continue

        seen.add(normalized[1])
        kind, url = normalized

        # Only the link's own labels - a parent container can hold a whole
        # team grid and would tie every person to every profile.
        parts = [a.get_text(" ", strip=True), a.get("aria-label", ""), a.get("title", "")]
        parts += [img.get("alt", "") for img in a.find_all("img")]
        parent = a.parent
        if parent and len(parent.find_all("a", href=LINKEDIN_URL_RE)) == 1:
            parent_text = parent.get_text(" ", strip=True)
            if len(parent_text) <= 150:
                parts.append(parent_text)

        links.append(LinkedInLink(kind=kind, url=url, context=" ".join(p for p in parts if p)))

    return links


# ============================================================
# HTML PARSING
# ============================================================

def parse_page(url: str, html: str) -> PageData:
    soup = BeautifulSoup(html, "lxml")

    # Sites built with Next.js & co. ship page data (and sometimes the only
    # copy of the contact email) inside <script> blocks. Third-party scripts
    # are full of tracking addresses, so only the company's own domain counts.
    site_domain = get_domain(url)
    script_emails = {
        email
        for tag in soup.find_all("script")
        for email in extract_emails(tag.decode_contents().replace("\\u0040", "@"))
        if domain_matches(email.split("@")[-1], site_domain)
    }

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    # Form placeholder/value attributes commonly contain fake example
    # addresses (e.g. placeholder="name@company.com" on a Contact Form 7
    # field) - strip them so they can't be mistaken for a real contact email.
    for tag in soup.find_all(["input", "textarea"]):
        tag.attrs.pop("placeholder", None)
        tag.attrs.pop("value", None)

    # Cloudflare's email-protection plugin hides real addresses behind a
    # data-cfemail attribute and a "[email protected]" placeholder, decoded
    # client-side by JS a plain scraper never runs. Decode it in place so
    # both the visible text (for name/title context) and the regex-based
    # extraction below see the real address.
    for tag in soup.find_all(attrs={"data-cfemail": True}):
        decoded = decode_cloudflare_email(tag["data-cfemail"])
        if decoded:
            tag.string = decoded

    linkedin_links = extract_linkedin_links(soup)

    text = soup.get_text(separator=" ", strip=True)

    # Scan the cleaned markup (script/style/svg already stripped above)
    # rather than the raw HTML, so analytics/tracking JS can't inject
    # false-positive "emails" into the extracted set.
    emails = set(extract_emails(str(soup))) | script_emails

    for a in soup.find_all("a"):
        href = a.get("href", "")
        if href.lower().startswith("mailto:"):
            email = href[7:].split("?")[0]
            if email:
                emails.add(normalize_email(email))

    return PageData(url=url, text=text, emails=sorted(emails), linkedin_links=linkedin_links)


# ============================================================
# CLUTCH EXTRACTION
# ============================================================

def open_clutch_context(p, headless: bool):
    """Persistent browser profile, so a Cloudflare check passed once keeps counting."""
    context = p.chromium.launch_persistent_context(
        CLUTCH_PROFILE_DIR,
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="en-US",
    )
    if headless:
        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            """
        )

    return context


def wait_for_cloudflare_clearance(
    page, max_wait_ms: int = CLUTCH_RENDER_WAIT_MS, min_wait_ms: int = 2000
) -> None:
    """
    Polls the page once a second for Cloudflare's "Just a moment..." JS
    challenge to clear, instead of always sleeping the full wait budget -
    a real challenge can take longer than the old flat sleep allowed, so
    this keeps waiting up to the max instead of giving up early. min_wait_ms
    still enforces the old sleep's floor even once cleared: the challenge
    text disappearing doesn't mean the listing itself has finished
    hydrating yet, and the already-cleared (cached-session) case is the
    common one, so it must get at least as much settle time as before.
    """
    waited_ms = 0
    for _ in range(max(1, max_wait_ms // 1000)):
        page.wait_for_timeout(1000)
        waited_ms += 1000
        try:
            cleared = "Just a moment" not in page.content()
        except Exception:
            cleared = False  # mid-navigation, which is what clearing looks like
        if cleared and waited_ms >= min_wait_ms:
            return


def render_clutch_listing_html(clutch_url: str) -> str:
    """
    Clutch serves a Cloudflare "Just a moment..." JS challenge to plain HTTP
    clients, so the listing has to be rendered with a real browser engine,
    using the profile that a human verified with SOURCE=clutch_verify.
    """
    with sync_playwright() as p:
        context = open_clutch_context(p, headless=not CLUTCH_HEADED)

        page = context.new_page()
        page.goto(clutch_url, wait_until="domcontentloaded", timeout=CLUTCH_PAGE_TIMEOUT_MS)
        wait_for_cloudflare_clearance(page)

        # Clutch lazy-loads extra providers behind a "See more providers"
        # button. Click it until it disappears/disables so the full list
        # for this page is present in the DOM.
        for _ in range(CLUTCH_MAX_SHOW_MORE_CLICKS):
            button = page.query_selector("#show-more-providers-button")

            if not button or not button.is_visible():
                break
            if button.get_attribute("data-show-enabled") == "false":
                break

            button.click()
            page.wait_for_timeout(1500)

        html = page.content()
        context.close()

        return html


def verify_clutch_access() -> bool:
    """
    Opens a visible browser on Clutch so a person can complete Cloudflare's
    check; returns True once the listing shows. The cookies stay in the
    profile for the headless runs that follow.
    """
    logger.info("A browser window will open: complete Cloudflare's check there (waiting up to %ds).", CLUTCH_VERIFY_WAIT_SECONDS)

    with sync_playwright() as p:
        context = open_clutch_context(p, headless=False)
        page = context.new_page()
        verified = False

        try:
            page.goto(f"https://clutch.co/{CLUTCH_COUNTRY_CATEGORY}", wait_until="domcontentloaded", timeout=CLUTCH_PAGE_TIMEOUT_MS)

            for _ in range(max(1, CLUTCH_VERIFY_WAIT_SECONDS // 2)):
                try:
                    if "provider-row" in page.content():
                        verified = True
                        break
                except Exception:
                    pass  # the page is mid-navigation, which is what passing the check does
                page.wait_for_timeout(2000)
        finally:
            context.close()

    return verified


CLUTCH_REDIRECT_MAX_HOPS = 8
CLUTCH_REDIRECT_ATTEMPTS = 2
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


def is_clutch_host(url: str) -> bool:
    host = urlsplit(url).hostname or ""

    return host == "clutch.co" or host.endswith(".clutch.co")


def resolve_clutch_redirect(redirect_href: str) -> Optional[str]:
    """
    Clutch "Visit Website" links point at r.clutch.co click-tracking
    redirects, not the company's real site. Follow the chain one hop at a
    time and stop at the first address outside clutch.co: that address IS the
    company's website, and requesting it would only make this lookup depend
    on the company's server being fast (a slow or busy site used to time out
    here and the company silently vanished from the list).
    """
    url = redirect_href

    for _ in range(CLUTCH_REDIRECT_MAX_HOPS):
        if not is_safe_url(url):
            logger.warning("Refusing to follow redirect to a non-public address: %s", url[:120])
            return None

        response = None

        for attempt in range(1, CLUTCH_REDIRECT_ATTEMPTS + 1):
            try:
                response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=False)
                break
            except requests.RequestException as e:
                logger.warning(
                    "Clutch redirect hop failed (%d/%d): %s | %s",
                    attempt, CLUTCH_REDIRECT_ATTEMPTS, url[:120], e
                )

        if response is None:
            return None

        location = response.headers.get("location")
        if response.status_code not in REDIRECT_STATUS_CODES or not location:
            return None  # chain ended while still on Clutch: no company address

        url = urljoin(url, location)

        if not is_clutch_host(url):
            if not is_safe_url(url):
                logger.warning("Refusing to resolve company address to a non-public address: %s", url[:120])
                return None

            parts = urlsplit(url)
            clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

            return normalize_url(clean_url)

    logger.warning("Clutch redirect chain too long: %s", redirect_href[:120])

    return None


def extract_clutch_companies(clutch_url: str) -> List[Company]:
    logger.info("Rendering Clutch listing: %s", clutch_url)

    rows = []

    for attempt in range(1, CLUTCH_RENDER_MAX_ATTEMPTS + 1):
        last_error = None  # only the latest attempt's outcome should reach the final error

        try:
            html = render_clutch_listing_html(clutch_url)
        except Exception as e:
            # A navigation timeout or browser hiccup is as temporary as an
            # unresolved Cloudflare challenge: try again before giving up.
            last_error = e
            logger.warning("Attempt %d/%d: rendering failed (%s), retrying.", attempt, CLUTCH_RENDER_MAX_ATTEMPTS, e)
            time.sleep(CLUTCH_RETRY_DELAY_SECONDS)
            continue

        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("div.provider-row")

        if rows:
            break

        if "Just a moment" in html:
            last_error = "Cloudflare challenge page"

        # The Cloudflare challenge occasionally hasn't fully resolved within
        # the fixed wait time - a fresh attempt usually clears it rather
        # than this being a real structure change.
        logger.warning(
            "Attempt %d/%d: no provider rows rendered, retrying.",
            attempt, CLUTCH_RENDER_MAX_ATTEMPTS
        )

    if not rows:
        if last_error is not None:
            raise RuntimeError(f"Could not load the Clutch page after {CLUTCH_RENDER_MAX_ATTEMPTS} attempts: {last_error}")

        raise RuntimeError(
            "No provider rows found on the Clutch page. "
            "Clutch may have changed its page structure."
        )

    listings = []

    for row in rows:
        name_el = row.select_one(".provider__title-link") or row.find(["h2", "h3", "h4"])
        name = name_el.get_text(" ", strip=True) if name_el else None

        link_el = row.select_one("a.website-link__item")
        if not name or not link_el or not link_el.get("href"):
            continue

        listings.append((name, link_el["href"]))

    with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
        websites = list(pool.map(resolve_clutch_redirect, [href for _, href in listings]))

    companies = []
    seen = set()

    for (name, _), website in zip(listings, websites):
        if not website:
            continue

        domain = get_domain(website)
        if not domain or domain == "clutch.co" or domain in seen:
            continue

        seen.add(domain)
        companies.append(Company(name=name, url=website))

    logger.info("Found %d companies", len(companies))

    return companies


# ============================================================
# PAGE DISCOVERY
# ============================================================

KEYWORDS = [
    "about", "team", "leadership", "management", "contact", "company",
    "people", "founder", "executive", "ceo", "cto", "director", "owner",
    "principal", "directie", "directeur", "eigenaar", "oprichter",
    "medewerkers", "organisatie", "contactgegevens",
    "privacy", "impressum", "imprint", "disclaimer", "voorwaarden", "kontakt", "kontakta",
]

# Exact URL path segments that reliably indicate an about/team/contact
# page. Checked as whole segments (not substrings), so a service page like
# "/cloud-management-outsourcing/" doesn't collide with this - only
# "/about-us/" or "/contact/" style paths do.
HIGH_VALUE_PATH_SEGMENTS = {
    "about", "about-us", "aboutus", "who-we-are", "company", "team",
    "our-team", "leadership", "management-team", "people", "founders",
    "executive-team", "contact", "contact-us", "contactus", "get-in-touch",
    "over-ons", "overons", "wie-zijn-wij", "ons-team", "het-team", "directie",
    "organisatie", "medewerkers", "contactgegevens", "neem-contact-op",
    # EU sites must publish a contact email, often only on legal pages.
    "privacy", "privacy-policy", "privacyverklaring", "impressum", "imprint", "disclaimer",
    "algemene-voorwaarden", "kontakt", "kontakta-oss",
}


def score_link(text: str, href: str) -> int:
    combined = f"{text} {href}".lower()
    weak_score = sum(1 for keyword in KEYWORDS if keyword in combined)

    path_segments = set(urlparse(href).path.lower().strip("/").split("/"))
    strong_score = len(path_segments & HIGH_VALUE_PATH_SEGMENTS)

    # An exact path-segment match (e.g. "/contact/") is a much stronger
    # signal than an incidental keyword substring hit, so it must always
    # sort above any number of weak matches.
    return strong_score * 1000 + weak_score


def discover_pages(homepage: str, html: str, max_pages: int) -> List[str]:
    if max_pages <= 0:  # MAX_PAGES_PER_COMPANY = 1 means "homepage only"
        return []

    soup = BeautifulSoup(html, "lxml")
    candidates = []

    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue

        absolute, _ = urldefrag(urljoin(homepage, href))
        if not same_domain(homepage, absolute):
            continue

        score = score_link(a.get_text(" ", strip=True), absolute)
        if score > 0:
            candidates.append((score, absolute))

    candidates.sort(key=lambda x: x[0], reverse=True)

    result = []
    seen = set()

    for _, url in candidates:
        # Dedupe on the normalized form so "/contact" and "/contact/" don't
        # both consume a slot in the crawl budget.
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

def probe_common_pages(start_url: str, pages: List[PageData]) -> List[PageData]:
    """Tries the usual contact/about URLs for sites whose menu isn't in the HTML."""
    parts = urlparse(start_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    seen = {normalize_url(page.url) for page in pages}
    found = []
    probes = 0

    for path in COMMON_CONTACT_PATHS:
        url = normalize_url(f"{origin}/{path}")
        if url in seen:
            continue
        if probes >= MAX_CONTACT_PROBES:
            break
        probes += 1

        html = fetch(url)
        if html:
            found.append(parse_page(url, html))
            if found[-1].emails:
                break

    return found


browser_slots = threading.Semaphore(BROWSER_WORKERS)


def render_company_pages(start_url: str) -> List[PageData]:
    """
    Opens the site in an ordinary headless browser so client-side rendered
    pages (React/Next.js shells) show their real content. This is not a
    workaround for blocking: robots.txt is honoured and any page answering
    with an error (403, 418, challenge pages) is simply skipped.
    """
    if not ENABLE_BROWSER_FALLBACK or not can_crawl(start_url):
        return []

    rendered = []

    with browser_slots:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_context(user_agent=USER_AGENT, locale="en-US").new_page()
                queue = [start_url]
                seen = set()

                while queue and len(rendered) < BROWSER_FALLBACK_MAX_PAGES:
                    url = queue.pop(0)
                    key = normalize_url(url)
                    if key in seen or not can_crawl(url):
                        continue
                    seen.add(key)

                    try:
                        response = page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_PAGE_TIMEOUT_MS)
                        page.wait_for_timeout(BROWSER_SETTLE_MS)  # let client-side rendering finish
                    except Exception as e:
                        logger.warning("Browser render failed: %s | %s", url, e)
                        continue

                    if response is None or response.status >= 400:
                        continue

                    html = page.content()
                    rendered.append(parse_page(url, html))

                    if len(rendered) == 1:
                        queue += discover_pages(url, html, BROWSER_FALLBACK_MAX_PAGES - 1)

                browser.close()
        except Exception as e:
            logger.warning("Browser fallback failed: %s | %s", start_url, e)

    return rendered


def crawl_company(company: Company) -> List[PageData]:
    logger.info("Crawling: %s | %s", company.name, company.url)

    start_url = company.url
    homepage_html = fetch(start_url)

    if not homepage_html:
        # Clutch's "visit website" link can point at a stale deep path
        # (e.g. a dropped /en locale prefix) even though the site is live
        # at its root domain. Retry there before giving up on the company.
        parts = urlparse(company.url)
        root_url = normalize_url(f"{parts.scheme}://{parts.netloc}")

        if root_url != start_url:
            logger.info("Homepage fetch failed, retrying at domain root: %s", root_url)
            start_url = root_url
            homepage_html = fetch(start_url)

    if not homepage_html:
        return []

    # The root fallback above is the address that actually answered - record
    # it as the company's URL so the output CSV doesn't keep the original
    # dead deep link (which a reader would click and get a 404 from).
    company.url = start_url

    pages = [parse_page(start_url, homepage_html)]
    discovered = discover_pages(start_url, homepage_html, MAX_PAGES_PER_COMPANY - 1)

    seen = {normalize_url(start_url)}

    for url in discovered:
        # "/contact" and "/contact/" (or the homepage with a trailing slash)
        # are the same page and must not spend a slot of the crawl budget.
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)

        html = fetch(url)
        if html:
            pages.append(parse_page(url, html))

    if not any(page.emails for page in pages):
        pages += probe_common_pages(start_url, pages)

    if not any(page.emails for page in pages):
        rendered = render_company_pages(start_url)

        if rendered:
            logger.info("Browser fallback used: %s (%d pages)", company.name, len(rendered))

            # The rendered homepage replaces the plain-HTTP shell if it has more content.
            if len(rendered[0].text) > len(pages[0].text):
                pages[0] = rendered[0]
                rendered = rendered[1:]

            pages += rendered

    return pages


# ============================================================
# LLM ANALYSIS
# ============================================================

LEADERSHIP_PATH_SEGMENTS = HIGH_VALUE_PATH_SEGMENTS - {
    "contact", "contact-us", "contactus", "get-in-touch",
    "contactgegevens", "neem-contact-op",
    "privacy", "privacy-policy", "privacyverklaring", "impressum", "imprint", "disclaimer",
    "algemene-voorwaarden", "kontakt", "kontakta-oss",
}


def is_leadership_page(page: PageData) -> bool:
    return bool(set(urlparse(page.url).path.lower().strip("/").split("/")) & LEADERSHIP_PATH_SEGMENTS)


ROLE_WORDS_RE = re.compile(
    r"founder|\bceo\b|\bcto\b|\bcoo\b|chief|director|directeur|directie|owner|eigenaar|oprichter"
    r"|partner|president|head of|hoofd|managing|bestuurder|gesch[aä]ftsf[uü]hrer|inhaber"
    r"|gr[uü]nder|\bvd\b|grundare|\bvp\b",
    re.IGNORECASE,
)


def relevant_text(text: str, emails: List[str], limit: int) -> str:
    """
    Keeps only the stretches of a page that can matter - around each email and
    each job title (names sit right next to titles) - instead of the first N
    characters, which are mostly menus. Fewer tokens, same information.
    """
    lowered = text.lower()
    spans = [(m_.start(), m_.end()) for m_ in ROLE_WORDS_RE.finditer(text)]

    for email in emails:
        index = lowered.find(email)
        if index >= 0:
            spans.append((index, index + len(email)))

    if not spans:
        return text[:min(limit, LLM_NO_SIGNAL_CHAR_LIMIT)]

    merged = []
    for start, end in sorted(spans):
        start, end = max(0, start - LLM_SNIPPET_WINDOW), min(len(text), end + LLM_SNIPPET_WINDOW)
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return " … ".join(text[start:end] for start, end in merged)[:limit]


def has_llm_signal(pages: List[PageData]) -> bool:
    """False when no page has an email or a job title - the model has nothing to extract."""
    return any(page.emails or ROLE_WORDS_RE.search(page.text) for page in pages)


def prepare_llm_context(company: Company, pages: List[PageData]) -> str:
    # Decision makers no longer need a published email to be useful (they
    # feed the LinkedIn lookup), so team/about pages go first even when
    # they contain no email, then other pages that do, then the homepage.
    ordered = sorted(pages, key=lambda page: (not is_leadership_page(page), not page.emails))

    sections = []
    used_chars = 0

    for page in ordered:
        if not (page.emails or is_leadership_page(page) or page is pages[0]):
            continue

        if is_leadership_page(page):
            limit = LLM_LEADERSHIP_PAGE_CHAR_LIMIT
        else:
            limit = LLM_EMAIL_PAGE_CHAR_LIMIT if page.emails else LLM_NO_EMAIL_PAGE_CHAR_LIMIT
        section = f"""
URL:
{page.url}

EXTRACTED EMAILS:
{json.dumps(page.emails)}

PAGE TEXT:
{relevant_text(page.text, page.emails, limit)}
"""
        if used_chars + len(section) > LLM_CONTEXT_CHAR_BUDGET:
            # Skip just this oversized page - a smaller, lower-priority page
            # later in `ordered` may still fit the remaining budget.
            continue

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


SYSTEM_PROMPT = """
You are an information-extraction agent.

Your task is to identify publicly displayed business
email addresses from a company's own website.

You must NOT invent, infer, generate, guess, or construct
email addresses.

Only return an email if the exact email address appears
in the supplied website data.

Your main task is to identify the company's decision makers
(name and title) and, where shown, their email addresses.

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

The website may be in Dutch. Treat Dutch titles the same way:

- Eigenaar / Mede-eigenaar
- Oprichter
- Directeur / Algemeen Directeur / Directeur-Eigenaar
- Bestuurder / Zaakvoerder
- Technisch / Financieel / Commercieel / Operationeel Directeur
- Hoofd (head of a department)

German and Swedish titles count too: Geschäftsführer, Inhaber,
Gründer, VD (verkställande direktör), Grundare, Ägare.

Prioritize actual people over generic departments.

For every decision maker:

1. Verify that the person's name appears in the website data.
2. Verify that their title/role appears or is clearly associated
   with the person.
3. Set email to the EXACT email address only if it appears in
   the website data and belongs to that person; otherwise set
   email to an empty string "". A decision maker without a
   published email must still be returned.
4. Do not associate a generic company email with a person unless
   the website explicitly connects them.
5. Do not infer email patterns such as first.last@company.com.

Return at most 5 decision makers, the most senior first
(Founder / CEO / Owner / Managing Director before CTO / COO,
before Directors, VPs and Heads of Department).

If at least one returned decision maker has an email:

- return an empty contact_emails list

If NO returned decision maker has an email:

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

RATE_LIMIT_WAIT_RE = re.compile(r"try again in (?:(\d+)m)?([\d.]+)s")


def parse_rate_limit_wait_seconds(message: str) -> Optional[float]:
    match = RATE_LIMIT_WAIT_RE.search(message or "")
    if not match:
        return None

    minutes = int(match.group(1)) if match.group(1) else 0

    return minutes * 60 + float(match.group(2))


llm_lock = threading.Lock()


def analyze_with_llm(company: Company, pages: List[PageData]) -> LLMDecision:
    if not USE_LLM:
        return LLMDecision()

    # One model call at a time: parallel calls would just share the same small
    # tokens-per-minute budget and trigger rate-limit errors for everyone.
    with llm_lock:
        return _analyze_with_llm(company, pages)


def _analyze_with_llm(company: Company, pages: List[PageData]) -> LLMDecision:
    # Daily free quota is the scarcest resource: skip the call when nothing
    # in the crawled pages could be extracted anyway.
    if not has_llm_signal(pages):
        return LLMDecision()

    context = prepare_llm_context(company, pages)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]

    response = None

    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.parse(
                model=LLM_MODEL, messages=messages, response_format=LLMDecision
            )
            break

        except RateLimitError as e:
            wait_seconds = parse_rate_limit_wait_seconds(str(e))

            # Short per-minute bursts are worth waiting out. A long wait
            # (e.g. a daily token quota reset) isn't - blocking for 20+
            # minutes on one company would stall the whole run, so let it
            # fail fast and move on instead.
            can_retry = (
                wait_seconds is not None
                and wait_seconds <= RATE_LIMIT_MAX_WAIT_SECONDS
                and attempt < RATE_LIMIT_MAX_RETRIES
            )
            if not can_retry:
                raise

            logger.warning(
                "Rate limited analyzing %s, waiting %.1fs before retry (%d/%d)",
                company.name, wait_seconds, attempt + 1, RATE_LIMIT_MAX_RETRIES
            )
            time.sleep(wait_seconds)

    result = response.choices[0].message.parsed

    return result or LLMDecision(decision_makers=[], contact_emails=[])


# ============================================================
# HARD VALIDATION
# ============================================================

GENERIC_EMAIL_PREFIXES = (
    "administratie@", "receptie@", "info@", "contact@", "hello@", "office@", "enquiries@", "enquiry@",
    "sales@", "business@", "support@", "admin@",
    # Department/role mailboxes - shared by whoever staffs them, never one
    # specific person's own address even when a model attributes one to a
    # named decision maker (e.g. "hr@acme.com" tagged as the CEO's email).
    "hr@", "careers@", "jobs@", "recruiting@", "recruitment@", "press@", "media@", "marketing@",
    "legal@", "privacy@", "billing@", "accounts@", "finance@", "noreply@", "no-reply@", "team@",
    "help@", "webmaster@", "postmaster@",
)


ROLE_RANK_PATTERNS = [
    (3, re.compile(r"\bvp\b|vice[\s-]+president")),
    # Dutch: a bare "Directeur" is the top role at a small firm; qualified
    # ones ("Technisch directeur") rank with the CTO/COO tier.
    (0, re.compile(
        r"founder|\bceo\b|chief executive|\bowner\b|president|managing director"
        r"|eigenaar|oprichter|bestuurder|zaakvoerder|algemeen directeur|directeur[\s/-]*eigenaar"
        r"|^\s*directeur\s*$"
        r"|gesch[aä]ftsf[uü]hrer|inhaber|gr[uü]nder|\bvd\b|grundare|verkst[aä]llande|[aä]gare"
    )),
    (1, re.compile(
        r"\bcto\b|chief tech|\bcoo\b|chief op|general manager|\bpartner\b"
        r"|(technisch|operationeel|financieel|commercieel) directeur"
    )),
    (2, re.compile(r"director|directeur|directie")),
    (4, re.compile(r"head of|\bhead\b|\bhoofd\b")),
]


def clean_title(title: str) -> str:
    """Drops headline/tagline text glued on after a slash or bullet ("CTO at X / Building teams")."""
    return re.split(r"\s*[/|⁄∕／·•]\s*", title or "")[0].strip()


def role_rank(title: str) -> int:
    """Lower is more senior; 9 means not a recognised decision-maker role."""
    title = (title or "").lower()

    # "Executive Assistant to the CEO" mentions the role but isn't the role.
    if re.search(r"assistant|assistent|secretar|sekret", title):
        return 9

    for rank, pattern in ROLE_RANK_PATTERNS:
        if pattern.search(title):
            return rank

    return 9


def validate_result(result: LLMDecision, pages: List[PageData]) -> LLMDecision:
    website_emails = set()
    for page in pages:
        website_emails.update(page.emails)

    site_text = alnum(" ".join(page.text for page in pages))

    decision_makers = []
    seen = set()
    # A shared company mailbox the model attributed to a specific person is
    # never that person's own email, even though it is a real address found
    # on the site - keep it as a company-level contact instead of losing it.
    rejected_generic_emails = set()

    for person in result.decision_makers:
        name = person.name.strip()
        key = alnum(name)

        # The name must genuinely appear on the site - an email is no longer
        # there to anchor a hallucinated person, so this is the check.
        if not key or key in seen or key not in site_text:
            continue
        seen.add(key)

        email = normalize_email(person.email)
        if email not in website_emails:
            email = ""
        elif email.startswith(GENERIC_EMAIL_PREFIXES):
            rejected_generic_emails.add(email)
            email = ""

        decision_makers.append(DecisionMaker(name=name, title=clean_title(person.title), email=email))

    decision_makers.sort(key=lambda person: role_rank(person.title))
    decision_makers = decision_makers[:MAX_DECISION_MAKERS]

    # If any decision maker has an email, ignore fallback contact emails -
    # but a generic mailbox rejected above still deserves to surface as the
    # company's own contact address.
    if any(person.email for person in decision_makers):
        return LLMDecision(decision_makers=decision_makers, contact_emails=sorted(rejected_generic_emails))

    # Otherwise fall back to contact emails. Trust the LLM's picks as long
    # as the exact email genuinely appears on the website - don't
    # second-guess its classification against a hardcoded prefix list,
    # since a legitimate general contact address doesn't have to be named
    # info@/contact@/etc.
    contact_emails = {
        normalize_email(email) for email in result.contact_emails
        if normalize_email(email) in website_emails
    } | rejected_generic_emails

    # If the LLM found nothing, fall back to a generic-prefix heuristic
    # over the emails actually extracted from the site.
    if not contact_emails:
        contact_emails = {
            email for email in website_emails
            if email.startswith(GENERIC_EMAIL_PREFIXES)
        }

    # An address on someone else's domain (a partner, an agency, a typo'd
    # look-alike) is not this company's contact, wherever it was found.
    if pages:
        site_domain = get_domain(pages[0].url)
        contact_emails = {email for email in contact_emails if brand_matches(email.split("@")[-1], site_domain)}

    # Still nothing: any address on the company's own domain beats an empty
    # cell (e.g. post@ or jan@), while third-party addresses stay excluded.
    if not contact_emails and pages:
        domain = get_domain(pages[0].url)
        contact_emails = {email for email in website_emails if domain_matches(email.split("@")[-1], domain)}

    return LLMDecision(decision_makers=decision_makers, contact_emails=sorted(contact_emails))


# ============================================================
# LINKEDIN LOOKUP
# ============================================================

class SearchBudget:
    def __init__(self, limit: int):
        self.remaining = limit

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


search_lock = threading.Lock()
last_search_at = 0.0


def throttle_search():
    """Space searches LINKEDIN_SEARCH_MIN_INTERVAL apart even with many workers."""
    global last_search_at

    with search_lock:
        wait = last_search_at + LINKEDIN_SEARCH_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        last_search_at = time.monotonic()


def search_web(query: str, budget: SearchBudget) -> List[dict]:
    if not ENABLE_LINKEDIN_SEARCH or not budget.take():
        return []

    for attempt in range(1, LINKEDIN_SEARCH_MAX_ATTEMPTS + 1):
        throttle_search()

        try:
            return DDGS(timeout=LINKEDIN_SEARCH_TIMEOUT).text(query, max_results=LINKEDIN_SEARCH_RESULTS) or []
        except (RatelimitException, DDGSTimeoutException) as e:
            # ddgs's own typed exceptions for these - always worth retrying,
            # regardless of whatever wording happens to be in the message.
            logger.warning("Search failed (%d/%d): %s | %s", attempt, LINKEDIN_SEARCH_MAX_ATTEMPTS, query, e)
            time.sleep(LINKEDIN_SEARCH_DELAY * attempt)
        except Exception as e:
            # Every other ddgs error (including "no results") comes back as
            # the same untyped exception, so the message is the only signal
            # left to tell "nothing found, asking again won't change that"
            # apart from a real failure worth retrying.
            if "no results" in str(e).lower():
                logger.info("No search results: %s", query)
                return []

            logger.warning("Search failed (%d/%d): %s | %s", attempt, LINKEDIN_SEARCH_MAX_ATTEMPTS, query, e)
            time.sleep(LINKEDIN_SEARCH_DELAY * attempt)

    return []


def result_head(result: dict) -> str:
    """
    DuckDuckGo glues neighbouring result titles onto each title
    ("Jane Doe - CEO at Acme | LinkedInJohn Roe - ..."), so only the text
    before the first "LinkedIn" belongs to this result.
    """
    title = re.split(r"linkedin", result.get("title", ""), flags=re.IGNORECASE)[0]

    # Long titles are truncated with an ellipsis and the next result's title
    # follows it, so everything after the first ellipsis belongs to someone else.
    title = re.split(r"\.\.\.|…", title)[0]

    return title.strip(" |-–—")


FORMER_EMPLOYEE_RE = re.compile(r"\b(ex|former|formerly|past)\b", re.IGNORECASE)


def company_keys(company: Company) -> set:
    """Normalised strings that, if found, tie a search hit to this company."""
    keys = {
        alnum(company.name),
        alnum(get_domain(company.url).split(".")[0]),
        alnum(company.name.split()[0]) if company.name.split() else "",
    }

    return {key for key in keys if len(key) >= 4}


def slug_matches_company(slug: str, keys: set) -> bool:
    slug = alnum(slug)

    return any(key in slug or (len(slug) >= 4 and slug in key) for key in keys)


def find_company_linkedin(company: Company, pages: List[PageData], budget: SearchBudget) -> str:
    if company.linkedin:
        return company.linkedin

    keys = company_keys(company)
    site_links = [link.url for page in pages for link in page.linkedin_links if link.kind == "company"]

    # A site can link other companies (partners, clients, case studies), so
    # prefer the link that looks like this company's own page.
    for url in site_links:
        if slug_matches_company(url.rsplit("/", 1)[-1], keys):
            return url

    # Numeric or abbreviated slugs (company/1349302) can't be matched by name;
    # then only the homepage's link (header/footer) is trusted.
    if pages:
        for link in pages[0].linkedin_links:
            if link.kind == "company":
                return link.url

    for result in search_web(f'site:linkedin.com/company "{company.name}"', budget):
        normalized = normalize_linkedin_url(result.get("href", ""))
        if normalized and normalized[0] == "company" and slug_matches_company(normalized[1].rsplit("/", 1)[-1], keys):
            return normalized[1]

    return ""


def name_tokens(name: str) -> List[str]:
    return [alnum(token) for token in name.split() if len(alnum(token)) >= 2]


def find_site_linkedin(person: Person, pages: List[PageData]) -> str:
    tokens = name_tokens(person.name)
    if len(tokens) < 2:
        return ""

    for page in pages:
        for link in page.linkedin_links:
            if link.kind != "in":
                continue

            haystack = alnum(link.url.rsplit("/", 1)[-1] + link.context)
            if all(token in haystack for token in tokens):
                return link.url

    return ""


def find_search_linkedin(person: Person, company: Company, budget: SearchBudget) -> str:
    tokens = name_tokens(person.name)
    if len(tokens) < 2:
        return ""

    keys = company_keys(company)
    query = f'site:linkedin.com/in "{person.name}" "{company.name}"'

    for result in search_web(query, budget):
        normalized = normalize_linkedin_url(result.get("href", ""))
        if not normalized or normalized[0] != "in":
            continue

        head = alnum(result_head(result))
        slug = alnum(normalized[1].rsplit("/", 1)[-1])
        snippet = alnum(result.get("body", "")[:300])

        name_ok = all(token in head or token in slug for token in tokens)
        company_ok = any(key in head or key in snippet for key in keys)

        if name_ok and company_ok:
            return normalized[1]

    return ""


def find_people_by_role(company: Company, budget: SearchBudget) -> List[Person]:
    """
    Fallback for sites that name nobody: search "<company> CEO/Founder" etc.
    and keep only hits whose own title line shows a decision-maker role at
    this company.
    """
    keys = company_keys(company)
    people = {}

    for roles in ("CEO Founder", "Eigenaar Directeur", "CTO COO")[:LINKEDIN_MAX_ROLE_SEARCHES]:
        for result in search_web(f'site:linkedin.com/in "{company.name}" {roles}', budget):
            normalized = normalize_linkedin_url(result.get("href", ""))
            head = result_head(result)
            if not normalized or normalized[0] != "in" or not head:
                continue

            parts = re.split(r"\s+[-–—|]\s+", head, maxsplit=1)
            name = parts[0].strip()
            title = parts[1].strip() if len(parts) > 1 else ""

            if not (2 <= len(name.split()) <= 4) or any(ch.isdigit() for ch in name):
                continue
            title = clean_title(title)
            if role_rank(title) > LINKEDIN_FALLBACK_MAX_ROLE_RANK or "associate" in title.lower():
                continue
            if FORMER_EMPLOYEE_RE.search(head):
                continue

            # Require the "<role> at <company>" shape so a company that is
            # merely listed among past employers doesn't count as current.
            current_role = re.split(r"\bat\b|\bbij\b|@", title, maxsplit=1, flags=re.IGNORECASE)
            if len(current_role) < 2:
                continue

            # The employer must be this company exactly ("Scalo", "Scalo Inc"),
            # not a namesake such as "Scalo Holdings".
            employer = alnum(re.split(r"[|,·•(\-–—]", current_role[1])[0])
            if not any(
                employer == key or employer in {key + suffix for suffix in COMPANY_NAME_SUFFIXES}
                for key in keys
            ):
                continue

            people.setdefault(normalized[1], Person(name=name, title=title, linkedin_url=normalized[1], unverified=True))

    return sorted(people.values(), key=lambda person: role_rank(person.title))


def enrich_linkedin(company: Company, pages: List[PageData], validated: LLMDecision) -> tuple:
    budget = SearchBudget(LINKEDIN_MAX_SEARCHES_PER_COMPANY)

    company_linkedin = find_company_linkedin(company, pages, budget)

    people = [
        Person(name=dm.name, title=dm.title, email=dm.email)
        for dm in validated.decision_makers
    ]

    for person in people:
        person.linkedin_url = (
            find_site_linkedin(person, pages) or find_search_linkedin(person, company, budget)
        )

    if not people:
        people = find_people_by_role(company, budget)[:MAX_DECISION_MAKERS]

    return company_linkedin, people


# ============================================================
# MATCH WEBSITE EMAILS TO PEOPLE
# ============================================================

NAME_NOISE = {"dr", "mr", "mrs", "ms", "prof", "ir", "drs", "mba", "phd", "jr", "sr"}


def name_parts(name: str) -> tuple:
    """(first, last) in folded lowercase letters; last is empty for a one-word name."""
    words = [alnum(word) for word in re.split(r"[\s,]+", name or "")]
    words = [word for word in words if word and word not in NAME_NOISE]

    if not words:
        return "", ""

    return words[0], (words[-1] if len(words) > 1 else "")


def email_local_candidates(first: str, last: str) -> set:
    """Local parts a person's work email commonly takes (compared with separators removed)."""
    if not last:
        return set()

    return {
        first + last, last + first, first[0] + last, last + first[0],
        first + last[0], last[0] + first,
    }


def match_site_email(person: Person, emails, site_domain: str, other_first_names: list) -> str:
    """
    A company-domain address found on the site whose local part is clearly this
    person's name (john.smith@, jsmith@, john@ when no one else shares the first name).
    Generic mailboxes (info@, sales@...) never match.
    """
    first, last = name_parts(person.name)
    if not first:
        return ""

    full_forms = email_local_candidates(first, last)
    first_is_unique = other_first_names.count(first) <= 1

    for email in sorted(emails):
        if email.startswith(GENERIC_EMAIL_PREFIXES):
            continue

        local, _, domain = email.partition("@")
        if not domain_matches(domain, site_domain):
            continue

        local = alnum(local)
        if local in full_forms or (local == first and len(first) >= 3 and first_is_unique):
            return email

    return ""


def attach_site_emails(people: List[Person], pages: List[PageData], company: Company) -> list:
    """Fills each person's missing email from the site; returns the emails it added."""
    website_emails = {email for page in pages for email in page.emails}
    site_domain = get_domain(company.url)
    first_names = [name_parts(person.name)[0] for person in people]
    added = []

    for person in people:
        if person.email:
            continue

        person.email = match_site_email(person, website_emails, site_domain, first_names)
        if person.email:
            added.append(person.email)

    return added


# ============================================================
# PROCESS COMPANY / CSV OUTPUT
# ============================================================

OUTPUT_COLUMNS = [
    "company_name", "company_url", "company_email", "company_linkedin",
    "first_name", "last_name", "contact_title", "decision_maker_email", "contact_linkedin",
]


# Excel/Sheets reads a cell as a formula when its first character is one of
# these, so a scraped company/person name starting with one (e.g. a company
# literally named "=1+1") must not reach the CSV verbatim - prefix it with a
# quote, which every spreadsheet app treats as plain text.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def sanitize_csv_field(value: str) -> str:
    return "'" + value if value[:1] in CSV_FORMULA_PREFIXES else value


def split_name(name: str) -> tuple:
    """(first_name, last_name), splitting at the first space. last_name is
    everything after it, not just the final word, so a middle name is never
    silently dropped - first_name + " " + last_name reconstructs the
    original exactly. Empty last_name for a single-word name.

    Whitespace is normalized first: a name scraped off a page can be joined
    by a non-breaking space (\xa0) or other unicode whitespace instead of a
    plain " ", which str.partition(" ") would not split on, leaving the
    whole name stuck in first_name."""
    name = " ".join((name or "").split())
    if not name:
        return "", ""
    first, _, rest = name.partition(" ")
    return first, rest.strip()


def join_name(first_name: str, last_name: str) -> str:
    """Inverse of split_name()."""
    return " ".join(part for part in (first_name, last_name) if part)


def join_company_emails(emails) -> str:
    """Joins a company's generic mailbox addresses into one field, so they
    can be repeated across every contact row like company_linkedin already
    is, instead of each spawning its own near-empty row."""
    return "; ".join(sorted(set(emails)))


def split_company_email(value: str) -> List[str]:
    """Inverse of join_company_emails()."""
    return [email.strip() for email in (value or "").split(";") if email.strip()]


def empty_result(company: Company) -> dict:
    row = {column: "" for column in OUTPUT_COLUMNS} | {
        "company_name": company.name,
        "company_url": company.url,
    }
    return {column: sanitize_csv_field(value) for column, value in row.items()}


def build_rows(company: Company, people: List[Person], contact_emails: list, company_linkedin: str) -> List[dict]:
    """
    One row per named decision maker. The company's generic mailbox
    address(es), if any, are joined into company_email and repeated on
    every row - the same way company_linkedin already is - rather than each
    one spawning its own separate, otherwise-blank row. A company with no
    contact at all still gets a single placeholder row so it isn't dropped
    from the sheet.
    """
    base = {
        "company_name": company.name,
        "company_url": company.url,
        "company_linkedin": company_linkedin,
        "company_email": join_company_emails(contact_emails),
    }

    rows = []
    for person in people:
        first_name, last_name = split_name(person.name)
        rows.append(base | {
            "first_name": first_name,
            "last_name": last_name,
            "contact_title": f"{person.title} [unverified]" if person.unverified else person.title,
            "contact_linkedin": person.linkedin_url,
            "decision_maker_email": person.email,
        })
    if not rows:
        rows = [base | {"first_name": "", "last_name": "", "contact_title": "", "contact_linkedin": "",
                         "decision_maker_email": ""}]

    return [{column: sanitize_csv_field(row.get(column, "")) for column in OUTPUT_COLUMNS} for row in rows]


def linkedin_incomplete(company_linkedin: str, people: List[Person]) -> bool:
    return not company_linkedin or not people or any(not person.linkedin_url for person in people)


def process_company(company: Company, retry_queue: list) -> List[dict]:
    """Returns this company's contact rows: one per decision maker/generic
    address found, or a single placeholder row when nothing was."""
    try:
        pages = crawl_company(company)

        if not pages:
            logger.warning("No pages found: %s", company.name)
            # Timeouts are often temporary (slow host, busy network): queue a
            # full second attempt. pages=None marks "crawl again from scratch".
            retry_queue.append((company, None, None, "", []))
            return [empty_result(company)]

        try:
            llm_result = analyze_with_llm(company, pages)
        except Exception:
            # Quota or API trouble must not throw away emails the crawl already
            # found: validate_result falls back to its heuristics on an empty result.
            logger.exception("LLM analysis failed, using heuristics only: %s", company.name)
            llm_result = LLMDecision()

        validated = validate_result(llm_result, pages)
        decision_maker_emails = sorted({dm.email for dm in validated.decision_makers if dm.email})
        contact_emails = list(validated.contact_emails)

        # Last resort: the source directory's own email for this company.
        if not decision_maker_emails and not contact_emails and company.directory_email:
            contact_emails = [company.directory_email]

        # LinkedIn is only the fallback: skip the (quota-limited) per-person
        # search when the website already gave us a decision maker's email -
        # but still keep their name/title, already validated against the
        # site, rather than leaving the row with an email and no attached
        # person. The company's own LinkedIn page is still worth finding:
        # find_company_linkedin() checks the source directory and the
        # already-crawled pages for free before ever spending search budget,
        # so skipping it entirely here would throw away free data.
        company_linkedin, people = "", []
        if decision_maker_emails:
            people = [Person(name=dm.name, title=dm.title, email=dm.email) for dm in validated.decision_makers]
            try:
                company_linkedin = find_company_linkedin(company, pages, SearchBudget(LINKEDIN_MAX_SEARCHES_PER_COMPANY))
            except Exception:
                logger.exception("Company LinkedIn lookup failed: %s", company.name)
        else:
            try:
                company_linkedin, people = enrich_linkedin(company, pages, validated)
            except Exception:
                # LinkedIn is a bonus: its failure must never cost us the emails.
                logger.exception("LinkedIn lookup failed: %s", company.name)

            # LinkedIn-found people (and every run without the LLM) never got an
            # email from the model: pair them with addresses already on the site.
            decision_maker_emails = sorted(set(decision_maker_emails) | set(attach_site_emails(people, pages, company)))
            contact_emails = [e for e in contact_emails if e not in decision_maker_emails]

            if linkedin_incomplete(company_linkedin, people):
                # Keep the crawl and LLM result so the retry only repeats the searches.
                retry_queue.append((company, pages, validated, company_linkedin, people))

        return build_rows(company, people, contact_emails, company_linkedin)

    except Exception:
        logger.exception("Company failed: %s", company.name)
        return [empty_result(company)]


def merge_people(old: List[Person], new: List[Person]) -> List[Person]:
    """Fill missing profile links from the retry; never replace what was already found."""
    if not old:
        return new

    new_urls = {alnum(person.name): person.linkedin_url for person in new if person.linkedin_url}

    for person in old:
        if not person.linkedin_url:
            person.linkedin_url = new_urls.get(alnum(person.name), "")

    return old


results_lock = threading.Lock()  # guards result rows and the CSV file


def flatten(groups) -> list:
    """Company row-groups (some possibly None/empty) into one flat list of rows."""
    return [row for group in groups if group for row in group]


def recrawl_company(company: Company, rows_by_url: dict, results: list):
    """Second attempt for a company whose first crawl found no pages at all."""
    try:
        fresh = process_company(company, [])  # throwaway queue: one retry only
    except Exception:
        logger.exception("Second crawl failed: %s", company.name)
        return

    has_data = any(
        any(row.get(column) for column in
            ("first_name", "decision_maker_email", "company_email", "contact_linkedin", "company_linkedin"))
        for row in fresh
    )

    try:
        with results_lock:
            group = rows_by_url.get(company.url)
            if group is not None and has_data:
                logger.info("Second crawl recovered: %s", company.name)
                group[:] = fresh
                save_results(flatten(results))
    except Exception:
        logger.exception("Second crawl merge failed: %s", company.name)


def retry_company(item: tuple, rows_by_url: dict, results: list):
    company, pages, validated, old_linkedin, old_people = item

    if pages is None:
        recrawl_company(company, rows_by_url, results)
        return

    try:
        new_linkedin, new_people = enrich_linkedin(company, pages, validated)
    except Exception:
        logger.exception("Second pass failed: %s", company.name)
        return

    # A failure merging this one company's result must not take the rest of
    # the chunk down with it (an uncaught exception here would propagate out
    # of second_pass and skip run_chunked's CSV write, losing every company
    # already scraped in this chunk, not just this one).
    try:
        people = merge_people(old_people, new_people)
        attach_site_emails(people, pages, company)

        with results_lock:
            group = rows_by_url.get(company.url)
            if group is None:
                return

            dm_emails = {p.email for p in people if p.email}
            generic_emails = sorted({
                email for row in group for email in split_company_email(row.get("company_email", ""))
                if email not in dm_emails
            })
            group[:] = build_rows(company, people, generic_emails, old_linkedin or new_linkedin)
            save_results(flatten(results))
    except Exception:
        logger.exception("Second pass merge failed: %s", company.name)


def second_pass(retry_queue: list, results: list):
    """`results` is run_companies()'s return value: one row-group per company."""
    logger.info(
        "Second pass: retrying %d companies (failed crawls and missing LinkedIn data) after %ds cool-down",
        len(retry_queue), SECOND_PASS_COOLDOWN_SECONDS,
    )
    time.sleep(SECOND_PASS_COOLDOWN_SECONDS)

    rows_by_url = {group[0]["company_url"]: group for group in results if group}

    with ThreadPoolExecutor(max_workers=max(1, COMPANY_WORKERS)) as pool:
        list(pool.map(lambda item: retry_company(item, rows_by_url, results), retry_queue))


_suppress_saves = False  # chunked mode appends rows itself instead of rewriting the CSV

# (OUTPUT_FILE, mtime) -> the groups we last loaded/wrote for it. save_results()
# is called once per company in a tight loop (see run_companies), and without
# this, each call did a full pd.read_csv + reparse of the whole shared file -
# an O(file_size) cost paid on every single company, growing quadratically
# over an N-company run as the file accumulates rows across sessions. The
# mtime check means any external change (a different process, a manual edit)
# still forces a fresh read, so this can never serve stale data.
_existing_groups_cache: Optional[tuple] = None


def load_existing_groups() -> dict:
    """domain -> that company's rows already on file, so a save merges into
    them instead of discarding whatever another run or source already put
    there. Keyed by domain (not the raw company_url string) for the same
    reason run_chunked's own resume check uses get_domain(): a "www." prefix
    or a different URL path for the same company must still match, the way
    extract_clutch_companies already dedupes companies elsewhere in this
    file."""
    global _existing_groups_cache

    if not os.path.exists(OUTPUT_FILE):
        return {}

    mtime = os.path.getmtime(OUTPUT_FILE)
    if _existing_groups_cache is not None:
        cached_path, cached_mtime, cached_groups = _existing_groups_cache
        if cached_path == OUTPUT_FILE and cached_mtime == mtime:
            return cached_groups

    try:
        df = pd.read_csv(OUTPUT_FILE, dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:
        return {}

    groups: dict = {}
    for row in df.to_dict("records"):
        groups.setdefault(get_domain(row["company_url"]), []).append(row)
    return groups


def merge_people_union(old: List[Person], new: List[Person]) -> List[Person]:
    """Combines two decision-maker lists for the same company, matched by
    name - or by email when a name is missing, which happens for a
    decision-maker email found directly on a site with no person attached
    (see process_company). An email-only entry must never be silently
    dropped just because it has no name to key on (alnum("") is falsy), and
    must be upgraded in place rather than duplicated once a later pass does
    attach a name to that same email. Each side fills in whatever the other
    is missing, and a person found on only one side is kept rather than
    dropped."""
    by_key: dict = {}
    by_email: dict = {}
    order: list = []

    for person in old + new:
        name_key = alnum(person.name)
        if not name_key and not person.email:
            continue

        existing = by_key.get(name_key) if name_key else None
        if existing is None and person.email:
            existing = by_email.get(person.email)

        if existing is None:
            copy = Person(person.name, person.title, person.email, person.linkedin_url, person.unverified)
            by_key[name_key or f"email:{person.email}"] = copy
            order.append(name_key or f"email:{person.email}")
            if copy.email:
                by_email[copy.email] = copy
            continue

        existing.title = existing.title or person.title
        existing.email = existing.email or person.email
        existing.linkedin_url = existing.linkedin_url or person.linkedin_url
        existing.unverified = existing.unverified and person.unverified
        if not alnum(existing.name) and person.name:
            existing.name = person.name
        if existing.email:
            by_email[existing.email] = existing

    return [by_key[key] for key in order]


def rows_to_contacts(rows: List[dict]) -> tuple:
    """Rebuilds (people, generic_emails, company_linkedin) from a company's
    already-saved rows - the inverse of build_rows(). Must strip the
    "[unverified]" suffix build_rows appends to contact_title back off (and
    restore the flag it came from), or a row that round-trips through here
    would bake the literal text into the title and silently lose the flag."""
    people, generic_emails, company_linkedin = [], [], ""
    for row in rows:
        company_linkedin = company_linkedin or row.get("company_linkedin", "")
        # company_email is repeated on every row for this company (like
        # company_linkedin), and can itself hold several "; "-joined
        # addresses - collect from every row, not just contact-less ones.
        generic_emails.extend(split_company_email(row.get("company_email", "")))
        # A row can be a decision-maker with no name on file yet (an email
        # found directly with nobody attached) - checking decision_maker_email
        # too keeps that row from falling through both branches and vanishing.
        if row.get("first_name") or row.get("last_name") or row.get("decision_maker_email"):
            title = row.get("contact_title", "")
            unverified = title.endswith(" [unverified]")
            people.append(Person(
                name=join_name(row.get("first_name", ""), row.get("last_name", "")),
                title=title.removesuffix(" [unverified]") if unverified else title,
                email=row.get("decision_maker_email", ""), linkedin_url=row.get("contact_linkedin", ""),
                unverified=unverified,
            ))
    return people, sorted(set(generic_emails)), company_linkedin


def merge_group(old_rows: Optional[List[dict]], new_rows: List[dict]) -> List[dict]:
    """Upserts a freshly scraped company's rows onto whatever is already on
    file for it: people merged by name, generic emails unioned, and the
    first non-empty company_linkedin wins - so a weaker rerun (site down,
    LLM quota spent) can never erase a contact an earlier run already
    found, but anything genuinely new still gets added."""
    if not old_rows:
        return new_rows

    old_people, old_generic, old_linkedin = rows_to_contacts(old_rows)
    new_people, new_generic, new_linkedin = rows_to_contacts(new_rows)

    people = merge_people_union(old_people, new_people)
    dm_emails = {p.email for p in people if p.email}
    generic_emails = sorted((set(old_generic) | set(new_generic)) - dm_emails)
    company_linkedin = new_linkedin or old_linkedin
    company = Company(
        name=new_rows[0].get("company_name") or old_rows[0].get("company_name", ""),
        url=new_rows[0]["company_url"],
    )

    return build_rows(company, people, generic_emails, company_linkedin)


def save_results(results: list):
    """`results` is a flat list of contact rows (possibly several per
    company); each save merges them onto whatever's already on OUTPUT_FILE."""
    global _existing_groups_cache

    if _suppress_saves:
        return

    new_groups: dict = {}
    for row in results:
        new_groups.setdefault(get_domain(row["company_url"]), []).append(row)

    existing = load_existing_groups()
    merged = {domain: rows for domain, rows in existing.items() if domain not in new_groups}
    for domain, rows in new_groups.items():
        merged[domain] = merge_group(existing.get(domain), rows)

    pd.DataFrame(flatten(merged.values()), columns=OUTPUT_COLUMNS).to_csv(OUTPUT_FILE, index=False, quoting=csv.QUOTE_ALL)
    _existing_groups_cache = (OUTPUT_FILE, os.path.getmtime(OUTPUT_FILE), merged)
    logger.info("Saved: %s", OUTPUT_FILE)


# ============================================================
# MAIN
# ============================================================

def run_companies(companies: List[Company], retry_queue: list) -> list:
    """Processes companies in parallel; each entry is that company's list of
    contact rows (always at least one), in the original company order."""
    rows = [None] * len(companies)

    def work(index: int):
        logger.info("[%d/%d] %s", index + 1, len(companies), companies[index].name)

        group = process_company(companies[index], retry_queue)

        with results_lock:
            rows[index] = group
            save_results(flatten(rows))  # progress survives a crash

    with ThreadPoolExecutor(max_workers=max(1, COMPANY_WORKERS)) as pool:
        list(pool.map(work, range(len(companies))))

    return [group for group in rows if group is not None]


def rerun_company(company: Company, old_rows: List[dict]) -> Optional[List[dict]]:
    """
    Re-crawls one company's site and asks the LLM for its decision makers.
    Returns the company's updated rows, or None when the crawl failed
    (nothing was learned, so it stays eligible for a later run). Lets
    RateLimitError through.
    """
    pages = crawl_company(Company(name=company.name, url=company.url))
    if not pages:
        return None

    validated = validate_result(analyze_with_llm(company, pages), pages)

    old_people, old_generic, old_linkedin = rows_to_contacts(old_rows)
    old_by_name = {alnum(person.name): person for person in old_people}
    people = []
    for dm in validated.decision_makers:
        previous = old_by_name.pop(alnum(dm.name), None)
        people.append(Person(
            name=dm.name, title=dm.title, email=dm.email,
            linkedin_url=previous.linkedin_url if previous else "",
        ))
    people += list(old_by_name.values())
    people = people[:MAX_DECISION_MAKERS]

    attach_site_emails(people, pages, company)

    dm_emails = {p.email for p in people if p.email}
    generic_emails = [e for e in old_generic if e not in dm_emails]

    return build_rows(company, people, generic_emails, old_linkedin)


def rerun_generic():
    """Gives companies with only generic mailboxes an LLM pass, until the daily quota runs out."""
    rows = pd.read_csv(OUTPUT_FILE, dtype=str, keep_default_na=False).to_dict("records")

    groups: dict = {}
    order: list = []
    for row in rows:
        domain = get_domain(row["company_url"])
        if domain not in groups:
            groups[domain] = []
            order.append(domain)
        groups[domain].append(row)

    def has_dm_email(domain):
        return any(row.get("decision_maker_email") for row in groups[domain])

    done = read_lines(RERUN_DONE_FILE)
    no_dm_email = [domain for domain in order if not has_dm_email(domain)]
    todo = [domain for domain in no_dm_email if groups[domain][0]["company_url"] not in done]
    logger.info("%d companies have no decision-maker email, %d not yet re-analysed", len(no_dm_email), len(todo))

    processed = improved = 0

    for domain in todo:
        if 0 < RERUN_MAX <= processed:
            break

        old_rows = groups[domain]
        company = Company(name=old_rows[0]["company_name"], url=old_rows[0]["company_url"])

        try:
            updated = rerun_company(company, old_rows)
        except RateLimitError:
            logger.warning("Groq quota used up after %d companies; run again later to continue.", processed)
            break
        except Exception:
            logger.exception("Re-analysis failed: %s", company.name)
            continue

        if updated is None:
            logger.warning("No pages found, left for a later run: %s", company.name)
            continue

        processed += 1
        found_emails = [row["decision_maker_email"] for row in updated if row["decision_maker_email"]]
        if found_emails:
            improved += 1
        groups[domain] = updated

        with open(RERUN_DONE_FILE, "a") as f:
            f.write(company.url + "\n")
        pd.DataFrame(flatten(groups[d] for d in order), columns=OUTPUT_COLUMNS).to_csv(OUTPUT_FILE, index=False, quoting=csv.QUOTE_ALL)
        logger.info("[%d] %s -> %s", processed, company.name, ";".join(found_emails) or "no decision-maker email")

    logger.info("Re-analysed %d companies, %d now have a decision-maker email.", processed, improved)


def clutch_page_url(base_url: str, page: int) -> str:
    return f"{base_url}{'&' if '?' in base_url else '?'}page={page}"


def collect_clutch_all(base_url: str) -> List[Company]:
    """Walks every listing page, resuming from the checkpoint files."""
    done = set()
    if os.path.exists(PAGES_DONE_FILE):
        with open(PAGES_DONE_FILE) as f:
            done = {int(line) for line in f if line.strip().isdigit()}

    for page in range(CLUTCH_ALL_PAGES):
        if page in done:
            continue

        try:
            found = extract_clutch_companies(clutch_page_url(base_url, page))
        except Exception as e:
            # Leave the page out of the checkpoint so the next run retries it.
            logger.warning("Listing page %d skipped: %s", page, e)
            continue

        with open(COMPANY_LIST_FILE, "a") as f:
            for company in found:
                f.write(json.dumps({"name": company.name, "url": company.url}) + "\n")
        with open(PAGES_DONE_FILE, "a") as f:
            f.write(f"{page}\n")

        logger.info("Listing page %d/%d: %d companies", page + 1, CLUTCH_ALL_PAGES, len(found))

    companies = load_companies()

    logger.info("Collected %d unique companies", len(companies))

    return companies


def load_companies(country: Optional[str] = None) -> List[Company]:
    """Unique-by-domain companies from the checkpoint file, optionally only one country's."""
    companies, seen = [], set()
    if os.path.exists(COMPANY_LIST_FILE):
        with open(COMPANY_LIST_FILE) as f:
            for line in f:
                item = json.loads(line)
                domain = get_domain(item["url"])
                if country and item.get("country") != country:
                    continue
                if domain and domain not in seen:
                    seen.add(domain)
                    companies.append(Company(name=item["name"], url=item["url"]))

    return companies


def read_lines(path: str) -> set:
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        return {line.strip() for line in f if line.strip()}


def discover_clutch_countries() -> List[str]:
    """Country codes from the /<cc>/developers links on Clutch's global listing, Sweden first."""
    for attempt in range(1, CLUTCH_RENDER_MAX_ATTEMPTS + 1):
        html = render_clutch_listing_html(f"https://clutch.co/{CLUTCH_COUNTRY_CATEGORY}")
        codes = set(re.findall(rf'href="(?:https://clutch\.co)?/([a-z]{{2}})/{re.escape(CLUTCH_COUNTRY_CATEGORY)}[/"?]', html))
        if codes:
            return sorted(codes, key=lambda code: (code != "se", code))
        logger.warning("Attempt %d/%d: no country links found, retrying.", attempt, CLUTCH_RENDER_MAX_ATTEMPTS)
        time.sleep(CLUTCH_RETRY_DELAY_SECONDS)

    raise RuntimeError("Could not discover Clutch countries (Cloudflare challenge?). Set CLUTCH_COUNTRIES=se,no,... instead.")


def collect_clutch_country(country: str) -> bool:
    """
    Walks one country's listing pages until Clutch returns an empty one,
    checkpointing each page as "<cc>:<page>". True when the end was reached;
    False when a page could not be loaded, so the country is retried next run.
    """
    base_url = f"https://clutch.co/{country}/{CLUTCH_COUNTRY_CATEGORY}"
    done_pages = read_lines(PAGES_DONE_FILE)

    for page in range(CLUTCH_COUNTRY_MAX_PAGES):
        if f"{country}:{page}" in done_pages:
            continue

        try:
            found = extract_clutch_companies(clutch_page_url(base_url, page))
        except RuntimeError as e:
            if str(e).startswith("No provider rows") and page > 0:
                logger.info("%s: listing ends after %d pages", country, page)
                return True
            logger.warning("%s page %d could not be loaded: %s", country, page, e)
            return False

        with open(COMPANY_LIST_FILE, "a") as f:
            for company in found:
                f.write(json.dumps({"name": company.name, "url": company.url, "country": country}) + "\n")
        with open(PAGES_DONE_FILE, "a") as f:
            f.write(f"{country}:{page}\n")

        logger.info("%s page %d: %d companies", country, page + 1, len(found))

    # The loop ran out of page budget, not out of listings: run_countries
    # still marks this country "done" and will never revisit it, so a
    # country with more than CLUTCH_COUNTRY_MAX_PAGES pages would silently
    # lose everything past the cap unless this is surfaced loudly.
    logger.warning(
        "%s: reached CLUTCH_COUNTRY_MAX_PAGES (%d) without an empty page - "
        "this country may have more companies than were collected. "
        "Raise CLUTCH_COUNTRY_MAX_PAGES and rerun if so.",
        country, CLUTCH_COUNTRY_MAX_PAGES,
    )
    return True


def run_countries(countries: List[str]):
    """Country by country: collect its listing, crawl its companies, then move on."""
    finished = read_lines(COUNTRIES_DONE_FILE)

    for number, country in enumerate(countries, 1):
        if country in finished:
            continue

        logger.info("=== Country %d/%d: %s ===", number, len(countries), country)

        if not collect_clutch_country(country):
            continue

        companies = load_companies(country)
        if MAX_COMPANIES > 0:
            companies = companies[:MAX_COMPANIES]
        logger.info("%s: %d companies listed", country, len(companies))
        run_chunked(companies)

        with open(COUNTRIES_DONE_FILE, "a") as f:
            f.write(f"{country}\n")


def run_chunked(companies: List[Company]):
    """Processes companies in chunks, appending each chunk's rows to the CSV."""
    global _suppress_saves

    done = set()
    if os.path.exists(OUTPUT_FILE):
        done = {get_domain(url) for url in pd.read_csv(OUTPUT_FILE, usecols=["company_url"])["company_url"].dropna()}

    todo = [c for c in companies if get_domain(c.url) not in done]
    logger.info("%d companies already done, %d to go", len(companies) - len(todo), len(todo))

    for start in range(0, len(todo), CHUNK_SIZE):
        chunk = todo[start:start + CHUNK_SIZE]
        retry_queue = []
        rows = []

        _suppress_saves = True
        try:
            rows = run_companies(chunk, retry_queue)
            if ENABLE_SECOND_PASS and retry_queue:
                second_pass(retry_queue, rows)
        except Exception:
            # Whatever already got scraped into `rows` must still reach the
            # CSV below even if second_pass (or something else) blew up
            # partway through - losing an unhandled error here used to mean
            # the whole chunk's already-found emails never got saved at all.
            logger.exception("Chunk processing hit an error; saving what was already scraped")
        finally:
            _suppress_saves = False

        if rows:
            pd.DataFrame(flatten(rows), columns=OUTPUT_COLUMNS).to_csv(
                OUTPUT_FILE, mode="a", header=not os.path.exists(OUTPUT_FILE), index=False, quoting=csv.QUOTE_ALL
            )
        logger.info("Chunk saved: %d/%d companies done", len(done) + start + len(chunk), len(companies))


def ensure_clutch_access() -> None:
    """A bare `python3 company_email_finder.py` on a machine that has never
    run SOURCE=clutch_verify (or whose profile's Cloudflare clearance has
    expired) should still work without a separate manual step first: probe
    with one quick headless request, and only fall back to opening a visible
    browser for the interactive check if that profile isn't cleared. One
    immediate retry first: a probe can land before the listing has fully
    hydrated even though the profile really is cleared, and that's worth a
    second quick look before paying for a visible-browser round trip."""
    for _ in range(2):
        try:
            html = render_clutch_listing_html(f"https://clutch.co/{CLUTCH_COUNTRY_CATEGORY}")
        except Exception:
            html = ""

        if "provider-row" in html:
            return

    logger.info(
        "Clutch access isn't verified yet (new profile, or an expired one) - "
        "opening a browser window for the one-time Cloudflare check."
    )
    if not verify_clutch_access():
        raise RuntimeError(
            "Clutch was not verified in time: a browser window opened for the "
            "Cloudflare check but it wasn't completed - run this again and "
            "complete it there."
        )
    logger.info("Clutch verified - continuing.")


def main():
    if SOURCE == "clutch_verify":
        if verify_clutch_access():
            logger.info("Clutch verified: later Clutch runs reuse this browser profile.")
        else:
            raise RuntimeError("Clutch was not verified in time: the Cloudflare check was not completed.")
        return

    if USE_LLM and not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY is missing.")

    if SOURCE in ("clutch", "clutch_all", "clutch_countries"):
        ensure_clutch_access()

    if SOURCE == "clutch":
        companies = extract_clutch_companies(CLUTCH_URL)
    elif SOURCE == "rerun_generic":
        if not USE_LLM:
            raise RuntimeError("rerun_generic needs the LLM: unset USE_LLM=0.")
        rerun_generic()
        logger.info("Finished.")
        return
    elif SOURCE == "clutch_countries":
        run_countries(CLUTCH_COUNTRIES or discover_clutch_countries())
        logger.info("Finished.")
        return
    elif SOURCE == "clutch_all":
        companies = collect_clutch_all(CLUTCH_URL)
        if MAX_COMPANIES > 0:
            companies = companies[:MAX_COMPANIES]
        run_chunked(companies)
        logger.info("Finished.")
        return
    else:
        raise RuntimeError(f"Unknown SOURCE {SOURCE!r}: use 'clutch', 'clutch_all', 'clutch_countries' or 'rerun_generic'.")

    if MAX_COMPANIES > 0:
        companies = companies[:MAX_COMPANIES]

    logger.info("Processing %d companies", len(companies))

    retry_queue = []
    results = run_companies(companies, retry_queue)

    if ENABLE_SECOND_PASS and retry_queue:
        second_pass(retry_queue, results)

    logger.info("Finished.")


if __name__ == "__main__":
    main()
