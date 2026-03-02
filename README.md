# Інтелектуальна система оцінки рейтингу товарів та виробників (MVP)

MVP-проєкт для:
-збору URL товарів з категорій Rozetka
-збору відгуків + рейтингу + даних товару
-збереження результатів у PostgreSQL
-побудови інтелектуального рейтингу товарів (SKU-level)
-аналітики по брендах та категоріях

Проєкт розділений на **скрипти збору** та **API-сервіс**.

---

## 📁 Структура проєкту

diploma_project/
│
├── app_v2.py # FastAPI сервіс: збір відгуків і запис у БД
├── collect_category_urls.py # Збір URL товарів з категорії Rozetka (Playwright)
├── run_range_to_db.py # Batch-запуск: відправка URL у API
├── product_urls.json # Список зібраних URL товарів
│
├── init_profile.py # Прогрів Playwright-профілю (Cloudflare)
├── pw_profile/ # Persistent browser profile (cookies, CF)
│
├── ml/
│   ├── 01_extract_raw_reviews.py  # Витяг даних з PostgreSQL у parquet
│   ├── 02_nlp_features.py         # Sentiment + NLP-ознаки (SKU-level)
│   ├── 03_dirichlet_smoothing.py  # Bayesian згладжування рейтингу
│   └── 04_final_scoring.py        # Інтегральний фінальний скор
│
└── README.md

---

## 🧠 Архітектура

[ Rozetka category ]
↓
collect_category_urls.py
↓
product_urls.json
↓
run_range_to_db.py ──► FastAPI (app_v2.py)
↓
PostgreSQL
↓
01_extract_dataset.py
↓
02_nlp_sentiment_and_filters.py
↓
03_dirichlet_smoothing.py
↓
04_final_rating_model.py
↓
Final product scores

- **Playwright** використовується тільки там, де потрібен JS
- **FastAPI** — єдина точка запису в БД
- **PostgreSQL** — основне сховище

---

## ⚙️ Вимоги

- Python **3.10+**
- PostgreSQL **14+**
- Google Chrome (або Chromium)
- Windows / Linux / macOS

Python-пакети:
```bash
pip install fastapi uvicorn playwright bs4 psycopg requests
playwright install
🗄️ База даних
Приклад DSN
postgresql://reviews_user:STRONG_PASSWORD@localhost:5432/reviews_db
Змінна середовища:

$env:DB_DSN="postgresql://reviews_user:YOUR_PASSWORD@localhost:5432/reviews_db"


🚀 Крок 1. Прогрів Cloudflare (обовʼязково 1 раз)
python init_profile.py
відкриється браузер

пройди Cloudflare / captcha вручну

закрий браузер

Профіль збережеться у pw_profile/.

🚀 Крок 2. Запуск API
uvicorn app_v2:app --host 0.0.0.0 --port 8000
Перевір:

http://localhost:8000/health

http://localhost:8000/docs

Endpoint для збору в БД:

POST /fetch/rozetka/to_db

🚀 Крок 3. Збір URL товарів з категорії
Приклад для категорії Зарядні станції:

python collect_category_urls.py
Результат:

product_urls.json
Формат:

[
  "https://rozetka.com.ua/ua/365360001/p365360001/",
  "https://rozetka.com.ua/ua/364123456/p364123456/"
]
✔️ URL вже очищені від /comments/
✔️ Без дублікатів

🚀 Крок 4. Batch-збір і запис у БД
Запуск з інтервалом:

$env:ROZETKA_API_URL="http://localhost:8000/fetch/rozetka/to_db"
python run_range_to_db.py --start 1 --end 20 --sleep 2
Параметри:

--start — початковий індекс у product_urls.json

--end — кінцевий (не включно)

--sleep — пауза між запитами (сек)

Приклад логів:

[1] OK  https://rozetka.com.ua/ua/365360001/p365360001/
[2] OK  https://rozetka.com.ua/ua/364123456/p364123456/
DONE ok=2 fail=0
Таблиця products
title
brand
sku
category_id
description
rating_avg
reviews_count

Таблиця reviews
rating (1–5)
text / pros / cons
review_date
source_url

Побудова інтелектуального рейтингу
Крок 1 — Експорт сирих даних
python ml/01_extract_dataset.py

Створюється:
data/raw_reviews.parquet
Містить:
sku
brand
category_id
category_name
rating
text
review metadata

Крок 2 — NLP-аналіз відгуків
python ml/02__nlp_sentiment_and_filters.py

Використовується модель:
cardiffnlp/twitter-xlm-roberta-base-sentiment

Результат:
data/features.parquet

На рівні SKU:
sent_pos_share
sent_neg_share
relevant_share
mismatch_share
avg_sent_score
reviews_count

Крок 3 — Bayesian згладжування (Dirichlet)
python ml/03_dirichlet_smoothing.py

Особливості:
агрегування на рівні SKU
апріорі рахується по category_id
використовується емпіричний Байєс
захист від малого n

Створюється:
data/products_agg.parquet

Містить:
sku
brand
category_name
n_1..n_5
reviews_count
rating_raw
rating_smoothed

Крок 4 — Інтегральний фінальний скор
python ml/04_final_rating_model.py

Фінальний скор враховує:
Bayesian rating (rating_smoothed)
частку негативних відгуків
частку mismatch (конфлікт rating ↔ sentiment)
релевантність текстів
confidence (кількість відгуків)

Результати
📦 products_final_scores_scenarios.parquet

Містить:
sku
brand
category_id
category_name
reviews_count
rating_raw
rating_smoothed
base_score
final_score__soft
final_score__balanced
final_score__strict
final_score__very_strict

final_score_scenarios_summary.csv
Глобальна статистика:
середній базовий рейтинг
середній фінальний рейтинг
частка падіння > 0.3
частка товарів з рейтингом < 3

final_score_scenarios_summary_by_category.csv
Середній фінальний скор по категоріях.

🧯 Типові проблеми
404 Not Found
перевір endpoint: /fetch/rozetka/to_db
перевір ROZETKA_API_URL

Cloudflare challenge
повторно запусти init_profile.py

не використовуй headless=True для першого запуску

Нема brand / sku
не всі сторінки Rozetka їх публікують

fallback: береться з JSON-LD або specs

🧩 Подальші покращення (roadmap)
🧠 sentiment analysis (BERT)
🌐 web-інтерфейс


Підсумок
Система реалізує:
Збір e-commerce даних
Збереження у PostgreSQL
NLP-аналіз текстових відгуків
Bayesian згладжування
Інтегральний скор
Аналітику по SKU / брендах / категоріях
