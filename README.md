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

The package separates environment/configuration (`env.py`, `settings.py`), REST authentication and parsing (`lighter_api.py`, `bingx_api.py`, `models.py`), position dates, risk and partial-close profit tracking (`state.py`, `risk.py`, `profit_tracking.py`), account collection (`tracker.py`), the Lighter live feed (`pnl_stream.py`), TP/SL email alerts (`close_alerts.py`, `email_alerts.py`), limit-order cancellation (`order_cancel.py`), and rendering/CLI orchestration (`ui.py`, `cli.py`).

The Lighter positions endpoint does not expose a single canonical "opened at" field. The tracker first tries to infer it from recent authenticated trades; if that is not available, it uses a matching stop-order creation time; if neither exists, it stores the first time the tracker saw the position in `state/position_state.json`.

BingX uses the position's `createTime` when supplied, with stop-order/first-seen fallback otherwise. BingX position IDs and exchange names keep its stored dates separate from Lighter's dates, including positions in the same asset. Failure to read one exchange does not prevent the other exchange's data from displaying.

API references: [BingX authentication](https://github.com/BingX-API/api-ai-skills/blob/main/skills/references/authentication.md), [perpetual positions](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-account/api-reference.md), and [open orders](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-trade/api-reference.md). Apart from a confirmed [limit-order cancel](#cancelling-limit-orders), the tracker only makes read requests.

## BingX Configuration

Add the following to your local `.env` (existing Lighter settings remain required):

```bash
BINGX_API_KEY=...
BINGX_API_SECRET=...
# Optional; defaults to the live perpetual API.
BINGX_BASE_URL=https://open-api.bingx.com
```

If neither BingX variable is set, the tracker runs with Lighter alone. If only one is set, the shared tables show that BingX authentication is needed. Reading needs only a read-only key; [cancelling limit orders](#cancelling-limit-orders) needs a key with perpetual futures trading permission. This integration covers BingX perpetual futures, including USDT/USDC markets; spot holdings and standard futures are outside these endpoints.

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
active-trades --hide-amounts
active-trades --email-alerts
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

Lighter position-tied stop losses can report zero original and remaining size because their size is determined from the current position when triggered. These stops count as current-position protection; a sized stop whose remaining quantity was exhausted does not. This follows [Lighter's conditional-order sizing](https://github.com/elliottech/lighter-prover/blob/main/circuit/src/transactions/internal_create_order.rs#L135-L153).

The dashboard also has two shared sections: positions and limit orders. Rows from every discovered Lighter account and the BingX account appear together, with a dim source label beneath the asset. Empty or unavailable reads remain labeled by source inside the shared sections. Narrow terminals use a compact layout so risk and UPNL amounts remain readable.

An active position's asset name turns cyan (the theme's blue) when its risk is exactly zero, or when cumulative net realized profit from observed partial closes exceeds 49% of its original tracked risk. Exactly 49% does not qualify. The original risk is the first available risk recorded before its first observed partial close; reducing its size or moving its stop does not lower this comparison baseline. Both partial sells of longs and partial buybacks of shorts count. A qualifying profit highlight stays until the position fully closes or reverses. Zero-risk amounts in position rows also use cyan.

Profit tracking starts when a position is first observed and is saved with position state across restarts. Existing profits from before that observation are excluded. The tracker adds exchange-reported realized PNL changes when it observes a size reduction; these changes can include fees and funding within the same update interval. Unrealized gains and realized PNL changes without a size reduction do not qualify. If original risk was unavailable before the first reduction, or realized PNL was unavailable for a reduction, incomplete history cannot establish a profit highlight.

In a live terminal, press `m` to hide or reveal dollar amounts. Hidden values display as `****` throughout totals, position risk and UPNL, limit-order risk, and dollar amounts in notices. Limit prices, dates, asset names and quantities remain visible. Use `--hide-amounts` to start masked, including with `--once` or `--plain`. Visibility is a session setting; values continue updating while masked.

Keyboard shortcuts are case-insensitive. Navigation aliases share the same action; each letter shortcut has one action:

| Action | Keys |
| --- | --- |
| Hide / reveal dollar amounts | `m` |
| Turn TP/SL email alerts on / off | `Ctrl+E` |
| Cancel a limit order (with its SL/TP) | `c`, then the order's `#`, `Enter`, `y` |
| Next page | `→`, `↓`, `n`, `Tab`, `Page Down` |
| Previous page | `←`, `↑`, `p`, `Shift+Tab`, `Page Up` |
| Quit | `q`, `Ctrl+C` |

Paging applies to the interactive Rich dashboard; `m`, `Ctrl+E`, `c` and `q` also work in live plain-text mode. Input is polled while waiting for the next display update, so shortcuts respond even with a long refresh interval. `--once` prints every account without interactive controls. The footer shows the mask toggle and page navigation in the space available.

Rows show:

- `#`
- `Asset`
- Exchange (and Lighter account)
- `Date open`
- `Short/Long?`
- `Risk in $`
- `UPNL`: signed USD value, green for gains and red for losses; `n/a` when unavailable.

Limit orders show asset, exchange, buy/sell side, limit price, remaining size, `RISK` in dollars, and whether the order is reduce only. Each order's risk uses the same calculation as the top `LIMIT RISK` total: the adverse distance from its limit price to its stop loss multiplied by its remaining size, excluding fees and slippage. Missing, ambiguous, or insufficient stop coverage shows `n/a`; reduce-only orders add no entry risk and show `$0.00`. Only active ordinary limit orders appear; stop-loss orders continue to serve risk calculations. BingX's remaining limit size subtracts executed quantity from original quantity. Unauthenticated, failed and disabled order reads have distinct states, so an unavailable order list is never shown as an empty list.

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

## Cancelling Limit Orders

In a live terminal, press `c`, type the `#` shown in the `LIMIT ORDERS` table, and press `Enter`. The top panel then shows the order (asset, side, size, price and account) and every stop-loss and take-profit order the cancel would also remove. Press `y` to cancel them, or `n`, `c`, `Backspace` or `Esc` to back out. Nothing is sent before `y`, and a `y` typed before the review appeared is ignored. While the prompt is open, letters other than `q`, `m` and `Ctrl+E` are ignored, and arrow and page keys still page. `--once` and non-interactive runs have no cancel option.

The SL/TP removed with an entry limit are:

- SL/TP orders linked to that entry, such as Lighter OTO/OTOCO children waiting for it to fill.
- Reduce-only stop-loss, take-profit and closing limit orders on the same account, market and position side (BingX hedge leg), when there is no open position on that side and no other entry limit there.

The following orders stay, and the review counts them:

- An SL/TP that protects an open position on that side, or that may serve another entry limit, stays.
- Orders linked to a different order are never touched.
- If positions could not be read, standalone SL/TP stay.

Cancelling a reduce-only exit limit cancels only that order.

The entry is cancelled first. The tracker then re-reads the account until the entry has left the book, and only then cancels the SL/TP, reselected from that fresh read and limited to those shown in the review. If the entry cancel is rejected, the order is still open, or the account cannot be re-read, every SL/TP stays in place. If the entry filled before the cancel arrived, its stops now protect a position and stay. After SL/TP cancels the account is read again; any still open are reported. The result appears as a dashboard notice for a minute, and the dashboard refreshes right away. One cancel runs at a time, in the background, so the dashboard keeps updating. Quitting waits for a cancel in progress to finish.

A partly filled entry shows its filled amount in the review: check that position's stop afterwards, since an exchange may cancel stops still waiting for the entry together with it.

Cancelling sends write requests, so it needs trading credentials:

- Lighter: the account's own API key pair (`LIGHTER_API_PRIVATE_KEY` / `LIGHTER_API_KEY_INDEX` for the base account, `LIGHTER_ACCOUNT_<index>_API_PRIVATE_KEY` / `LIGHTER_ACCOUNT_<index>_API_KEY_INDEX` for a sub-account). Read-only tokens cannot cancel. Each order cancel is a signed [cancel transaction](https://apidocs.lighter.xyz/docs/get-started-for-programmers) using the exchange's order index.
- BingX: `DELETE /openApi/swap/v2/trade/order` by symbol and order ID, with an API key that has perpetual futures trading permission. A read-only key reports `the API key cannot trade` and nothing is cancelled.

## Email Alerts

In a live terminal, press `Ctrl+E` to turn TP/SL email alerts on or off; the `ACTIVE TRADES` panel shows `Email alerts ON` or `off`. Use `--email-alerts` to start with them on, which also works for live sessions without an interactive terminal. While on, every take-profit or stop-loss order that fills sends one plain-text email. Partial TP/SL fills count. Market, limit and other manual closes do not send alerts. Alerts cover fills from the moment they are turned on (with a 10-second clock-skew allowance), and each fill is emailed once per session, including after toggling off and on. The setting lasts only for the session.

The email leads with realized PNL, followed by asset, position side, account, size closed, trigger and average fill price, and close time (UTC). BingX alerts also show the closing order's commission. `--hide-amounts` does not mask email contents.

Fills are read from exchange order history every `--refresh` interval (minimum 10 seconds), in a background worker, so alerts arrive within about one refresh of the fill:

- Lighter: authenticated `GET /api/v1/accountInactiveOrders` finds filled `stop-loss` / `take-profit` orders. Realized PNL is the sum of `ask_account_pnl` / `bid_account_pnl` on that order's fills from `GET /api/v1/trades`. If a fill's PNL is not available yet, the alert waits up to three checks, then sends with `n/a`. Accounts without order-read credentials are skipped.
- BingX: signed `GET /openApi/swap/v2/trade/allOrders` finds filled `STOP_MARKET`, `STOP`, `TRAILING_STOP_MARKET` (SL), `TAKE_PROFIT_MARKET` and `TAKE_PROFIT` (TP) orders, using the order's reported `profit` and `commission`.

Emails use the shared AWS SES SMTP variables (the same as SFP): `SES_SMTP_HOST`, `SES_SMTP_PORT` (default 587 with STARTTLS; 465 uses implicit TLS), `SES_SMTP_USER` or `SES_IAM_USER`, `SES_SMTP_PASS`, `EMAIL_FROM`, optional `EMAIL_FROM_NAME`, and `ALERT_EMAIL_TO` (comma-separated). If any are missing, `Ctrl+E` leaves alerts off and the dashboard names the missing variable. Sent alerts, send failures and incomplete exchange reads appear as dashboard notices. A failed send is retried on the next check, up to three attempts.

## Google Sheets Journal

The optional journal sync writes directly to the online C2 Arc journal using the Google Sheets API. It is **disabled by default**. The supplied XLSX is a layout reference; the tracker does not edit or upload it. Google access setup and the fixed journal capital can be completed later.

Once enabled, each full REST refresh also reads execution history for the configured Lighter accounts and BingX perpetual account. It logs a position when it opens, updates the same row after partial exits, and marks it Closed only when its executed quantity reaches zero. This includes manual market/limit exits and TP/SL fills. Scale-ins remain on that row; a reversal closes the old row and starts a new one. A complete open-and-close between polls is recovered from history.

The writer expects the provided `Sheet1` layout, with headers on row 5 and trades from row 6. It preserves TF, Setup, manual Potential RR values, Description, existing risk percentages, Charts Entry, and existing Charts Exit text/formulas. Dates and hours use `Europe/Sofia` by default. Entry dates of new rows and close dates written at full closure display the English weekday initial, for example `16.10.2026 (F)`, while remaining numeric dates. Descriptions stay available for your own notes; legacy `[active-trades] ... [/active-trades]` status blocks are removed automatically. New rows use the journal's currency, percentage and time formats while retaining formatting already present on empty template rows. `Copy of Sheet1` is the preserved original and is rejected as a sync target.

| Journal field | Automatic behavior |
| --- | --- |
| Nr, Entry Date, Hour, Asset, S/L | Filled for a new row from execution data. |
| Trade Active? | Open through partial closes; Closed after the final execution. |
| Risk | First available dollar stop risk before a partial close divided by configured capital. Existing manual percentages remain authoritative. Unknown original risk stays blank. |
| Potential RR | Planned reward / initial stop distance, rounded to two decimals. Uses the furthest profitable TP trigger or reducing limit for the same position. Existing values are preserved. |
| P/L $ | Cumulative realized execution P/L, updated after each partial or final exit. Unrealized P/L is excluded. |
| Profit % | Realized P/L divided by the fixed journal capital. |
| Close Date | New open rows start with `-`; replaced with the actual close date at full closure, shown with its weekday initial. |
| W/L/BE | Filled only at full closure. Default BE tolerance is ±$1; configure it to match your convention. |
| TF, Setup | Remain available for manual input because executions do not describe the setup. |
| Charts Entry | Available for manual input. |
| Charts Exit | On full closure, writes a reference URL to the generated chart in an empty cell. Uses original entry/SL/TP, exact open/close times and exchange candles. Hosted automatically; no server or API key required. Existing text/formulas are preserved. |

For example, an entry with $20 original risk that sells half at 1R records about $10 realized P/L (before fees), stays Open, keeps `-` as its close date, and has no final W/L/BE.

### Automatic exit charts

Every fully closed position with a captured original plan receives a chart,
including manual exits and TP/SL fills. Partial closes retain the Open row and
generate no final chart. The PNG uses the reference's dark background, muted blue
rising candles and light gray falling candles. Blue reward and muted red risk
regions show the long/short plan, starting at the exact execution opening time
and ending two candle intervals after the final close. Entry, SL and TP labels
retain the recorded Decimal precision. Dates use the journal timezone.

The chart reads genuine candles from the trade's exchange and chooses a resolution
suited to the trade's duration. Empty padding after a just-closed trade remains
empty until candles exist. Original SL/TP prices must have been observed while
the position was open; missing plans or unavailable candle history produce a
notice, never invented prices. The original plan stays fixed through stop moves
and partial closes; multiple targets use the furthest profitable target, as for
Potential RR. Trades that open and close entirely between observations may have
no recoverable original SL/TP plan.

Generated files are cached beside the journal database in `exit_charts/`.
The app uploads charts to qu.ax and stores the returned clickable reference URL
in Charts Exit. This works without running a server, creating a bucket, or
obtaining an API key. Uploaded charts are publicly readable by anyone with the
link, including their price levels and trade times; credentials and account IDs
are excluded. Uploads select the host's permanent storage option.
For details and an optional Google Cloud Storage alternative, see
[chart hosting](docs/exit-chart-storage.md).
Upload or chart errors do not block close-date/P&L updates and retry on later
refreshes, reusing cached PNGs and completed uploads across restarts.
Charts Exit contains the full-resolution reference URL, like an existing
TradingView snapshot link. Row heights and column widths stay unchanged.
The link opens the app-generated chart; it is not a TradingView-hosted snapshot.

An [illustrative example](examples/exit-chart-example.png) is generated by
`python examples/generate_exit_chart.py`. It uses explicitly labeled synthetic
candles for visual review; production uses exchange data.

Potential RR is `(TP - entry) / (entry - SL)` for longs and `(entry - TP) / (SL - entry)` for shorts, before fees. The first valid entry/stop plan is saved before any partial exit, and the first valid RR is retained after stop moves, partial exits and restarts. If TP is added later, the saved entry/stop basis can still supply RR. If the original stop was never observed, or entry/SL/TP is missing or invalid, RR stays blank rather than being inferred from a breakeven stop. A trade that opens and closes entirely while the tracker is off may therefore have no automatic RR.

P/L uses exchange-reported closing P/L when supplied. Otherwise it calculates realized P/L from actual quantities and prices using average entry cost. BingX execution commissions are included, including entry commissions once P/L is displayed. Lighter execution fees and funding payments are not reconciled; funding is excluded for both exchanges. These calculation details stay in this guide rather than being appended to your trade descriptions.

Set `ACTIVE_TRADES_JOURNAL_CAPITAL` to your fixed USD journal capital when ready. Until then, new Risk and Profit % cells stay blank while dollar P/L still updates. Capital is stored per tracked trade; changing the setting later applies to trades without a saved capital basis. Stop risk captured after a trade was already partially closed is not presented as its original risk. Existing manual risk percentages remain authoritative.

On first run, the tracker reads the previous 30 days to reconstruct currently open positions. It does not import trades already closed before journal tracking started. It adopts an existing Open row only when asset, side, and entry date match uniquely; a recently closed matching row can also be adopted when the close date agrees. Ambiguous matches or mismatched dates stop that write and produce a dashboard notice. This prevents guessing between positions on different accounts. Positions predating the available history are deferred until their complete history can be supplied. Choose a larger initial lookback (up to 90 days) and a new journal state path if necessary.

Executions, history checkpoints and original risk are stored in `state/journal.sqlite3`. Keep this file across restarts. The tracker rereads a five-minute overlap to catch delayed records and resumes after downtime within the exchange's available history. Google failures retain pending work locally and retry on the next refresh. API failures and quantity mismatches do not close positions or substitute zero P/L. The first history scan can take longer than a normal refresh; subsequent syncs run in the dashboard's existing background refresh worker.

Google row metadata ties each position to its row without adding visible columns. New row values and their identity are written in one [atomic batch](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate), allowing recovery when a response is lost. Inserts and row moves are resolved from metadata on each sync. The supplied summary formulas have bounded ranges (including W/L counts ending at row 100); the writer extends affected summary ranges when newly logged rows exceed those bounds. Other tabs are untouched. Run only one journal writer per spreadsheet; a local lock protects processes using the same state file. Do not share a state file across different exchange accounts or journal targets.

To configure remote access later:

1. [Enable the Google Sheets API](https://developers.google.com/workspace/guides/enable-apis) in a Google Cloud project.
2. [Create a service account and JSON key](https://developers.google.com/workspace/guides/create-credentials#service-account). Store the key outside this repository. Share this specific spreadsheet with the service account's email address as **Editor**. Domain-wide delegation is unnecessary.
3. Install the optional client dependency in this project's environment:

   ```bash
   pip install -e ".[journal]"
   ```

4. Add these settings to the local or shared `.env`:

   ```bash
   ACTIVE_TRADES_JOURNAL_ENABLED=true
   ACTIVE_TRADES_JOURNAL_SPREADSHEET=https://docs.google.com/spreadsheets/d/1OXRK3PwwuZ6nAYDpJDLAnMKQw0165ttemINsZLk3pjw/edit
   ACTIVE_TRADES_JOURNAL_SHEET=Sheet1
   ACTIVE_TRADES_JOURNAL_CREDENTIALS=/absolute/path/to/service-account.json
   # Enter your fixed capital amount here when ready.
   ACTIVE_TRADES_JOURNAL_CAPITAL=
   ```

5. Run `active-trades --journal-check` to check Google read/write authorization and headers. It reads the sheet and sends a valid write-permission probe that replaces a literal check value with itself in A1, skipping formulas and leaving cell contents, formatting and metadata unchanged. Then run `active-trades` for ongoing automatic updates, or `active-trades --once` for one actual sync. Before using `--once`, stop any running dashboard with `Ctrl+C` so the two processes do not compete for the journal lock. Keep exchange history-read credentials configured for every account to be journaled.

If reads succeed but a journal write returns HTTP 403, open the spreadsheet's Share dialog and give the service-account email **Editor** access. Viewer access passes a read check but cannot update the journal. If it already has Editor access, check the tab's protected ranges. API-disabled and missing-scope errors have separate diagnostic messages. `Another process is syncing this journal` means a running instance holds the local lock; let it finish or stop that instance before launching a separate sync.

The Sheets client requests only the Sheets scope; the chart publisher separately
requests the Cloud Storage object read/write scope only when an optional chart
bucket is configured. The default qu.ax publisher needs no Google scope.
The journal does not make the spreadsheet public, send emails, or place exchange
orders. All settings are listed in `.env.example`;
`ACTIVE_TRADES_JOURNAL_ENABLED=false` disables collection and remote journal requests.
