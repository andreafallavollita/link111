"""LinkedIn sender v3: send connection requests from the prospects DB queue.

Features beyond send_requests_v2:
  - Reads prospects from SQLite (top-N by score, language priority).
  - Pre-check profile state (skip if connected/pending/follow-only) using same
    logic as v2.
  - Sends WITHOUT note (compatible with LinkedIn Free; preserves the 5/month
    note quota).
  - Time spread: distributes target sends across business hours (9-18 default)
    with randomized jitter. Pattern is far more human than a tight batch.
  - Cap detection: when LinkedIn shows 'You've reached the weekly invitation
    limit' the script marks the cap with a 7-day resume date and exits.
  - CAPTCHA detection: pauses 10 min, retries once, then exits if still blocked.
  - Daily quota tracking in DB (daily_quota table).
  - Status updates in prospects table (sent, already_connected, error, etc.).

Run with --dry-run to validate prospect selection without opening the browser.
"""

from __future__ import annotations

import argparse
import builtins
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import growth_db as db


# ============================================================
# Safe print for Windows console
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


_orig_print = builtins.print


def print(*args, **kwargs):
    safe_args = [_safe_str(a) for a in args]
    try:
        _orig_print(*safe_args, flush=True, **kwargs)
    except Exception:
        try:
            _orig_print(*safe_args, **kwargs)
        except Exception:
            pass


# ============================================================
# Configuration / constants
# ============================================================

SCRIPT_DIR = Path(__file__).parent
PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA", "C:\\Temp")) / "LinkedInAutomationProfile"
DEBUG_DIR = Path(os.environ.get("TEMP", "."))

WEEKLY_CAP_PHRASES = [
    "you've reached the weekly invitation limit",
    "hai raggiunto il limite settimanale di inviti",
    "weekly invitation limit",
    "limite settimanale di inviti",
]
COMMERCIAL_CAP_PHRASES = [
    "commercial use limit",
    "limite per uso commerciale",
]
CAPTCHA_PHRASES = [
    "verify you're a human",
    "verifica di essere",
    "security verification",
    "verifica di sicurezza",
    "are you a robot",
]

LOGIN_OK_INDICATORS = ["/feed", "/mynetwork", "/in/", "/home"]
LOGIN_NEEDED_INDICATORS = ["/login", "/signup", "/checkpoint", "/uas/"]


# ============================================================
# Chrome / login (riuso da v2/discovery)
# ============================================================

def init_driver() -> webdriver.Chrome:
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
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
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
                print("Could not find login fields; please log in manually.")
        else:
            print("LINKEDIN_EMAIL/PASSWORD not set; please log in manually.")
        print(f"Waiting up to {max_wait_seconds}s for login/CAPTCHA/2FA...")
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


# ============================================================
# State detection
# ============================================================

def get_page_text(driver: webdriver.Chrome) -> str:
    try:
        return (driver.page_source or "").lower()
    except WebDriverException:
        return ""


def detect_weekly_cap(driver: webdriver.Chrome) -> bool:
    text = get_page_text(driver)
    return any(p in text for p in WEEKLY_CAP_PHRASES)


def detect_commercial_limit(driver: webdriver.Chrome) -> bool:
    text = get_page_text(driver)
    return any(p in text for p in COMMERCIAL_CAP_PHRASES)


def detect_captcha(driver: webdriver.Chrome) -> bool:
    url = driver.current_url.lower()
    if "/checkpoint" in url or "/challenge" in url:
        return True
    text = get_page_text(driver)
    return any(p in text for p in CAPTCHA_PHRASES)


def _profile_header_scope(driver: webdriver.Chrome):
    """Return the profile header container, NOT the whole page.

    LinkedIn layout: <main> contains the profile header (with the main
    Connect/Follow/Message buttons); the right <aside> contains
    'People you may know' cards each with their own '+ Collegati' quick
    action. We must restrict the action detection to the main profile
    header to avoid matching sidebar buttons belonging to other people.
    """
    try:
        main = driver.find_element(By.CSS_SELECTOR, "main")
        return main
    except WebDriverException:
        return driver


def _top_card_scope(driver: webdriver.Chrome):
    """Return the profile header container that includes name + action buttons row.
    Walks up from h1 until it finds a container with at least 2 action-row buttons
    (Segui, Invia messaggio, Altro, Collegati, etc.). Logs bounding boxes."""
    try:
        main = driver.find_element(By.CSS_SELECTOR, "main")
        deadline = time.time() + 2.0
        while time.time() < deadline:
            best_h1 = None
            for h1 in main.find_elements(By.CSS_SELECTOR, "h1"):
                try:
                    if not h1.is_displayed():
                        continue
                    txt = (h1.text or "").strip()
                    if len(txt.split()) < 2:
                        continue
                    y = h1.location.get("y", 9999)
                    if y > 600:
                        continue
                    best_h1 = h1
                    break
                except WebDriverException:
                    continue

            if best_h1 is None:
                time.sleep(0.2)
                continue

            parent = best_h1
            for _ in range(8):
                try:
                    parent = parent.find_element(By.XPATH, "..")
                except WebDriverException:
                    break
                if not parent.is_displayed():
                    continue
                action_btns = []
                for btn in parent.find_elements(By.CSS_SELECTOR, "button, a, [role='button']"):
                    try:
                        txt = (btn.text or "").strip().lower()
                        aria = (btn.get_attribute("aria-label") or "").lower()
                        combined = f"{txt} {aria}"
                        if any(p in combined for p in [
                            "segui", "seguí", "follow",
                            "invia messaggio", "send message", "message",
                            "collegati", "connect", "invita", "invite",
                            "altro", "more",
                            "altre azioni", "more actions",
                        ]):
                            action_btns.append(btn)
                    except WebDriverException:
                        continue
                if len(action_btns) >= 2:
                    py = parent.location.get("y", 0)
                    if py < 600:
                        try:
                            sz = parent.size
                            print(f"     [DETAIL] top_card_location=x={parent.location['x']},y={parent.location['y']},w={sz['width']},h={sz['height']}")
                            print(f"     [DETAIL] header_name_location=x={best_h1.location['x']},y={best_h1.location['y']}")
                            locs = [f"x={b.location['x']},y={b.location['y']}" for b in action_btns[:5]]
                            print(f"     [DETAIL] action_buttons_locations=[{'; '.join(locs)}]")
                        except WebDriverException:
                            pass
                        return parent

            try:
                return best_h1.find_element(By.XPATH, "..")
            except WebDriverException:
                pass
            time.sleep(0.2)
        return main
    except WebDriverException:
        return driver


def _is_button_in_real_top_card(button, top_card, target_name: str = "") -> tuple[bool, str]:
    """Validate that a button belongs to the real profile action row.
    Uses geometric proximity + descendant check + name validation + aside rejection.
    Reasons: VALID, VALID_NEAR_HEADER, OUTSIDE_TOP_CARD,
    DIFFERENT_PROFILE_CARD, TOO_FAR_FROM_HEADER, IN_SIDEBAR."""
    try:
        # Reject if button is inside an <aside> (LinkedIn sidebar suggestions)
        try:
            walk = button
            for _ in range(10):
                walk = walk.find_element(By.XPATH, "..")
                tag = walk.tag_name.lower()
                if tag == "aside":
                    return False, "IN_SIDEBAR"
                if id(walk) == id(top_card):
                    break
        except WebDriverException:
            pass

        # Check geometric proximity to the header name
        try:
            name_el = top_card.find_element(By.CSS_SELECTOR, "h1")
            name_y = name_el.location.get("y", 0)
            btn_y = button.location.get("y", 0)
            name_x = name_el.location.get("x", 0)
            btn_x = button.location.get("x", 0)
            # Tight threshold: action row is within ~200px of name; 400 is generous
            if abs(btn_y - name_y) > 400:
                return False, "TOO_FAR_FROM_HEADER"
            if btn_x > 1800:
                return False, "IN_SIDEBAR"
        except WebDriverException:
            pass

        # Check the button's ancestor chain doesn't contain a different name h1
        if target_name:
            tn = target_name.lower().strip()
            walk = button
            for _ in range(5):
                try:
                    walk = walk.find_element(By.XPATH, "..")
                except WebDriverException:
                    break
                try:
                    for h1 in walk.find_elements(By.CSS_SELECTOR, "h1"):
                        h1t = (h1.text or "").strip().lower()
                        if len(h1t.split()) >= 2 and tn not in h1t and h1t != tn:
                            return False, "DIFFERENT_PROFILE_CARD"
                except WebDriverException:
                    continue

        # Strong match: button is a descendant of top_card
        try:
            top_id = id(top_card)
            p = button
            for _ in range(10):
                p = p.find_element(By.XPATH, "..")
                if id(p) == top_id:
                    return True, "VALID"
        except WebDriverException:
            pass

        # Accept if geometrically near the header (handles sibling action rows)
        return True, "VALID_NEAR_HEADER"

    except WebDriverException:
        return False, "EXCEPTION"


