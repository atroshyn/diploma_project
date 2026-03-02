import re
import asyncio
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import json
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
import sys, asyncio
from datetime import datetime
import os
import psycopg
from psycopg.types.json import Jsonb

DB_DSN = os.getenv("DB_DSN", "postgresql://reviews_user:CHANGE_ME_STRONG@localhost:5432/reviews_db")

UA_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

APP_TITLE = "Rozetka Reviews Collector (Async Persistent Context)"
PROFILE_DIR = Path("pw_profile").resolve()

# Скільки одночасно сторінок дозволяємо (не плутати з воркерами uvicorn)
MAX_CONCURRENT_PAGES = 2

# Глобальний стан (1 контекст на процес)
_pw = None
_ctx: BrowserContext | None = None
_page_sem = asyncio.Semaphore(MAX_CONCURRENT_PAGES)

app = FastAPI(title=APP_TITLE)


class FetchReq(BaseModel):
    product_url: str



def normalize_ws(s: str | None) -> str | None:
    if not s:
        return None
    return " ".join(s.split()).strip() or None

def _safe_json_loads(s: str) -> dict | list | None:
    try:
        return json.loads(s)
    except Exception:
        return None

def _iter_jsonld_items(obj):
    # JSON-LD може бути dict або list або graph
    if obj is None:
        return
    if isinstance(obj, list):
        for x in obj:
            yield from _iter_jsonld_items(x)
    elif isinstance(obj, dict):
        # @graph
        if "@graph" in obj and isinstance(obj["@graph"], list):
            for x in obj["@graph"]:
                yield from _iter_jsonld_items(x)
        else:
            yield obj

