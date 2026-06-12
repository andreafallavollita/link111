"""Inspect what kind of a[href*='/in/'] elements are returned."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from selenium.webdriver.common.by import By

from discovery_agent import build_chrome, ensure_logged_in

URL = (
    "https://www.linkedin.com/search/results/people/"
    "?keywords=Marketing%20Director"
    "&origin=FACETED_SEARCH"
    "&network=%5B%22S%22%2C%22O%22%5D"
    "&geoUrn=%5B%22101165590%22%5D"
    "&page=1"
)

driver = build_chrome()
ensure_logged_in(driver)
driver.get(URL)
time.sleep(12)

anchors = driver.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")
print(f"Total anchors with /in/: {len(anchors)}")
for i, a in enumerate(anchors):
    href = a.get_attribute("href") or ""
    text = (a.text or "").strip().replace("\n", " | ")[:80]
    classes = a.get_attribute("class") or ""
    print(f"[{i}] class={classes[:60]:<60s} href={href[-50:]:<50s} text={text}")

print("\n--- Try other patterns ---")
for sel in [
    "a.app-aware-link[href*='/in/']",
    "a[data-test-app-aware-link][href*='/in/']",
    "div[data-chameleon-result-urn] a[href*='/in/']",
    "li div[data-chameleon-result-urn]",
    "div.entity-result",
    "li.reusable-search__result-container",
    "div.reusable-search__entity-result-list",
    "ul[role='list'] > li",
]:
    try:
        count = len(driver.find_elements(By.CSS_SELECTOR, sel))
    except Exception as exc:
        count = f"err: {exc}"
    print(f"  {sel:<60s} -> {count}")

driver.quit()
