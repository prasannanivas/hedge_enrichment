"""
Hedge Fund Contact Enrichment - Gemini Agent (PydanticAI + Web Search)
=======================================================================
Strategy (in order per manager):
  1. Gemini agent searches the web for the official website URL
  2. Scrape that website's contact pages for emails/phones
  3. Gemini agent searches the web for contacts (SEC filings, IR pages, etc.)
  4. Score, merge, save with resume support

Agents have two tools available:
  - web_search(query)  — DuckDuckGo search, returns top N result snippets
  - fetch_url(url)     — fetches a page and returns cleaned text

This lets Gemini actively search (like Claude does) rather than guessing
from training-data memory alone.

Usage:
    python agent.py --key YOUR_GEMINI_KEY --limit 10              # test 10
    python agent.py --key YOUR_GEMINI_KEY                         # full 912
    python agent.py --key YOUR_GEMINI_KEY --resume                # resume
    python agent.py --key YOUR_GEMINI_KEY --model google:gemini-2.5-flash
"""

import argparse
import csv
import logging
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

# =============================================================================
# Config
# =============================================================================

INPUT_CSV     = "active_managers.csv"
OUTPUT_CSV    = "enriched_contacts.csv"
PROGRESS_FILE = "progress.json"
LOG_FILE      = "agent.log"

# gemini-2.5-pro free tier: 5 RPM, 25 RPD
# gemini-2.5-flash free tier: 15 RPM, 1500 RPD
DEFAULT_MODEL = "google:gemini-2.5-pro"
GEMINI_RPM    = 4     # stay 1 below the free-tier cap; raise if on a paid key

REQUEST_TIMEOUT = 14
SCRAPE_DELAY    = 1.2

SKIP_DOMAINS = [
    "linkedin.com", "bloomberg.com", "reuters.com", "wikipedia.org",
    "nilssonhedge.com", "hedgefund.net", "preqin.com", "pitchbook.com",
    "crunchbase.com", "wsj.com", "ft.com", "forbes.com", "businesswire.com",
    "prnewswire.com", "sec.gov", "nfa.futures.org", "twitter.com", "x.com",
    "facebook.com", "youtube.com", "instagram.com", "reddit.com",
    "morningstar.com", "investing.com", "marketwatch.com",
]

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# =============================================================================
# Logging — UTF-8 safe on Windows
# =============================================================================

def _utf8_stream_handler():
    try:
        stream = open(sys.stdout.fileno(), mode="w",
                      encoding="utf-8", buffering=1, closefd=False)
        return logging.StreamHandler(stream)
    except Exception:
        return logging.StreamHandler(sys.stdout)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh  = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh  = _utf8_stream_handler()
_sh.setFormatter(_fmt)
log  = logging.getLogger("agent")
log.setLevel(logging.INFO)
log.addHandler(_fh)
log.addHandler(_sh)

# =============================================================================
# Rate limiter — sliding window, prevents 429s proactively
# =============================================================================

class _RateLimiter:
    def __init__(self, rpm: int):
        self._rpm = rpm
        self._calls: deque = deque()

    def wait(self):
        now = time.time()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= self._rpm:
            sleep_for = 61 - (now - self._calls[0])
            if sleep_for > 0:
                log.info(f"  Rate limiter: waiting {sleep_for:.1f}s ({self._rpm} RPM cap)")
                time.sleep(sleep_for)
        self._calls.append(time.time())

_rate_limiter = _RateLimiter(GEMINI_RPM)

# =============================================================================
# Regex helpers
# =============================================================================

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(\+?[\d]{1,3}[\s\-.]?)?(\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4})")

JUNK_DOMAINS  = {"example.com", "test.com", "domain.com", "sentry.io",
                 "wixpress.com", "squarespace.com"}
JUNK_PREFIXES = {"noreply", "no-reply", "donotreply", "webmaster", "postmaster"}