def check_action_on_profile(driver: webdriver.Chrome) -> str:
    """Returns one of: 'connect', 'pending', 'message', 'follow', 'none'.

    On LinkedIn (2024+):
    - 'In sospeso' / 'Pending' button means invitation already sent.
    - 'Invia messaggio' / 'Send a message' is shown both for 1st-degree and
      for non-connected profiles (free users see it as 'Send a message' too).
      So this alone is not a reliable signal of connection.
    - 'Collegati' / 'Connect' / 'Invita a collegarsi' is the main connect CTA.
    - 'Segui' / 'Follow' is shown on non-connected profiles; the actual
      'Connect' option is hidden inside the More actions (...) menu.
    """
    scope = _profile_header_scope(driver)
    try:
        for el in scope.find_elements(By.CSS_SELECTOR, "button, a, [role='button']"):
            try:
                txt = (el.text or "").strip().lower()
                aria = (el.get_attribute("aria-label") or "").lower()
            except WebDriverException:
                continue
            if (
                "in sospeso" in txt
                or "in sospeso" in aria
                or "pending" in txt
                or "pending" in aria
            ):
                return "pending"
    except WebDriverException:
        pass
    try:
        for el in scope.find_elements(By.CSS_SELECTOR, "button, a"):
            try:
                aria = (el.get_attribute("aria-label") or "").lower()
                txt = (el.text or "").strip().lower()
            except WebDriverException:
                continue
            if (
                ("invita" in aria and "collegarsi" in aria)
                or ("invite" in aria and ("connect" in aria or "to connect" in aria))
                or txt in ("collegati", "connetti", "connect", "+ collegati", "+ connect")
            ):
                return "connect"
    except WebDriverException:
        pass
    try:
        for btn in scope.find_elements(By.TAG_NAME, "button"):
            try:
                aria = (btn.get_attribute("aria-label") or "").lower()
                txt = (btn.text or "").strip().lower()
            except WebDriverException:
                continue
            if aria.startswith("segui") or aria.startswith("follow "):
                return "follow"
            if txt in ("segui", "follow", "+ segui", "+ follow"):
                return "follow"
    except WebDriverException:
        pass
    return "none"


def _extract_profile_slug(url: str) -> str:
    m = re.search(r'/in/([^/?]+)', url)
    return m.group(1).lower() if m else ""


def _get_profile_header_name(driver: webdriver.Chrome) -> str | None:
    deadline = time.time() + 3.0
    while time.time() < deadline:
        scope = _profile_header_scope(driver)
        selectors = [
            "h1",
            "h1.text-heading-xlarge",
            "h2.text-heading-xlarge",
            "*[class*='text-heading']",
            "section h1",
            "div[class*='mt2'] h1",
            "div[class*='ph5'] h1",
        ]
        for sel in selectors:
            try:
                for el in scope.find_elements(By.CSS_SELECTOR, sel):
                    txt = (el.text or "").strip()
                    if txt and len(txt.split()) >= 2:
                        return txt
            except WebDriverException:
                continue
        time.sleep(0.3)
    return None


def _get_modal_target_name(driver: webdriver.Chrome, modal_text: str | None = None) -> str | None:
    if modal_text:
        name = _extract_name_from_modal_text(modal_text)
        if name:
            return name
    try:
        text = (driver.page_source or "").lower()
        return _extract_name_from_modal_text(text)
    except WebDriverException:
        pass
    return None


def _verify_profile_match(driver: webdriver.Chrome, target_name: str, target_url: str) -> tuple[str, str]:
    """Returns (status, detail) where status is:
    URL_MATCH — slugs match, profile is the right person.
    URL_REDIRECT_CANDIDATE — slugs differ but page is a valid /in/ profile.
    URL_INVALID — not a profile page (feed, search, login, etc)."""
    current_url = driver.current_url.lower()
    target_slug = _extract_profile_slug(target_url)
    current_slug = _extract_profile_slug(current_url)
    header_name = _get_profile_header_name(driver)
    detail = f"target_slug={target_slug} current_slug={current_slug} header_name={header_name}"

    if not current_slug or "/in/" not in current_url:
        return "URL_INVALID", f"NOT_PROFILE_PAGE {detail}"

    if target_slug and current_slug and target_slug == current_slug:
        name_ok = False
        if header_name and target_name:
            hn = header_name.lower().strip()
            tn = target_name.lower().strip()
            name_ok = (tn in hn) or (hn in tn)
        if name_ok:
            return "URL_MATCH", f"MATCH {detail}"
        if header_name is None:
            return "URL_MATCH", f"URL_MATCH_HEADER_MISSING {detail}"
        return "URL_MATCH", f"PARTIAL_MATCH (slug ok, name not found in header: expected='{target_name}') {detail}"

    return "URL_REDIRECT_CANDIDATE", f"REDIRECT {detail}"


def open_more_actions_menu(driver: webdriver.Chrome, *, dry_run: bool = False) -> bool:
    """Click the '...' More actions button on the profile top card. Returns True
    if a dropdown was opened (a menu with Connect/Collegati should be visible
    afterwards)."""
    scope = _top_card_scope(driver)
    candidates = []
    try:
        candidates.extend(scope.find_elements(By.CSS_SELECTOR, "button, a, [role='button']"))
    except WebDriverException:
        pass
    for btn in candidates:
        try:
            aria = (btn.get_attribute("aria-label") or "").lower()
            txt = (btn.text or "").strip().lower()
        except WebDriverException:
            continue
        if (
            "altre azioni" in aria
            or "more actions" in aria
            or "more actions" in txt
            or "altre azioni" in txt
            or "more" == aria
            or aria.startswith("more")
            or aria.startswith("altre")
            or aria.startswith("altro")
            or aria.startswith("other actions")
            or "overflow" in aria
            or txt in ("...", "•••", "···", "altro", "more")
        ):
            try:
                if btn.is_displayed() and btn.is_enabled():
                    loc = btn.location
                    size = btn.size
                    print(f"     [DETAIL] more_button_location=x={loc['x']},y={loc['y']},w={size['width']},h={size['height']}")
                    print(f"     [DETAIL] more_button_scope=TOP_CARD")
                    if dry_run:
                        print(f"     [DETAIL] dry-run: would click 'Altro' in TOP_CARD")
                        return True
                    try:
                        btn.click()
                    except WebDriverException:
                        driver.execute_script("arguments[0].click();", btn)
                    time.sleep(0.8)
                    return True
            except WebDriverException:
                continue
    return False


def _find_visible_menu_after_click(driver: webdriver.Chrome):
    """Find the first visible div[role='menu'] that appeared after a click."""
    try:
        time.sleep(0.5)
        for menu in driver.find_elements(By.CSS_SELECTOR, "div[role='menu']"):
            try:
                if menu.is_displayed():
                    return menu
            except WebDriverException:
                continue
    except WebDriverException:
        pass
    return None


def _close_dropdown(driver: webdriver.Chrome):
    """Close an open dropdown by pressing Escape."""
    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.3)
    except WebDriverException:
        pass


def _wait_for_modal(driver: webdriver.Chrome):
    """Wait up to 5s for the modal popup, return its container element.
    Falls back to Shadow DOM walk if standard selectors fail."""
    deadline = time.time() + 3.0
    while time.time() < deadline:
        # Phase 1: standard selectors
        for sel in [
            "div[role='dialog']",
            "artdeco-modal",
            "[class*='artdeco-modal']",
            "section[role='dialog']",
            "div[class*='modal']",
        ]:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if el.is_displayed():
                            return el
                    except WebDriverException:
                        continue
            except WebDriverException:
                continue

        # Phase 2: Shadow DOM walk via JS
        try:
            result = driver.execute_script(r"""
                function norm(s) {
                    return (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                }
                function walk(root) {
                    if (!root || !root.querySelectorAll) return null;
                    var all = root.querySelectorAll('*');
                    for (var i = 0; i < all.length; i++) {
                        var e = all[i];
                        var txt = norm(e.innerText || e.textContent);
                        if (txt.indexOf('invia senza nota') >= 0 || txt.indexOf('send without a note') >= 0 ||
                            txt.indexOf('aggiungi una nota') >= 0 || txt.indexOf('add a note') >= 0) {
                            var p = e;
                            while (p && p !== document.body && p !== document.documentElement) {
                                var role = p.getAttribute ? p.getAttribute('role') : null;
                                var cls = typeof p.className === 'string' ? p.className : '';
                                var tag = (p.tagName || '').toLowerCase();
                                if (role === 'dialog' || role === 'alertdialog' ||
                                    cls.indexOf('artdeco-modal') >= 0 || cls.indexOf('modal') >= 0 ||
                                    tag === 'artdeco-modal') {
                                    return p;
                                }
                                p = p.parentElement || p.parentNode;
                            }
                            return e;
                        }
                        if (e.shadowRoot) {
                            var r = walk(e.shadowRoot);
                            if (r) return r;
                        }
                    }
                    return null;
                }
                return walk(document);
            """)
            if result:
                return result
        except WebDriverException:
            pass

        time.sleep(0.3)

    return None


