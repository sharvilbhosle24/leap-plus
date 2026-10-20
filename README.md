# Leap+

**Live Dublin transport times, a live bus map and an AI assistant for UCD students, plus a
Stripe top-up flow for a *simulated* Leap balance.** Built at HackUCD 2026.

> **Simulated Leap ledger — no NTA integration.** The balance lives only in this app's own
> database. Leap+ cannot top up a real Leap card and cannot tap a validator. Payments use the
> Stripe sandbox with test cards only. No real money moves.

## What it does

- **Live departures** for every DART, rail and Luas station in Dublin, plus 2,474 bus stops in
  south Dublin. Each shows minutes to departure, how late or early it's running, and an
  "updated Xs ago" label so you always know how fresh the data is.
- **Live bus map** of around 700 Dublin buses, labelled by route. Tap a bus for its direction
  and how old its position is; tap a stop to zoom to it.
- **Favourite stops** on the home screen with their next departures. Search stops by name or by
  the number on the pole (e.g. `768`).
- **Ask Leap**, an AI assistant. Ask "train to Clonsilla from Connolly?" (typos are fine) or
  "when's my next bus?". Leap+ works out the real times itself and the AI only words the answer,
  so it can't make up a bus.
- **Top-ups with Stripe (sandbox)**: one-off top-ups (€3–€100) and a €12/week auto top-up.
  The balance changes only when Stripe's signed webhook confirms the payment, and a replayed
  event can never credit twice.

## How it works

- **Live times = timetable + live delay.** The NTA's GTFS-Realtime feed mostly reports how late
  each trip is rather than when it will arrive, so Leap+ adds that delay to the NTA's static
  timetable, which it imports into SQLite.
- **Map** positions come from the NTA's GTFS-Realtime `Vehicles` feed, drawn with Leaflet on
  OpenStreetMap.
- **Ask Leap** runs through OpenRouter. Before calling the model, the server finds the stations
  named in the question with typo-tolerant matching, computes their departures and any direct
  trains between them, and hands those to the model as the only facts it may use.
- **Polite to the NTA:** each feed is fetched at most once a minute and shared by every user.
  If the NTA is down, the app shows the last good data or the timetable, and says so.

## What we found in the data

The full log is in [TEST_LOG.md](TEST_LOG.md), with each test input, what happened and what we changed.

- **98.7%** of realtime trip IDs matched the static timetable, so live times could be joined
  reliably.
- **85%** of the feed's updates give only a delay, not a time, which is why Leap+ needs the
  timetable at all.
- Some **Luas predictions were 24 minutes early**, which isn't plausible. Leap+ now ignores
  predictions more than 5 minutes early and shows the timetable time, labelled as such.
- Some **bus positions were 18 minutes old**. Leap+ hides buses that haven't reported in
  5 minutes and says how many it hid.
- The **positions feed has buses only**, with no DART or Luas, so trains appear as departure
  times rather than on the map.
- Trains to Clonsilla say **"Maynooth"** on the front. Ask Leap therefore finds trains that call
  at both stations instead of matching destination names.

## Limits

- **The balance is simulated** and there is **no login**: every request acts as one fictional
  demo user.
- **Bus times cover south Dublin only** (south of the Liffey, Lucan to Dalkey to Bray).
  DART, rail and Luas cover all of Dublin.
- **Ask Leap finds direct trips only**, not journeys with a change.
- **Trips that run just after midnight aren't handled yet.**
- **Automated tests** cover config, the ledger, validation and Stripe webhooks (53 tests).
  Live times, the map and Ask Leap were checked by hand against the live feeds (see TEST_LOG.md).
- **Runs locally only.** Stripe webhooks reach your laptop through the Stripe CLI.

## Run it locally

