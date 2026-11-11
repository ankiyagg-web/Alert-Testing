"""
Cashify 5G phone price alert -> Telegram   (version 3: brand by brand)

Opens each brand page on Cashify one by one, scrolls to load all phones,
and sends a Telegram message for every phone that is:
  - in stock
  - 5G
  - priced below LIMIT_5G

Run:  py cashify_alert.py
"""

import json
import os
import re
import requests
from playwright.sync_api import sync_playwright

# ---------- SETTINGS ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]   # stored safely in GitHub Secrets
CHAT_ID = os.environ["CHAT_ID"]       # stored safely in GitHub Secrets

ONLY_5G = True            # True = only 5G phones. False = also alert non-5G
LIMIT_5G = 30000          # 5G phone must be below this price
LIMIT_NORMAL = 11000      # used only when ONLY_5G = False (non-5G limit)

ANY_PRICE_BRANDS = ["Nothing"]   # for these brands: alert for ANY in-stock phone, any price, 5G or not

HEADLESS = True           # set False to watch the browser (try this if 0 phones found)
SCROLLS = 12              # more scrolls = more phones per brand
REPEAT_ALERTS = True      # True = alert on EVERY run for every matching in-stock phone
SEEN_FILE = "seen.json"   # only used when REPEAT_ALERTS = False

BASE = "https://www.cashify.in/buy-refurbished-mobile-phones/"
# brand name -> page names to try (first one that shows phones is used)
BRANDS = {
    "Samsung": ["samsung"],
    "OnePlus": ["oneplus"],
    "Xiaomi": ["xiaomi", "mi"],
    "Poco": ["poco"],
    "Vivo": ["vivo"],
    "Oppo": ["oppo"],
    "Motorola": ["motorola"],
    "Realme": ["realme"],
    "Nothing": ["nothing"],
}
# ------------------------------

PRICE = re.compile(r"₹\s?([\d,]{4,7})")


def parse_text(lines, page_url):
    """Read phone cards from the page text (works even if page layout changes)."""
    phones = []
    name_idx = [i for i, l in enumerate(lines) if re.search(r" - Refurbished\s*$", l)]
    for n, i in enumerate(name_idx):
        end = name_idx[n + 1] - 3 if n + 1 < len(name_idx) else len(lines)
        after = lines[i + 1:max(end, i + 2)]
        before = lines[max(0, i - 3):i]
        block = " | ".join(before + after).lower()
        price = None
        for l in after:
            m = PRICE.search(l)
            if m and "off" not in l.lower():
                price = int(m.group(1).replace(",", ""))
                break
        if price is None:
            continue
        name = re.sub(r"\s*- Refurbished\s*$", "", lines[i]).strip()
        in_stock = "out of stock" not in block and "sold out" not in block
        phones.append((name, price, in_stock))
    return phones


LINK_JS = """
() => {
  const out = {};
  const norm = s => s.split(/\\s+/).join(' ').trim();
  const els = Array.from(document.querySelectorAll('*')).filter(
    e => e.children.length === 0 && /- Refurbished\\s*$/.test(e.textContent.trim()));
  for (const e of els) {
    const name = norm(e.textContent).replace(/\\s*- Refurbished$/, '').trim();
    let node = e, href = null;
    for (let i = 0; i < 10 && node && !href; i++) {
      if (node.tagName === 'A' && node.href) { href = node.href; break; }
      if (node.querySelectorAll) {
        const hrefs = new Set(Array.from(node.querySelectorAll("a[href*='renewed-']")).map(a => a.href));
        if (hrefs.size === 1) { href = Array.from(hrefs)[0]; break; }
        if (hrefs.size > 1) break;
      }
      node = node.parentElement;
    }
    if (href) out[name] = href;
  }
  return out;
}
"""


def get_links(page):
    """Map phone name -> its own page link."""
    try:
        return page.evaluate(LINK_JS)
    except Exception:
        return {}


def scrape_brand(page, brand, slugs):
    for slug in slugs:
        url = BASE + slug
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            for _ in range(SCROLLS):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1200)
            text = page.inner_text("body")
        except Exception as e:
            print(f"  {brand}: could not open {url} ({e.__class__.__name__})")
            continue

        phones = parse_text([l.strip() for l in text.split("\n") if l.strip()], url)
        if not phones:
            continue

        links = get_links(page)
        result = []
        for name, price, in_stock in phones:
            key = " ".join(name.split())
            result.append((name, price, in_stock, links.get(key, url)))
        found = sum(1 for r in result if r[3] != url)
        print(f"  {brand}: found own link for {found} of {len(result)} phones")
        return result
    print(f"  {brand}: no phones found (page name may be different)")
    return []


def send_telegram(text):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
        timeout=30,
    )


def main():
    try:
        seen = set(json.load(open(SEEN_FILE)))
    except Exception:
        seen = set()

    deals = []
    total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page(
            viewport={"width": 1300, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            locale="en-IN",
        )
        for brand, slugs in BRANDS.items():
            phones = scrape_brand(page, brand, slugs)
            in_stock = [x for x in phones if x[2]]
            print(f"{brand}: {len(phones)} phones, {len(in_stock)} in stock")
            total += len(phones)
            for name, price, stock, link in phones:
                if not stock:
                    continue
                is_5g = "5g" in name.lower()
                if brand in ANY_PRICE_BRANDS:
                    ok = True
                elif ONLY_5G:
                    ok = is_5g and price < LIMIT_5G
                else:
                    ok = price < (LIMIT_5G if is_5g else LIMIT_NORMAL)
                key = f"{name}|{price}"
                if ok and (REPEAT_ALERTS or key not in seen):
                    seen.add(key)
                    deals.append(f"{name} - Rs {price}{' (5G)' if is_5g else ''}\n{link}")
        browser.close()

    print(f"\nChecked {total} phones in total.")
    if deals:
        for i in range(0, len(deals), 8):
            send_telegram("Cashify price alert:\n\n" + "\n\n".join(deals[i:i + 8]))
        print(f"Sent {len(deals)} deal(s) to Telegram.")
    else:
        print("No new deals.")

    json.dump(sorted(seen), open(SEEN_FILE, "w"))


if __name__ == "__main__":
    main()