def _close_modal(driver: webdriver.Chrome):
    """Close any open modal. Works for standard and Shadow DOM popups."""
    try:
        # 1) Standard close buttons
        for sel in [
            "button[aria-label='Chiudi']",
            "button[aria-label='Close']",
            ".artdeco-modal__dismiss",
            "button[class*='dismiss']",
        ]:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed():
                    btn.click()
                    time.sleep(0.3)
                    if not _modal_button_present(driver):
                        return
            except WebDriverException:
                continue

        # 2) JS shadow DOM walk for X / Chiudi buttons
        driver.execute_script(r"""
            (function() {
                function norm(s) { return (s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
                function walk(root) {
                    if (!root || !root.querySelectorAll) return;
                    var all = root.querySelectorAll('*');
                    for (var i = 0; i < all.length; i++) {
                        var e = all[i];
                        var tag = (e.tagName || '').toLowerCase();
                        if (tag === 'button' || tag === 'a' || e.getAttribute('role') === 'button') {
                            var txt = norm(e.innerText || e.textContent);
                            var aria = norm(e.getAttribute('aria-label'));
                            if (aria === 'chiudi' || aria === 'close' || txt === 'x' || txt === '\u00d7') {
                                try { e.click(); return; } catch(err) {}
                            }
                        }
                        if (e.shadowRoot) walk(e.shadowRoot);
                    }
                }
                walk(document);
            })();
        """)
        time.sleep(0.3)

        # 3) ESC fallback (press twice)
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.3)
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.3)
    except WebDriverException:
        pass


def _close_pre_existing_modal(driver: webdriver.Chrome):
    """Quickly close any leftover modal before processing a new profile."""
    try:
        for sel in ["div[role='dialog']", "artdeco-modal", "[class*='artdeco-modal']"]:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed():
                        _close_modal(driver)
                        print("     [DETAIL] PRE_EXISTING_MODAL_CLOSED")
                        return
            except WebDriverException:
                continue
    except WebDriverException:
        pass


def _extract_name_from_modal_text(text: str) -> str | None:
    text_lower = text.lower()
    for pat in [
        r'invito a\s+(.+?)\s+aggiungendo',
        r'invito a\s+(.+?)\.',
        r'invito a\s+(.+?)$',
        r'connect invitation to\s+(.+?)\s+by adding',
        r'invitation to\s+(.+?)\s+by adding',
        r'invitation to\s+(.+?)\.',
        r'personalizza il tuo invito a\s+(.+?)\s+aggiungendo',
        r'customize your invitation to\s+(.+?)\s+adding',
        r'aggiungere una nota al tuo invito a\s+(.+?)(?:\s*<|\s*\.)',
        r'add a note to your invitation to\s+(.+?)(?:\s*<|\s*\.)',
    ]:
        m = re.search(pat, text_lower)
        if m:
            name = m.group(1).strip()
            name = re.sub(r'\s+', ' ', name).strip()
            name = name.replace('\u2019', "'").replace('\u2018', "'")
            if name and len(name) > 2:
                return name
    return None


def _debug_modal(driver: webdriver.Chrome, slug: str, target_name: str = ""):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = DEBUG_DIR / f"debug_modal_{slug}_{ts}"

    print(f"     [DEBUG_MODAL] current_url={driver.current_url}")
    print(f"     [DEBUG_MODAL] window_handles={driver.window_handles}")
    try:
        print(f"     [DEBUG_MODAL] active_window_title={driver.title}")
    except WebDriverException:
        print(f"     [DEBUG_MODAL] active_window_title=ERROR")

    all_visible_buttons = []
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, "button, a, [role='button']"):
            try:
                if el.is_displayed():
                    txt = (el.text or "").strip()[:80]
                    all_visible_buttons.append(txt)
            except WebDriverException:
                continue
    except WebDriverException:
        pass
    print(f"     [DEBUG_MODAL] visible_buttons_text={all_visible_buttons}")

    candidates = []
    for sel in [
        "div[role='dialog']",
        "artdeco-modal",
        "[class*='artdeco-modal']",
        "section[role='dialog']",
        "div[class*='modal']",
    ]:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        candidates.append(f"{sel}:{el.text[:50]}")
                except WebDriverException:
                    continue
        except WebDriverException:
            continue
    print(f"     [DEBUG_MODAL] modal_candidate_count={len(candidates)}")
    for c in candidates:
        print(f"     [DEBUG_MODAL]   candidate={c}")

    modal_text_raw = None
    try:
        modal_text_raw = driver.execute_script(r"""
            function norm(s) { return (s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
            function walk(root) {
                if (!root || !root.querySelectorAll) return null;
                var all = root.querySelectorAll('*');
                for (var i = 0; i < all.length; i++) {
                    var e = all[i];
                    var txt = norm(e.innerText || e.textContent);
                    if (txt.indexOf('invia senza nota') >= 0 || txt.indexOf('send without a note') >= 0 ||
                        txt.indexOf('aggiungi una nota') >= 0 || txt.indexOf('add a note') >= 0) {
                        var p = e;
                        while (p && p !== document.body && p !== document.documentElement) {
                            var role = p.getAttribute ? p.getAttribute('role') : null;
                            var cls = typeof p.className === 'string' ? p.className : '';
                            var tag = (p.tagName || '').toLowerCase();
                            if (role === 'dialog' || role === 'alertdialog' ||
                                cls.indexOf('artdeco-modal') >= 0 || cls.indexOf('modal') >= 0 ||
                                tag === 'artdeco-modal') {
                                return p.innerText || p.textContent || '';
                            }
                            p = p.parentElement || p.parentNode;
                        }
                        return e.innerText || e.textContent || '';
                    }
                    if (e.shadowRoot) {
                        var r = walk(e.shadowRoot);
                        if (r) return r;
                    }
                }
                return null;
            }
            return walk(document);
        """)
    except WebDriverException:
        pass

    print(f"     [DEBUG_MODAL] modal_text_raw={repr(modal_text_raw[:1000]) if modal_text_raw else 'None'}")

    if modal_text_raw:
        modal_name = _extract_name_from_modal_text(modal_text_raw)
        print(f"     [DEBUG_MODAL] modal_name_detected={modal_name}")
    else:
        print(f"     [DEBUG_MODAL] modal_name_detected=None")

    try:
        driver.save_screenshot(str(base.with_suffix(".png")))
        print(f"     [DEBUG_MODAL] screenshot_saved={base.name}.png")
    except WebDriverException:
        pass
    try:
        full_html = driver.page_source or ""
        (base.with_suffix(".html")).write_text(full_html, encoding="utf-8")
        print(f"     [DEBUG_MODAL] page_html_saved={base.name}.html")
    except WebDriverException:
        pass
    if modal_text_raw:
        try:
            (base.with_suffix(".txt")).write_text(modal_text_raw, encoding="utf-8")
            print(f"     [DEBUG_MODAL] modal_text_saved={base.name}.txt")
        except Exception:
            pass


