# Polymarket Weather Arbitrage Strategy

This repository addition contains an execution-oriented MVP for Polymarket weather-market arbitrage. It is designed to be deployed in phases: dry-run scanning first, local WebSocket order-book validation second, paper execution third, and only then live trading with tight feature gates.

## Core idea

The system prioritizes deterministic or near-deterministic price-constraint violations before any weather forecasting edge.

1. **YES/NO complement**

   In a binary market, one YES and one NO share pay at least 1.00 total at resolution. A buy-both opportunity exists when:

   ```text
   ask_yes + ask_no + taker_fees + slippage_buffer < 1.00
   ```

   The reverse opportunity exists when:

   ```text
   bid_yes + bid_no - taker_fees - slippage_buffer > 1.00
   ```

   The reverse path requires split/collateral operations and is intentionally disabled for first live trading.

2. **Nested threshold implication**

   For the same location, same date window, same metric, and same settlement source:

   ```text
   T >= 85F implies T >= 80F
   ```

   Therefore this combination has a minimum payout of 1.00:

   ```text
   buy YES(T >= 80F) + buy NO(T >= 85F)
   ```

   It is only valid when the parser confirms the same underlying event. Before live trading, sampled candidates must be manually reviewed.

3. **NegRisk / mutually exclusive full set**

   If exactly one outcome can win and all relevant outcomes are represented, buying the whole YES set below 1.00 or buying all NO shares below K-1 can be a constraint trade. This is not enabled for first live trading because outcome-set completeness and augmented outcomes must be verified.

4. **Weather model value trades**

   Forecast-based trades are a later module. They should be maker-first, have a large edge buffer, and never be mixed with the deterministic arbitrage scanner until the scanner logs are stable.

## Deployment gates

The code can be deployed in one pass, but permissions should be opened gradually.

```text
Gate 1: dry-run scanner
  - No wallet
  - No key
  - No order signing
  - Writes opportunities.csv and stdout logs

Gate 2: WebSocket book cache
  - REST is used for startup snapshots and recovery
  - WebSocket maintains local books
  - Local top of book must be reconciled with REST snapshots

Gate 3: paper executor
  - Simulates sequential FOK buy legs
  - Records leg success/failure, residual exposure, and theoretical PnL
  - Tests failure handling before real funds are enabled

Gate 4: limited live trading
  - Enable only YES_NO_BUY_BOTH
  - Disable threshold nested, NegRisk, split/sell, and forecast trades
  - Small notional limits and kill switch required
```

## Recommended first live feature flags

```bash
export PM_LIVE_TRADING=false
export PM_ALLOW_KINDS=YES_NO_BUY_BOTH
export PM_MAX_NOTIONAL_PER_TRADE=10
export PM_MAX_DAILY_LOSS=25
export PM_MIN_EDGE=0.02
export PM_MIN_SHARES=5
export PM_MAX_SHARES=20
export PM_MAX_BOOK_AGE_MS=500
```

The deliberately conservative `PM_MIN_EDGE=0.02` means at least 2 cents per share after estimated taker fees. Reduce this only after measuring fill failures, stale-book rate, and post-fill residual risk.

## Important operating principle

Do not treat a detected candidate as an executable arbitrage unless all of these are true:

```text
- The opportunity is calculated from executable book depth, not midpoint.
- Fees are included.
- Tick size and minimum size are valid.
- Local book age is below the max age threshold.
- All legs have enough depth for the intended size.
- The market parser has no unresolved warning.
- Paper execution has shown the residual handling logic works.
```

The first production target is not maximum yield. It is to prove that the scanner sees real, executable, fee-adjusted constraints and that the executor does not create uncontrolled single-leg exposure.
