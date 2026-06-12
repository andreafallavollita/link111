"""LinkedIn discovery agent.

Discovers prospect profiles via:
  1. People search by job-title keywords filtered by country and connection degree.
  2. Engagement scraping: extracts commenters from recent posts on target hashtags.

Each prospect is scored 0-100 and inserted into the prospects table if not already
in existing_contacts or already discovered.

LinkedIn Free has aggressive search rate-limits (the so-called 'commercial use
limit'). This script:
  - Caps queries per run.
  - Adds randomized delays.
  - Detects the 'commercial use' banner and stops cleanly.
  - Prefers hashtag engagement (less throttled than people search).

Output table: prospects (status='discovered').
"""

from __future__ import annotations

import argparse
import builtins
import json
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import growth_db as db

# Global stats for filter debugging
FILTER_STATS = {
    "candidates_seen": 0,
    "inserted_new": 0,
    "skipped_score_below_min": 0,
    "skipped_duplicate": 0,
    "skipped_existing_sent": 0,
    "skipped_existing_connected": 0,
    "skipped_existing_pending": 0,
    "skipped_missing_data": 0,
    "skipped_parse_error": 0,
    "skipped_other": 0,
}

# ============================================================
# Console-safe print (LinkedIn names contain non-cp1252 chars)
# ============================================================

def _safe_str(s) -> str:
    if s is None:
        return ""
    text = str(s)
    enc = sys.stdout.encoding or "utf-8"
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode(enc, errors="replace").decode(enc)


_original_print = builtins.print


def print(*args, **kwargs):
    safe_args = [_safe_str(a) for a in args]
    try:
        _original_print(*safe_args, flush=True, **kwargs)
    except Exception:
        try:
            _original_print(*safe_args, **kwargs)
        except Exception:
            pass


# ============================================================
# Configuration
# ============================================================

SCRIPT_DIR = Path(__file__).parent
PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA", "C:\\Temp")) / "LinkedInAutomationProfile"
DEBUG_DIR = Path(os.environ.get("TEMP", "."))

ICP_JOB_TITLES = {
    "cmo_head": {
        "it": ["CMO", "Direttore Marketing", "Responsabile Marketing", "Head of Marketing", "VP Marketing"],
        "en": ["CMO", "Chief Marketing Officer", "VP Marketing", "Marketing Director", "Head of Marketing", "Head of Growth"],
    },
    "ops_automation": {
        "it": ["Marketing Automation", "MarTech", "Marketing Operations", "CRM Manager"],
        "en": ["Marketing Automation Manager", "MarTech Lead", "Marketing Operations", "Growth Engineer", "CRM Manager"],
    },
    "founder_ai": {
        "it": ["Founder AI marketing", "CEO marketing automation", "CTO AI marketing"],
        "en": ["Founder AI marketing", "CEO AI marketing", "Co-founder AI martech", "CTO marketing automation"],
    },
    "consultant_agency": {
        "it": ["Consulente marketing AI", "Consulente AI marketing", "Agency owner marketing AI"],
        "en": ["AI marketing consultant", "MarTech consultant", "Marketing AI consultant", "Fractional CMO AI"],
    },
}

COUNTRY_GEO_URN = {
    "Italy": "103350119",
    "France": "105015875",
    "Germany": "101282230",
    "United Kingdom": "101165590",
    "Spain": "105646813",
    "Netherlands": "102890719",
    "Switzerland": "106693272",
    "Belgium": "100565514",
    "Austria": "103883259",
    "Portugal": "100364837",
    "Ireland": "104738515",
}

COUNTRY_LANG_DEFAULT = {
    "Italy": "it",
    "France": "en",
    "Germany": "en",
    "United Kingdom": "en",
    "Spain": "en",
    "Netherlands": "en",
    "Switzerland": "en",
    "Belgium": "en",
    "Austria": "en",
    "Portugal": "en",
    "Ireland": "en",
}

TARGET_HASHTAGS = [
    "aimarketing",
    "marketingautomation",
    "martech",
    "aitools",
    "growthhacking",
    "contentmarketing",
    "seo",
    "performancemarketing",
    "intelligenzaartificialemarketing",
    "automazionemarketing",
]