def _debug_action_state(driver: webdriver.Chrome, prospect, target_name: str):
    pid = int(prospect["id"])
    url = prospect["profile_url"]
    print(f"\n{'='*60}")
    print(f"DEBUG ACTION STATE — #{pid} {target_name}")
    print(f"{'='*60}")
    print(f"  target_name={target_name}")
    print(f"  target_url={url}")

    driver.get(url)
    time.sleep(3)
    current_url = driver.current_url
    print(f"  current_url={current_url}")
    slug = _extract_profile_slug(current_url)
    target_slug = _extract_profile_slug(url)
    print(f"  target_slug={target_slug}")
    print(f"  current_slug={slug}")

    match_status, detail = _verify_profile_match(driver, target_name, url)
    print(f"  profile_match_status={match_status} ({detail})")

    top_card = _top_card_scope(driver)
    try:
        tc_loc = top_card.location
        tc_sz = top_card.size
        print(f"  top_card_location=x={tc_loc['x']},y={tc_loc['y']},w={tc_sz['width']},h={tc_sz['height']}")
    except (WebDriverException, AttributeError):
        print(f"  top_card_location=NOT_FOUND (fallback to page root)")

    # Print action buttons found inside top_card
    all_action_btns = []
    # Use top_card as search root if it's a valid element, else fallback to driver
    search_root = driver
    try:
        _ = top_card.location
        search_root = top_card
    except AttributeError:
        pass
    try:
        for el in search_root.find_elements(By.CSS_SELECTOR, "button, a, [role='button']"):
            try:
                txt = (el.text or "").strip()
                aria = (el.get_attribute("aria-label") or "").strip()
                loc = el.location
                sz = el.size
                displayed = el.is_displayed()
                enabled = el.is_enabled()
                all_action_btns.append({
                    "text": txt, "aria": aria,
                    "x": loc["x"], "y": loc["y"],
                    "w": sz["width"], "h": sz["height"],
                    "displayed": displayed, "enabled": enabled,
                })
            except WebDriverException:
                continue
    except WebDriverException:
        pass

    print(f"  action_row_buttons:")
    for b in all_action_btns:
        print(f"    - text={b['text']!r} aria={b['aria']!r} "
              f"loc=({b['x']},{b['y']}) size=({b['w']}x{b['h']}) "
              f"displayed={b['displayed']} enabled={b['enabled']}")

    # Determine action state from top-card buttons only (prioritize connect over pending)
    candidate_btns = [b for b in all_action_btns if b["displayed"] and b["enabled"]]
    action = "no_safe_action"
    strategy = "no_candidate_buttons"
    evidence_text = ""
    evidence_location = None
    evidence_scope = ""

    for b in candidate_btns:
        txt = b["text"].lower().strip()
        aria = b["aria"].lower().strip()
        # Phase 1: Collegati / Connect
        if ("collegati" in txt and txt not in ("+ collegati", "+ connect")) or \
           txt in ("collegati", "connetti", "connect") or \
           "invita" in aria or "collegarsi" in aria:
            action = "connect"
            strategy = "connect_button_top_card"
            evidence_text = b["text"]
            evidence_location = (b["x"], b["y"])
            evidence_scope = "TOP_CARD_ACTION_ROW"
            break

    if action != "connect":
        for b in candidate_btns:
            txt = b["text"].lower().strip()
            aria = b["aria"].lower().strip()
            # Phase 2: Follow / Segui
            if aria.startswith("segui") or aria.startswith("follow ") or \
               txt in ("segui", "follow", "+ segui", "+ follow"):
                action = "follow"
                strategy = "follow_button_top_card"
                evidence_text = b["text"]
                evidence_location = (b["x"], b["y"])
                evidence_scope = "TOP_CARD_ACTION_ROW"
                break

    if action not in ("connect", "follow"):
        for b in candidate_btns:
            txt = b["text"].lower().strip()
            aria = b["aria"].lower().strip()
            # Phase 3: Pending — only if no connect/follow found
            for phrase in ("in attesa", "pending", "annulla invito", "cancel invitation",
                           "invito inviato", "invitation sent"):
                if phrase in txt or phrase in aria:
                    action = "pending"
                    strategy = "pending_phrase_top_card"
                    evidence_text = b["text"]
                    evidence_location = (b["x"], b["y"])
                    evidence_scope = "TOP_CARD_ACTION_ROW"
                    break
            if action == "pending":
                break

    print(f"  detected_action={action}")
    print(f"  action_detection_strategy={strategy}")
    print(f"  action_evidence_text={evidence_text!r}")
    if evidence_location:
        print(f"  action_evidence_location=x={evidence_location[0]},y={evidence_location[1]}")
    print(f"  action_evidence_scope={evidence_scope}")
    print(f"{'='*60}")
    print(f"[DEBUG-ACTION-STATE] Completed. No clicks, no sends, no DB changes.")
    print(f"Premere INVIO per chiudere il browser...")
    input()


def _dropdown_roots(driver: webdriver.Chrome):
    roots = []
    seen = set()
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, "div[role='menu'], div[role='dialog'], div[role='listbox'], div.artdeco-popover, div.artdeco-popover--menu"):
            key = id(el)
            if key not in seen:
                roots.append(el)
                seen.add(key)
    except WebDriverException:
        pass
    return roots


def _is_connect_item(el) -> bool:
    try:
        txt = (el.text or "").strip().lower()
        aria = (el.get_attribute("aria-label") or "").lower()
    except WebDriverException:
        return False
    full = f"{txt} {aria}"
    if txt in ("collegati", "connetti", "connect", "invita a collegarsi", "invite to connect"):
        return True
    if "collegati" in txt and len(txt) < 40:
        return True
    if "connect" in txt and len(txt) < 40:
        return True
    if ("invita" in aria and "collegarsi" in aria) or ("invite" in aria and "connect" in aria):
        return True
    return "collegati" in full and "connetti" not in full


def click_connect_in_dropdown(driver: webdriver.Chrome) -> bool:
    """Inside an open More actions dropdown, click the Collegati/Connect item."""
    roots = _dropdown_roots(driver)
    candidates = []
    for root in roots:
        try:
            for el in root.find_elements(By.CSS_SELECTOR, "div[role='button'], button, a, li[role='menuitem']"):
                if _is_connect_item(el):
                    candidates.append(el)
        except WebDriverException:
            continue
    for el in candidates:
        try:
            if el.is_displayed() and el.is_enabled():
                try:
                    el.click()
                except WebDriverException:
                    driver.execute_script("arguments[0].click();", el)
                time.sleep(0.5)
                return True
        except WebDriverException:
            continue
    return False


def click_connect_on_profile(driver: webdriver.Chrome, *, dry_run: bool = False,
                              target_name: str = "") -> tuple[bool, str]:
    """Returns (success, detail). detail indicates scope and action.
    Uses _is_button_in_real_top_card() to reject buttons from suggestion cards,
    sidebar, or other profiles."""
    scope = _top_card_scope(driver)

    # Phase A: find a direct Collegati button inside the real top card
    direct_connect = None
    for el in scope.find_elements(By.CSS_SELECTOR, "button, a, [role='button']"):
        try:
            txt = (el.text or "").strip().lower()
            aria = (el.get_attribute("aria-label") or "").lower()
        except WebDriverException:
            continue
        full = f"{txt} {aria}"
        if (
            ("invita" in aria and "collegarsi" in aria)
            or ("invite" in aria and "connect" in aria)
            or txt in ("collegati", "connetti", "connect", "+ collegati", "+ connect")
            or ("collegati" in txt and len(txt) < 40)
        ):
            is_valid, reason = _is_button_in_real_top_card(el, scope, target_name)
            if is_valid:
                direct_connect = el
                break
            else:
                try:
                    loc = el.location
                    print(f"     [DETAIL] rejected_button_text={txt}")
                    print(f"     [DETAIL] rejected_button_location=x={loc['x']},y={loc['y']}")
                    print(f"     [DETAIL] rejected_reason={reason}")
                except WebDriverException:
                    pass

    if direct_connect is not None:
        try:
            if direct_connect.is_displayed() and direct_connect.is_enabled():
                loc = direct_connect.location
                size = direct_connect.size
                print(f"     [DETAIL] connect_button_location=x={loc['x']},y={loc['y']},w={size['width']},h={size['height']}")
                print(f"     [DETAIL] connect_button_scope=TOP_CARD")
                print(f"     [DETAIL] connect_button_text='{(direct_connect.text or '').strip()}'")
                if dry_run:
                    print(f"     [DETAIL] dry-run: would click TOP_CARD 'Collegati'")
                    return True, "dry_run_top_card"
                try:
                    direct_connect.click()
                except WebDriverException:
                    driver.execute_script("arguments[0].click();", direct_connect)
                return True, "clicked_top_card"
        except WebDriverException:
            pass

    # Phase B: no direct Collegati — find and click "Altro" inside real top card
    more_btn = None
    for btn in scope.find_elements(By.CSS_SELECTOR, "button, a, [role='button']"):
        try:
            aria = (btn.get_attribute("aria-label") or "").lower()
            txt = (btn.text or "").strip().lower()
        except WebDriverException:
            continue
        if (
            "altre azioni" in aria
            or "more actions" in aria
            or "more actions" in txt
            or "altre azioni" in txt
            or "more" == aria
            or aria.startswith("more")
            or aria.startswith("altre")
            or aria.startswith("altro")
            or aria.startswith("other actions")
            or "overflow" in aria
            or txt in ("...", "•••", "···", "altro", "more")
        ):
            is_valid, reason = _is_button_in_real_top_card(btn, scope, target_name)
            if is_valid and btn.is_displayed() and btn.is_enabled():
                more_btn = btn
                break
            else:
                try:
                    loc = btn.location
                    print(f"     [DETAIL] rejected_more_button_text={txt}")
                    print(f"     [DETAIL] rejected_more_button_location=x={loc['x']},y={loc['y']}")
                    print(f"     [DETAIL] rejected_more_reason={reason}")
                except WebDriverException:
                    pass

    if more_btn is None:
        return False, "more_button_not_in_top_card"

    loc = more_btn.location
    size = more_btn.size
    print(f"     [DETAIL] more_button_location=x={loc['x']},y={loc['y']},w={size['width']},h={size['height']}")
    print(f"     [DETAIL] more_button_scope=TOP_CARD")

    # Click More (always real click, even in dry-run, to verify the dropdown)
    try:
        more_btn.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", more_btn)
    time.sleep(0.8)

    # Find the first visible menu that appeared after the click
    menu_root = _find_visible_menu_after_click(driver)
    if menu_root is None:
        if dry_run:
            _close_dropdown(driver)
        return False, "no_visible_menu_after_more_click"

    # Look for Collegati in this menu ONLY
    collegati_item = None
    for el in menu_root.find_elements(By.CSS_SELECTOR, "div[role='button'], button, a, li[role='menuitem']"):
        if _is_connect_item(el) and el.is_displayed():
            collegati_item = el
            break

    if collegati_item is None:
        if dry_run:
            _close_dropdown(driver)
        return False, "collegati_not_in_dropdown"

    if dry_run:
        print(f"     [DETAIL] dry-run: found 'Collegati' in dropdown after TOP_CARD 'Altro'")
        print(f"     [DETAIL] dry-run: would click TOP_CARD_DROPDOWN 'Collegati'")
        _close_dropdown(driver)
        return True, "dry_run_dropdown"

    # Click Collegati in the TOP_CARD_DROPDOWN menu
    try:
        collegati_item.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", collegati_item)
    print(f"     [DETAIL] connect_button_scope=TOP_CARD_DROPDOWN")
    return True, "clicked_dropdown"


