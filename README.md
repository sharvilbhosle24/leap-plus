# Leap+

A better TFI Live for UCD students commuting into Dublin: live bus positions on a map,
favourite stops and journeys, a top-up flow for a **simulated** Leap balance, and an AI
assistant ("Ask Leap") that answers only from our own data. Built for HackUCD 2026.

> **Simulated Leap ledger — no NTA integration.** The balance lives only in this app's
> SQLite database. Leap+ cannot top up a real Leap card and cannot tap a validator.
> Payments use the Stripe sandbox with test cards only.

## Status

| Stage | Feature | State |
|---|---|---|
| 0 | Skeleton: config, schema, health check, 4-tab shell, demo user | ✅ done |
| 1 | Ledger + Stripe top-up and weekly auto-top-up | ✅ done |
| 2 | Live bus map (NTA GTFS-Realtime) + stop departures | not started |
| 3 | Favourite stops | not started |
| 4 | Favourite journeys | not started |
| 5 | Ask Leap (OpenRouter) + eval | not started |

## Run it

Needs Python 3.12 or newer (developed on 3.14). From the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000>. On a laptop it renders as a phone-width column.
The app runs with an empty `.env`; features whose keys are missing say so instead of breaking.

### Stripe webhooks (needed for top-ups to land)

The ledger is credited **only** by Stripe's webhook, so keep this running in a second terminal
whenever you test or demo payments:

```bash
stripe listen --forward-to localhost:8000/api/stripe/webhook --events checkout.session.completed,invoice.paid,customer.subscription.updated,customer.subscription.deleted
```

The first time, print the signing secret and paste it into `.env` as `STRIPE_WEBHOOK_SECRET`:

```bash
stripe listen --print-secret
```

Then restart `uvicorn` so it reads the new value. Pay with Stripe's test card
**4242 4242 4242 4242**, any future expiry date, any CVC.

To prove a replayed event can't double-credit, resend a processed event and watch the
server log print `duplicate_event`:

```bash
stripe events resend <evt_id from the server log>
```

Run the tests:

```bash
python -m pytest -q
```

## Keys you need (all go in `.env`, never in the browser or the repo)

| Variable | Where to get it | Needed from |
|---|---|---|
| `STRIPE_SECRET_KEY` | Stripe Dashboard, sandbox → Developers → API keys → Secret key (`sk_test_…`). Live keys are refused at startup. | Stage 1 |
| `STRIPE_WEBHOOK_SECRET` | Printed by `stripe listen` when it starts (`whsec_…`) | Stage 1 |
| `NTA_API_KEY` | developer.nationaltransport.ie → subscribe to product **GTFS Realtime** → Primary key. Sent in header `x-api-key`. | Stage 2 |
| `OPENROUTER_API_KEY` | openrouter.ai → Keys | Stage 5 |
| `OPENROUTER_MODEL` | Chosen in Stage 5 after checking tool-calling support | Stage 5 |

`APP_BASE_URL` (default `http://localhost:8000`) and `DATABASE_PATH` (default `data/leapplus.db`) are optional.

## Pricing (Stripe sandbox)

| Option | Price | What the customer gets |
|---|---|---|
| One-off top-up | €3.00 to €100.00, presets €3 / €12 / €20 | That amount added to the simulated Leap+ balance, once |
| **Weekly auto top-up** | **€12.00 / week** (EUR) | €12.00 added to the simulated balance every 7 days until cancelled |