POSITIVE_HEADLINE_KEYWORDS = [
    "ai", "artificial intelligence", "intelligenza artificiale", "automation", "automazione",
    "marketing", "growth", "martech", "saas", "cmo", "agency", "agenzia", "founder",
    "ceo", "cto", "consultant", "consulente", "crm", "performance", "seo", "content",
    "demand", "lead generation", "digital", "head of",
]

INDUSTRY_KEYWORDS = [
    "ai", "automation", "martech", "marketing", "saas", "agency", "agenzia",
    "growth", "digital", "consulting", "consulenza", "tech", "intelligence",
    "data", "analytics", "platform", "studio",
]

NEGATIVE_KEYWORDS = [
    "student", "studente", "intern", "stagista", "looking for", "in cerca di",
    "open to work", "seeking", "job seeker",
]

COMMERCIAL_LIMIT_PHRASES = [
    "commercial use limit",
    "limite per uso commerciale",
    "you've reached the monthly limit",
    "hai raggiunto il limite mensile",
]

LOGIN_OK_INDICATORS = ["/feed", "/mynetwork", "/in/", "/home"]
LOGIN_NEEDED_INDICATORS = ["/login", "/signup", "/checkpoint", "/uas/"]


# ============================================================
# Chrome setup
# ============================================================

def build_chrome(profile_dir: Path = PROFILE_DIR) -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--disable-notifications")
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    )
    profile_dir.mkdir(parents=True, exist_ok=True)
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--profile-directory=Default")
    service = ChromeService()
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    driver.implicitly_wait(3)
    return driver


def ensure_logged_in(driver: webdriver.Chrome, max_wait_seconds: int = 600) -> bool:
    print("Checking login status...")
    driver.get("https://www.linkedin.com/feed")
    time.sleep(4)
    url = driver.current_url.lower()
    if any(x in url for x in LOGIN_NEEDED_INDICATORS):
        print("Not logged in. Going to login page...")
        load_dotenv()
        email = os.environ.get("LINKEDIN_EMAIL")
        pwd = os.environ.get("LINKEDIN_PASSWORD")
        driver.get("https://www.linkedin.com/login")
        time.sleep(4)
        if email and pwd:
            try:
                e = driver.find_element(By.CSS_SELECTOR, "input[type='email']")
                p = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
                e.clear(); e.send_keys(email)
                p.clear(); p.send_keys(pwd)
                p.submit()
            except NoSuchElementException:
                print("Could not find email/password inputs; please log in manually.")
        else:
            print("LINKEDIN_EMAIL/PASSWORD not set; please log in manually.")
        print(f"Waiting up to {max_wait_seconds}s for you to complete login (CAPTCHA/2FA)...")
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            time.sleep(5)
            cur = driver.current_url.lower()
            if any(x in cur for x in LOGIN_OK_INDICATORS) and not any(
                x in cur for x in LOGIN_NEEDED_INDICATORS
            ):
                print("Login successful.")
                return True
        print("Login timeout.")
        return False
    print("Already logged in.")
    return True


def detect_commercial_limit(driver: webdriver.Chrome) -> bool:
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    except Exception:
        return False
    return any(phrase in body_text for phrase in COMMERCIAL_LIMIT_PHRASES)


# ============================================================
# Scoring
# ============================================================

def log_candidate_debug(source, name, headline, url, score, min_score, status, reason, debug_enabled=False):
    if not debug_enabled:
        return
    
    # Safe strings
    s_name = name or "N/A"
    s_headline = headline or "N/A"
    s_url = url or "N/A"
    s_score = f"{score}" if score is not None else "N/A"
    
    print(f" [DEBUG] {source:10} | {s_name:20} | Score: {s_score:3} (min {min_score}) | Status: {status:12} | Reason: {reason}")

def print_filter_summary():
    print("\n" + "="*40)
    print("   DISCOVERY FILTER SUMMARY")
    print("="*40)
    for k, v in FILTER_STATS.items():
        print(f" {k:25}: {v}")
    print("="*40 + "\n")