def _modal_button_present(driver: webdriver.Chrome) -> bool:
    try:
        return bool(driver.execute_script(
            r"""
            (function() {
                function norm(s) {
                    return (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                }
                function walk(root, out) {
                    if (!root || !root.querySelectorAll) return out;
                    var all = root.querySelectorAll('*');
                    for (var i = 0; i < all.length; i++) {
                        var e = all[i];
                        out.push(e);
                        if (e.shadowRoot) walk(e.shadowRoot, out);
                    }
                    return out;
                }
                var out = walk(document, []);
                for (var i = 0; i < out.length; i++) {
                    var e = out[i];
                    var tag = (e.tagName || '').toLowerCase();
                    var txt = norm(e.innerText || e.textContent);
                    var aria = norm(e.getAttribute('aria-label'));
                    if (
                        txt === 'invia senza nota' ||
                        txt === 'send without a note' ||
                        txt === 'invia ora' ||
                        txt === 'send now' ||
                        txt === 'invia' ||
                        txt === 'send' ||
                        aria === 'invia senza nota' ||
                        aria === 'send without a note' ||
                        aria === 'invia' ||
                        aria === 'send'
                    ) {
                        return true;
                    }
                }
                return false;
            })();
            """
        ))
    except WebDriverException:
        return False


def _click_modal_send_button_js(driver: webdriver.Chrome) -> bool:
    try:
        return bool(driver.execute_script(
            r"""
            (function() {
                function norm(s) {
                    return (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                }
                function isClickableCandidate(e) {
                    var tag = (e.tagName || '').toLowerCase();
                    return tag === 'button' || tag === 'a' || e.getAttribute('role') === 'button';
                }
                function clickNode(e) {
                    try {
                        e.click();
                        return true;
                    } catch (err) {}
                    try {
                        e.dispatchEvent(new MouseEvent('click', {
                            bubbles: true,
                            cancelable: true,
                            view: window
                        }));
                        return true;
                    } catch (err) {}
                    return false;
                }
                function walk(root, out) {
                    if (!root || !root.querySelectorAll) return out;
                    var all = root.querySelectorAll('*');
                    for (var i = 0; i < all.length; i++) {
                        var e = all[i];
                        out.push(e);
                        if (e.shadowRoot) walk(e.shadowRoot, out);
                    }
                    return out;
                }
                var out = walk(document, []);
                for (var i = 0; i < out.length; i++) {
                    var e = out[i];
                    if (!isClickableCandidate(e)) continue;
                    var txt = norm(e.innerText || e.textContent);
                    var aria = norm(e.getAttribute('aria-label'));
                    if (
                        txt === 'invia senza nota' ||
                        txt === 'send without a note' ||
                        txt === 'invia ora' ||
                        txt === 'send now' ||
                        txt === 'invia' ||
                        txt === 'send' ||
                        aria === 'invia senza nota' ||
                        aria === 'send without a note' ||
                        aria === 'invia' ||
                        aria === 'send'
                    ) {
                        return clickNode(e);
                    }
                }
                return false;
            })();
            """
        ))
    except WebDriverException:
        return False


def _find_modal_iframe(driver: webdriver.Chrome):
    """Return only the iframe that actually contains the send modal."""
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            ifs = driver.find_elements(By.TAG_NAME, "iframe")
        except WebDriverException:
            return None
        for fr in ifs:
            try:
                driver.switch_to.frame(fr)
                if _modal_button_present(driver):
                    return fr
            except WebDriverException:
                pass
            finally:
                try:
                    driver.switch_to.default_content()
                except WebDriverException:
                    pass
        time.sleep(0.5)
    return None


def _find_send_without_note_button(driver: webdriver.Chrome,
                                    modal_container=None) -> tuple:
    """Find the 'Invia senza nota' / 'Send without a note' button.

    Returns (element_or_None, strategy, details_dict).
    Strategies: MODAL_CONTAINER, SPAN_TO_BUTTON, JS_DEEP, GLOBAL_DIAG.
    """
    detail = {
        "found": False,
        "strategy": None,
        "text": None,
        "displayed": False,
        "enabled": False,
        "location": None,
        "size": None,
        "aria_disabled": None,
        "disabled_attr": None,
        "outer_html": None,
    }

    def _fill(el, strategy):
        detail["found"] = True
        detail["strategy"] = strategy
        try:
            detail["text"] = (el.text or "").strip()
            detail["displayed"] = el.is_displayed()
            detail["enabled"] = el.is_enabled()
            loc = el.location
            sz = el.size
            detail["location"] = f"x={loc['x']},y={loc['y']}"
            detail["size"] = f"w={sz['width']},h={sz['height']}"
            detail["aria_disabled"] = el.get_attribute("aria-disabled")
            detail["disabled_attr"] = el.get_attribute("disabled")
            detail["outer_html"] = (el.get_attribute("outerHTML") or "")[:500]
        except WebDriverException:
            pass
        return el, strategy, detail

    # Strategy A: inside modal container
    if modal_container:
        try:
            for btn in modal_container.find_elements(By.CSS_SELECTOR, "button, a, [role='button']"):
                txt = (btn.text or "").strip().lower()
                aria = (btn.get_attribute("aria-label") or "").lower()
                if txt in ("invia senza nota", "send without a note") or aria in ("invia senza nota", "send without a note"):
                    if btn.is_displayed():
                        return _fill(btn, "MODAL_CONTAINER")
        except WebDriverException:
            pass

    # Strategy B: span-to-button
    try:
        for span in driver.find_elements(By.TAG_NAME, "span"):
            stxt = (span.text or "").strip().lower()
            if stxt in ("invia senza nota", "send without a note"):
                parent = span.find_element(By.XPATH, "./ancestor::button")
                if parent and parent.is_displayed():
                    return _fill(parent, "SPAN_TO_BUTTON")
    except WebDriverException:
        pass

    # Strategy C: JavaScript deep search (Shadow DOM)
    try:
        result = driver.execute_script(r"""
            function norm(s) { return (s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
            function walk(root) {
                if (!root || !root.querySelectorAll) return null;
                var all = root.querySelectorAll('*');
                for (var i = 0; i < all.length; i++) {
                    var e = all[i];
                    var tag = (e.tagName || '').toLowerCase();
                    if (tag === 'button' || tag === 'a' || e.getAttribute('role') === 'button') {
                        var txt = norm(e.innerText || e.textContent);
                        var aria = norm(e.getAttribute('aria-label'));
                        if (txt === 'invia senza nota' || txt === 'send without a note' ||
                            aria === 'invia senza nota' || aria === 'send without a note') {
                            return e;
                        }
                    }
                    if (e.shadowRoot) {
                        var r = walk(e.shadowRoot);
                        if (r) return r;
                    }
                }
                return null;
            }
            return walk(document);
        """)
        if result:
            return _fill(result, "JS_DEEP")
    except WebDriverException:
        pass

    # Strategy D: global diagnostic scan
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, "button, a, [role='button']"):
            try:
                txt = (el.text or "").strip().lower()
                aria = (el.get_attribute("aria-label") or "").lower()
                if txt in ("invia senza nota", "send without a note") or aria in ("invia senza nota", "send without a note"):
                    if el.is_displayed():
                        return _fill(el, "GLOBAL_DIAG")
            except WebDriverException:
                continue
    except WebDriverException:
        pass

    return None, "NOT_FOUND", detail


def _check_send_success(driver: webdriver.Chrome) -> bool:
    """Verify if invitation was sent by checking for pending/cancel signals."""
    try:
        scope = _profile_header_scope(driver)
        for el in scope.find_elements(By.CSS_SELECTOR, "button, a, [role='button']"):
            txt = (el.text or "").strip().lower()
            aria = (el.get_attribute("aria-label") or "").lower()
            combined = f"{txt} {aria}"
            for signal in [
                "in sospeso", "pending", "annulla invito", "cancel invitation",
                "invito inviato", "invitation sent",
            ]:
                if signal in combined:
                    return True
    except WebDriverException:
        pass
    return False


