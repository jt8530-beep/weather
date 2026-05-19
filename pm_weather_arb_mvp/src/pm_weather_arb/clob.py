from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import requests

from .config import Config
from .types import BookLevel, OrderBook
from .util import chunks, dec, first_present


class ClobPublicClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()

    def get_book(self, token_id: str) -> OrderBook:
        url = f"{self.config.clob_host.rstrip('/')}/book"
        resp = self.session.get(url, params={"token_id": token_id}, timeout=self.config.http_timeout)
        resp.raise_for_status()
        return parse_book(resp.json(), token_id)

    def get_books(self, token_ids: Iterable[str], batch_size: int = 250) -> Dict[str, OrderBook]:
        token_ids = [str(t) for t in token_ids if t]
        out: Dict[str, OrderBook] = {}
        url = f"{self.config.clob_host.rstrip('/')}/books"
        for batch in chunks(token_ids, batch_size):
            body = [{"token_id": token_id} for token_id in batch]
            try:
                resp = self.session.post(url, json=body, timeout=self.config.http_timeout)
                resp.raise_for_status()
                payload = resp.json()
                books_payload = payload.get("books", payload) if isinstance(payload, dict) else payload
                if not isinstance(books_payload, list):
                    raise ValueError("unexpected /books response shape")
                for raw in books_payload:
                    token = str(first_present(raw, "asset_id", "assetId", "token_id", default=""))
                    if not token:
                        # Fallback to requested order if response omits asset_id.
                        continue
                    out[token] = parse_book(raw, token)
            except Exception:
                # Keep scanner useful even if batch endpoint shape changes.
                for token_id in batch:
                    out[token_id] = self.get_book(token_id)
        return out


def _levels(raw_levels: Optional[List[Dict[str, Any]]], reverse: bool) -> List[BookLevel]:
    levels = []
    for item in raw_levels or []:
        if not isinstance(item, dict):
            continue
        price = dec(first_present(item, "price", "p", default="0"))
        size = dec(first_present(item, "size", "q", default="0"))
        if price > 0 and size > 0:
            levels.append(BookLevel(price=price, size=size))
    return sorted(levels, key=lambda x: x.price, reverse=reverse)


def parse_book(raw: Dict[str, Any], fallback_token_id: str) -> OrderBook:
    token_id = str(first_present(raw, "asset_id", "assetId", "token_id", default=fallback_token_id))
    return OrderBook(
        token_id=token_id,
        bids=_levels(raw.get("bids"), reverse=True),
        asks=_levels(raw.get("asks"), reverse=False),
        market=str(first_present(raw, "market", default="")) or None,
        tick_size=str(first_present(raw, "tick_size", "tickSize", default="")) or None,
        min_order_size=dec(first_present(raw, "min_order_size", "minOrderSize", default="0")) or None,
        neg_risk=bool(raw.get("neg_risk")) if "neg_risk" in raw else None,
        hash=str(first_present(raw, "hash", default="")) or None,
    )