def score_prospect(
    *,
    full_name: str,
    headline: Optional[str],
    company: Optional[str],
    location: Optional[str],
    connection_degree: Optional[int],
    icp_segment: Optional[str],
    source: str,
    language_hint: Optional[str] = None,
) -> tuple[int, dict]:
    breakdown: dict = {}
    headline_low = (headline or "").lower()
    company_low = (company or "").lower()
    location_low = (location or "").lower()

    if any(neg in headline_low for neg in NEGATIVE_KEYWORDS):
        return 0, {"negative_match": True}

    kw_hits = sum(1 for kw in POSITIVE_HEADLINE_KEYWORDS if kw in headline_low)
    kw_score = min(30, kw_hits * 5)
    breakdown["headline_keywords"] = kw_score

    icp_score = 0
    if icp_segment and icp_segment in ICP_JOB_TITLES:
        all_titles = (
            ICP_JOB_TITLES[icp_segment]["it"] + ICP_JOB_TITLES[icp_segment]["en"]
        )
        if any(t.lower() in headline_low for t in all_titles):
            icp_score = 25
        elif any(t.lower().split()[0] in headline_low for t in all_titles if t):
            icp_score = 12
    breakdown["icp_match"] = icp_score

    industry_score = 0
    if any(ind in company_low for ind in INDUSTRY_KEYWORDS):
        industry_score = 15
    elif any(ind in headline_low for ind in INDUSTRY_KEYWORDS):
        industry_score = 8
    breakdown["industry"] = industry_score

    degree_score = 0
    if connection_degree == 2:
        degree_score = 15
    elif connection_degree == 3:
        degree_score = 5
    breakdown["degree"] = degree_score

    lang_score = 0
    if language_hint == "it" or "ital" in location_low or "italy" in location_low:
        lang_score = 10
    breakdown["language"] = lang_score

    source_score = 5 if source.startswith("engagement") else 0
    breakdown["source"] = source_score

    total = kw_score + icp_score + industry_score + degree_score + lang_score + source_score
    return min(100, total), breakdown


def guess_language(full_name: str, headline: Optional[str], location: Optional[str]) -> str:
    txt = f"{full_name} {headline or ''} {location or ''}".lower()
    italian_markers = ["italia", "milano", "roma", "torino", "napoli", "bologna", "firenze", " presso ", "responsabile", "direttore", "consulente"]
    if any(m in txt for m in italian_markers):
        return "it"
    return "en"


# ============================================================
# Search discovery
# ============================================================

def build_search_url(keywords: str, geo_urn: Optional[str], page: int = 1) -> str:
    params = {
        "keywords": keywords,
        "origin": "FACETED_SEARCH",
        "network": '["S","O"]',
    }
    if geo_urn:
        params["geoUrn"] = f'["{geo_urn}"]'
    if page > 1:
        params["page"] = str(page)
    qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"https://www.linkedin.com/search/results/people/?{qs}"


def parse_search_card(card) -> Optional[dict]:
    try:
        is_anchor = (card.tag_name or "").lower() == "a"
        href = ""
        if is_anchor:
            href = card.get_attribute("href") or ""
        else:
            try:
                a = card.find_element(By.CSS_SELECTOR, "a[href*='/in/']")
                href = a.get_attribute("href") or ""
            except NoSuchElementException:
                return None
        if "/in/" not in href:
            return None
        href = href.split("?")[0]

        if is_anchor:
            text = (card.text or "").strip()
        else:
            try:
                a = card.find_element(By.CSS_SELECTOR, "a[href*='/in/']")
                text = (a.text or "").strip()
            except NoSuchElementException:
                text = (card.text or "").strip()
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            return None
        full_name = lines[0]
        if len(full_name) < 3 or len(full_name) > 80:
            return None

        headline = lines[1] if len(lines) >= 2 else ""
        location = lines[2] if len(lines) >= 3 else ""

        degree = None
        deg_text = " ".join(lines).lower()
        if "1�" in deg_text or "1st" in deg_text:
            degree = 1
        elif "2�" in deg_text or "2nd" in deg_text:
            degree = 2
        elif "3�" in deg_text or "3rd" in deg_text:
            degree = 3

        company = ""
        m = re.search(r"\b(?:at|presso|@)\s+(.+)$", headline, re.IGNORECASE)
        if m:
            company = m.group(1).strip()
        title = headline
        if " at " in headline.lower():
            title = re.split(r"\s+at\s+", headline, flags=re.IGNORECASE)[0].strip()
        elif " presso " in headline.lower():
            title = re.split(r"\s+presso\s+", headline, flags=re.IGNORECASE)[0].strip()

        return {
            "full_name": full_name,
            "profile_url": href,
            "headline": headline or None,
            "title": title or None,
            "company": company or None,
            "location": location or None,
            "connection_degree": degree,
        }
    except StaleElementReferenceException:
        return None
    except Exception:
        return None