def click_send_in_modal(driver: webdriver.Chrome, *,
                        modal_container=None,
                        debug_send_click: bool = False,
                        target_name: str = "") -> str:
    """Click 'Invia senza nota' with 4 attempts and detailed diagnostics.
    Returns 'sent', 'send_click_failed', or 'debug_send_click_skip'."""

    btn, strategy, detail = _find_send_without_note_button(driver, modal_container)

    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        driver.save_screenshot(str(DEBUG_DIR / f"linkedin_before_click_{ts}.png"))
    except WebDriverException:
        pass

    print(f"     [DETAIL] send_button_found={detail['found']}")
    print(f"     [DETAIL] send_button_strategy={detail['strategy']}")
    print(f"     [DETAIL] send_button_text={detail['text']}")
    print(f"     [DETAIL] send_button_displayed={detail['displayed']}")
    print(f"     [DETAIL] send_button_enabled={detail['enabled']}")
    print(f"     [DETAIL] send_button_location={detail['location']}")
    print(f"     [DETAIL] send_button_size={detail['size']}")
    print(f"     [DETAIL] send_button_aria_disabled={detail['aria_disabled']}")
    print(f"     [DETAIL] send_button_disabled_attr={detail['disabled_attr']}")

    if not detail["found"]:
        print(f"     [state] click_send_in_modal=error (send button not found)")
        try:
            full_html = driver.page_source or ""
            (DEBUG_DIR / f"linkedin_nosendbtn_{ts}.html").write_text(full_html, encoding="utf-8")
        except Exception:
            pass
        return "send_click_failed"

    if debug_send_click:
        print("     [debug-send-click] Bottone 'Invia senza nota' trovato. NON clicco.")
        print("     Premi INVIO per chiudere browser...")
        input()
        return "debug_send_click_skip"

    # 4 attempts
    attempts = [
        ("SCROLL_CLICK", lambda: (
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn),
            ActionChains(driver).move_to_element(btn).perform(),
            btn.click(),
        )),
        ("JS_CLICK", lambda: driver.execute_script("arguments[0].click();", btn)),
        ("COORDINATE_CLICK", lambda: (
            ActionChains(driver).move_to_element_with_offset(btn, 0, 0).click().perform(),
        )),
        ("KEYBOARD_ENTER", lambda: (
            btn.send_keys(Keys.ENTER),
        )),
    ]

    for attempt_name, click_fn in attempts:
        try:
            click_fn()
            time.sleep(2)

            if _check_send_success(driver):
                print(f"     [DETAIL] click_attempt={attempt_name}")
                print(f"     [DETAIL] send_confirmation=PENDING_STATE_DETECTED")
                print(f"     [DETAIL] outcome=SENT")
                return "sent"

            if not _modal_button_present(driver):
                if check_action_on_profile(driver) == "pending":
                    print(f"     [DETAIL] click_attempt={attempt_name}")
                    print(f"     [DETAIL] send_confirmation=PENDING_STATE_DETECTED")
                    print(f"     [DETAIL] outcome=SENT")
                    return "sent"
                print(f"     [DETAIL] click_attempt={attempt_name}")
                print(f"     [DETAIL] send_confirmation=MODAL_GONE_BUT_NO_PENDING")
                return "sent"

        except Exception as exc:
            print(f"     [DETAIL] click_attempt={attempt_name} exception={exc.__class__.__name__}:{exc}")
            continue

    # All attempts failed — save debug artifacts
    print(f"     [DETAIL] click_send_in_modal=error (all 4 attempts failed)")
    try:
        ts2 = datetime.now().strftime("%Y%m%d_%H%M%S")
        driver.save_screenshot(str(DEBUG_DIR / f"linkedin_click_fail_{ts2}.png"))
        full_html = driver.page_source or ""
        (DEBUG_DIR / f"linkedin_click_fail_{ts2}.html").write_text(full_html, encoding="utf-8")
        if btn:
            try:
                outer = btn.get_attribute("outerHTML") or ""
                (DEBUG_DIR / f"linkedin_click_fail_{ts2}_btn.html").write_text(outer, encoding="utf-8")
            except Exception:
                pass
    except Exception:
        pass

    return "send_click_failed"


# ============================================================
# Smart name matching
# ============================================================

def _normalize_name(name: str) -> str:
    """Lowercase, remove accents, remove punctuation, collapse spaces."""
    nfkd = unicodedata.normalize("NFKD", name.strip().lower())
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    ascii_str = re.sub(r"[^\w\s]", " ", ascii_str)
    return re.sub(r"\s+", " ", ascii_str).strip()


def _tokenize(name: str) -> list[str]:
    return _normalize_name(name).split()


def _names_match_safe(target_name: str, modal_name: str,
                      target_slug: str | None = None,
                      current_slug: str | None = None) -> tuple[bool, str]:
    """Returns (is_match, strategy) where strategy is EXACT, FIRST_LAST,
    ORDERED_SUBSET, SLUG_TOKEN_MATCH, or NO_MATCH.

    Always returns NO_MATCH if modal_name is None or empty.
    """
    if not modal_name or not modal_name.strip():
        return False, "NO_MATCH"

    target_tokens = _tokenize(target_name)
    modal_tokens = _tokenize(modal_name)

    if not target_tokens or not modal_tokens:
        return False, "NO_MATCH"

    target_norm = _normalize_name(target_name)
    modal_norm = _normalize_name(modal_name)

    # EXACT
    if target_norm == modal_norm:
        return True, "EXACT"

    # FIRST + LAST token must match (handles middle names/patronymics)
    if (target_tokens[0] == modal_tokens[0]
            and target_tokens[-1] == modal_tokens[-1]):
        return True, "FIRST_LAST"

    # ORDERED_SUBSET: all target tokens appear in modal in the same order
    it = iter(modal_tokens)
    matched = 0
    for tok in target_tokens:
        for m in it:
            if m == tok:
                matched += 1
                break
    if matched == len(target_tokens) and matched <= len(modal_tokens):
        return True, "ORDERED_SUBSET"

    # SLUG_TOKEN_MATCH: all target tokens in current slug tokens + at least
    # one modal token also in slug tokens (prevents false match on completely
    # different names like "Gabriele Serio" when slugs are "marc-chemoul")
    if target_slug and current_slug:
        slug_tokens = _tokenize(current_slug.replace("-", " "))
        st = set(slug_tokens)
        modal_in_slug = any(tok in st for tok in modal_tokens)
        if modal_in_slug and all(tok in st for tok in target_tokens):
            return True, "SLUG_TOKEN_MATCH"

    return False, "NO_MATCH"


# ============================================================
# Send orchestration per prospect
# ============================================================

