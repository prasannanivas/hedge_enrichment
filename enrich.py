"""
Hedge Fund Contact Enrichment Pipeline
========================================
Runs locally on your machine.
Sources: SEC IAPD, NFA BASIC, website scraping, Hunter.io (optional)

Usage:
    python enrich.py                        # full run, all 912 managers
    python enrich.py --limit 20             # test run, first 20 managers
    python enrich.py --resume               # resume from where it stopped
    python enrich.py --hunter-key YOUR_KEY  # enable Hunter.io email lookup
"""

import csv
import json
import os
import re
import sys
import time
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

# =============================================================================
# Config
# =============================================================================

INPUT_CSV      = "active_managers.csv"
OUTPUT_CSV     = "enriched_contacts.csv"
PROGRESS_FILE  = "progress.json"
LOG_FILE       = "enrich.log"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

REQUEST_DELAY   = 1.5    # seconds between requests
REQUEST_TIMEOUT = 14

# =============================================================================
# Logging  --  force UTF-8 on Windows CP1252 console
# =============================================================================

def _utf8_stream_handler() -> logging.StreamHandler:
    """StreamHandler that writes UTF-8 regardless of Windows console encoding."""
    try:
        # Python 3.7+ on Windows: reopen stdout in UTF-8 mode
        stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8",
                      buffering=1, closefd=False)
        return logging.StreamHandler(stream)
    except Exception:
        h = logging.StreamHandler(sys.stdout)
        h.setStream(sys.stdout)
        return h

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)

_sh = _utf8_stream_handler()
_sh.setFormatter(_fmt)

log = logging.getLogger("enrich")
log.setLevel(logging.INFO)
log.addHandler(_fh)
log.addHandler(_sh)

# =============================================================================
# Helpers
# =============================================================================

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(\+?1[\s\-.]?)?(\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4})")

STRIP_SUFFIXES = re.compile(
    r"\b(LLC|LP|Ltd\.?|Limited|Inc\.?|Corp\.?|"
    r"Capital Management|Asset Management|"
    r"Advisors?|Investments?|Partners?|Group|AG|SA|BV|GmbH)\b",
    re.IGNORECASE,
)

JUNK_DOMAINS = {
    "example.com", "test.com", "domain.com", "yourdomain.com",
    "email.com", "mail.com", "placeholder.com", "sentry.io",
    "wixpress.com", "squarespace.com",
}

JUNK_PREFIXES = {
    "noreply", "no-reply", "donotreply", "webmaster",
    "postmaster", "admin@admin", "support@support",
}

SKIP_DOMAINS = [
    "linkedin.com", "bloomberg.com", "reuters.com", "wikipedia.org",
    "nilssonhedge.com", "hedgefund.net", "preqin.com", "pitchbook.com",
    "crunchbase.com", "wsj.com", "ft.com", "forbes.com", "businesswire.com",
    "prnewswire.com", "sec.gov", "nfa.futures.org", "twitter.com",
    "facebook.com", "youtube.com", "instagram.com",
]


def clean_name(name: str) -> list:
    """Return search variants for a manager name (deduped)."""
    variants = [name]
    stripped = STRIP_SUFFIXES.sub("", name).strip(" ,.")
    if stripped and stripped != name:
        variants.append(stripped)
    words = stripped.split()
    if len(words) > 2:
        variants.append(" ".join(words[:2]))
    return list(dict.fromkeys(variants))


def safe_get(url: str, session: requests.Session, retries: int = 2):
    for attempt in range(retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                            allow_redirects=True)
            return r
        except requests.exceptions.SSLError:
            try:
                r = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                                allow_redirects=True, verify=False)
                return r
            except Exception:
                return None
        except Exception as e:
            if attempt == retries:
                log.debug(f"Failed {url}: {e}")
            time.sleep(0.5)
    return None


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
# Source: SEC IAPD
# =============================================================================