def parse_product_details_from_html(html: str, product_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    # --- title ---
    title = None
    h1 = soup.select_one("h1")
    if h1:
        title = normalize_ws(h1.get_text(" ", strip=True))

    # --- rozetka_product_id з url: /p543550585/ ---
    m = re.search(r"/p(\d+)/", product_url)
    rozetka_product_id = m.group(1) if m else None

    # --- description ---
    desc_node = (
        soup.select_one('[data-testid="product-description"]')
        or soup.select_one("rz-product-description")
        or soup.select_one(".product-about")
    )
    description_html = str(desc_node) if desc_node else None
    description_text = normalize_ws(desc_node.get_text(" ", strip=True)) if desc_node else None

    # --- specs (best-effort) ---
    specs: dict[str, str] = {}

    # table tr -> th/td
    for row in soup.select("table tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            k = normalize_ws(cells[0].get_text(" ", strip=True))
            v = normalize_ws(cells[1].get_text(" ", strip=True))
            if k and v:
                specs[k] = v

    # dl/dt/dd
    for dt in soup.select("dl dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            k = normalize_ws(dt.get_text(" ", strip=True))
            v = normalize_ws(dd.get_text(" ", strip=True))
            if k and v:
                specs[k] = v

    # --- JSON-LD: brand/sku/mpn ---
    brand = None
    sku = None

    # збираємо всі script[type="application/ld+json"]
    for tag in soup.select('script[type="application/ld+json"]'):
        data = _safe_json_loads(tag.get_text(strip=True))
        if data is None:
            continue

        for item in _iter_jsonld_items(data):
            # шукаємо Product
            if not isinstance(item, dict):
                continue
            t = item.get("@type") or item.get("type")
            # інколи @type може бути list
            if isinstance(t, list):
                is_product = any(x == "Product" for x in t)
            else:
                is_product = (t == "Product")

            if not is_product:
                continue

            # brand: може бути dict {"@type":"Brand","name":"Apple"} або рядок
            b = item.get("brand")
            if isinstance(b, dict):
                brand = brand or normalize_ws(b.get("name"))
            elif isinstance(b, str):
                brand = brand or normalize_ws(b)

            # sku / mpn
            sku = sku or normalize_ws(item.get("sku"))
            sku = sku or normalize_ws(item.get("mpn"))

            # іноді модель/артикул лежить у offers.sku
            offers = item.get("offers")
            if isinstance(offers, dict):
                sku = sku or normalize_ws(offers.get("sku"))

    # --- fallback з specs ---
    if not brand:
        for key in ["Бренд", "Brand", "Виробник", "Производитель", "Марка"]:
            if key in specs:
                brand = specs[key]
                break

    if not sku:
        for key in ["Артикул", "Код", "Код виробника", "Код товара", "Модель", "SKU", "Part Number"]:
            if key in specs:
                sku = specs[key]
                break

    # --- fallback з title (дуже грубо, але краще ніж None) ---
    if not brand and title:
        brand = title.split(" ", 1)[0]

    return {
        "url": product_url,
        "rozetka_product_id": rozetka_product_id,
        "title": title or "Unknown title",
        "brand": brand,
        "sku": sku,
        "description_html": description_html,
        "description_text": description_text,
        "specs_json": specs or None,
    }

async def fetch_product_html(product_url: str, *, timeout_ms: int = 120_000) -> str:
    ctx = await ensure_context()
    page = await safe_new_page(ctx)
    try:
        await page.goto(product_url, wait_until="domcontentloaded", timeout=timeout_ms)
        # щоб контент точно зʼявився
        try:
            await page.wait_for_selector("h1", timeout=20_000)
        except Exception:
            pass
        return await page.content()
    finally:
        await safe_close_page(page)


async def expand_all_reviews_with_show_more(page: Page, *, max_clicks: int = 120) -> int:
    """
    Натискає "Показати ще" доки:
      - елемент з цим текстом існує/видимий
      - після кліку збільшується кількість відгуків
    Повертає кількість успішних кліків.
    """

    # 1) початкове завантаження
    try:
        await page.wait_for_selector("text=Відгук від покупця", timeout=30_000)
    except Exception:
        pass

    # будемо рахувати відгуки через кількість зірок (найстабільніше для Rozetka)
    stars_sel = 'rz-comment-rating [data-testid="stars-rating"]'
    def _stars_locator():
        return page.locator(stars_sel)

    prev = 0
    try:
        prev = await _stars_locator().count()
    except Exception:
        prev = 0

    clicks_done = 0

    for _ in range(max_clicks):
        # 2) шукаємо будь-який елемент, який містить текст "Показати ще"
        btn = page.get_by_text("Показати ще", exact=False).first

        try:
            if await btn.count() == 0:
                break
            if not await btn.is_visible():
                break
        except Exception:
            break

        # 3) скрол до кнопки
        try:
            await btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(200)
        except Exception:
            pass

        # 4) клік: спочатку нормальний, якщо не спрацює — JS click
        clicked = False
        try:
            await btn.click(timeout=3_000)
            clicked = True
        except Exception:
            try:
                await btn.evaluate("(el) => el.click()")
                clicked = True
            except Exception:
                clicked = False

        if not clicked:
            break

        clicks_done += 1

        # 5) чекаємо збільшення кількості відгуків
        grew = False
        for _ in range(60):  # ~60 * 250ms = 15s
            await page.wait_for_timeout(250)
            try:
                cur = await _stars_locator().count()
            except Exception:
                cur = prev

            if cur > prev:
                prev = cur
                grew = True
                break

        # 6) якщо після кліку не підвантажилось — виходимо
        if not grew:
            break

    return clicks_done

def upsert_product(conn, *, category_id: int, data: dict[str, Any]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.products (
              category_id, rozetka_product_id, title, brand, sku, url,
              description_html, description_text, specs_json
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (url) DO UPDATE SET
              category_id      = EXCLUDED.category_id,
              rozetka_product_id = EXCLUDED.rozetka_product_id,
              title            = EXCLUDED.title,
              brand            = EXCLUDED.brand,
              sku              = EXCLUDED.sku,
              description_html = EXCLUDED.description_html,
              description_text = EXCLUDED.description_text,
              specs_json       = EXCLUDED.specs_json,
              updated_at       = now()
            RETURNING id
            """,
            (
                category_id,
                data.get("rozetka_product_id"),
                data.get("title"),
                data.get("brand"),
                data.get("sku"),
                data.get("url"),
                data.get("description_html"),
                data.get("description_text"),
                Jsonb(data.get("specs_json")) if data.get("specs_json") is not None else None,
            ),
        )
        product_id = cur.fetchone()[0]
    conn.commit()
    return product_id


async def go_next_reviews_page(page: Page) -> bool:
    """
    Переходить на наступну сторінку відгуків.
    Повертає True якщо клікнули і перейшли, False якщо next немає.
    """
    # Спроба 1: rel=next (найстабільніше якщо є)
    next_a = page.locator("a[rel='next']").first
    if await next_a.count() > 0:
        prev = page.url
        await next_a.click()
        await page.wait_for_load_state("domcontentloaded")
        return page.url != prev

    # Спроба 2: кнопка/лінк "Далі"
    next_btn = page.locator("a:has-text('Далі'), button:has-text('Далі'), a:has-text('Следующая'), button:has-text('Следующая')").first
    if await next_btn.count() > 0:
        prev = page.url
        await next_btn.click()
        await page.wait_for_load_state("domcontentloaded")
        return page.url != prev

    return False


async def fetch_all_review_pages(comments_url: str, *, timeout_ms: int = 120_000, max_pages: int = 200) -> list[dict[str, Any]]:
    """
    Збирає payload-и (html+ratings) з усіх сторінок comments.
    """
    ctx = await ensure_context()
    page = await safe_new_page(ctx)
    try:
        await page.goto(comments_url, wait_until="domcontentloaded", timeout=timeout_ms)

        all_payloads: list[dict[str, Any]] = []
        seen_urls = set()

        for _ in range(max_pages):
            # страховка від циклу
            if page.url in seen_urls:
                break
            seen_urls.add(page.url)

            # ---- зняти html+ratings з поточної сторінки ----
            try:
                await page.wait_for_selector('rz-comment-rating [data-testid="stars-rating"]', timeout=30_000)
            except Exception:
                pass
            
            # розгорнути всі відгуки однієї сторінки через "Показати ще"
            await expand_all_reviews_with_show_more(page, max_clicks=120)

            stars = page.locator('rz-comment-rating [data-testid="stars-rating"]')
            n = await stars.count()

            ratings: list[int | None] = []
            for i in range(n):
                style = await stars.nth(i).get_attribute("style") or ""
                ratings.append(clamp_star_rating(rating_from_style(style)))

            html = await page.content()

            # CF check саме тут
            if looks_like_cloudflare_challenge(html):
                raise HTTPException(
                    status_code=502,
                    detail=("Cloudflare challenge returned instead of content. "
                            "Зроби прогрів профілю (headless=False) у init_profile.py і пройди challenge вручну."),
                )

            all_payloads.append({
                "html": html,
                "ratings": ratings,
                "page_url": page.url,
            })

            # ---- next page? ----
            moved = await go_next_reviews_page(page)
            if not moved:
                break

            # легка пауза, щоб не “лупити”
            await page.wait_for_timeout(600)

        return all_payloads
    finally:
        await safe_close_page(page)


def rating_from_style(style: str) -> float | None:
    """
    style приклад: "width: calc(100% - 2px);" або "width: 80%;"
    Повертає рейтинг 0..5 (може бути дробовий).
    """
    if not style:
        return None
    s = style.replace(" ", "").lower()

    # width: calc(XX% - 2px) або width: XX%
    m = re.search(r"width:(?:calc\()?(\d+(?:\.\d+)?)%?", s)
    if not m:
        return None

    pct = float(m.group(1))  # 0..100
    return 5.0 * pct / 100.0

def clamp_star_rating(val: float | None) -> int | None:
    if val is None:
        return None
    r = int(round(val))
    return max(1, min(5, r))


def rozetka_comments_url(product_url: str) -> str:
    product_url = product_url.split("?")[0].rstrip("/") + "/"
    return product_url + "comments/"


def looks_like_cloudflare_challenge(html: str) -> bool:
    s = (html or "").lower()

    # Типові маркери саме challenge-сторінок
    challenge_markers = [
        "cf-chl-",                 # cf-chl-bypass / cf-chl-widget / cf-chl-jschl
        "/cdn-cgi/challenge",      # challenge endpoint
        "/cdn-cgi/l/chk_captcha",  # captcha endpoint
        "managed challenge",       # cf managed challenge
        "just a moment",           # заголовок
        "checking your browser",   # текст
        "cf-ray",                  # часто є на challenge
    ]

    # Якщо є "cloudflare" без цих маркерів — це НЕ доказ challenge
    return any(m in s for m in challenge_markers)


def parse_rozetka_reviews_from_html(html: str, source_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)

    chunks = text.split("Відгук від покупця.")
    if len(chunks) == 1:
        chunks = text.split("Отзыв от покупателя.")

    reviews: list[dict[str, Any]] = []
    for chunk in chunks[1:]:
        chunk = chunk.split("Відповісти")[0].split("Ответить")[0].strip()

        m_date = re.search(r"\b(\d{1,2}\s+[^\d]+\s+\d{4})\b", chunk)
        date = m_date.group(1).strip() if m_date else None

        pros = None
        cons = None
        if "Переваги:" in chunk:
            pros = chunk.split("Переваги:", 1)[1].split("Недоліки:", 1)[0].strip()
        if "Недоліки:" in chunk:
            cons = chunk.split("Недоліки:", 1)[1].strip()

        lines = [ln.strip() for ln in chunk.split("\n") if ln.strip()]
        body = None
        for ln in lines:
            if any(x in ln for x in ["Продавець:", "Серія:", "Колір:", "Вбудована пам'ять:"]):
                continue
            if ln in ["Переваги:", "Недоліки:"]:
                continue
            body = ln
            break

        if body or pros or cons:
            reviews.append({
                "date": date,
                "text": body,
                "pros": pros,
                "cons": cons,
                "source": "rozetka",
                "url": source_url,
            })

    return reviews


async def ensure_context() -> BrowserContext:
    global _pw, _ctx
    if _ctx is not None:
        return _ctx

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    _pw = await async_playwright().start()
    _ctx = await _pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",
        headless=False,  # <- ВАЖЛИВО: для тесту CF постав False
        locale="uk-UA",
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        args=[
            "--disable-blink-features=AutomationControlled",
        ],
    )

    await _ctx.add_init_script("""
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)

    return _ctx

async def safe_new_page(ctx: BrowserContext) -> Page:
    """
    Відкриває нову вкладку з контролем паралельності.
    """
    await _page_sem.acquire()
    try:
        page = await ctx.new_page()
        return page
    except Exception:
        _page_sem.release()
        raise


async def safe_close_page(page: Page) -> None:
    """
    Закриває вкладку і звільняє слот семафора.
    """
    try:
        await page.close()
    finally:
        _page_sem.release()


async def fetch_html(url: str, *, timeout_ms: int = 120_000) -> dict[str, Any]:
    global _ctx

    async def _one_try() -> dict[str, Any]:
        ctx = await ensure_context()
        page = await safe_new_page(ctx)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            # Debug (щоб бачити CF чи ні)
            try:
                print("DEBUG url:", page.url)
                print("DEBUG title:", await page.title())
            except Exception:
                pass

            # Дочекатись, щоб відгуки + зірки точно зʼявились у DOM
            # (цей селектор є на твоєму фрагменті HTML)
            try:
                await page.wait_for_selector(
                    'rz-comment-rating [data-testid="stars-rating"]',
                    timeout=30_000
                )
            except Exception:
                # не фейлимось — просто підемо далі
                pass
            
            # доклікати "Показати ще"
            clicks = await expand_all_reviews_with_show_more(page, max_clicks=200)
            print("DEBUG show_more_clicks:", clicks)
            print(
                "DEBUG stars_count:",
                await page.locator('rz-comment-rating [data-testid="stars-rating"]').count()
            )

            # Витягуємо рейтинги по кожному відгуку
            # 1) знаходимо всі блоки зірок
            stars = page.locator('rz-comment-rating [data-testid="stars-rating"]')
            n = await stars.count()

            ratings: list[int | None] = []
            for i in range(n):
                el = stars.nth(i)
                style = await el.get_attribute("style") or ""
                val = rating_from_style(style)
                ratings.append(clamp_star_rating(val))

            html = await page.content()

            return {
                "html": html,
                "ratings": ratings,     # список рейтингів у порядку появи на сторінці
                "ratings_count": n,
                "final_url": page.url,
                "title": await page.title(),
            }
        finally:
            await safe_close_page(page)

    try:
        payload = await _one_try()
    except PlaywrightTimeoutError:
        raise HTTPException(status_code=504, detail=f"Timeout while loading: {url}")
    except Exception:
        # self-heal: перезапустити контекст і повторити 1 раз
        await reset_context()
        try:
            payload = await _one_try()
        except PlaywrightTimeoutError:
            raise HTTPException(status_code=504, detail=f"Timeout while loading (after restart): {url}")
        except Exception as e2:
            raise HTTPException(status_code=502, detail=f"Failed to load page: {type(e2).__name__}: {e2}") from e2

    html = payload.get("html", "")
    if looks_like_cloudflare_challenge(html):
        raise HTTPException(
            status_code=502,
            detail=(
                "Cloudflare challenge returned instead of content. "
                "Зроби прогрів профілю (headless=False) у init_profile.py і пройди challenge вручну."
            ),
        )

    return payload



async def reset_context() -> None:
    """
    Акуратно закриває контекст і Playwright (якщо є),
    щоб можна було підняти знову.
    """
    global _pw, _ctx
    try:
        if _ctx is not None:
            await _ctx.close()
    except Exception:
        pass
    finally:
        _ctx = None

    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:
        pass
    finally:
        _pw = None



def parse_ua_date(s: str | None):
    if not s:
        return None
    s = s.strip().lower()
    m = re.match(r"(\d{1,2})\s+([^\d]+)\s+(\d{4})", s)
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2).strip()
    year = int(m.group(3))
    month = UA_MONTHS.get(month_name)
    if not month:
        return None
    return datetime(year, month, day).date()


def ensure_unknown_category(conn) -> int:
    cur = conn.execute(
        "INSERT INTO categories(name, url) VALUES (%s, %s) "
        "ON CONFLICT (url) DO UPDATE SET name=EXCLUDED.name "
        "RETURNING id",
        ("Unknown", "about:unknown"),
    )
    return cur.fetchone()[0]



def insert_reviews(conn, *, product_id: int, comments_url: str, reviews: list[dict[str, Any]]) -> int:
    rows = []
    for r in reviews:
        rows.append((
            product_id,
            "rozetka",                 # source
            None,                      # source_review_id (поки нема)
            comments_url,              # source_url
            None,                      # author_name
            None,                      # author_badge
            r.get("rating"),           # rating
            parse_ua_date(r.get("date")),
            r.get("text"),             # ✅ було body -> стало text
            r.get("pros"),
            r.get("cons"),
            None,                      # raw_json
        ))

    if not rows:
        return 0

    with conn.cursor() as cur:
        # (опційно) MVP-дедуп (якщо не маєш source_review_id)
        cur.execute("""
          CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_mvp_dedupe
          ON public.reviews (product_id, review_date, rating, left(coalesce(text,''), 80))
        """)

        cur.executemany(
            """
            INSERT INTO public.reviews (
              product_id, source, source_review_id, source_url,
              author_name, author_badge, rating, review_date,
              text, pros, cons, raw_json
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
            """,
            rows,
        )

    conn.commit()
    return len(rows)



@app.on_event("startup")
async def on_startup():
    # Піднімаємо контекст одразу, щоб перший запит не “грівся”
    await ensure_context()


@app.on_event("shutdown")
async def on_shutdown():
    await reset_context()


@app.get("/health")
async def health():
    return {"ok": True, "max_concurrent_pages": MAX_CONCURRENT_PAGES}


@app.post("/fetch/rozetka/to_db")
async def fetch_to_db(req: FetchReq):

    product_url = req.product_url.split("?")[0].rstrip("/") + "/"

    # 1) fetch product page html
    product_html = await fetch_product_html(product_url, timeout_ms=120_000)

    # 2) parse details
    product_data = parse_product_details_from_html(product_html, product_url)

    product = req.product_url.split("?")[0].rstrip("/") + "/"
    comments_url = rozetka_comments_url(product)

    payloads = await fetch_all_review_pages(comments_url, timeout_ms=120_000)

    all_reviews: list[dict[str, Any]] = []
    for payload in payloads:
        reviews = parse_rozetka_reviews_from_html(payload["html"], comments_url)
        ratings = payload.get("ratings", [])
        for i, r in enumerate(reviews):
            r["rating"] = ratings[i] if i < len(ratings) else None
        all_reviews.extend(reviews)

    # ---- DB write ----
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            # psycopg v3: execute через conn теж можна; лишаю просто
            pass
        # простіше без cursor:
        cat_id = ensure_unknown_category(conn)
        #product_id = upsert_product(conn, category_id=cat_id, url=product, title=None)
        # 3) upsert product -> product_id
        product_id = upsert_product(conn, category_id=cat_id, data=product_data)
        inserted = insert_reviews(conn, product_id=product_id, comments_url=comments_url, reviews=all_reviews)
        conn.commit()

    return {"product_url": product, "pages": len(payloads), "count": len(all_reviews), "attempted_insert": inserted}
