"""Subagent 1/3: Scrape LinkedIn connections via browser and save to JSON.

This script ONLY opens the browser, logs in if needed, scrapes the connections
page, and writes the result to a JSON file. It does NOT touch Excel.

Output: connections_list.json
"""

import os
import sys
import time
import argparse
import json
import re
from pathlib import Path
import builtins

def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import NoSuchElementException

# Resolve paths
SCRIPT_DIR = Path(__file__).parent
PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA", "C:\\Temp")) / "LinkedInAutomationProfileCheck"
DEBUG_DIR = Path(os.environ.get("TEMP", "."))
DEFAULT_OUTPUT = SCRIPT_DIR / "connections_list.json"

CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"


def normalize_name(name: str) -> str:
    if not name:
        return ''
    n = name.strip().lower()
    n = (n.replace('à', 'a').replace('è', 'e').replace('é', 'e')
           .replace('ì', 'i').replace('ò', 'o').replace('ù', 'u'))
    n = re.sub(r'\s+', ' ', n)
    return n


def build_chrome(profile_dir: Path) -> webdriver.Chrome:
    chrome_options = Options()
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
    profile_dir.mkdir(parents=True, exist_ok=True)
    chrome_options.add_argument(f"--user-data-dir={profile_dir}")
    chrome_options.add_argument("--profile-directory=Default")
    service = ChromeService()
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    driver.implicitly_wait(5)
    return driver


def ensure_logged_in(driver: webdriver.Chrome, email: str, password: str, max_wait_seconds: int = 600) -> None:
    print("Checking login status...")
    driver.get("https://www.linkedin.com/feed")
    time.sleep(5)
    url_lower = driver.current_url.lower()
    if any(x in url_lower for x in ['/login', '/signup', '/checkpoint', '/uas/']):
        print("Not logged in. Going to login page...")
        driver.get("https://www.linkedin.com/login")
        time.sleep(5)
        try:
            email_input = None
            for sel in [
                "input[type='email']",
                "input#username",
                "input[name='session_key']",
                "input[name='email']",
            ]:
                inputs = driver.find_elements(By.CSS_SELECTOR, sel)
                cand = next((e for e in inputs if e.is_displayed()), None)
                if cand:
                    email_input = cand
                    break
            pwd_input = None
            for sel in [
                "input[type='password']",
                "input#password",
                "input[name='session_password']",
                "input[name='password']",
            ]:
                inputs = driver.find_elements(By.CSS_SELECTOR, sel)
                cand = next((p for p in inputs if p.is_displayed()), None)
                if cand:
                    pwd_input = cand
                    break
            if not email_input or not pwd_input:
                print("Could not find email/password fields; please log in manually.")
            else:
                email_input.send_keys(email)
                pwd_input.send_keys(password)
                clicked = False
                for btn in driver.find_elements(By.TAG_NAME, "button"):
                    if btn.is_displayed() and (btn.text.strip() in ["Accedi", "Sign in", "Log in"] or
                                                ("Accedi" in btn.text or "Sign in" in btn.text or "Log in" in btn.text)
                                                and not any(x in btn.text for x in ["Google", "Microsoft", "Apple"])):
                        btn.click()
                        clicked = True
                        break
                if not clicked:
                    pwd_input.submit()
        except Exception as e:
            print(f"Login error (continuing to manual wait): {e}")

    print("Waiting for login to fully complete (up to %d seconds)..." % max_wait_seconds)
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        url = driver.current_url.lower()
        if any(x in url for x in ['/login', '/signup', '/uas/']):
            print("Still on login page...")
            time.sleep(3)
            continue
        if '/checkpoint/' in url:
            print("\n*** CHALLENGE DETECTED ***")
            print("URL:", driver.current_url)
            print("Please solve it manually in the Chrome window that just opened.")
            print("Waiting for you to complete it...\n")
            time.sleep(10)
            continue
        if '/feed' in url or '/mynetwork' in url:
            print("Login confirmed (URL contains /feed or /mynetwork).")
            return
        try:
            nav = driver.find_element(By.ID, "global-nav")
            if nav and nav.is_displayed():
                print("Login confirmed (#global-nav present).")
                return
        except NoSuchElementException:
            pass
        print(f"  Current URL: {driver.current_url}, waiting...")
        time.sleep(3)
    raise TimeoutError("Login did not complete within the timeout. Please re-run after solving manually.")