def lookup_sec_iapd(name: str, session: requests.Session) -> dict:
    result = {}
    for variant in clean_name(name):
        url = (
            "https://efts.sec.gov/LATEST/search-index?"
            f"q=%22{quote(variant)}%22&forms=ADV"
            "&dateRange=custom&startdt=2020-01-01&enddt=2025-12-31"
        )
        r = safe_get(url, session)
        if not r or r.status_code != 200:
            time.sleep(REQUEST_DELAY)
            continue
        try:
            data = r.json()
            hits = data.get("hits", {}).get("hits", [])
            if hits:
                src = hits[0].get("_source", {})
                result["sec_crd"] = src.get("file_num", "")
                result["source_sec"] = hits[0].get("_id", "")
                log.info(f"  SEC hit for '{variant}': {src.get('display_names','')}")
                break
        except Exception:
            pass
        finally:
            time.sleep(REQUEST_DELAY)
    return result


# =============================================================================
# Source: NFA BASIC
# =============================================================================

def lookup_nfa(name: str, session: requests.Session) -> dict:
    result = {}
    url = (
        "https://www.nfa.futures.org/basicnet/Basic.aspx?"
        f"txtNFAID=&txtFirmName={quote(name)}"
        "&txtIndivName=&chkCPO=on&chkCTA=on&chkIB=on&chkFCM=on&btnSearch=Search"
    )
    r = safe_get(url, session)
    if not r or r.status_code != 200:
        time.sleep(REQUEST_DELAY)
        return result
    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("table.table tr")
    for row in rows[1:3]:
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cols) >= 3:
            firm_name = cols[0].lower()
            if any(v.lower() in firm_name for v in clean_name(name)):
                result["nfa_firm"]   = cols[0]
                result["nfa_id"]     = cols[1] if len(cols) > 1 else ""
                result["nfa_status"] = cols[2] if len(cols) > 2 else ""
                log.info(f"  NFA hit: {cols[0]}")
                break
    time.sleep(REQUEST_DELAY)
    return result


# =============================================================================
# Source: Website discovery via DuckDuckGo
# =============================================================================

def find_website(name: str, session: requests.Session) -> str:
    """
    Find the official website for a fund manager using DuckDuckGo HTML search.
    Tries multiple query formulations.
    """
    queries = [
        f'"{name}" fund official website',
        f'{name} hedge fund',
        f'{name} asset management',
    ]

    for query in queries:
        url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        r = safe_get(url, session)
        time.sleep(REQUEST_DELAY)

        if not r or r.status_code != 200:
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        # DuckDuckGo HTML: result links are in <a class="result__a"> 
        # and URLs in <a class="result__url"> or span.result__url
        candidates = []

        # Method 1: result__url span (shows the bare domain)
        for el in soup.select("span.result__url, a.result__url"):
            href = el.get_text(strip=True)
            if href:
                candidates.append(href)

        # Method 2: result__a href (the actual link, may be redirected through DDG)
        for el in soup.select("a.result__a"):
            href = el.get("href", "")
            # DDG wraps in //duckduckgo.com/l/?uddg=<encoded_url>
            if "uddg=" in href:
                from urllib.parse import unquote, parse_qs, urlparse as _up
                qs = parse_qs(_up(href).query)
                if "uddg" in qs:
                    candidates.append(unquote(qs["uddg"][0]))
            elif href.startswith("http"):
                candidates.append(href)

        for href in candidates:
            if not href:
                continue
            if not href.startswith("http"):
                href = "https://" + href
            # strip trailing path noise from bare-domain results
            parsed = urlparse(href)
            domain = parsed.netloc.replace("www.", "")
            if not domain:
                continue
            if any(skip in domain for skip in SKIP_DOMAINS):
                continue
            # Looks like an actual company domain
            website = f"https://{parsed.netloc}"
            log.info(f"  Website candidate: {website}")
            return website

    return ""


# =============================================================================
# Source: Website scraping
# =============================================================================

def scrape_contact_page(website: str, session: requests.Session) -> dict:
    result = {"website": website, "emails": [], "phones": []}
    if not website:
        return result

    base = website.rstrip("/")
    pages = [
        base,
        base + "/contact",
        base + "/contact-us",
        base + "/about",
        base + "/investor-relations",
        base + "/team",
        base + "/about-us",
    ]

    for page_url in pages:
        r = safe_get(page_url, session)
        time.sleep(0.5)
        if not r or r.status_code not in (200,):
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        clean_text = soup.get_text(" ", strip=True)

        emails = extract_emails(clean_text)
        phones = extract_phones(clean_text)

        # mailto: links are most reliable
        for a in soup.select("a[href^='mailto:']"):
            email = a["href"].replace("mailto:", "").split("?")[0].strip().lower()
            if email and "@" in email:
                emails.insert(0, email)

        # tel: links
        for a in soup.select("a[href^='tel:']"):
            phone = a["href"].replace("tel:", "").strip()
            if phone:
                phones.insert(0, phone)

        result["emails"].extend(emails)
        result["phones"].extend(phones)

        # stop after first page that yields something
        if emails or phones:
            break

    result["emails"] = list(dict.fromkeys(result["emails"]))
    result["phones"] = list(dict.fromkeys(result["phones"]))
    return result


