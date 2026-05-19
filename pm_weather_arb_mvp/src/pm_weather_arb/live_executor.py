from __future__ import annotations

import os
from decimal import Decimal
from typing import Iterable

from .types import Opportunity


class DryRunExecutor:
    def execute(self, opportunity: Opportunity) -> None:
        print(f"DRY_RUN would_execute kind={opportunity.kind} profit={opportunity.expected_profit} size={opportunity.size}")
        for leg in opportunity.legs:
            print(f"  {leg.action} {leg.outcome} token={leg.token_id} size={leg.size} avg={leg.avg_price}")


class LiveExecutor:
    """
    Minimal live-execution adapter skeleton.

    Keep this disabled until the scanner has run cleanly in production logs.
    For actual submission, install py_clob_client_v2 and pass the correct tick_size
    and neg_risk option for each leg's market.
    """

    def __init__(self, host: str, chain_id: int = 137):
        if os.getenv("PM_LIVE_TRADING", "false").lower() != "true":
            raise RuntimeError("Set PM_LIVE_TRADING=true only after dry-run validation.")
        try:
            from py_clob_client_v2 import ApiCreds, ClobClient  # type: ignore
        except Exception as exc:
            raise RuntimeError("Install py_clob_client_v2 to enable live execution.") from exc

        pk = os.environ["PK"]
        creds = None
        if os.getenv("CLOB_API_KEY"):
            creds = ApiCreds(
                api_key=os.environ["CLOB_API_KEY"],
                api_secret=os.environ["CLOB_SECRET"],
                api_passphrase=os.environ["CLOB_PASS_PHRASE"],
            )
        temp = ClobClient(host=host, chain_id=chain_id, key=pk)
        creds = creds or temp.create_or_derive_api_key()
        self.client = ClobClient(host=host, chain_id=chain_id, key=pk, creds=creds)

    def execute_fok_buy_legs(self, opportunity: Opportunity, tick_size: str = "0.01", neg_risk: bool = False) -> None:
        """Live FOK buy-only helper. Split/sell and merge flows need separate collateral ops."""
        from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side  # type: ignore

        for leg in opportunity.legs:
            if leg.action != "BUY":
                raise NotImplementedError("This helper only submits BUY market FOK legs.")
            # amount is USDC not shares. Use avg_price * size as a cap-like estimate.
            amount = float((leg.avg_price or Decimal("0")) * leg.size)
            resp = self.client.create_and_post_market_order(
                order_args=MarketOrderArgs(
                    token_id=leg.token_id,
                    amount=amount,
                    side=Side.BUY,
                    order_type=OrderType.FOK,
                ),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
                order_type=OrderType.FOK,
            )
            print(resp)
