from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

import websockets

from .clob import parse_book
from .types import BookLevel, OrderBook
from .util import dec, first_present

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

UpdateCallback = Callable[[str, OrderBook], Awaitable[None] | None]


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class MarketBookCache:
    """Local in-memory book cache updated from Polymarket market-channel messages."""

    books: Dict[str, OrderBook] = field(default_factory=dict)
    updated_at_ms: Dict[str, int] = field(default_factory=dict)
    tick_size_by_market: Dict[str, str] = field(default_factory=dict)

    def seed(self, books: Dict[str, OrderBook]) -> None:
        ts = now_ms()
        for token_id, book in books.items():
            self.books[str(token_id)] = book
            self.updated_at_ms[str(token_id)] = ts
            if book.market and book.tick_size:
                self.tick_size_by_market[book.market] = book.tick_size

    def get(self, token_id: str) -> Optional[OrderBook]:
        return self.books.get(str(token_id))

    def age_ms(self, token_id: str) -> Optional[int]:
        ts = self.updated_at_ms.get(str(token_id))
        if ts is None:
            return None
        return max(0, now_ms() - ts)

    def as_dict(self) -> Dict[str, OrderBook]:
        return dict(self.books)

    def max_age_ms(self, token_ids: Iterable[str]) -> Optional[int]:
        ages: List[int] = []
        for token_id in token_ids:
            age = self.age_ms(str(token_id))
            if age is None:
                return None
            ages.append(age)
        return max(ages) if ages else None

    def apply_message(self, message: Dict[str, Any]) -> List[str]:
        event_type = str(message.get("event_type") or message.get("type") or "")
        if event_type == "book":
            return [self.apply_book(message)]
        if event_type == "price_change":
            return self.apply_price_change(message)
        if event_type == "tick_size_change":
            market = str(first_present(message, "market", default=""))
            new_tick = str(first_present(message, "new_tick_size", "newTickSize", default=""))
            if market and new_tick:
                self.tick_size_by_market[market] = new_tick
            token_id = str(first_present(message, "asset_id", "assetId", default=""))
            if token_id in self.books and new_tick:
                self.books[token_id].tick_size = new_tick
                self.updated_at_ms[token_id] = _message_ts(message)
                return [token_id]
        return []

    def apply_book(self, message: Dict[str, Any]) -> str:
        token_id = str(first_present(message, "asset_id", "assetId", "token_id", default=""))
        if not token_id:
            raise ValueError("book message missing asset_id")
        book = parse_book(message, token_id)
        self.books[token_id] = book
        self.updated_at_ms[token_id] = _message_ts(message)
        if book.market and book.tick_size:
            self.tick_size_by_market[book.market] = book.tick_size
        return token_id

    def apply_price_change(self, message: Dict[str, Any]) -> List[str]:
        changed: List[str] = []
        ts = _message_ts(message)
        market = str(first_present(message, "market", default="")) or None
        for change in message.get("price_changes") or []:
            if not isinstance(change, dict):
                continue
            token_id = str(first_present(change, "asset_id", "assetId", default=""))
            if not token_id:
                continue
            side = str(first_present(change, "side", default="")).upper()
            price = dec(first_present(change, "price", default="0"))
            size = dec(first_present(change, "size", default="0"))
            book = self.books.get(token_id)
            if book is None:
                book = OrderBook(token_id=token_id, bids=[], asks=[], market=market)
                self.books[token_id] = book
            if side == "BUY":
                book.bids = _upsert_level(book.bids, price, size, reverse=True)
            elif side == "SELL":
                book.asks = _upsert_level(book.asks, price, size, reverse=False)
            if market:
                book.market = market
            book.hash = str(first_present(change, "hash", default=book.hash or "")) or book.hash
            self.updated_at_ms[token_id] = ts
            changed.append(token_id)
        return changed


def _message_ts(message: Dict[str, Any]) -> int:
    raw = first_present(message, "timestamp", "ts", default="")
    try:
        ts = int(str(raw))
        return ts if ts > 0 else now_ms()
    except Exception:
        return now_ms()


def _upsert_level(levels: List[BookLevel], price: Decimal, size: Decimal, reverse: bool) -> List[BookLevel]:
    out = [level for level in levels if level.price != price]
    if price > 0 and size > 0:
        out.append(BookLevel(price=price, size=size))
    return sorted(out, key=lambda level: level.price, reverse=reverse)


class MarketWebSocketRunner:
    def __init__(self, asset_ids: Iterable[str], cache: Optional[MarketBookCache] = None, url: str = MARKET_WS_URL):
        self.asset_ids = [str(asset_id) for asset_id in asset_ids if asset_id]
        self.cache = cache or MarketBookCache()
        self.url = url
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self, on_update: Optional[UpdateCallback] = None, reconnect_delay: float = 2.0) -> None:
        if not self.asset_ids:
            return
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "assets_ids": self.asset_ids,
                                "type": "market",
                                "custom_feature_enabled": True,
                            }
                        )
                    )
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        for message in _decode_messages(raw):
                            changed = self.cache.apply_message(message)
                            if on_update:
                                for token_id in changed:
                                    book = self.cache.get(token_id)
                                    if book is None:
                                        continue
                                    maybe_await = on_update(token_id, book)
                                    if maybe_await is not None:
                                        await maybe_await
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"ws_reconnect reason={type(exc).__name__}: {exc}")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=reconnect_delay)
                except asyncio.TimeoutError:
                    pass


def _decode_messages(raw: str | bytes) -> List[Dict[str, Any]]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []
