import pandas as pd
import numpy as np

RAW = "data/raw_reviews.parquet"
OUT_AGG = "data/products_agg.parquet"

def dirichlet_smoothed_rating(counts_1to5: np.ndarray, alpha_1to5: np.ndarray) -> float:
    counts = counts_1to5.astype(float)
    alpha = alpha_1to5.astype(float)
    post = counts + alpha
    probs = post / post.sum()
    stars = np.arange(1, 6, dtype=float)
    return float((stars * probs).sum())

def _ensure_1to5_cols(pivot: pd.DataFrame) -> pd.DataFrame:
    for k in [1, 2, 3, 4, 5]:
        if k not in pivot.columns:
            pivot[k] = 0
    return pivot[[1, 2, 3, 4, 5]].copy()

def _mode_or_nan(s: pd.Series):
    s = s.dropna()
    if s.empty:
        return np.nan
    m = s.mode()
    return m.iloc[0] if not m.empty else s.iloc[0]

def main():
    df = pd.read_parquet(RAW)

    # must-have: sku
    df = df[df["sku"].notna()].copy()
    df["sku"] = df["sku"].astype(str).str.strip()
    df = df[df["sku"].str.len() > 0].copy()

    # rating clean (на випадок якщо RAW ще не нормалізований)
    df["rating"] = pd.to_numeric(df["rating"], errors="coerce")
    df = df[df["rating"].notna()].copy()
    df["rating"] = df["rating"].astype(int).clip(1, 5)

    # --- 1) counts per SKU per star ---
    pivot_sku = (
        df.assign(cnt=1)
          .pivot_table(index="sku", columns="rating", values="cnt", aggfunc="sum", fill_value=0)
    )
    pivot_sku = _ensure_1to5_cols(pivot_sku)

    # --- 2) sku -> meta (mode) ---
    sku_cat = df.groupby("sku")["category_id"].apply(_mode_or_nan)

    # brand та category_name — теж на рівні sku через mode
    sku_brand = df.groupby("sku")["brand"].apply(_mode_or_nan) if "brand" in df.columns else None
    sku_cat_name = df.groupby("sku")["category_name"].apply(_mode_or_nan) if "category_name" in df.columns else None

    # --- 3) prior per category (Dirichlet alpha by category_id) ---
    pivot_cat = (
        df.assign(cnt=1)
          .pivot_table(index="category_id", columns="rating", values="cnt", aggfunc="sum", fill_value=0)
    )
    pivot_cat = _ensure_1to5_cols(pivot_cat)

    # глобальний fallback апріорі
    global_counts = pivot_sku.sum(axis=0).values.astype(float)
    global_probs = global_counts / global_counts.sum() if global_counts.sum() > 0 else np.ones(5) / 5

    prior_strength = 20.0
    alpha_global = global_probs * prior_strength

    # alpha per category
    alpha_by_cat = {}
    for cat_id, row in pivot_cat.iterrows():
        cat_counts = row.values.astype(float)
        s = cat_counts.sum()
        if s <= 0:
            alpha_by_cat[cat_id] = alpha_global
        else:
            cat_probs = cat_counts / s
            alpha_by_cat[cat_id] = cat_probs * prior_strength

    # --- 4) smoothed rating per SKU ---
    work = pivot_sku.copy()
    work["category_id"] = sku_cat

    def smooth_row(row: pd.Series) -> float:
        cat_id = row["category_id"]
        alpha = alpha_by_cat.get(cat_id, alpha_global)
        counts = row[[1, 2, 3, 4, 5]].values
        return dirichlet_smoothed_rating(counts, alpha)

    smoothed = work.apply(smooth_row, axis=1)

    # --- 5) raw mean rating per SKU (для контролю) ---
    raw_mean = df.groupby("sku")["rating"].mean()

    # --- 6) output ---
    out = pivot_sku.copy()
    out.columns = [f"n_{k}" for k in [1, 2, 3, 4, 5]]
    out["reviews_count"] = out.sum(axis=1)
    out["category_id"] = sku_cat
    if sku_cat_name is not None:
        out["category_name"] = sku_cat_name
    if sku_brand is not None:
        out["brand"] = sku_brand

    out["rating_raw"] = raw_mean
    out["rating_smoothed"] = smoothed

    out.reset_index().to_parquet(OUT_AGG, index=False)
    print("Saved:", OUT_AGG, "rows (SKU):", len(out))

if __name__ == "__main__":
    main()