def send_to_prospect(driver: webdriver.Chrome, profile_url: str, *,
                     target_name: str = "", dry_run: bool = False,
                     debug_modal: bool = False,
                     debug_send_click: bool = False,
                     keep_browser_open: bool = False) -> str:
    """Returns one of: 'sent', 'already_connected', 'already_pending',
    'no_action', 'weekly_cap', 'captcha', 'error', 'name_mismatch', 'dry_run_skip',
    'debug_modal_skip', 'debug_send_click_skip', 'send_click_failed'."""
    try:
        driver.get(profile_url)
        try:
            WebDriverWait(driver, 5).until(lambda d: "/in/" in (d.current_url.lower()))
        except TimeoutException:
            pass
    except WebDriverException:
        return "error"

    _close_pre_existing_modal(driver)

    current_url = driver.current_url
    match_status, match_detail = _verify_profile_match(driver, target_name, profile_url)
    header_name = _get_profile_header_name(driver) or "N/A"
    target_slug = _extract_profile_slug(profile_url)
    current_slug = _extract_profile_slug(current_url)

    print(f"     [DETAIL] target_name={target_name}")
    print(f"     [DETAIL] target_url={profile_url}")
    print(f"     [DETAIL] current_url={current_url}")
    print(f"     [DETAIL] target_slug={target_slug}")
    print(f"     [DETAIL] current_slug={current_slug}")
    print(f"     [DETAIL] profile_match_status={match_status}")
    print(f"     [DETAIL] detected_header_name={header_name}")
    print(f"     [DETAIL] match_detail={match_detail}")

    if match_status == "URL_INVALID":
        if dry_run:
            print(f"     [DETAIL] dry-run: would skip (not a profile page)")
        return "name_mismatch"

    if detect_captcha(driver):
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            driver.save_screenshot(str(DEBUG_DIR / f"linkedin_captcha_{ts}.png"))
        except WebDriverException:
            pass
        print(f"     [DETAIL] outcome=CAPTCHA_OR_CHECKPOINT")
        return "captcha"
    if detect_weekly_cap(driver):
        print(f"     [DETAIL] outcome=WEEKLY_CAP")
        return "weekly_cap"

    action = check_action_on_profile(driver)
    print(f"     [state] action={action}")

    if dry_run:
        if action in ("connect", "follow"):
            success, detail = click_connect_on_profile(driver, dry_run=True, target_name=target_name)
            if not success:
                print(f"     [DETAIL] dry-run: no safe connect button found in TOP_CARD or DROPDOWN")
                print(f"     [DETAIL] dry-run: rejected unsafe connect button outside top card")
        elif action == "pending":
            print(f"     [DETAIL] dry-run: would skip (already pending)")
        else:
            print(f"     [DETAIL] dry-run: would skip (action={action})")
        return "dry_run_skip"

    if action == "pending":
        return "already_pending"
    if action in ("connect", "follow"):
        if action == "follow":
            print("     [state] follow -> opening more actions menu")
        success, click_detail = click_connect_on_profile(driver, dry_run=False, target_name=target_name)
        if not success:
            print(f"     [state] click_connect_on_profile failed (detail={click_detail})")
            if click_detail in ("more_button_not_in_top_card", "collegati_not_in_dropdown",
                                "no_visible_menu_after_more_click"):
                return "skipped_no_safe_connect_button"
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                driver.save_screenshot(str(DEBUG_DIR / f"linkedin_send_err_{ts}.png"))
            except WebDriverException:
                pass
            return "error"

        if detect_weekly_cap(driver):
            return "weekly_cap"
        if detect_captcha(driver):
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                driver.save_screenshot(str(DEBUG_DIR / f"linkedin_captcha_{ts}.png"))
            except WebDriverException:
                pass
            print(f"     [DETAIL] outcome=CAPTCHA_OR_CHECKPOINT")
            return "captcha"

        modal_container = _wait_for_modal(driver)
        modal_text = None
        if modal_container:
            try:
                modal_text = driver.execute_script(
                    "return arguments[0].innerText || arguments[0].textContent || ''",
                    modal_container
                ).strip()
            except WebDriverException:
                try:
                    modal_text = modal_container.text.strip()
                except WebDriverException:
                    pass
        modal_name = _get_modal_target_name(driver, modal_text=modal_text)

        slug = _extract_profile_slug(driver.current_url) or "unknown"
        slug_ok = slug and target_slug and slug == target_slug
        print(f"     [DETAIL] slug_match={'YES' if slug_ok else 'NO'}")
        print(f"     [DETAIL] modal_name={modal_name}")

        if debug_modal:
            _debug_modal(driver, slug, target_name)
            print(f"     [DETAIL] modal_text_raw={repr(modal_text[:500]) if modal_text else 'None'}")
            if modal_container:
                try:
                    btns = modal_container.find_elements(By.CSS_SELECTOR, "button, a, [role='button']")
                    print(f"     [DETAIL] modal_buttons={[b.text.strip() for b in btns]}")
                except WebDriverException:
                    pass
            print("     Premere INVIO per chiudere popup e continuare...")
            input()
            _close_modal(driver)
            return "debug_modal_skip"

        if modal_name is None:
            has_send_btn = _modal_button_present(driver)
            if has_send_btn:
                print(f"     [DETAIL] MODAL_VISIBLE_BUT_TEXT_EXTRACTION_FAILED (send button present but name not extracted)")
            else:
                print(f"     [DETAIL] MODAL_NAME_NOT_DETECTED: popup found but name parser failed")
            _debug_modal(driver, slug, target_name)
            try:
                _close_modal(driver)
            except WebDriverException:
                pass
            return "name_mismatch"

        modal_ok = False
        name_match_strategy = "NO_MATCH"
        if target_name and modal_name:
            modal_ok, name_match_strategy = _names_match_safe(
                target_name, modal_name,
                target_slug=target_slug, current_slug=slug,
            )

        target_norm = _normalize_name(target_name) if target_name else ""
        modal_norm = _normalize_name(modal_name) if modal_name else ""
        print(f"     [DETAIL] target_name_normalized={target_norm}")
        print(f"     [DETAIL] modal_name_normalized={modal_norm}")
        print(f"     [DETAIL] name_match_strategy={name_match_strategy}")

        if not modal_ok:
            print(f"     [DETAIL] outcome=NAME_MISMATCH_MODAL (modal='{modal_name}', expected='{target_name}', strategy={name_match_strategy}, slug={slug})")
            _debug_modal(driver, slug, target_name)
            try:
                _close_modal(driver)
            except WebDriverException:
                pass
            return "name_mismatch"

        print(f"     [DETAIL] modal_match=YES")
        print(f"     [DETAIL] outcome=READY_TO_SEND")

        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            driver.save_screenshot(str(DEBUG_DIR / f"linkedin_modal_{ts}.png"))
        except WebDriverException:
            pass
        result = click_send_in_modal(driver, modal_container=modal_container,
                                     debug_send_click=debug_send_click,
                                     target_name=target_name)
        if result == "sent":
            time.sleep(1.5)
            if detect_weekly_cap(driver):
                return "weekly_cap"
            if check_action_on_profile(driver) == "pending":
                print(f"     [DETAIL] outcome=SENT")
                return "sent"
            print(f"     [DETAIL] outcome=SENT (modal gone)")
            return "sent"
        if result == "debug_send_click_skip":
            return "debug_send_click_skip"
        if result == "send_click_failed":
            print(f"     [state] click_send_in_modal=send_click_failed")
            if keep_browser_open:
                print("     [keep-browser-open] Browser lasciato aperto per debug manuale.")
            return "send_click_failed"
        print(f"     [state] click_send_in_modal={result}")
        return "error"
    if action == "message":
        return "already_connected"
    return "no_action"


# ============================================================
# Scheduling helpers
# ============================================================

def seconds_until(target_hour: int) -> float:
    now = datetime.now()
    target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        return 0.0
    return (target - now).total_seconds()


def compute_spread(remaining: int, end_hour: int, jitter_pct: float = 0.4) -> float:
    """Returns seconds to sleep until next send, given remaining sends and end hour."""
    if remaining <= 0:
        return 0.0
    now = datetime.now()
    end = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if end <= now:
        return 60.0
    remaining_sec = max(60.0, (end - now).total_seconds())
    base = remaining_sec / remaining
    jitter = base * jitter_pct
    return max(45.0, random.uniform(base - jitter, base + jitter))