def extract_emails(text: str) -> list:
    results = []
    for e in EMAIL_RE.findall(text):
        e = e.lower().strip(".")
        domain = e.split("@")[-1]
        prefix = e.split("@")[0]
        if domain in JUNK_DOMAINS:
            continue
        if any(prefix.startswith(j) for j in JUNK_PREFIXES):
            continue
        if re.search(r"\.(png|jpg|gif|svg|webp|css|js)$", domain):
            continue
        results.append(e)
    return list(dict.fromkeys(results))


def extract_phones(text: str) -> list:
    phones = []
    for m in PHONE_RE.finditer(text):
        p = m.group().strip()
        digits = re.sub(r"\D", "", p)
        if 10 <= len(digits) <= 15:
            phones.append(p)
    return list(dict.fromkeys(phones))


def score_email(email: str, website: str) -> int:
    if not email:
        return 0
    score = 20
    domain = email.split("@")[-1]
    prefix = email.split("@")[0]
    if website:
        site_domain = urlparse(website).netloc.replace("www.", "")
        if domain == site_domain or domain.endswith("." + site_domain):
            score += 40
    good_prefixes = {"ir", "investorrelations", "investor", "info",
                     "contact", "office", "hello", "compliance"}
    if prefix in good_prefixes:
        score += 20
    if "." in prefix and len(prefix.split(".")) == 2:
        score += 10
    return min(score, 100)


# =============================================================================
# HTTP scraping
# =============================================================================

def safe_get(url: str, session: requests.Session):
    try:
        r = session.get(url, headers=SCRAPE_HEADERS,
                        timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return r
    except requests.exceptions.SSLError:
        try:
            return session.get(url, headers=SCRAPE_HEADERS,
                               timeout=REQUEST_TIMEOUT,
                               allow_redirects=True, verify=False)
        except Exception:
            return None
    except Exception:
        return None


def scrape_website(website: str, session: requests.Session) -> dict:
    result = {"emails": [], "phones": []}
    if not website:
        return result

    base  = website.rstrip("/")
    pages = [
        base,
        base + "/contact",
        base + "/contact-us",
        base + "/about",
        base + "/about-us",
        base + "/investor-relations",
        base + "/team",
        base + "/en/contact",
        base + "/en/contact/",
    ]

    for page_url in pages:
        r = safe_get(page_url, session)
        time.sleep(SCRAPE_DELAY)
        if not r or r.status_code != 200:
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)

        emails = extract_emails(text)
        phones = extract_phones(text)

        for a in soup.select("a[href^='mailto:']"):
            em = a["href"].replace("mailto:", "").split("?")[0].strip().lower()
            if em and "@" in em:
                emails.insert(0, em)

        for a in soup.select("a[href^='tel:']"):
            ph = a["href"].replace("tel:", "").strip()
            if ph:
                phones.insert(0, ph)

        result["emails"].extend(emails)
        result["phones"].extend(phones)

        if emails or phones:
            break

    result["emails"] = list(dict.fromkeys(result["emails"]))
    result["phones"] = list(dict.fromkeys(result["phones"]))
    return result


# =============================================================================
# Pydantic schemas — enforced at the token level by Gemini structured output
# =============================================================================

class WebsiteResult(BaseModel):
    url: str = Field(
        default="",
        description=(
            "Official website base URL, e.g. https://www.abbeycapital.com. "
            "Empty string if unknown or not confidently known."
        ),
    )


class ContactResult(BaseModel):
    email: str = Field(
        default="",
        description="Best publicly known contact email. Empty string if unknown — do NOT guess.",
    )
    phone: str = Field(
        default="",
        description="Main office phone number with country code. Empty string if unknown.",
    )
    contact_name: str = Field(
        default="",
        description="Name of IR officer, CEO, or main public contact. Empty if unknown.",
    )
    contact_title: str = Field(
        default="",
        description="Job title of the contact person.",
    )
    confidence: Literal["high", "medium", "low"] = Field(
        default="low",
        description="Confidence in the accuracy of the data returned.",
    )
    source_note: str = Field(
        default="",
        description="Where this info comes from publicly, e.g. 'SEC Form ADV', 'company website'.",
    )


