import time
import numpy as np
import pandas as pd
import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

# моделі для тесту
MODELS = [
    "cardiffnlp/twitter-xlm-roberta-base-sentiment",
    "cointegrated/rubert-tiny-sentiment-balanced",  
]

LABEL_MAP_DEFAULT = {
    # для моделей типу cardiffnlp: labels = ['negative','neutral','positive']
    "LABEL_0": "neg",
    "LABEL_1": "neu",
    "LABEL_2": "pos",
    "negative": "neg",
    "neutral": "neu",
    "positive": "pos",
    "NEGATIVE": "neg",
    "NEUTRAL": "neu",
    "POSITIVE": "pos",
}

def predict_sentiment(model_name: str, texts: list[str], batch_size: int = 16):
    tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=True)
    model = AutoModelForSequenceClassification.from_pretrained(model_name)
    model.eval()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)

    preds = []
    probs = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        enc = tokenizer(
            batch,
            truncation=True,
            padding=True,
            max_length=256,
            return_tensors="pt",
        ).to(device)

        with torch.no_grad():
            out = model(**enc)
            p = torch.softmax(out.logits, dim=-1).cpu().numpy()

        idx = p.argmax(axis=1)
        labels = [model.config.id2label[int(j)] for j in idx]
        labels = [LABEL_MAP_DEFAULT.get(l, l) for l in labels]

        preds.extend(labels)
        probs.extend(p.max(axis=1).tolist())

    return preds, probs

def main():
    df = pd.read_parquet("data/raw_reviews.parquet")
    df = df[df["text"].notna()].copy()
    df["text"] = df["text"].astype(str).str.strip()
    df = df[df["text"] != ""].copy()

    # можемо обмежити
    sample = df.sample(min(10000, len(df)), random_state=42).copy()
    texts = sample["text"].tolist()

    rows = []
    for m in MODELS:
        t0 = time.time()
        pred, conf = predict_sentiment(m, texts, batch_size=16)
        dt = time.time() - t0
        rows.append({
            "model": m,
            "n": len(texts),
            "sec": round(dt, 2),
            "avg_conf": float(np.mean(conf)),
            "share_neg": float(np.mean([p == "neg" for p in pred])),
            "share_neu": float(np.mean([p == "neu" for p in pred])),
            "share_pos": float(np.mean([p == "pos" for p in pred])),
        })

    out = pd.DataFrame(rows).sort_values("sec")
    print(out.to_string(index=False))
    out.to_csv("data/sentiment_models_compare.csv", index=False, encoding="utf-8")
    print("Saved: data/sentiment_models_compare.csv")

if __name__ == "__main__":
    main()