# =============================================================================
# Source: Hunter.io (optional)
# =============================================================================

def lookup_hunter(domain: str, api_key: str, session: requests.Session) -> dict:
    result = {}
    if not api_key or not domain:
        return result
    url = f"https://api.hunter.io/v2/domain-search?domain={domain}&api_key={api_key}&limit=5"
    r = safe_get(url, session)
    if not r or r.status_code != 200:
        return result
    try:
        data = r.json().get("data", {})
        phone = data.get("phone_number", "")
        if phone:
            result["phone_hunter"] = phone
        emails_data = data.get("emails", [])
        priority = ["investor relations", "ir", "compliance", "cio", "coo", "partner"]
        sorted_e = sorted(
            emails_data,
            key=lambda e: any(t in (e.get("position") or "").lower() for t in priority),
            reverse=True,
        )
        if sorted_e:
            best = sorted_e[0]
            result["email_hunter"]            = best.get("value", "")
            result["email_hunter_confidence"] = best.get("confidence", 0)
            result["contact_name_hunter"]     = f"{best.get('first_name','')} {best.get('last_name','')}".strip()
            result["contact_title_hunter"]    = best.get("position", "")
        log.info(f"  Hunter: {len(emails_data)} emails found for {domain}")
    except Exception as e:
        log.debug(f"Hunter error: {e}")
    time.sleep(REQUEST_DELAY)
    return result


# =============================================================================
# Progress tracking
# =============================================================================

def load_progress() -> dict:
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_progress(progress: dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)


# =============================================================================
# Output
# =============================================================================

OUTPUT_FIELDS = [
    "manager_id", "manager_name", "type", "style", "strategy", "sector",
    "website",
    "email_1", "email_2",
    "phone_1", "phone_2",
    "email_score",
    "contact_name", "contact_title",
    "nfa_id", "nfa_status",
    "sec_crd",
    "source",
    "status",
    "notes",
    "last_checked",
]

STATUS_VERIFIED      = "Verified"
STATUS_NEEDS_REVIEW  = "Needs Review"
STATUS_NOT_FOUND     = "No Reliable Contact Found"


def build_output_row(manager: dict, enriched: dict) -> dict:
    emails  = enriched.get("emails", [])
    phones  = enriched.get("phones", [])

    email_1 = emails[0] if emails else enriched.get("email_hunter", "")
    email_2 = emails[1] if len(emails) > 1 else ""
    phone_1 = phones[0] if phones else enriched.get("phone_hunter", "")
    phone_2 = phones[1] if len(phones) > 1 else ""

    website     = enriched.get("website", "")
    email_score = score_email(email_1, website)

    if email_1 and email_score >= 60:
        status = STATUS_VERIFIED
    elif email_1 or phone_1:
        status = STATUS_NEEDS_REVIEW
    else:
        status = STATUS_NOT_FOUND

    sources = []
    if enriched.get("source_sec"):
        sources.append("SEC")
    if enriched.get("nfa_id"):
        sources.append("NFA")
    if enriched.get("email_hunter"):
        sources.append("Hunter")
    if website:
        sources.append("Website")

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
        "phone_2":       phone_2,
        "email_score":   email_score,
        "contact_name":  enriched.get("contact_name_hunter", ""),
        "contact_title": enriched.get("contact_title_hunter", ""),
        "nfa_id":        enriched.get("nfa_id", ""),
        "nfa_status":    enriched.get("nfa_status", ""),
        "sec_crd":       enriched.get("sec_crd", ""),
        "source":        ", ".join(sources) if sources else "None",
        "status":        status,
        "notes":         enriched.get("notes", ""),
        "last_checked":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


# =============================================================================
# Main pipeline
# =============================================================================

