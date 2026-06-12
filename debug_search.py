"""Quick debug for one search URL: see what the DOM looks like."""

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
print("URL:", URL)
driver.get(URL)
time.sleep(12)
print("Final URL:", driver.current_url)
print("Title:", driver.title)
print("Anchors /in/:", len(driver.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")))
print("Body text (first 500):")
print(driver.find_element(By.TAG_NAME, "body").text[:500])
debug_path = Path(r"C:\Users\andrea.fallavollita\AppData\Local\Temp\debug_search_after.png")
driver.save_screenshot(str(debug_path))
print(f"Screenshot: {debug_path}")
driver.quit()
