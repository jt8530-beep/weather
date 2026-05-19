from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import requests

from .config import Config, DEFAULT_WEATHER_KEYWORDS
from .types import Market, Token
from .util import first_present, jsonish, norm_text, truthy


class GammaClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()

    def list_active_events(
        self,
        pages: int = 5,
        limit: int = 100,
        order: str = "volume_24hr",
        ascending: bool = False,
    ) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for page in range(pages):
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": page * limit,
                "order": order,
                "ascending": str(ascending).lower(),
            }
            url = f"{self.config.gamma_host.rstrip('/')}/events"
            resp = self.session.get(url, params=params, timeout=self.config.http_timeout)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict) and "events" in payload:
                batch = payload["events"]
            else:
                batch = payload
            if not batch:
                break
            events.extend(batch)
            if len(batch) < limit:
                break
        return events


def is_weatherish_event(event: Dict[str, Any], keywords: Iterable[str] = DEFAULT_WEATHER_KEYWORDS) -> bool:
    markets = event.get("markets") or []
    tags = event.get("tags") or []
    tag_text = " ".join(str(t.get("label") or t.get("slug") or t.get("name") or "") for t in tags if isinstance(t, dict))
    market_text = " ".join(
        norm_text(m.get("question"), m.get("description"), m.get("slug"))
        for m in markets
        if isinstance(m, dict)
    )
    haystack = norm_text(
        event.get("title"),
        event.get("slug"),
        event.get("description"),
        event.get("category"),
        event.get("subcategory"),
        tag_text,
        market_text,
    )
    return any(k.lower() in haystack for k in keywords)


def parse_markets_from_events(events: Iterable[Dict[str, Any]], only_weatherish: bool = True) -> List[Market]:
    parsed: List[Market] = []
    for event in events:
        if only_weatherish and not is_weatherish_event(event):
            continue
        event_id = str(first_present(event, "id", "eventId", default=""))
        event_slug = str(first_present(event, "slug", default=""))
        event_title = str(first_present(event, "title", "question", default=""))
        event_neg_risk = truthy(first_present(event, "negRisk", "neg_risk", "enableNegRisk", default=False))
        markets = event.get("markets") or []
        for raw in markets:
            if not isinstance(raw, dict):
                continue
            outcomes = jsonish(first_present(raw, "outcomes", default=[]), []) or []
            clob_ids = jsonish(first_present(raw, "clobTokenIds", "clob_token_ids", "tokenIds", default=[]), []) or []
            tokens_payload = raw.get("tokens") or []
            tokens: List[Token] = []
            if tokens_payload and isinstance(tokens_payload, list):
                for item in tokens_payload:
                    if not isinstance(item, dict):
                        continue
                    outcome = str(first_present(item, "outcome", "name", default=""))
                    token_id = str(first_present(item, "token_id", "tokenId", "id", default=""))
                    if outcome and token_id:
                        tokens.append(Token(outcome=outcome, token_id=token_id))
            if not tokens and outcomes and clob_ids and len(outcomes) == len(clob_ids):
                tokens = [Token(outcome=str(o), token_id=str(tid)) for o, tid in zip(outcomes, clob_ids) if tid]
            if not tokens or len(tokens) < 2:
                continue
            market = Market(
                event_id=event_id,
                event_slug=event_slug,
                event_title=event_title,
                market_id=str(first_present(raw, "id", "marketId", default="")),
                market_slug=str(first_present(raw, "slug", default="")),
                question=str(first_present(raw, "question", "title", default="")),
                description=str(first_present(raw, "description", "resolutionSource", default="")),
                condition_id=str(first_present(raw, "conditionId", "condition_id", "questionID", default="")),
                neg_risk=truthy(first_present(raw, "negRisk", "neg_risk", default=event_neg_risk)) or event_neg_risk,
                enable_order_book=truthy(first_present(raw, "enableOrderBook", "enable_order_book", default=True)),
                active=truthy(first_present(raw, "active", default=True)),
                closed=truthy(first_present(raw, "closed", default=False)),
                outcomes=[str(x) for x in outcomes] if outcomes else [t.outcome for t in tokens],
                tokens=tokens,
                minimum_tick_size=(str(first_present(raw, "minimum_tick_size", "minimumTickSize", default="")) or None),
                fees_enabled=(truthy(raw["feesEnabled"]) if "feesEnabled" in raw else None),
                raw=raw,
            )
            if market.enable_order_book and market.active and not market.closed:
                parsed.append(market)
    return parsed
