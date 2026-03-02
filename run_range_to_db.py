import json
import time
import argparse
import requests

API_URL = "http://localhost:8000/fetch/rozetka/to_db"   
IN_FILE = "product_urls.json"

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=int, required=True, help="0-based start index inclusive")
    p.add_argument("--end", type=int, required=True, help="0-based end index exclusive")
    p.add_argument("--sleep", type=float, default=1.0, help="sleep between requests")
    args = p.parse_args()

    with open(IN_FILE, "r", encoding="utf-8") as f:
        urls = json.load(f)

    batch = urls[args.start:args.end]
    print(f"Batch size: {len(batch)} (indexes {args.start}:{args.end})")

    ok = 0
    fail = 0

    for i, url in enumerate(batch, start=args.start):
        payload = {"product_url": url}
        try:
            resp = requests.post(API_URL, json=payload, timeout=600)  # довго, бо playwright
            if resp.status_code >= 400:
                print(f"[{i}] FAIL {resp.status_code} {url} -> {resp.text[:300]}")
                fail += 1
            else:
                data = resp.json()
                print(f"[{i}] OK {url} -> reviews={data.get('count') or data.get('reviews_count')} inserted={data.get('attempted_insert') or data.get('inserted')}")
                ok += 1
        except Exception as e:
            print(f"[{i}] EXC {url} -> {type(e).__name__}: {e}")
            fail += 1

        time.sleep(args.sleep)

    print(f"DONE ok={ok} fail={fail}")

if __name__ == "__main__":
    main()