def enrich_manager(manager: dict, session: requests.Session, hunter_key: str = "") -> dict:
    name      = manager["manager_name"]
    fund_type = manager["type"]
    log.info(f"Processing: {name} [{fund_type}]")

    enriched = {}

    # 1. SEC IAPD (skip crypto funds)
    if fund_type not in ("Crypto",):
        enriched.update(lookup_sec_iapd(name, session))

    # 2. NFA BASIC (CTAs only)
    if fund_type == "CTA":
        enriched.update(lookup_nfa(name, session))

    # 3. Find website
    website = find_website(name, session)
    if website:
        enriched["website"] = website

        # 4. Scrape contact pages
        contact = scrape_contact_page(website, session)
        enriched["emails"] = contact.get("emails", [])
        enriched["phones"] = contact.get("phones", [])

        # 5. Hunter.io (optional)
        if hunter_key:
            domain = urlparse(website).netloc.replace("www.", "")
            enriched.update(lookup_hunter(domain, hunter_key, session))
            if not enriched.get("emails") and enriched.get("email_hunter"):
                enriched["emails"] = [enriched["email_hunter"]]

    return enriched


def main():
    parser = argparse.ArgumentParser(description="Hedge Fund Contact Enrichment")
    parser.add_argument("--limit",      type=int, default=0,  help="Process only N managers (0=all)")
    parser.add_argument("--resume",     action="store_true",  help="Resume from saved progress")
    parser.add_argument("--hunter-key", default="",           help="Hunter.io API key")
    parser.add_argument("--input",      default=INPUT_CSV,    help="Input CSV")
    parser.add_argument("--output",     default=OUTPUT_CSV,   help="Output CSV")
    args = parser.parse_args()

    managers = []
    with open(args.input, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            managers.append(row)

    if args.limit:
        managers = managers[: args.limit]

    log.info(f"Loaded {len(managers)} managers to enrich")

    progress     = load_progress() if args.resume else {}
    completed_ids = set(progress.keys())

    session = requests.Session()
    session.headers.update(HEADERS)

    write_header = not Path(args.output).exists() or not args.resume
    out_file = open(args.output, "a" if args.resume else "w",
                    newline="", encoding="utf-8")
    writer = csv.DictWriter(out_file, fieldnames=OUTPUT_FIELDS)
    if write_header:
        writer.writeheader()

    stats = {"verified": 0, "needs_review": 0, "not_found": 0, "errors": 0}

    try:
        for i, manager in enumerate(managers):
            mid = manager["manager_id"]

            if mid in completed_ids:
                log.info(f"[{i+1}/{len(managers)}] Skipping {manager['manager_name']} (done)")
                continue

            log.info(f"\n[{i+1}/{len(managers)}] --------------------------------")
            try:
                enriched = enrich_manager(manager, session, args.hunter_key)
                row      = build_output_row(manager, enriched)
                writer.writerow(row)
                out_file.flush()

                stat_key = {
                    STATUS_VERIFIED:     "verified",
                    STATUS_NEEDS_REVIEW: "needs_review",
                    STATUS_NOT_FOUND:    "not_found",
                }.get(row["status"], "not_found")
                stats[stat_key] += 1

                progress[mid] = row["status"]
                save_progress(progress)

                log.info(f"  -> {row['status']} | email={row['email_1']} | phone={row['phone_1']}")

            except KeyboardInterrupt:
                log.info("Interrupted. Progress saved. Run with --resume to continue.")
                break
            except Exception as e:
                log.error(f"  Error processing {manager['manager_name']}: {e}")
                row = build_output_row(manager, {"notes": f"Error: {e}"})
                row["status"] = STATUS_NOT_FOUND
                writer.writerow(row)
                stats["errors"] += 1
                progress[mid] = "error"
                save_progress(progress)
    finally:
        out_file.close()

    total = sum(stats.values())
    log.info(
        f"\n=== ENRICHMENT COMPLETE ===\n"
        f"  Total     : {total}\n"
        f"  Verified  : {stats['verified']}\n"
        f"  Review    : {stats['needs_review']}\n"
        f"  Not found : {stats['not_found']}\n"
        f"  Errors    : {stats['errors']}\n"
        f"  Output    : {args.output}"
    )


if __name__ == "__main__":
    main()
