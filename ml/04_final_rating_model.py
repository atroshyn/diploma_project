import pandas as pd
import numpy as np
from pathlib import Path

FEATURES = "data/features.parquet"
AGG = "data/products_agg.parquet"

OUT_DATA = "data/products_final_scores_scenarios.parquet"
OUT_SUMMARY = "data/final_score_scenarios_summary.csv"

SCENARIOS = [
    ("soft",        0.40, 1.00, 0.50, 10),
    ("balanced",    0.80, 1.20, 1.00, 10),
    ("strict",      1.30, 1.50, 1.50, 10),
    ("very_strict", 1.70, 1.80, 2.00, 15),
]

def compute_penalty(prod, lam, gamma, beta_mismatch, min_reviews_for_full_penalty):
    share_neg = prod["share_neg"].astype(float)
    mismatch = prod["mismatch_rate"].astype(float)
    relevant = prod["relevant_rate"].astype(float)
    n = prod["reviews_count"].fillna(0).astype(float)

    trust = 0.5 + 0.5 * relevant
    mismatch_boost = 1.0 + beta_mismatch * mismatch
    conf = np.minimum(1.0, n / float(max(1, min_reviews_for_full_penalty)))

    return lam * (share_neg ** gamma) * mismatch_boost * trust * conf

def summarize_global(prod, score_col):
    base = prod["rating_smoothed"].astype(float).clip(1, 5)
    s = prod[score_col].astype(float)
    delta = s - base

    return {
        "scenario": score_col.replace("final_score__", ""),
        "skus": int(len(prod)),
        "avg_base": float(base.mean()),
        "avg_final": float(s.mean()),
        "avg_delta": float(delta.mean()),
        "share_drop_gt_0.3": float((delta < -0.3).mean()),
        "share_drop_gt_0.7": float((delta < -0.7).mean()),
        "share_final_lt_3": float((s < 3.0).mean()),
    }

def main():
    Path("data").mkdir(parents=True, exist_ok=True)

    feat = pd.read_parquet(FEATURES)
    agg = pd.read_parquet(AGG)

    # --- 1) normalize features ---
    feat = feat.drop(columns=["reviews_count"], errors="ignore")

    feat = feat.rename(columns={
        "sent_pos_share": "share_pos",
        "sent_neg_share": "share_neg",
        "relevant_share": "relevant_rate",
        "mismatch_share": "mismatch_rate",
    })

    if "share_neu" not in feat.columns:
        feat["share_neu"] = (
            1.0 - feat["share_pos"].astype(float) - feat["share_neg"].astype(float)
        ).clip(0, 1)

    for c in ["share_pos", "share_neu", "share_neg", "relevant_rate", "mismatch_rate"]:
        if c not in feat.columns:
            feat[c] = 0.0
        feat[c] = feat[c].fillna(0.0).astype(float).clip(0, 1)

    # --- 2) merge on SKU ---
    prod = agg.merge(feat, on="sku", how="left")

    defaults = {
        "share_pos": 0.0,
        "share_neu": 1.0,
        "share_neg": 0.0,
        "relevant_rate": 1.0,
        "mismatch_rate": 0.0,
    }

    for c, v in defaults.items():
        prod[c] = prod[c].fillna(v)

    prod["base_score"] = prod["rating_smoothed"].astype(float).clip(1, 5)

    # --- 3) compute scenarios ---
    summaries = []
    for name, lam, gamma, beta_mismatch, min_n in SCENARIOS:
        pen = compute_penalty(prod, lam, gamma, beta_mismatch, min_n)
        col = f"final_score__{name}"
        prod[col] = (prod["base_score"] - pen).clip(1, 5)

        # global summary
        summaries.append(summarize_global(prod, col))

    # --- 4) SAVE FULL DATA ---
    # гарантуємо що бренд і категорія є
    final_cols_order = [
        "sku",
        "brand",
        "category_id",
        "category_name",
        "reviews_count",
        "rating_raw",
        "rating_smoothed",
        "base_score",
    ] + [c for c in prod.columns if c.startswith("final_score__")]

    # беремо тільки ті, що реально існують
    final_cols_order = [c for c in final_cols_order if c in prod.columns]

    prod[final_cols_order].to_parquet(OUT_DATA, index=False)
    print("Saved:", OUT_DATA, "rows:", len(prod))

    # --- 5) SAVE SUMMARY (додаємо розбивку по категоріях) ---
    summary_df = pd.DataFrame(summaries)

    # агрегована статистика по категоріях (для balanced сценарію як основного)
    if "final_score__balanced" in prod.columns:
        cat_summary = (
            prod.groupby(["category_id", "category_name"])
                .agg(
                    skus=("sku", "count"),
                    avg_base=("rating_smoothed", "mean"),
                    avg_final_balanced=("final_score__balanced", "mean"),
                )
                .reset_index()
        )
        cat_summary.to_csv(
            OUT_SUMMARY.replace(".csv", "_by_category.csv"),
            index=False,
            encoding="utf-8"
        )

    summary_df.to_csv(OUT_SUMMARY, index=False, encoding="utf-8")
    print("Saved:", OUT_SUMMARY)
    print(summary_df.to_string(index=False))

if __name__ == "__main__":
    main()