You need Python 3.12 or newer (developed on 3.14). Optionally, the [Stripe CLI](https://docs.stripe.com/stripe-cli) for payments.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` (see the keys table below). Then download the NTA timetable (163 MB) and import
the stops and timetable. The import takes about 10 seconds and builds a database of about 290 MB.

```bash
mkdir -p data/gtfs && curl -fSL -o data/gtfs/GTFS_Realtime.zip https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip
```

```bash
python scripts/import_stops.py
```

Start the app and open <http://localhost:8000>. On a laptop it renders as a phone-width column.

```bash
uvicorn app.main:app --reload --port 8000
```

For payments, keep Stripe's webhook forwarder running in a second terminal:

```bash
stripe listen --forward-to localhost:8000/api/stripe/webhook --events checkout.session.completed,invoice.paid,customer.subscription.updated,customer.subscription.deleted
```

The first time, put its signing secret (`stripe listen --print-secret`) in `.env` as
`STRIPE_WEBHOOK_SECRET`, then restart `uvicorn`. Pay with Stripe's test card
**4242 4242 4242 4242**, any future expiry date and any CVC.

Run the tests:

```bash
python -m pytest -q
```

### Keys (all go in `.env`, never in the browser or the repo)

| Variable | Where to get it |
|---|---|
| `STRIPE_SECRET_KEY` | Stripe Dashboard (sandbox) → Developers → API keys → Secret key (`sk_test_…`). Live keys are refused at startup. |
| `STRIPE_WEBHOOK_SECRET` | `stripe listen --print-secret` (`whsec_…`) |
| `NTA_API_KEY` | [developer.nationaltransport.ie](https://developer.nationaltransport.ie) → subscribe to the **GTFS Realtime** product → Primary key (sent in the `x-api-key` header) |
| `OPENROUTER_API_KEY` | [openrouter.ai](https://openrouter.ai) → Keys |
| `OPENROUTER_MODEL` | Any OpenRouter chat model slug |

Every feature is optional. If a key is missing, that part of the app says it's turned off
instead of breaking.

## Pricing (Stripe sandbox)

| Option | Price | What the customer gets |
|---|---|---|
| One-off top-up | €3.00 to €100.00 | That amount added to the simulated balance, once |
| **Weekly auto top-up** | **€12.00 / week** | €12.00 added every 7 days until cancelled |

**Why €12 a week:** €12.00 is TFI's Dublin weekly fare cap for Student and Young Adult Leap
cards ([TFI 90 Minute Fare](https://www.transportforireland.ie/fares/leap-card/tfi-90-minute-fare/),
checked October 2026). It's the most a student can be charged in a week for capped Dublin
travel, so one weekly top-up covers the week. The €3 minimum is one day's cap.

### How a payment reaches the balance

1. `POST /api/topups` or `POST /api/subscription` validates the amount and creates a Stripe
   Checkout Session. Nothing is credited yet.
2. After paying, Stripe redirects back. The redirect proves nothing, so the app only shows
   "Confirming with Stripe…" until the webhook arrives.
3. `POST /api/stripe/webhook` verifies the `Stripe-Signature` header, skips event ids it has
   already seen, and credits inside one database transaction. Each Stripe payment can be
   credited at most once, even if it arrives in two different events.

## Project layout

```
app/
  main.py         FastAPI app: API routes under /api, serves the frontend at /
  config.py       Reads .env once at startup; refuses live Stripe keys
  db.py           SQLite connections, tables, demo user
  schema.sql      Table definitions (money is integer cents)
  errors.py       One JSON error shape for every API failure
  ledger.py       Money rules and credit(), the only code that changes a balance
  payments.py     Stripe Checkout Sessions, card options, payment status, cancel
  webhooks.py     Verifies Stripe webhooks and credits exactly once
  stops.py        Station list, bus stops and favourites
  realtime.py     NTA GTFS-Realtime: live departures, trips between stops, bus positions
  stop_finder.py  Finds stations named in a question, typos included
  ask.py          Ask Leap: computes the facts, asks OpenRouter to word the answer
scripts/
  import_stops.py Imports stops and the timetable from the NTA GTFS zip
static/
  index.html, styles.css, app.js   The mobile web app (no framework, no build step)
tests/            pytest suite
TEST_LOG.md       Inputs tested, what happened, and what we changed
```

## API

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | Server status and which features are available |
| GET | `/api/me` | The demo user and their simulated balance (integer cents) |
| GET | `/api/stations` | Dublin DART/rail/Luas stations and south Dublin bus stops |
| GET / PUT / DELETE | `/api/favourites[/{stop_id}]` | List, add or remove favourite stops |
| GET | `/api/stops/{stop_id}/departures` | Next departures with live status and feed age |
| GET | `/api/vehicles` | Live bus positions in Dublin (stale ones hidden and counted) |
| POST | `/api/ask` | Ask Leap. Body `{"message": "..."}` |
| GET | `/api/card`, `/api/transactions` | Top-up options and ledger history |
| POST | `/api/topups`, `/api/subscription` | Start a Stripe Checkout (sandbox) |
| DELETE | `/api/subscription` | Cancel the weekly auto top-up |
| GET | `/api/checkout/{session_id}` | Whether the webhook has confirmed a payment yet |
| POST | `/api/stripe/webhook` | Stripe only. Signature-verified |

Every error is `{"error": {"code": "...", "message": "..."}}`, and `message` is safe to show users.

## Data and credits

- Live data from the [NTA GTFS-Realtime API](https://developer.nationaltransport.ie) (product
  "GTFS Realtime"). Timetable from the NTA's
  [static GTFS](https://www.transportforireland.ie/transitData/PT_Data.html) (`GTFS_Realtime.zip`).
- Map data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, drawn with
  [Leaflet](https://leafletjs.com).
- Fares from [Transport for Ireland](https://www.transportforireland.ie/fares/leap-card/tfi-90-minute-fare/).
- Users, balances and transactions are synthetic. Leap+ is a student hackathon project and isn't
  affiliated with the NTA, TFI or Leap Card.
