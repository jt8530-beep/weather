#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Enrich wallet alpha market_watchlist with Gamma metadata.

No wallet, no signing, no orders.

The first Phase 1 run showed all wallet history categories as UNKNOWN because
trade payloads did not include market metadata. This script scans active Gamma
events and matches market_watchlist ids against market_id / condition_id / slug.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List

from dotenv import load_dotenv

from pm_weather_arb.config import Config
from pm_weather_arb.gamma import GammaClient, parse_markets_from_events
from pm_weather_arb.util import first_present


CATEGORY_RULES = [
    ("weather", ["temperature", "weather", "rain", "snow", "hurricane", "tornado", "wind"]),
    ("crypto", ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "doge", "crypto", "binance", "coinbase"]),
    ("sports", ["nba", "nfl", "nhl", "mlb", "ufc", "soccer", "champions league", "premier league", "tennis", "golf", "f1"]),
    ("politics", ["election", "trump", "biden", "republican", "democrat", "senate", "house", "governor", "president", "minister"]),
    ("economics", ["fed", "inflation", "cpi", "rate", "recession", "gdp", "unemployment", "tariff"]),
    ("business", ["earnings", "ipo", "tesla", "nvidia", "apple", "microsoft", "stock", "spacex", "openai"]),
    ("culture", ["movie", "album", "song", "grammy", "oscar", "taylor", "weeknd", "sabrina", "box office"]),
]


def norm(s: object) -> str:
    return re.sub(r"\s+", " ", str(s or "").lower()).strip()


def infer_category(*parts: object) -> str:
    text = norm(" ".join(str(x or "") for x in parts))
    for cat, kws in CATEGORY_RULES:
        if any(k in text for k in kws):
            return cat
    return "unknown"


def read_csv(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: List[dict], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def fetch_gamma_markets(pages: int, limit: int, order: str) -> Dict[str, dict]:
    gamma = GammaClient(Config())
    events = []
    for page in range(pages):
        batch = gamma.list_events_raw({
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": page * limit,
            "order": order,
            "ascending": "false",
        })
        if not batch:
            break
        events.extend(batch)
        if len(batch) < limit:
            break

    idx: Dict[str, dict] = {}
    for e in events:
        event_id = str(first_present(e, "id", "eventId", default=""))
        event_title = str(first_present(e, "title", "question", default=""))
        event_slug = str(first_present(e, "slug", default=""))
        event_cat = str(first_present(e, "category", "subcategory", default=""))
        tags = e.get("tags") or []
        tag_text = " ".join(str(t.get("label") or t.get("slug") or t.get("name") or "") for t in tags if isinstance(t, dict))
        for raw in e.get("markets") or []:
            if not isinstance(raw, dict):
                continue
            market_id = str(first_present(raw, "id", "marketId", default=""))
            condition_id = str(first_present(raw, "conditionId", "condition_id", "questionID", default=""))
            market_slug = str(first_present(raw, "slug", default=""))
            q = str(first_present(raw, "question", "title", default=""))
            desc = str(first_present(raw, "description", "resolutionSource", default=""))
            cat = infer_category(event_cat, tag_text, event_title, event_slug, market_slug, q, desc)
            rec = {
                "event_id": event_id,
                "event_title": event_title,
                "event_slug": event_slug,
                "market_id_gamma": market_id,
                "condition_id": condition_id,
                "market_slug": market_slug,
                "question": q,
                "description": desc,
                "category_enriched": cat,
                "event_category_raw": event_cat,
                "tags_raw": tag_text,
            }
            for key in [market_id, condition_id, market_slug]:
                if key:
                    idx[key.lower()] = rec
    return idx


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--market-watchlist", default="paper_logs/wallet_alpha/market_watchlist.csv")
    p.add_argument("--output", default="paper_logs/wallet_alpha/market_watchlist_enriched.csv")
    p.add_argument("--pages", type=int, default=50)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--order", default="volume_24hr")
    args = p.parse_args()

    load_dotenv()
    rows = read_csv(Path(args.market_watchlist))
    idx = fetch_gamma_markets(args.pages, args.limit, args.order)
    out = []
    matched = 0
    for r in rows:
        market_id = str(r.get("market_id") or "").lower()
        meta = idx.get(market_id)
        if meta:
            matched += 1
        meta = meta or {}
        out.append({
            **r,
            "matched_gamma": "1" if meta else "0",
            "category_enriched": meta.get("category_enriched", r.get("top_category", "UNKNOWN") or "UNKNOWN"),
            "event_title": meta.get("event_title", ""),
            "event_slug": meta.get("event_slug", ""),
            "market_slug": meta.get("market_slug", ""),
            "question": meta.get("question", ""),
            "condition_id": meta.get("condition_id", ""),
            "event_category_raw": meta.get("event_category_raw", ""),
            "tags_raw": meta.get("tags_raw", ""),
        })

    fields = list(out[0].keys()) if out else ["market_id", "matched_gamma", "category_enriched"]
    write_csv(Path(args.output), out, fields)
    cats = Counter([r.get("category_enriched", "unknown") for r in out])
    print(
        f"MARKET_ENRICH_SUMMARY input={len(rows)} gamma_index={len(idx)} matched={matched} "
        f"unmatched={len(rows)-matched} output={args.output} cats=" + ",".join(f"{k}:{v}" for k, v in cats.most_common(10))
    )
    for r in out[:30]:
        print(
            f"MARKET_ENRICH market={r.get('market_id')} matched={r.get('matched_gamma')} cat={r.get('category_enriched')} "
            f"event=\"{str(r.get('event_title'))[:80]}\" q=\"{str(r.get('question'))[:80]}\""
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
