import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

#зарядні станції CATEGORY_URL = "https://rozetka.com.ua/ua/zaryadnie-stantsii-4674585/c4674585/"
CATEGORY_URL = "https://rozetka.com.ua/ua/mobile-phones/c80003/filter/seller=rozetka/"
OUT_FILE = "product_urls.json"

def normalize(url: str | None) -> str | None:
    if not url:
        return None
    url = url.split("?")[0]
    if re.search(r"/p\d+/", url):
        if not url.endswith("/"):
            url += "/"
        return url
    return None

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # важливо для CF
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="uk-UA",
        )
        page = await ctx.new_page()

        await page.goto(CATEGORY_URL, wait_until="domcontentloaded")

        urls = set()
        clicks = 0

        while True:
            # 1) зібрати всі товари, які вже є
            links = await page.locator('a[href*="/p"]').all()
            for a in links:
                href = await a.get_attribute("href")
                if not href:
                    continue

                # абсолютний url
                if href.startswith("/"):
                    href = "https://rozetka.com.ua" + href

                # прибрати query
                href = href.split("?")[0].strip()

                # ✅ відкидаємо службові сторінки
                if "/comments/" in href:
                    continue
                if "/questions/" in href:
                    continue
                if "/characteristics/" in href:
                    continue
                if "/seller/" in href:
                    continue

                # ✅ лишаємо тільки товарний патерн:
                # /.../p123456789/
                if not re.search(r"/p\d+/", href):
                    continue

                if not href.endswith("/"):
                    href += "/"

                urls.add(href)

            # 2) спробувати натиснути "Показати ще"
            show_more = page.locator("text=Показати ще")
            if await show_more.count() == 0:
                break

            try:
                await show_more.first.click()
                clicks += 1
                await page.wait_for_timeout(1500)
            except Exception:
                break

            # safety limit
            if clicks >= 7:
                break

        await browser.close()

    urls = sorted(urls)
    Path(OUT_FILE).write_text(
        json.dumps(urls, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print(f"Collected {len(urls)} product URLs")

if __name__ == "__main__":
    asyncio.run(main())