def collect_search_cards(driver: webdriver.Chrome) -> list:
    """Waits for at least one /in/ link to appear (LinkedIn loads results via
    XHR), then returns result cards. LinkedIn uses random class names (e.g.
    `bd116eb9 _8a8d57fc`) and many duplicated /in/ links per card (profile,
    avatar, mutual connections). We filter for the main profile anchor: the
    one whose text spans multiple lines (name + headline + location)."""
    try:
        WebDriverWait(driver, 15).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")) > 0
        )
    except TimeoutException:
        pass
    for _ in range(4):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.6)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.4)

    anchors = driver.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")
    main_anchors: list = []
    seen_hrefs: set = set()
    for a in anchors:
        try:
            href = a.get_attribute("href") or ""
            if not href:
                continue
            if "/sales/" in href or "/company/" in href:
                continue
            normalized = href.split("?")[0].rstrip("/")
            if normalized in seen_hrefs:
                continue
            text = (a.text or "").strip()
            if "\n" not in text and len(text) < 40:
                continue
            seen_hrefs.add(normalized)
            main_anchors.append(a)
        except WebDriverException:
            continue

    if main_anchors:
        return main_anchors

    if not anchors:
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            debug_html = DEBUG_DIR / f"linkedin_search_debug_{ts}.html"
            debug_png = DEBUG_DIR / f"linkedin_search_debug_{ts}.png"
            debug_html.write_text(driver.page_source, encoding="utf-8")
            try:
                driver.save_screenshot(str(debug_png))
            except WebDriverException:
                pass
            print(f"     [debug] saved {debug_html} and {debug_png}")
        except OSError as exc:
            print(f"     [debug] could not save debug: {exc}")
    return []


def discover_via_search(
    driver: webdriver.Chrome,
    *,
    max_queries: int,
    max_pages_per_query: int,
    min_score: int,
    countries: list[str],
    icp_segments: list[str],
    languages: list[str],
    rng: random.Random,
) -> dict:
    started = db.now_iso()
    queries_run = 0
    total_found = 0
    total_new = 0

    queue: list[tuple[str, str, str, str]] = []
    for icp in icp_segments:
        for lang in languages:
            if lang not in ICP_JOB_TITLES.get(icp, {}):
                continue
            for kw in ICP_JOB_TITLES[icp][lang]:
                for country in countries:
                    queue.append((icp, lang, kw, country))
    rng.shuffle(queue)
    print(f"  Search queue: {len(queue)} potential (icp x lang x keyword x country)")

    with db.db_session() as conn:
        for icp, lang, keyword, country in queue:
            if queries_run >= max_queries:
                break
            geo = COUNTRY_GEO_URN.get(country)
            query_label = f"[{icp}|{lang}|{country}] {keyword}"
            print(f"  -> Query {queries_run + 1}/{max_queries}: {query_label}")
            for page in range(1, max_pages_per_query + 1):
                url = build_search_url(keyword, geo, page)
                try:
                    driver.get(url)
                except WebDriverException as exc:
                    print(f"     [error] navigation failed: {exc}")
                    db.log_discovery_run(
                        conn, source="search", query=query_label, page=page,
                        found_count=0, new_count=0, started_at=db.now_iso(),
                        status="error", error_message=str(exc),
                    )
                    break
                time.sleep(rng.uniform(3, 6))

                if detect_commercial_limit(driver):
                    print("     [STOP] Commercial use limit reached on search.")
                    db.log_discovery_run(
                        conn, source="search", query=query_label, page=page,
                        found_count=0, new_count=0, started_at=db.now_iso(),
                        status="rate_limited",
                    )
                    return {
                        "queries_run": queries_run,
                        "total_found": total_found,
                        "total_new": total_new,
                        "stopped_reason": "commercial_limit",
                        "started_at": started,
                    }

                for _ in range(3):
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
                    time.sleep(0.8)
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(0.8)

                cards = collect_search_cards(driver)
                if not cards:
                    print(f"     page {page}: no cards found, moving on")
                    break

                page_found = 0
                page_new = 0
                for card in cards:
                    info = parse_search_card(card)
                    if not info:
                        continue
                    page_found += 1
                    profile_id = db.extract_profile_id(info["profile_url"])
                    if db.is_existing_contact(
                        conn,
                        full_name=info["full_name"],
                        profile_url=info["profile_url"],
                        profile_id=profile_id,
                    ):
                        continue
                    lang_guess = guess_language(
                        info["full_name"], info["headline"], info["location"]
                    )
                    score, breakdown = score_prospect(
                        full_name=info["full_name"],
                        headline=info["headline"],
                        company=info["company"],
                        location=info["location"],
                        connection_degree=info["connection_degree"],
                        icp_segment=icp,
                        source="search",
                        language_hint=lang_guess,
                    )
                    if score < min_score:
                        continue
                    inserted = db.insert_prospect(
                        conn,
                        full_name=info["full_name"],
                        profile_url=info["profile_url"],
                        score=score,
                        source="search",
                        source_detail=query_label,
                        headline=info["headline"],
                        company=info["company"],
                        title=info["title"],
                        location=info["location"],
                        country=country,
                        language=lang_guess,
                        connection_degree=info["connection_degree"],
                        icp_segment=icp,
                        score_breakdown=breakdown,
                    )
                    if inserted:
                        page_new += 1
                total_found += page_found
                total_new += page_new
                print(f"     page {page}: {page_found} cards, +{page_new} new prospects")
                db.log_discovery_run(
                    conn, source="search", query=query_label, page=page,
                    found_count=page_found, new_count=page_new,
                    started_at=db.now_iso(), status="ok",
                )
                time.sleep(rng.uniform(8, 14))
            queries_run += 1
            time.sleep(rng.uniform(15, 30))

    return {
        "queries_run": queries_run,
        "total_found": total_found,
        "total_new": total_new,
        "stopped_reason": "queue_exhausted" if queries_run < max_queries else "max_queries",
        "started_at": started,
    }


