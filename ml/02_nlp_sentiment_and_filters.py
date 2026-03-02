import os
import pandas as pd
from transformers import pipeline

RAW = "data/raw_reviews.parquet"
OUT = "data/features.parquet"

SENTIMENT_MODEL = os.environ.get(
    "SENTIMENT_MODEL",
    "cardiffnlp/twitter-xlm-roberta-base-sentiment"
)

def sentiment_to_3(label: str) -> str:
    l = (label or "").lower()
    if "neg" in l:
        return "neg"
    if "neu" in l:
        return "neu"
    if "pos" in l:
        return "pos"
    return "neu"

def is_relevant_quality_rule(text: str) -> bool:
    t = (text or "").lower()
    bad_markers = ["доставка", "нова пошта", "кур'єр", "упаковк",
                   "сервіс", "менеджер", "оплата", "кредит", "розстрочк",
                   "повернен", "гаранті", "обмін"]

    # відсікаємо "чисту логістику" тільки якщо коротко
    if any(m in t for m in bad_markers) and len(t) < 150:
        return False

    return True

def mismatch_rule(sent3: str, rating: int) -> bool:
    if sent3 == "neg" and rating >= 4:
        return True
    if sent3 == "pos" and rating <= 2:
        return True
    return False

def main():
    df = pd.read_parquet(RAW)

    # 🔒 працюємо тільки з SKU
    df = df[df["sku"].notna()].copy()

    sent_pipe = pipeline(
        "text-classification",
        model=SENTIMENT_MODEL,
        tokenizer=SENTIMENT_MODEL,
        truncation=True,
        max_length=256,      
        padding=True,
        top_k=None
    )

    texts = df["text"].astype(str).tolist()
    preds = sent_pipe(texts, batch_size=16)

    labels = []
    scores = []

    for p in preds:
        if isinstance(p, list):
            p0 = max(p, key=lambda x: x["score"])
        else:
            p0 = p
        labels.append(p0["label"])
        scores.append(float(p0["score"]))

    df["sent_label_raw"] = labels
    df["sent_score"] = scores
    df["sentiment"] = df["sent_label_raw"].apply(sentiment_to_3)

    df["relevant_quality"] = df["text"].apply(is_relevant_quality_rule)
    df["mismatch"] = df.apply(
        lambda r: mismatch_rule(r["sentiment"], int(r["rating"])),
        axis=1
    )

    # ---------------------------------------------------
    # 🔥 АГРЕГАЦІЯ НА РІВНІ SKU
    # ---------------------------------------------------

    agg = (
        df.groupby("sku")
          .agg(
              reviews_count=("rating", "count"),
              sent_pos=("sentiment", lambda x: (x == "pos").sum()),
              sent_neu=("sentiment", lambda x: (x == "neu").sum()),
              sent_neg=("sentiment", lambda x: (x == "neg").sum()),
              relevant_share=("relevant_quality", "mean"),
              mismatch_share=("mismatch", "mean"),
              avg_sent_score=("sent_score", "mean"),
          )
    )

    # нормалізовані частки
    agg["sent_pos_share"] = agg["sent_pos"] / agg["reviews_count"]
    agg["sent_neg_share"] = agg["sent_neg"] / agg["reviews_count"]

    agg.reset_index().to_parquet(OUT, index=False)

    print("Saved:", OUT, "rows (SKU):", len(agg))


if __name__ == "__main__":
    main()
