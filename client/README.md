# Workbench client (M1.4)

The workbench panel is a plain-JavaScript DSH Client bundle (`../client.js`). It
renders inside the Harness Web UI as a root `main` panel selected from the
`sidebar.panellist` entry; it never reads the service token and never talks to
the Python service or the exchange directly.

## Data sources (Host routes only)

| Route | Used for |
|---|---|
| `GET /trading-workbench/api/state` | top bar, overview KPIs, last round, round list, service facts, gaps |
| `GET /trading-workbench/api/health` | Host version, Harness LLM route, probe snapshot |
| `GET /trading-workbench/api/rounds/:id` | full `RoundView` for the selected round (lazy, cached per selection) |
| `POST /trading-workbench/api/run` | one manual round; `409` means a round is already running |

Polling: `state` every 8 s, `health` every 30 s, plus an extra 3 s `state` reload
while `in_flight` is set. Page visibility pauses both. No SSE, no WebSocket, no
external chart library or font.

## Views

- **Overview** — KPI strip (today's rounds/trades/no-trade/failed, last duration,
  data quality), equity chart, current position table, risk + service facts, last
  round summary, service gaps.
- **Chain** — round picker plus a six-stage track (analysis / red / blue /
  decision / risk / executor). Red and blue each show their opening and response
  call, so both debate rounds stay visible. A stage with no calls shows the early
  stop reason.
- **Evidence drawer** — five tabs: evidence (snapshot summary), prompt (version,
  model, effort, request/response hashes), output JSON (the `RoundView`
  projection), hard rules (P1/P2 and risk fields), timing (per-call latency and
  tokens). Opened from a call card; `Esc` closes it and focus returns.
- **Orders / Review / Capital** — consume the Host's own `orders`, `fills`,
  `reviews`, `account` and `budget` fields. Orders shows Host orders and fills;
  review lists Host reviews; capital separates the account snapshot, account
  values and risk budget. Empty arrays render the Host's gap/empty reason.

## Empty states and gaps

The Host now exposes `equityCurve`, `positions`, `orders`, `fills`, `reviews`,
`account` and `portfolio.gaps`. The panel never claims a field is missing from
the Host; every empty state shows the reason the Host reported:

| Surface | Reason source |
|---|---|
| Equity chart | `equityCurve.gap` / `equityCurve.empty_reason` (for example `E_EQUITY_CURVE_EMPTY · NO_EQUITY_POINTS`) |
| Positions | `positionsEmpty.gap` or a `portfolio.gaps` entry about positions |
| Orders / fills | `ordersEmpty`/`fillsEmpty` gap, or a matching `portfolio.gaps` entry |
| Reviews | `reviewsEmpty` gap, or a matching `portfolio.gaps` entry |
| Account values | `account.gap` / `account.empty` / `account.empty_reason` (`E_ACCOUNT_EMPTY · DEMO_CREDENTIALS_MISSING`) |
| Risk budget | `budget.gap` when the Host returns no budget values |

Only when the Host reports no gap/empty flag does the copy fall back to
"no data yet". The overview's gap card lists `state.gaps`, `portfolio.gaps` and
the per-surface gap fields, deduplicated; it is not a static “not exposed” note.

Freshness uses the round's `freshness.produced_at`: fresh ≤ 15 s, delayed ≤ 180 s,
expired above that (or when the service reports `BLOCKED`). The Host remains the
authority for whether a round may run.

## Reason codes and blocking detail

A round's `reasonCode` may carry a blocking sub-code, e.g.
`EXECUTION_BLOCKED:OTHER`. The Host `round-view.js` projects a presentation-safe
`reasonDetail` (from `execution.rationale` / `result.detail`, truncated and with
token-like strings redacted) plus up to three `reasonNotes` (from the t86
`{reason, confidence, regime, notes, warnings}` detail or the analysis
`market_read.notes` / `warnings`). The client renders `执行阻断（<CODE>）` plus
that detail, or `暂无详细原因` when no detail exists — `EXECUTION_BLOCKED:*` never
falls back to “unknown reason”. `NO_CANDIDATE` shows the reason plus 1–3 notes or
warnings instead of only “没有合格标的”. No prompt text, token or credential is
read for these lines.

## Local checks

```powershell
node trading-workbench\plugin\client\checks.mjs        # registration, locale, tokens, boundaries, stale copy
node trading-workbench\plugin\client\render-smoke.mjs  # renders the panel with Host-shaped payloads
$env:TWB_LIVE_STATE_URL='http://127.0.0.1:59201/trading-workbench/api/state'
node trading-workbench\plugin\client\render-smoke.mjs  # adds live-shape checks (needs the Host)
node --check trading-workbench\plugin\client.js
```

`render-smoke.mjs` supplies a tiny hooks runtime because no browser is available
in this environment; it drives the real components, view tabs, evidence drawer,
run and refresh buttons through their own handlers. It asserts that the real Host
shape never renders "not exposed" copy and that empty states show the reported
gap/empty reason. It is a render-path check, not a visual check.

## Styling

Only the 14 live `--dsw-*` tokens from `docs/05a-DSH实测校正.md` are used
(`bg-base`, `bg-layer-1`, `bg-layer-2`, `bg-overlay`, `border-l1/l2`,
`brand-primary`, `label-primary/secondary`, `state-error/idle/success/warn-primary`).
Chart strokes use tokens; derived translucency uses `color-mix()` over a token.
No literal hex, `rgb()` or `hsl()` appears in the bundle.