# ============================================================
# Engagement discovery
# ============================================================

def discover_via_engagement(
    driver: webdriver.Chrome,
    *,
    hashtags: list[str],
    max_posts_per_tag: int,
    max_comments_per_post: int,
    min_score: int,
    rng: random.Random,
    debug_filters: bool = False,
) -> dict:
    started = db.now_iso()
    total_found = 0
    total_new = 0
    tags_processed = 0

    with db.db_session() as conn:
        for tag in hashtags:
            tag_clean = tag.lstrip("#").lower()
            url = f"https://www.linkedin.com/feed/hashtag/?keywords={tag_clean}"
            print(f"  -> Hashtag #{tag_clean}")
            try:
                driver.get(url)
            except WebDriverException as exc:
                print(f"     [error] navigation failed: {exc}")
                continue
            time.sleep(rng.uniform(4, 7))

            if detect_commercial_limit(driver):
                print("     [STOP] Commercial use limit reached on hashtag feed.")
                break

            for _ in range(5):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(rng.uniform(1.2, 2.0))

            post_links: list[str] = []
            try:
                anchors = driver.find_elements(
                    By.CSS_SELECTOR,
                    "a[href*='/feed/update/urn:li:activity:'], a[href*='/posts/']",
                )
                seen = set()
                for a in anchors:
                    href = (a.get_attribute("href") or "").split("?")[0]
                    if href and href not in seen and ("/feed/update/" in href or "/posts/" in href):
                        seen.add(href)
                        post_links.append(href)
                    if len(post_links) >= max_posts_per_tag:
                        break
            except WebDriverException:
                pass

            print(f"     found {len(post_links)} posts to inspect")

            for post_url in post_links[:max_posts_per_tag]:
                try:
                    driver.get(post_url)
                except WebDriverException:
                    continue
                time.sleep(rng.uniform(3, 6))
                if detect_commercial_limit(driver):
                    print("     [STOP] Commercial use limit reached on post.")
                    return {
                        "tags_processed": tags_processed,
                        "total_found": total_found,
                        "total_new": total_new,
                        "stopped_reason": "commercial_limit",
                        "started_at": started,
                    }

                for _ in range(4):
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(rng.uniform(1, 2))
                try:
                    btns = driver.find_elements(
                        By.XPATH,
                        "//button[contains(., 'comment') or contains(., 'commenti') or contains(., 'altri commenti') or contains(., 'more comments')]",
                    )
                    for b in btns[:3]:
                        try:
                            driver.execute_script("arguments[0].click();", b)
                            time.sleep(rng.uniform(1, 2))
                        except WebDriverException:
                            continue
                except WebDriverException:
                    pass

                commenters_seen = set()
                page_found = 0
                page_new = 0

                comment_anchors = driver.find_elements(
                    By.CSS_SELECTOR,
                    "article a[href*='/in/'], .comments-comment-item a[href*='/in/']"
                )
                for a in comment_anchors[: max_comments_per_post * 3]:
                     href = (a.get_attribute("href") or "").split("?")[0]
                     if "/in/" not in href or href in commenters_seen:
                         continue
                     commenters_seen.add(href)

                     name = ""
                     try:
                         span = a.find_element(By.CSS_SELECTOR, "span[aria-hidden='true']")
                         name = (span.text or "").strip()
                     except NoSuchElementException:
                         name = (a.text or "").split("\n")[0].strip()
                     if not name or len(name) < 3:
                         FILTER_STATS["skipped_missing_data"] += 1
                         log_candidate_debug("engagement", name, "", href, None, min_score, "skipped", "missing_full_name", debug_filters)
                         continue
                     headline = ""
                     try:
                         parent = a.find_element(By.XPATH, "./ancestor::article[1] | ./ancestor::*[contains(@class,'comments-comment-item')][1]")
                         for sel in [".comments-post-meta__headline", "[class*='headline']", "[class*='subtitle']"]:
                             try:
                                 h = parent.find_element(By.CSS_SELECTOR, sel)
                                 if h and (h.text or "").strip():
                                     headline = h.text.strip()
                                     break
                             except NoSuchElementException:
                                 continue
                     except (NoSuchElementException, WebDriverException):
                         pass

                     page_found += 1
                     profile_id = db.extract_profile_id(href)
                     if db.is_existing_contact(
                         conn, full_name=name, profile_url=href, profile_id=profile_id
                     ):
                         # Determine why it's existing
                         if db.get_prospect_status(conn, href) == "sent":
                             FILTER_STATS["skipped_existing_sent"] += 1
                             log_candidate_debug("engagement", name, headline, href, None, min_score, "skipped", "already_sent", debug_filters)
                         elif db.get_prospect_status(conn, href) == "already_connected":
                             FILTER_STATS["skipped_existing_connected"] += 1
                             log_candidate_debug("engagement", name, headline, href, None, min_score, "skipped", "already_connected", debug_filters)
                         elif db.get_prospect_status(conn, href) == "already_pending":
                             FILTER_STATS["skipped_existing_pending"] += 1
                             log_candidate_debug("engagement", name, headline, href, None, min_score, "skipped", "already_pending", debug_filters)
                         else:
                             FILTER_STATS["skipped_duplicate"] += 1
                             log_candidate_debug("engagement", name, headline, href, None, min_score, "skipped", "duplicate", debug_filters)
                         continue

                     lang_guess = guess_language(name, headline, None)
                     score, breakdown = score_prospect(
                         full_name=name,
                         headline=headline,
                         company=None,
                         location=None,
                         connection_degree=None,
                         icp_segment=None,
                         source="engagement",
                         language_hint=lang_guess,
                     )
                     if score < min_score:
                         FILTER_STATS["skipped_score_below_min"] += 1
                         log_candidate_debug("engagement", name, headline, href, score, min_score, "skipped", "score_below_min", debug_filters)
                         continue
                     inserted = db.insert_prospect(
                         conn,
                         full_name=name,
                         profile_url=href,
                         score=score,
                         source="engagement_hashtag",
                         source_detail=f"#{tag_clean} | {post_url[-30:]}",
                         headline=headline or None,
                         language=lang_guess,
                         score_breakdown=breakdown,
                     )
                     if inserted:
                         FILTER_STATS["inserted_new"] += 1
                         log_candidate_debug("engagement", name, headline, href, score, min_score, "inserted", "ok", debug_filters)
                         page_new += 1
                     else:
                         FILTER_STATS["skipped_db_insert_error"] += 1
                         log_candidate_debug("engagement", name, headline, href, score, min_score, "skipped", "db_insert_error", debug_filters)
                     if page_found >= max_comments_per_post:
                         break

                total_found += page_found
                total_new += page_new
                print(f"     post: {page_found} commenters, +{page_new} new prospects")
                db.log_discovery_run(
                    conn, source="engagement", query=f"#{tag_clean}", page=None,
                    found_count=page_found, new_count=page_new,
                    started_at=db.now_iso(), status="ok",
                )
                time.sleep(rng.uniform(6, 12))
            tags_processed += 1
            time.sleep(rng.uniform(20, 40))

    return {
        "tags_processed": tags_processed,
        "total_found": total_found,
        "total_new": total_new,
        "stopped_reason": "completed",
        "started_at": started,
    }