def _extract_card_names(driver: webdriver.Chrome) -> list[str]:
    names = []
    seen_anchors = set()
    anchors = driver.find_elements(By.CSS_SELECTOR, "a[href*='linkedin.com/in/']")
    for a in anchors:
        try:
            href = a.get_attribute('href') or ''
            if href in seen_anchors:
                continue
            text = (a.text or '').strip()
            if not text:
                continue
            seen_anchors.add(href)
            text_first_line = text.split('\n')[0].strip()
            if text_first_line:
                names.append(text_first_line)
        except Exception:
            continue
    return names


def _scroll_inner_container(driver: webdriver.Chrome) -> None:
    driver.execute_script("""
        const containers = document.querySelectorAll('main, .scaffold-finite-scroll, section.scaffold-finite-scroll, [class*="finite-scroll"]');
        for (const c of containers) {
            if (c.scrollHeight > c.clientHeight + 10) {
                c.scrollTop = c.scrollHeight;
            }
        }
        const all = document.querySelectorAll('*');
        for (const el of all) {
            const style = window.getComputedStyle(el);
            if ((style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 10) {
                el.scrollTop = el.scrollHeight;
            }
        }
        window.scrollTo(0, document.body.scrollHeight);
        return true;
    """)


def _click_show_more_if_any(driver: webdriver.Chrome) -> bool:
    for btn_text in ["Mostra altro", "Mostra altri", "Show more", "Show more results", "Load more"]:
        for btn in driver.find_elements(By.TAG_NAME, "button"):
            try:
                if btn.is_displayed() and btn_text.lower() in (btn.text or '').lower():
                    btn.click()
                    return True
            except Exception:
                continue
    return False


def scrape_connections(driver: webdriver.Chrome) -> list[str]:
    print(f"Navigating to {CONNECTIONS_URL}")
    driver.get(CONNECTIONS_URL)
    time.sleep(5)
    print(f"URL: {driver.current_url}")
    driver.save_screenshot(str(DEBUG_DIR / "connections_page.png"))

    all_names_seen: list[str] = []
    seen_norm: set[str] = set()
    last_anchor_count = -1
    stable_iters = 0
    for i in range(300):  # safety cap
        _scroll_inner_container(driver)
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys("\ue010" * 5)
        except Exception:
            pass
        _click_show_more_if_any(driver)
        time.sleep(1.5)
        anchors = driver.find_elements(By.CSS_SELECTOR, "a[href*='linkedin.com/in/']")
        names_now = _extract_card_names(driver)
        for n in names_now:
            nn = normalize_name(n)
            if nn and nn not in seen_norm:
                seen_norm.add(nn)
                all_names_seen.append(n)
        if len(anchors) == last_anchor_count:
            stable_iters += 1
            if stable_iters >= 10:
                break
        else:
            stable_iters = 0
        last_anchor_count = len(anchors)
        if i % 5 == 0:
            print(f"  scroll iter {i+1}: {len(anchors)} anchors visible, {len(all_names_seen)} unique names collected")

    print(f"Final anchor count: {len(anchors) if 'anchors' in locals() else 0}")
    print(f"Total unique connection names collected: {len(all_names_seen)}")

    html_path = DEBUG_DIR / "connections_page.html"
    html_path.write_text(driver.page_source, encoding='utf-8')

    return all_names_seen


def main():
    parser = argparse.ArgumentParser(description="Subagent 1: Scrape LinkedIn connections to JSON.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output JSON file.")
    args = parser.parse_args()

    email = os.getenv("LINKEDIN_EMAIL")
    password = os.getenv("LINKEDIN_PASSWORD")
    if not email or not password:
        try:
            from dotenv import load_dotenv
            load_dotenv()
            email = os.getenv("LINKEDIN_EMAIL")
            password = os.getenv("LINKEDIN_PASSWORD")
        except ImportError:
            pass
    if not email or not password:
        raise EnvironmentError("Set LINKEDIN_EMAIL and LINKEDIN_PASSWORD in environment or .env file.")

    driver = build_chrome(PROFILE_DIR)
    try:
        ensure_logged_in(driver, email, password)
        names = scrape_connections(driver)
        print(f"Scraped {len(names)} unique connection names.")
        Path(args.output).write_text(json.dumps(names, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"Saved to {args.output}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
