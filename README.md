# Active Trades

Terminal tracker for Lighter and BingX perpetual positions and limit orders. Lighter's base account and every sub-account tied to the same L1 wallet are included. Both exchanges share one positions table and one limit-orders table.

## How It Works

1. Loads `.env` by searching from the current folder and package source location upward. The file can live in this repository or in a parent directory shared with other projects.
2. Reads `LIGHTER_ACCOUNT_INDEX` and fetches that account with `GET /api/v1/account`.
3. Uses the returned `l1_address` to call `GET /api/v1/accountsByL1Address` and discover the main account plus sub-accounts.
4. Polls each account with `GET /api/v1/account?active_only=true` and extracts non-zero perp positions.
5. Uses authenticated `GET /api/v1/accountActiveOrders` for every account, including accounts without positions, to find limit orders and matching reduce-only stop-loss orders. Public `GET /api/v1/orderBooks` supplies symbols for orders in markets without positions.
6. Calculates risk as the adverse distance from entry to stop loss multiplied by position size; stops at or beyond breakeven have zero risk.
7. In live mode, subscribes to public `account_all_positions` and `market_stats/all` WebSocket channels. Position updates and mark prices drive UPNL, and the display refreshes every 0.5 seconds by default.
8. When `BINGX_API_KEY` or `BINGX_API_SECRET` is configured, reads BingX perpetual positions with signed `GET /openApi/swap/v2/user/positions` and all pending orders with signed `GET /openApi/swap/v2/trade/openOrders`. Requests use HMAC-SHA256 and the `X-BX-APIKEY` header. Both variables are required for successful reads.
9. Adds BingX positions and ordinary limit orders to the shared tables. Closing stop-loss orders supply risk calculations; take-profit and conditional orders are excluded from the limit-order table. Each row carries a small exchange label; Lighter rows also identify the account.

The package separates environment/configuration (`env.py`, `settings.py`), REST authentication and parsing (`lighter_api.py`, `bingx_api.py`, `models.py`), position dates and risk (`state.py`, `risk.py`), account collection (`tracker.py`), the Lighter live feed (`pnl_stream.py`), and rendering/CLI orchestration (`ui.py`, `cli.py`).

The Lighter positions endpoint does not expose a single canonical "opened at" field. The tracker first tries to infer it from recent authenticated trades; if that is not available, it uses a matching stop-order creation time; if neither exists, it stores the first time the tracker saw the position in `state/position_state.json`.

BingX uses the position's `createTime` when supplied, with stop-order/first-seen fallback otherwise. BingX position IDs and exchange names keep its stored dates separate from Lighter's dates, including positions in the same asset. Failure to read one exchange does not prevent the other exchange's data from displaying.

