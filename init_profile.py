import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright

# ВАЖЛИВО ДЛЯ WINDOWS (див. проблему №2)
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

PROFILE_DIR = Path("pw_profile").resolve()

async def main():
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            locale="uk-UA",
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = await ctx.new_page()
        await page.goto("https://rozetka.com.ua/", wait_until="domcontentloaded", timeout=60_000)

        print("Пройди cookies/CF і ЗАКРИЙ ВІКНО браузера. Скрипт завершиться сам.")

        # Чекаємо БЕЗ ЛІМІТУ, поки ти закриєш вікно/контекст
        await ctx.wait_for_event("close", timeout=0)

if __name__ == "__main__":
    asyncio.run(main())
