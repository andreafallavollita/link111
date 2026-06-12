"""Debug: visit a profile and dump button structure."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from selenium.webdriver.common.by import By

from discovery_agent import build_chrome, ensure_logged_in
from sender_v3 import click_connect_on_profile

URLS = [
    "https://www.linkedin.com/in/carlalopezmartinez/",
]

driver = build_chrome()
ensure_logged_in(driver)

for url in URLS:
    print(f"\n=== {url} ===")
    driver.get(url)
    time.sleep(4)
    buttons = driver.find_elements(By.CSS_SELECTOR, "button, a")
    print(f"Total buttons/links: {len(buttons)}")
    for i, b in enumerate(buttons):
        try:
            tag = (b.tag_name or "").upper()
            txt = (b.text or "").strip()[:40]
            aria = (b.get_attribute("aria-label") or "")[:60]
            cls = (b.get_attribute("class") or "")[:40]
        except Exception:
            continue
        if txt or aria:
            print(f"[{i}] <{tag}> txt='{txt}' aria='{aria}' cls='{cls}'")
        if any(k in (txt + " " + aria).lower() for k in ["collegat", "connect", "invita a collegar", "invite to connect", "send invitation"]):
            print(f"   ^^ CONNECT-LIKE detected")
    debug_path = Path(r"C:\Users\andrea.fallavollita\AppData\Local\Temp\debug_buttons.png")
    driver.save_screenshot(str(debug_path))
    print(f"Screenshot: {debug_path}")

    print("\n--- BEFORE click ---")
    try:
        result = driver.execute_script("return document.querySelectorAll('iframe').length;")
        print(f"iframes before: {result}")
    except Exception as e:
        print(f"  err: {e}")
    try:
        result = driver.execute_script("return document.readyState;")
        print(f"readyState before: {result}")
    except Exception as e:
        print(f"  err: {e}")
    try:
        result = driver.execute_script("return window.location.href;")
        print(f"url before: {result}")
    except Exception as e:
        print(f"  err: {e}")

    print("\n--- Clicking connect... ---")
    if click_connect_on_profile(driver):
        time.sleep(3)
        # Quick JS sanity
        try:
            r = driver.execute_script("return 42;")
            print(f"sanity 42: {r}")
        except Exception as e:
            print(f"sanity err: {e}")
        try:
            r = driver.execute_script("return document.readyState;")
            print(f"readyState: {r}")
        except Exception as e:
            print(f"  err: {e}")
        try:
            r = driver.execute_script("return document.title;")
            print(f"title: {r}")
        except Exception as e:
            print(f"  err: {e}")
        try:
            r = driver.execute_script("return document.querySelectorAll('button').length;")
            print(f"button count: {r}")
        except Exception as e:
            print(f"  err: {e}")
        try:
            r = driver.execute_script("return document.body ? document.body.innerText.length : -1;")
            print(f"body innerText length: {r}")
        except Exception as e:
            print(f"  err: {e}")
        try:
            r = driver.execute_script("return document.body ? document.body.innerHTML.indexOf('Invia senza nota') : -2;")
            print(f"invia index in innerHTML: {r}")
        except Exception as e:
            print(f"  err: {e}")
        try:
            r = driver.execute_script("return document.querySelectorAll('iframe').length;")
            print(f"iframe count: {r}")
        except Exception as e:
            print(f"  err: {e}")
        try:
            r = driver.execute_script("""
            (function() {
                var ifs = document.querySelectorAll('iframe');
                var out = '';
                for (var i = 0; i < ifs.length; i++) {
                    out += i + ':src=' + (ifs[i].src || '').substring(0, 80) + '|name=' + (ifs[i].name || '') + '|';
                }
                return out;
            })();
            """)
            print(f"iframes: {r}")
        except Exception as e:
            print(f"  err: {e}")
        # Check if there's a fixed-position overlay
        try:
            r = driver.execute_script("""
            (function() {
                var all = document.querySelectorAll('*');
                var overlay = null;
                for (var i = 0; i < all.length; i++) {
                    var s = window.getComputedStyle(all[i]);
                    if (s.position === 'fixed' && (all[i].textContent || '').indexOf('Invia senza nota') !== -1) {
                        overlay = String(all[i].tagName) + ':' + (all[i].id || '') + ':' + (all[i].className || '').substring(0, 60);
                        break;
                    }
                }
                return overlay || 'no fixed overlay with text';
            })();
            """)
            print(f"fixed overlay scan: {r}")
        except Exception as e:
            print(f"  err: {e}")
        # Check documentElement.outerHTML for Invia senza nota
        try:
            r = driver.execute_script("return document.documentElement.outerHTML.indexOf('Invia senza nota');")
            print(f"docHTML index: {r}")
        except Exception as e:
            print(f"  err: {e}")
        try:
            r = driver.execute_script("return document.documentElement.outerHTML.length;")
            print(f"docHTML length: {r}")
        except Exception as e:
            print(f"  err: {e}")
        # Save outerHTML to file
        try:
            r = driver.execute_script("return document.documentElement.outerHTML;")
            with open(r"C:\Users\andrea.fallavollita\AppData\Local\Temp\debug_after.html", "w", encoding="utf-8") as fh:
                fh.write(r)
            print(f"  saved outerHTML ({len(r)} chars) to debug_after.html")
        except Exception as e:
            print(f"  err: {e}")
        # Also save page_source via driver (different from outerHTML, may include iframes/closed shadow)
        try:
            ps = driver.page_source
            with open(r"C:\Users\andrea.fallavollita\AppData\Local\Temp\debug_page_source.html", "w", encoding="utf-8") as fh:
                fh.write(ps)
            print(f"  page_source saved ({len(ps)} chars)")
        except Exception as e:
            print(f"  err: {e}")
        # Count buttons with shadow root
        try:
            r = driver.execute_script("""
            (function() {
                var n = document.querySelectorAll('*');
                var withShadow = 0;
                var labels = [];
                for (var i = 0; i < n.length; i++) {
                    if (n[i].shadowRoot) {
                        withShadow++;
                        if (labels.length < 10) labels.push(String(n[i].tagName || 'unknown'));
                    }
                }
                return withShadow + ' shadowRoots; first tags: ' + labels.join(',');
            })();
            """)
            print(f"shadow root scan: {r}")
        except Exception as e:
            print(f"  shadow err: {e}")
        # Find "Invia senza nota" anywhere in document (including shadow)
        try:
            r = driver.execute_script("""
            (function() {
                function find(root, text) {
                    try {
                        var all = root.querySelectorAll('*');
                        for (var i = 0; i < all.length; i++) {
                            var t = (all[i].textContent || '');
                            if (t.indexOf(text) !== -1 && all[i].children.length === 0) {
                                return String(all[i].tagName) + ':' + t.substring(0, 60);
                            }
                            if (all[i].shadowRoot) {
                                var f = find(all[i].shadowRoot, text);
                                if (f) return 'SHADOW/' + f;
                            }
                        }
                    } catch(e) {}
                    return null;
                }
                var f = find(document, 'Invia senza nota');
                return f ? f : 'NOT FOUND';
            })();
            """)
            print(f"find 'Invia senza nota': {r}")
        except Exception as e:
            print(f"  find err: {e}")
        # Try also with simple contains
        try:
            r = driver.execute_script("""
            (function() {
                var xpathResult = document.evaluate("//*[normalize-space(text())='Invia senza nota']", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
                var n = xpathResult.singleNodeValue;
                if (n) return 'xpath hit: ' + String(n.tagName);
                return 'xpath miss';
            })();
            """)
            print(f"xpath invia senza nota: {r}")
        except Exception as e:
            print(f"  xpath err: {e}")
        # Look for all <button> elements with text 'Invia' (use raw HTML)
        try:
            r = driver.execute_script("""
            (function() {
                var html = document.documentElement.outerHTML;
                var idx = html.indexOf('Invia senza nota');
                if (idx === -1) return 'NO in HTML';
                return 'found at ' + idx + ' snippet: ' + html.substring(Math.max(0, idx-100), idx+100);
            })();
            """)
            print(f"raw HTML search: {r}")
        except Exception as e:
            print(f"  html err: {e}")
        # Window handles check
        try:
            handles = driver.window_handles
            print(f"window_handles: {len(handles)}")
            for h in handles:
                try:
                    driver.switch_to.window(h)
                    print(f"  {h}: title='{driver.title}' url='{driver.current_url[:80]}'")
                except Exception as e:
                    print(f"  {h}: err {e}")
            if handles:
                driver.switch_to.window(handles[0])
        except Exception as e:
            print(f"  handles err: {e}")
        # Wait 5s and re-dump
        time.sleep(5)
        try:
            r = driver.execute_script("return document.querySelectorAll('iframe').length;")
            print(f"iframe count after 5s: {r}")
        except Exception as e:
            print(f"  err: {e}")
        try:
            r = driver.execute_script("return document.body ? document.body.innerHTML.indexOf('Invia senza nota') : -2;")
            print(f"invia index after 5s: {r}")
        except Exception as e:
            print(f"  err: {e}")
        # Try to find shadow roots in iframe content
        try:
            r = driver.execute_script("""
            (function() {
                var ifs = document.querySelectorAll('iframe');
                var out = [];
                for (var i = 0; i < ifs.length; i++) {
                    try {
                        var doc = ifs[i].contentDocument;
                        if (!doc) { out.push(i + ':no-contentDocument'); continue; }
                        var html = doc.documentElement ? doc.documentElement.outerHTML.length : -1;
                        var hasInvia = doc.documentElement && doc.documentElement.outerHTML.indexOf('Invia senza nota') !== -1;
                        out.push(i + ':htmlLen=' + html + ',hasInvia=' + hasInvia);
                    } catch(e) { out.push(i + ':err ' + e.message); }
                }
                return out.join('|');
            })();
            """)
            print(f"iframe content scan: {r}")
        except Exception as e:
            print(f"  err: {e}")
        modal_path = Path(r"C:\Users\andrea.fallavollita\AppData\Local\Temp\debug_modal.png")
        driver.save_screenshot(str(modal_path))
    else:
        print("connect click failed")

driver.quit()