API references: [BingX authentication](https://github.com/BingX-API/api-ai-skills/blob/main/skills/references/authentication.md), [perpetual positions](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-account/api-reference.md), and [open orders](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-trade/api-reference.md). This tracker only makes read requests to BingX.

## BingX Configuration

Add the following to your local `.env` (existing Lighter settings remain required):

```bash
BINGX_API_KEY=...
BINGX_API_SECRET=...
# Optional; defaults to the live perpetual API.
BINGX_BASE_URL=https://open-api.bingx.com
```

If neither BingX variable is set, the tracker runs with Lighter alone. If only one is set, the shared tables show that BingX authentication is needed. This integration covers BingX perpetual futures, including USDT/USDC markets; spot holdings and standard futures are outside these endpoints.

## Install

Requires Python 3.11 or newer. From the cloned repository:

```bash
cd active-trades
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

If you do not already have a parent `.env`, create one in the repository root:

```bash
cp .env.example .env
```

Set `LIGHTER_ACCOUNT_INDEX`, then add Lighter order-read credentials and both BingX credentials as needed. The `.env` file is local configuration and is excluded from Git. Publishing or cloning this repository does not upload or recreate a parent `.env`.

An existing shared layout works without moving the environment file:

```text
workspace/
  .env
  active-trades/
    pyproject.toml
    src/
```

Run `active-trades` from the repository or its parent directory. Existing non-empty environment variables take precedence over values loaded from `.env`.

To run the test suite:

```bash
python -m pytest
```

## Run

```bash
active-trades
```

Useful variants:

```bash
active-trades --refresh 30
active-trades --pnl-refresh 1
active-trades --once
active-trades --list-accounts
active-trades --accounts 1,2,3
active-trades --no-orders
```

`--refresh` / `ACTIVE_TRADES_REFRESH_SECONDS` controls full account, order and metadata reads (default 15 seconds). These run in a background worker while the dashboard continues updating. `--pnl-refresh` / `ACTIVE_TRADES_PNL_REFRESH_SECONDS` controls the display cadence (default 0.5 seconds; minimum 0.5). UPNL uses a persistent read-only WebSocket connection rather than an HTTP request per trade or display tick. The actual arrival of new values depends on Lighter's feed; an unchanged price keeps the same UPNL. The stream reconnects automatically and displays a notice while unavailable; full REST refreshes still provide fallback values.

BingX positions, UPNL, and orders update on the full `--refresh` interval; `--pnl-refresh` controls Lighter's streaming display. The dashboard labels these cadences separately when BingX is configured. `--no-orders` skips order reads for both exchanges. `--accounts`, `--account-index`, and `--list-accounts` apply to Lighter's account discovery.

Lighter's live UPNL uses the exchange's reported value and, when a newer mark price arrives, adjusts it by `signed_size * (new_mark - previous_mark)`. Without a reported baseline it uses `signed_size * (mark - entry)`. This retains the precision of Lighter's reported PNL when the displayed average entry is rounded. BingX displays its API-reported `unrealizedProfit`. Funding and realized PNL are separate fields.

Protocol details: [Lighter WebSocket reference](https://apidocs.lighter.xyz/docs/websocket-reference). The separate stream avoids high-frequency REST polling under [Lighter's rate limits](https://apidocs.lighter.xyz/docs/rate-limits).

## Display

The top `ACTIVE TRADES` panel groups general data: status, position and account counts, update times, refresh cadences, `TOTAL UPNL`, `POSITION RISK`, and `LIMIT RISK`. The UPNL total sums unrealized PNL across all open positions from every tracked Lighter account and BingX, updating with the position values. Gains are green and losses are red. If position data is missing, the available subtotal is labeled partial; if no values are available, it shows `n/a`. No open positions shows `$0.00`. This general section stays visible while paging.

`POSITION RISK` totals the downside from current entries to their stop losses. `LIMIT RISK` estimates additional downside if all remaining entry limits fill and then reach their stop losses, using limit prices and remaining quantities. Reducing exit orders are excluded, and locked profits do not offset losses. These are estimates at entry and SL prices, excluding fees and slippage. Missing or ambiguous stops, insufficient stop coverage, and unavailable reads make the affected total incomplete: a known subtotal shows `≥$…`, while no known risk shows `n/a`. The yellow `Risk incomplete: SL/data` note identifies incomplete estimates; a confirmed zero is green.

The dashboard also has two shared sections: positions and limit orders. Rows from every discovered Lighter account and the BingX account appear together, with a dim source label beneath the asset. Empty or unavailable reads remain labeled by source inside the shared sections. Narrow terminals use a compact layout so risk and UPNL amounts remain readable.

When an interactive terminal is too short to fit the dashboard, use the arrow keys or `n`/`p` to move through pages (`Tab` also advances). `q` or `Ctrl+C` exits. `--once` prints every account without paging.

Rows show:

- `#`
- `Asset`
- Exchange (and Lighter account)
- `Date open`
- `Short/Long?`
- `Risk in $`
- `UPNL`: signed USD value, green for gains and red for losses; `n/a` when unavailable.

Limit orders show asset, exchange, buy/sell side, limit price, remaining size, and whether the order is reduce only. Only active ordinary limit orders appear; stop-loss orders continue to serve risk calculations. BingX's remaining limit size subtracts executed quantity from original quantity. Unauthenticated, failed and disabled order reads have distinct states, so an unavailable order list is never shown as an empty list.

Risk states:

- `$0.00`: a stop exists and is at/beyond breakeven.
- `No SL`: no active reduce-only stop-loss order was found.
- `Auth`: the account needs its own credentials before stop orders can be read.

Lighter API keys are account-index scoped; to read limit orders and calculate risk for every sub-account, add either a read-only token or API key pair for that specific account. Live UPNL does not require these credentials:

```bash
LIGHTER_ACCOUNT_123_API_PRIVATE_KEY=...
LIGHTER_ACCOUNT_123_API_KEY_INDEX=4

# or
LIGHTER_ACCOUNT_123_READ_ONLY_AUTH_TOKEN=...
```