# ============================================================
# Main
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=25,
                        help="Sends to attempt today (cap-protected)")
    parser.add_argument("--limit", type=int, default=0,
                        help="If >0, hard-cap sends for test runs (overrides --target)")
    parser.add_argument("--start-hour", type=int, default=9)
    parser.add_argument("--end-hour", type=int, default=18)
    parser.add_argument("--min-score", type=int, default=40)
    parser.add_argument("--languages-priority", default="it,en")
    parser.add_argument("--max-pool", type=int, default=200,
                        help="How many prospects to pull from DB (we filter down to target)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Select prospects and print plan, do not open browser")
    parser.add_argument("--ignore-schedule", action="store_true",
                        help="Skip start-hour wait, run immediately (for tests)")
    parser.add_argument("--fast-debug", action="store_true",
                        help="Max 3 profiles, fast pauses 3-8s, detailed logs")
    parser.add_argument("--debug-modal", action="store_true",
                        help="Click Collegati, show popup text, wait for ENTER before closing")
    parser.add_argument("--debug-send-click", action="store_true",
                        help="Find send button, show details, do NOT click, wait for ENTER")
    parser.add_argument("--keep-browser-open-on-error", action="store_true",
                        help="Don't close browser if send click fails")
    parser.add_argument("--debug-action-state", action="store_true",
                        help="Debug mode: find TOP_CARD and print action state")
    parser.add_argument("--prospect-id", type=int,
                        help="Test a specific prospect by ID")
    args = parser.parse_args()

    # ---- DEBUG ACTION STATE: safe read-only mode (no sends, no clicks) ----
    if args.debug_action_state:
        db.init_db()
        if args.prospect_id:
            with db.db_session() as conn:
                row = conn.execute("SELECT * FROM prospects WHERE id = ?",
                                   (args.prospect_id,)).fetchone()
            if not row:
                print(f"Prospect #{args.prospect_id} not found.")
                return 2
            prospects = [row]
        else:
            with db.db_session() as conn:
                prospects = db.get_top_prospects(
                    conn, limit=1, min_score=args.min_score,
                    statuses=("discovered", "queued"),
                    prefer_languages=[l.strip() for l in args.languages_priority.split(",") if l.strip()],
                )
            if not prospects:
                print("No prospects in queue.")
                return 0

        print(f"Pulled {len(prospects)} prospect(s) for debug-action-state.")
        driver = init_driver()
        if not ensure_logged_in(driver):
            return 2
        try:
            for p in prospects:
                _debug_action_state(driver, p, p["full_name"])
        finally:
            try:
                driver.quit()
            except Exception:
                pass
        print("\nSummary: Sent today: 0")
        return 0

    target = args.limit if args.limit > 0 else args.target
    if args.fast_debug:
        target = min(target, 3)
    if target <= 0:
        print("Target must be > 0")
        return 2

    db.init_db()
    started = db.now_iso()

    with db.db_session() as conn:
        cap_active, resume = db.is_cap_active(conn)
        if cap_active:
            print(f"[STOP] Weekly cap active until {resume}. Exiting.")
            db.log_run_health(
                conn, component="sender_v3", started_at=started,
                status="skipped_cap", items_processed=0,
                extra={"cap_resume_date": resume},
            )
            return 0

        today_row = db.ensure_today_quota(conn, target)
        already_sent_today = int(today_row["sent_count"])
        remaining = max(0, target - already_sent_today)
        print(f"Today: sent {already_sent_today}/{target}, remaining {remaining}")
        if remaining <= 0:
            print("Today's target already reached.")
            db.log_run_health(
                conn, component="sender_v3", started_at=started,
                status="target_already_met", items_processed=0,
            )
            return 0

        prospects = db.get_top_prospects(
            conn,
            limit=args.max_pool,
            min_score=args.min_score,
            statuses=("discovered", "queued"),
            prefer_languages=[l.strip() for l in args.languages_priority.split(",") if l.strip()],
        )

    print(f"Pulled {len(prospects)} candidate prospects (min_score={args.min_score})")
    if not prospects:
        print("No prospects in queue. Run discovery_agent.py first.")
        return 0

    plan = prospects[:remaining]
    print(f"Plan: attempt {len(plan)} sends today, spread on {args.start_hour:02d}-{args.end_hour:02d}")
    for p in plan[:10]:
        print(f"  - [score={p['score']}] {p['full_name']} | {p['headline'] or ''}")
    if len(plan) > 10:
        print(f"  ... and {len(plan) - 10} more")

    if args.dry_run:
        print("\n[dry-run] Launching browser to verify each profile without sending.")
    else:
        print("Proceeding with real sends.")

    now = datetime.now()
    if not args.ignore_schedule:
        if now.hour < args.start_hour:
            wait = seconds_until(args.start_hour)
            print(f"Waiting {int(wait)}s ({wait/60:.1f}min) until start hour {args.start_hour}...")
            try:
                time.sleep(wait)
            except KeyboardInterrupt:
                print("Interrupted during wait.")
                return 130
        elif now.hour >= args.end_hour:
            print(f"Current hour {now.hour} >= end hour {args.end_hour}. Nothing to do today.")
            return 0

    driver = None
    sent = skipped = errors = captcha_count = 0
    cap_hit = False
    keep_browser_for_debug = False

    try:
        driver = init_driver()
        if not ensure_logged_in(driver):
            with db.db_session() as conn:
                db.log_run_health(
                    conn, component="sender_v3", started_at=started,
                    status="login_failed", items_processed=0,
                )
            return 2

        for i, prospect in enumerate(plan, 1):
            now = datetime.now()
            if not args.ignore_schedule and now.hour >= args.end_hour:
                print(f"[STOP] End of business hours reached ({args.end_hour}).")
                break

            pid = int(prospect["id"])
            name = prospect["full_name"]
            url = prospect["profile_url"]
            score = prospect["score"]
            print(f"\n[{i}/{len(plan)}] (#{pid} score={score}) {name}")
            print(f"  {url}")

            outcome = send_to_prospect(driver, url, target_name=name, dry_run=args.dry_run,
                                       debug_modal=args.debug_modal,
                                       debug_send_click=args.debug_send_click,
                                       keep_browser_open=args.keep_browser_open_on_error)
            print(f"  -> {outcome}")

            with db.db_session() as conn:
                if outcome == "sent":
                    db.update_prospect_status(conn, pid, "sent")
                    db.increment_today_quota(conn, sent=1)
                    sent += 1
                elif outcome == "already_connected":
                    db.update_prospect_status(conn, pid, "already_connected")
                    db.upsert_existing_contact(
                        conn, full_name=name, source="sender_discovery",
                        profile_url=url, status="connected",
                    )
                    db.increment_today_quota(conn, skipped=1)
                    skipped += 1
                elif outcome == "already_pending":
                    db.update_prospect_status(conn, pid, "already_pending")
                    db.increment_today_quota(conn, skipped=1)
                    skipped += 1
                elif outcome == "skipped_no_safe_connect_button":
                    db.update_prospect_status(conn, pid, "skipped", notes="no_safe_connect_button")
                    db.increment_today_quota(conn, skipped=1)
                    skipped += 1
                elif outcome == "follow_only":
                    db.update_prospect_status(conn, pid, "skipped", notes="follow_only")
                    db.increment_today_quota(conn, skipped=1)
                    skipped += 1
                elif outcome == "no_action":
                    db.update_prospect_status(conn, pid, "skipped", notes="no_action_button")
                    db.increment_today_quota(conn, skipped=1)
                    skipped += 1
                elif outcome == "captcha":
                    captcha_count += 1
                    db.update_prospect_status(conn, pid, "queued", notes="captcha_stop")
                    db.increment_today_quota(conn, captcha=1)
                    print("  CAPTCHA/checkpoint detected. Run fermato. Risolvi manualmente e riavvia.")
                    cap_hit = True
                    break
                elif outcome == "weekly_cap":
                    cap_hit = True
                    db.update_prospect_status(conn, pid, "queued", notes="weekly_cap_hit")
                    resume_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
                    db.mark_cap_reached(conn, resume_date, note="weekly invitation limit reached during send")
                    print(f"  [STOP] WEEKLY CAP HIT. Resume after {resume_date}.")
                    break
                elif outcome == "name_mismatch":
                    db.update_prospect_status(conn, pid, "skipped", notes="name_mismatch")
                    db.increment_today_quota(conn, skipped=1)
                    skipped += 1
                elif outcome == "dry_run_skip":
                    pass
                elif outcome in ("debug_modal_skip", "debug_send_click_skip"):
                    db.update_prospect_status(conn, pid, "skipped", notes=outcome)
                    db.increment_today_quota(conn, skipped=1)
                    skipped += 1
                elif outcome == "send_click_failed":
                    db.update_prospect_status(conn, pid, "queued", notes="send_click_failed")
                    db.increment_today_quota(conn, error=1)
                    errors += 1
                    if args.keep_browser_open_on_error:
                        print("     [keep-browser-open-on-error] Browser lasciato aperto per debug manuale.")
                        keep_browser_for_debug = True
                else:
                    db.update_prospect_status(conn, pid, "error", error_message=outcome)
                    db.increment_today_quota(conn, error=1)
                    errors += 1
                    if args.keep_browser_open_on_error:
                        keep_browser_for_debug = True

            remaining_after = max(0, target - (already_sent_today + sent))
            if remaining_after <= 0:
                print(f"\n[DONE] Reached daily target {target}.")
                break
            if args.fast_debug:
                wait = random.uniform(1, 3)
                print(f"  [fast-debug] Next in {wait:.0f}s...")
                time.sleep(wait)
            elif outcome == "sent":
                if not args.ignore_schedule:
                    wait = compute_spread(remaining_after, args.end_hour)
                    print(f"  Next send in {int(wait)}s ({wait/60:.1f}min) -> {(datetime.now() + timedelta(seconds=wait)).strftime('%H:%M:%S')}")
                    time.sleep(wait)
                else:
                    time.sleep(random.uniform(60, 180))
            elif outcome in ("name_mismatch", "no_action", "already_connected", "already_pending", "follow_only", "skipped_no_safe_connect_button", "dry_run_skip"):
                wait = random.uniform(2, 5)
                print(f"  [skip] Next in {wait:.0f}s...")
                time.sleep(wait)
            else:
                wait = random.uniform(5, 10)
                print(f"  [error] Next in {wait:.0f}s...")
                time.sleep(wait)

        with db.db_session() as conn:
            db.end_today_quota(
                conn,
                note=f"sent={sent} skipped={skipped} errors={errors} cap_hit={cap_hit}",
            )
            db.log_run_health(
                conn, component="sender_v3", started_at=started,
                status="ok" if not cap_hit else "cap_hit",
                items_processed=sent,
                extra={
                    "sent": sent, "skipped": skipped, "errors": errors,
                    "captcha": captcha_count, "cap_hit": cap_hit,
                },
            )

        print("\n" + "=" * 60)
        print("SENDER SUMMARY")
        print("=" * 60)
        print(f"  Sent today:        {sent}")
        print(f"  Skipped:           {skipped}")
        print(f"  Errors:            {errors}")
        print(f"  CAPTCHA events:    {captcha_count}")
        print(f"  Cap hit:           {cap_hit}")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        with db.db_session() as conn:
            db.end_today_quota(conn, note="interrupted")
            db.log_run_health(
                conn, component="sender_v3", started_at=started,
                status="interrupted", items_processed=sent,
            )
        return 130
    except Exception as exc:
        print(f"FATAL: {exc}")
        with db.db_session() as conn:
            db.log_run_health(
                conn, component="sender_v3", started_at=started,
                status="error", items_processed=sent,
                error_message=str(exc),
            )
        return 1
    finally:
        if driver is not None and not keep_browser_for_debug:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
