# Experimental (research-only) modules

Nothing in this package is reachable from `polybot.runtime.build_runtime`, the
trading engine, the worker, or any API route. Importing these modules cannot
place, cancel, size, or price an order. They exist so the research work behind
them stays reviewable instead of looking like shipped functionality.

| Module | What it is | Why it is not wired |
|---|---|---|
| `pair_accumulator.py` | Maker-first YES/NO pair quoting with fee-aware edge maths | Needs pair-level inventory execution (`order_groups`) plus a paired-position risk budget that the single-account engine does not implement yet |
| `crypto_direction.py` | Bounded directional overlay against pair inventory | Depends on the pair inventory and order-group execution above |
| `polybot.inventory.pair_book` (not in this package) | Durable pair-fill accounting used by the two modules above | Kept next to the durable `pair_inventory_events` schema |

## What *is* wired

`polybot.market_filters.crypto_updown.CryptoUpDownFilter` is the one classifier
that is deliberately connected to execution: set
`POLYBOT_MARKET_FILTER=crypto_updown` and the engine narrows each cycle to
short-horizon crypto Up/Down markets (default `POLYBOT_CRYPTO_UPDOWN_ASSETS=BTC,ETH`)
before forecasting. The AI/risk/execution path afterwards is unchanged, and the
filter can only *remove* markets from the candidate set.

## Promoting a module out of `experimental/`

To ship one of these, a change has to add: (1) durable order-group execution
with the same fencing/lease guarantees as single orders, (2) risk limits that
bind total pair exposure rather than per-token exposure, (3) an execution-guard
check on every new submission path, and (4) tests proving the fail-closed paths
still hold. Until then the honest state is "not wired".