# =============================================================================
# Web tools — given to agents so Gemini can search instead of guessing
# =============================================================================

_ddgs = DDGS()

def _web_search(query: str, max_results: int = 6) -> str:
    """Run a DuckDuckGo search and return a text block of results."""
    try:
        results = list(_ddgs.text(query, max_results=max_results))
        if not results:
            return "No results found."
        lines = []
        for r in results:
            lines.append(f"Title: {r.get('title','')}")
            lines.append(f"URL: {r.get('href','')}")
            lines.append(f"Snippet: {r.get('body','')}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Search error: {e}"


def _fetch_url(url: str, max_chars: int = 4000) -> str:
    """Fetch a URL and return cleaned visible text (capped to avoid token bloat)."""
    try:
        r = requests.get(url, headers=SCRAPE_HEADERS,
                         timeout=REQUEST_TIMEOUT, allow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        return text[:max_chars]
    except Exception as e:
        return f"Fetch error: {e}"


# =============================================================================
# PydanticAI agents
# =============================================================================

WEBSITE_SYSTEM = """\
You are a financial data assistant with web search capability.
Identify the official investor-facing website of a hedge fund or asset manager.

Use the web_search tool to search for the fund, then fetch_url to confirm it.
Rules:
- Return ONLY the base domain URL (e.g. https://www.abbeycapital.com)
- Do NOT return LinkedIn, Bloomberg, Wikipedia, Preqin, news sites, or data aggregators
- If you cannot find a credible official website after searching, return an empty string\
"""

CONTACT_SYSTEM = """\
You are a financial research assistant with web search capability.
Find publicly available contact information for a hedge fund or asset manager.

Search strategies to try:
- "<fund name> investor relations contact email"
- "<fund name> SEC Form ADV"
- "<fund name> SEC EDGAR"
- "<fund name> site:sec.gov"
- "<fund name> official website contact"

Then fetch promising pages (company site, SEC filings) to extract real data.
Rules:
- Only include information genuinely found in search results or fetched pages
- Do NOT invent or guess email addresses — leave email empty if not found
- Do NOT include personal mobile numbers
- Prefer official company websites, SEC/CFTC Form ADV filings, or press releases\
"""


def make_agents(model_name: str) -> tuple:
    """Create PydanticAI agents with web search tools.
    Must be called after os.environ[GOOGLE_API_KEY] is set."""

    website_agent = Agent(
        model_name,
        output_type=WebsiteResult,
        system_prompt=WEBSITE_SYSTEM,
        retries=2,
    )
    contact_agent = Agent(
        model_name,
        output_type=ContactResult,
        system_prompt=CONTACT_SYSTEM,
        retries=2,
    )

    @website_agent.tool_plain
    def web_search(query: str) -> str:
        """Search the web for information about a fund."""
        log.info(f"    [tool] web_search: {query!r}")
        return _web_search(query)

    @website_agent.tool_plain
    def fetch_url(url: str) -> str:
        """Fetch a web page and return its visible text."""
        log.info(f"    [tool] fetch_url: {url!r}")
        return _fetch_url(url)

    @contact_agent.tool_plain
    def web_search(query: str) -> str:  # noqa: F811
        """Search the web for contact information."""
        log.info(f"    [tool] web_search: {query!r}")
        return _web_search(query)

    @contact_agent.tool_plain
    def fetch_url(url: str) -> str:  # noqa: F811
        """Fetch a web page and return its visible text."""
        log.info(f"    [tool] fetch_url: {url!r}")
        return _fetch_url(url)

    return website_agent, contact_agent


# =============================================================================
# Gemini lookups — rate limiter wraps every run_sync call
# =============================================================================

def gemini_find_website(agent: Agent, name: str, fund_type: str) -> str:
    _rate_limiter.wait()
    try:
        result = agent.run_sync(f"Fund: {name}\nType: {fund_type}")
        url = (result.output.url or "").strip()
        log.info(f"  Gemini website: {url!r}")
        if not url:
            return ""
        match = re.search(r"https?://[^\s\"'<>]+", url)
        if match:
            u = match.group(0).rstrip(".,)")
            parsed = urlparse(u)
            if parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
        return ""
    except Exception as e:
        log.warning(f"  Website lookup failed: {e}")
        return ""


def gemini_find_contacts(agent: Agent, name: str, fund_type: str, website: str) -> dict:
    _rate_limiter.wait()
    try:
        result = agent.run_sync(
            f"Fund: {name}\nType: {fund_type}\nKnown website: {website or 'not found'}"
        )
        data = result.output
        log.info(f"  Gemini contacts: email={data.email!r} conf={data.confidence!r}")
        return data.model_dump()
    except Exception as e:
        log.warning(f"  Contact lookup failed: {e}")
        return {}


# =============================================================================
# Progress + output
# =============================================================================

OUTPUT_FIELDS = [
    "manager_id", "manager_name", "type", "style", "strategy", "sector",
    "website",
    "email_1", "email_2",
    "phone_1",
    "email_score",
    "contact_name", "contact_title",
    "confidence",
    "source",
    "status",
    "notes",
    "last_checked",
]

STATUS_VERIFIED     = "Verified"
STATUS_NEEDS_REVIEW = "Needs Review"
STATUS_NOT_FOUND    = "No Reliable Contact Found"


def load_progress(path: str = PROGRESS_FILE) -> dict:
    if Path(path).exists():
        import json
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_progress(progress: dict, path: str = PROGRESS_FILE):
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)


def build_row(manager: dict, website: str, scraped: dict, gc: dict) -> dict:
    emails = scraped.get("emails", [])
    phones = scraped.get("phones", [])

    email_1 = emails[0] if emails else gc.get("email", "")
    email_2 = emails[1] if len(emails) > 1 else ""
    phone_1 = phones[0] if phones else gc.get("phone", "")

    email_score = score_email(email_1, website)

    sources = []
    if website:              sources.append("Gemini:URL")      # Gemini found the website
    if emails or phones:     sources.append("WebScrape")       # scraped from that site
    if gc.get("email") or gc.get("phone"): sources.append("Gemini:Contacts")  # Gemini found contacts via search

    if email_1 and email_score >= 60:
        status = STATUS_VERIFIED
    elif email_1 or phone_1:
        status = STATUS_NEEDS_REVIEW
    else:
        status = STATUS_NOT_FOUND

    return {
        "manager_id":    manager["manager_id"],
        "manager_name":  manager["manager_name"],
        "type":          manager["type"],
        "style":         manager["style"],
        "strategy":      manager["strategy"],
        "sector":        manager["sector"],
        "website":       website,
        "email_1":       email_1,
        "email_2":       email_2,
        "phone_1":       phone_1,
        "email_score":   email_score,
        "contact_name":  gc.get("contact_name", ""),
        "contact_title": gc.get("contact_title", ""),
        "confidence":    gc.get("confidence", ""),
        "source":        ", ".join(sources) if sources else "None",
        "status":        status,
        "notes":         gc.get("source_note", ""),
        "last_checked":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key",    required=True, help="Google AI Studio API key")
    parser.add_argument("--model",  default=DEFAULT_MODEL,
                        help="PydanticAI model string, e.g. google:gemini-2.5-pro")
    parser.add_argument("--rpm",    type=int, default=GEMINI_RPM,
                        help="Max requests per minute (default 4 for free-tier 2.5-pro)")
    parser.add_argument("--limit",         type=int, default=0)
    parser.add_argument("--offset",        type=int, default=0,
                        help="Skip first N managers (for parallel chunks)")
    parser.add_argument("--resume",        action="store_true")
    parser.add_argument("--input",         default=INPUT_CSV)
    parser.add_argument("--output",        default=OUTPUT_CSV)
    parser.add_argument("--progress-file", default=PROGRESS_FILE,
                        help="Progress JSON path (use different files for parallel runs)")
    args = parser.parse_args()

    # PydanticAI reads this env var to authenticate with Google AI Studio
    os.environ["GOOGLE_API_KEY"] = args.key

    # Update rate limiter if caller passed --rpm
    _rate_limiter._rpm = args.rpm

    website_agent, contact_agent = make_agents(args.model)
    session = requests.Session()
    session.headers.update(SCRAPE_HEADERS)

    import csv as _csv
    managers = []
    with open(args.input, newline="", encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            managers.append(row)
    if args.offset:
        managers = managers[args.offset:]
    if args.limit:
        managers = managers[:args.limit]

    log.info(
        f"Model: {args.model} | RPM cap: {args.rpm} | "
        f"Offset: {args.offset} | Managers: {len(managers)}"
    )

    progress      = load_progress(args.progress_file) if args.resume else {}
    completed_ids = set(progress.keys())

    write_header = not Path(args.output).exists() or not args.resume
    out_file = open(args.output, "a" if args.resume else "w",
                    newline="", encoding="utf-8")
    writer = _csv.DictWriter(out_file, fieldnames=OUTPUT_FIELDS)
    if write_header:
        writer.writeheader()

    stats = {"verified": 0, "needs_review": 0, "not_found": 0, "errors": 0}

    try:
        for i, manager in enumerate(managers):
            mid  = manager["manager_id"]
            name = manager["manager_name"]

            if mid in completed_ids:
                log.info(f"[{i+1}/{len(managers)}] SKIP {name}")
                continue

            log.info(f"\n[{i+1}/{len(managers)}] {name} [{manager['type']}]")

            try:
                # Step 1: Gemini finds the website
                website = gemini_find_website(website_agent, name, manager["type"])

                # Step 2: Scrape website
                scraped = {}
                if website:
                    log.info(f"  Scraping {website}")
                    scraped = scrape_website(website, session)
                    log.info(f"  Scraped -> emails={scraped['emails']} phones={scraped['phones']}")

                # Step 3: Gemini fills in contacts from public knowledge
                gc = gemini_find_contacts(contact_agent, name, manager["type"], website)

                # Step 4: Build row + save
                row = build_row(manager, website, scraped, gc)
                writer.writerow(row)
                out_file.flush()

                stat_key = {
                    STATUS_VERIFIED:     "verified",
                    STATUS_NEEDS_REVIEW: "needs_review",
                    STATUS_NOT_FOUND:    "not_found",
                }.get(row["status"], "not_found")
                stats[stat_key] += 1

                progress[mid] = row["status"]
                save_progress(progress, args.progress_file)

                log.info(
                    f"  -> {row['status']} | "
                    f"email={row['email_1']} | "
                    f"phone={row['phone_1']} | "
                    f"conf={row['confidence']}"
                )

            except KeyboardInterrupt:
                log.info("Interrupted. Run with --resume to continue.")
                break
            except Exception as e:
                log.error(f"  ERROR: {e}")
                row = build_row(manager, "", {}, {"source_note": f"Error: {e}"})
                row["status"] = STATUS_NOT_FOUND
                writer.writerow(row)
                stats["errors"] += 1
                progress[mid] = "error"
                save_progress(progress, args.progress_file)

    finally:
        out_file.close()

    total = sum(stats.values())
    log.info(
        f"\n=== DONE ===\n"
        f"  Total     : {total}\n"
        f"  Verified  : {stats['verified']}\n"
        f"  Review    : {stats['needs_review']}\n"
        f"  Not found : {stats['not_found']}\n"
        f"  Errors    : {stats['errors']}\n"
        f"  Output    : {args.output}"
    )


if __name__ == "__main__":
    main()