# ============================================================
# Main
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["search", "engagement", "both"], default="both")
    parser.add_argument("--max-queries", type=int, default=8,
                        help="Max search queries per run (Free LinkedIn limits hard)")
    parser.add_argument("--max-pages", type=int, default=3,
                        help="Max pages per search query")
    parser.add_argument("--max-hashtags", type=int, default=4,
                        help="Max hashtags to process per run")
    parser.add_argument("--max-posts-per-tag", type=int, default=3)
    parser.add_argument("--max-comments-per-post", type=int, default=15)
    parser.add_argument("--min-score", type=int, default=40)
    parser.add_argument("--countries", default="Italy,Germany,France,United Kingdom,Spain,Netherlands")
    parser.add_argument("--icp", default="cmo_head,ops_automation,founder_ai,consultant_agency")
    parser.add_argument("--languages", default="it,en")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--queue-target", type=int, default=500,
                        help="Stop discovery when queue >= this number (status='discovered')")
    args = parser.parse_args()

    db.init_db()
    rng = random.Random(args.seed) if args.seed else random.Random()
    started = db.now_iso()

    with db.db_session() as conn:
        current_queue = conn.execute(
            "SELECT COUNT(*) AS c FROM prospects WHERE status = 'discovered'"
        ).fetchone()["c"]
    print(f"Current discovered prospects in queue: {current_queue} (target: {args.queue_target})")
    if current_queue >= args.queue_target:
        print("Queue already at/above target. Nothing to do.")
        return 0

    countries = [c.strip() for c in args.countries.split(",") if c.strip()]
    icp_segments = [s.strip() for s in args.icp.split(",") if s.strip()]
    languages = [l.strip() for l in args.languages.split(",") if l.strip()]

    driver = None
    try:
        driver = build_chrome()
        if not ensure_logged_in(driver):
            print("Cannot continue without login.")
            return 2

        if args.mode in ("engagement", "both"):
            print("\n=== ENGAGEMENT DISCOVERY ===")
            tags = TARGET_HASHTAGS[: args.max_hashtags]
            rng.shuffle(tags)
            stats_eng = discover_via_engagement(
                driver,
                hashtags=tags,
                max_posts_per_tag=args.max_posts_per_tag,
                max_comments_per_post=args.max_comments_per_post,
                min_score=args.min_score,
                rng=rng,
                debug_filters=False,
            )
            print(f"  Engagement summary: {stats_eng}")

        with db.db_session() as conn:
            current = conn.execute(
                "SELECT COUNT(*) AS c FROM prospects WHERE status = 'discovered'"
            ).fetchone()["c"]

        if args.mode in ("search", "both") and current < args.queue_target:
            print("\n=== SEARCH DISCOVERY ===")
            stats_search = discover_via_search(
                driver,
                max_queries=args.max_queries,
                max_pages_per_query=args.max_pages,
                min_score=args.min_score,
                countries=countries,
                icp_segments=icp_segments,
                languages=languages,
                rng=rng,
            )
            print(f"  Search summary: {stats_search}")

        with db.db_session() as conn:
            stats = db.stats_overview(conn)
            db.log_run_health(
                conn,
                component="discovery_agent",
                started_at=started,
                status="ok",
                items_processed=stats["prospects_total"],
                extra=stats,
            )

        print("\n=== DISCOVERY DONE ===")
        print(f"  Prospects total:        {stats['prospects_total']}")
        print(f"  In queue (discovered):  {stats['prospects_by_status'].get('discovered', 0)}")
        print(f"  Sent:                   {stats['prospects_by_status'].get('sent', 0)}")
        for icp, c in stats["prospects_by_icp"].items():
            print(f"    - {icp}: {c}")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130
    except Exception as exc:
        print(f"FATAL: {exc}")
        with db.db_session() as conn:
            db.log_run_health(
                conn, component="discovery_agent", started_at=started,
                status="error", error_message=str(exc),
            )
        return 1
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