**Why €12/week:** TFI's Dublin weekly fare cap for Student and Young Adult Leap cards is €12.00
(Monday to Sunday), with a €1.00 90-minute fare and a €3.00 daily cap
([TFI 90 Minute Fare](https://www.transportforireland.ie/fares/leap-card/tfi-90-minute-fare/), checked 2026-10-10).
€12 is the most a student can be charged in a week for capped Dublin travel, so one weekly
top-up covers a full week and the balance can't run dry mid-commute. The minimum one-off
top-up (€3.00) is one day's cap. The €100 maximum is our own cap to catch typos.
*Assumption to verify with students:* they mainly use capped services (Dublin Bus, Go-Ahead
city routes, Luas, DART in Zone 1). Express, Nitelink and commuter buses are not capped.

## How a payment reaches the ledger

1. The UI calls `POST /api/topups` or `POST /api/subscription`. The server validates the amount,
   creates a Stripe Checkout Session and returns its URL. Nothing is credited.
2. The student pays on Stripe's hosted page and is redirected to `/?checkout=success&session_id=…`.
   The redirect proves nothing, so the UI only shows "Confirming with Stripe…" and polls
   `GET /api/checkout/{id}`.
3. Stripe sends a signed webhook. `POST /api/stripe/webhook` verifies the `Stripe-Signature`
   header, skips any event id it has seen before, and credits in one SQLite transaction:
   - `checkout.session.completed` (payment mode, `payment_status=paid`, EUR) credits a one-off top-up.
   - `checkout.session.completed` (subscription mode) records the subscription. It does **not** credit.
   - `invoice.paid` credits each weekly payment, including the first.
   - `customer.subscription.updated` / `.deleted` keep the subscription status in sync.
4. Each Stripe payment (Checkout Session or Invoice id) can be credited at most once, even if it
   arrives in two different events (`UNIQUE` on `transactions.stripe_object_id`).

## Project layout

```
app/
  main.py      FastAPI app: API routes under /api, serves the frontend at /
  config.py    Reads .env once at startup; refuses live Stripe keys
  db.py        SQLite connections, creates tables, seeds the demo user
  schema.sql   Table definitions (money is INTEGER cents)
  errors.py    One JSON error shape for every API failure
  ledger.py    Money rules (limits, weekly price) and credit(), the only balance writer
  payments.py  Creates Stripe Checkout Sessions; card options; payment status; cancel
  webhooks.py  Verifies Stripe webhooks and credits the ledger exactly once
static/
  index.html   The mobile shell: header, disclaimer, 4 tabs
  styles.css   Dark theme, one accent colour
  app.js       Tab routing, fetch helper, loading/empty/error states
tests/
  conftest.py      Points tests at a throwaway database
  test_stage0.py   Stage 0 checkpoint tests
  test_stage1.py   Ledger, validation and webhook tests (Stripe faked, signatures real)
TEST_LOG.md    Ordinary, boundary and failure inputs per stage, and what we changed
```

## API so far

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | DB status, which integrations have keys (booleans only), which features the UI may show |
| GET | `/api/me` | The demo user and their simulated balance in integer cents |
| GET | `/api/card` | Top-up limits and presets, the weekly plan, the current subscription |
| GET | `/api/transactions` | Ledger entries, newest first |
| POST | `/api/topups` | Body `{"amount_cents": int}`. Returns a Stripe Checkout URL |
| POST | `/api/subscription` | Starts weekly auto top-up. Returns a Stripe Checkout URL (409 if already active) |
| DELETE | `/api/subscription` | Cancels weekly auto top-up at Stripe |
| GET | `/api/checkout/{session_id}` | `pending` / `subscription_created` / `credited`, as recorded by the webhook |
| POST | `/api/stripe/webhook` | Stripe only. Signature-verified |

Every error is `{"error": {"code": "...", "message": "..."}}`; `message` is safe to show users.

## Deliberate simplifications

- **No login.** Every request acts as one seeded, fictional user ("Demo Student",
  `demo.student@example.com`). Real accounts would come before any real launch.
- **Simulated balance.** See the note at the top. There is no connection to the NTA's Leap systems.
- **Local only.** Runs on one laptop; Stripe webhooks reach it through the Stripe CLI.

## Data sources

- NTA GTFS-Realtime API v2 (developer.nationaltransport.ie), product "GTFS Realtime". *(Stage 2)*
- NTA static GTFS `GTFS_Realtime.zip` from transportforireland.ie, the schedule the NTA says to use with GTFS-R. *(Stage 2)*
  Copy in use: downloaded 2026-10-10 into `data/gtfs/` (git-ignored), 163 MB,
  `feed_version 85A4A120-2091-49ED-8848-0ACC877337CD`, valid 2026-10-09 to 2027-10-09,
  SHA-256 `b698e168…62e30a1d29e`. To fetch it again:

  ```bash
  mkdir -p data/gtfs && curl -fSL -o data/gtfs/GTFS_Realtime.zip https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip
  ```
- Users, balances and transactions are synthetic and clearly labelled as such.
