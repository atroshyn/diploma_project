import os
import pandas as pd
from sqlalchemy import create_engine
from ml.config import DB_DSN, RAW_REVIEWS_PARQUET, DATA_DIR

SQL = """
SELECT
  r.id               AS review_id,
  r.product_id,
  r.rating,
  r.text             AS text,
  r.pros,
  r.cons,
  r.review_date,
  r.source_url       AS review_url,

  p.id               AS product_pk,
  p.title            AS product_name,
  p.brand            AS brand,
  p.sku              AS sku,
  p.url              AS product_url,
  p.category_id      AS category_id,

  c.name             AS category_name
FROM reviews r
JOIN products p ON p.id = r.product_id
LEFT JOIN categories c ON c.id = p.category_id
"""

def _norm_series(s: pd.Series) -> pd.Series:
    return (
        s.fillna("")
         .astype(str)
         .str.replace(r"\s+", " ", regex=True)
         .str.strip()
    )

def _build_effective_text(df: pd.DataFrame) -> pd.Series:
    """
    Склеюємо text + pros/cons, щоб не втрачати відгуки без text.
    """
    text = _norm_series(df["text"])
    pros = _norm_series(df["pros"])
    cons = _norm_series(df["cons"])

    eff = (
        text.where(text.str.len() > 0, "")
        + (("\nPros: " + pros).where(pros.str.len() > 0, ""))
        + (("\nCons: " + cons).where(cons.str.len() > 0, ""))
    ).str.strip()

    return eff

def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    engine = create_engine(DB_DSN)
    df = pd.read_sql(SQL, engine)

    # 1) rating clean
    df["rating"] = pd.to_numeric(df["rating"], errors="coerce")
    df = df[df["rating"].notna()].copy()
    df["rating"] = df["rating"].astype(int).clip(1, 5)

    # 2) sku must-have (бо весь пайплайн на SKU)
    df["sku"] = _norm_series(df["sku"])
    df = df[df["sku"].str.len() > 0].copy()

    # 3) category_id normalize (корисно для groupby)
    df["category_id"] = pd.to_numeric(df["category_id"], errors="coerce")

    # 4) effective text (text/pros/cons)
    df["text"] = _build_effective_text(df)
    df = df[df["text"].str.len() > 0].copy()

    # 5) normalize meta strings
    for col in ["brand", "product_name", "category_name", "product_url", "review_url"]:
        if col in df.columns:
            df[col] = _norm_series(df[col])

    df.to_parquet(RAW_REVIEWS_PARQUET, index=False)
    print("Saved:", RAW_REVIEWS_PARQUET, "rows:", len(df))
    print("Columns:", list(df.columns))

if __name__ == "__main__":
    main()
