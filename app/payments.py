"""Stripe Checkout: one-off top-ups, the weekly auto top-up, and payment status for the UI.

Nothing in this file credits the ledger. Money only arrives through webhooks.py,
after Stripe confirms the payment.
"""

import logging
import sqlite3

import stripe
from fastapi import APIRouter, Depends, FastAPI, Path, Request
from pydantic import BaseModel, ConfigDict, StrictInt

from app.config import settings
from app.db import DEMO_USER_ID, get_db
from app.errors import ApiError, error_response
from app.ledger import (
    CURRENCY,
    MAX_TOPUP_CENTS,
    MIN_TOPUP_CENTS,
    TOPUP_PRESETS_CENTS,
    WEEKLY_PLAN_CENTS,
    euros,
    list_transactions,
)

log = logging.getLogger("leapplus.payments")
router = APIRouter(prefix="/api")

# Subscription statuses that mean "the user already has weekly auto top-up".
CURRENT_STATUSES = ("active", "trialing", "past_due")

_client: stripe.StripeClient | None = None


def stripe_client() -> stripe.StripeClient:
    global _client
    if not settings.stripe_secret_key:
        raise ApiError(503, "stripe_not_configured", "Top-up is turned off: the server has no Stripe key.")
    if _client is None:
        _client = stripe.StripeClient(settings.stripe_secret_key)
    return _client


def install_stripe_error_handlers(app: FastAPI) -> None:
    """Turn Stripe SDK exceptions into clear, user-safe API errors (details go to the log)."""

    @app.exception_handler(stripe.AuthenticationError)
    async def _auth(_: Request, exc: stripe.AuthenticationError):
        log.error("Stripe rejected our API key: %s", exc)
        return error_response(503, "stripe_auth_failed", "Stripe rejected the server's API key. Check STRIPE_SECRET_KEY.")

    @app.exception_handler(stripe.APIConnectionError)
    async def _unreachable(_: Request, exc: stripe.APIConnectionError):
        log.error("Could not reach Stripe: %s", exc)
        return error_response(502, "stripe_unreachable", "Couldn't reach Stripe. Check your connection and try again.")

    @app.exception_handler(stripe.StripeError)
    async def _other(_: Request, exc: stripe.StripeError):
        log.error("Stripe error: %s", exc)
        return error_response(502, "stripe_error", exc.user_message or "Stripe couldn't start the payment. Please try again.")


def _customer_id(db: sqlite3.Connection, client: stripe.StripeClient) -> str:
    """The demo user's Stripe Customer, created on first use and reused afterwards."""
    user = db.execute(
        "SELECT name, email, stripe_customer_id FROM users WHERE id = ?", (DEMO_USER_ID,)
    ).fetchone()
    if user["stripe_customer_id"]:
        try:
            customer = client.v1.customers.retrieve(user["stripe_customer_id"])
            if not getattr(customer, "deleted", False):
                return customer.id
        except stripe.InvalidRequestError:
            pass  # deleted, or STRIPE_SECRET_KEY now points at a different sandbox
    customer = client.v1.customers.create(
        params={"name": user["name"], "email": user["email"], "metadata": {"leapplus_user_id": str(DEMO_USER_ID)}}
    )
    with db:
        db.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", (customer.id, DEMO_USER_ID))
    return customer.id


def _start_checkout(db: sqlite3.Connection, *, kind: str, amount_cents: int, mode: str, line_item: dict, extra: dict) -> dict:
    client = stripe_client()
    base = settings.app_base_url
    session = client.v1.checkout.sessions.create(
        params={
            "mode": mode,
            "customer": _customer_id(db, client),
            "client_reference_id": str(DEMO_USER_ID),
            "line_items": [line_item],
            "allowed_payment_method_types": ["card"],
            "metadata": {"leapplus_user_id": str(DEMO_USER_ID), "kind": kind},
            # Stripe replaces {CHECKOUT_SESSION_ID} itself. The redirect only tells the UI
            # which payment to wait for; it never credits anything.
            "success_url": f"{base}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{base}/?checkout=cancelled",
            **extra,
        }
    )
    with db:
        db.execute(
            "INSERT INTO checkout_sessions (id, user_id, kind, amount_cents) VALUES (?, ?, ?, ?)",
            (session.id, DEMO_USER_ID, kind, amount_cents),
        )
    log.info("Created %s checkout %s for %s", kind, session.id, euros(amount_cents))
    return {"session_id": session.id, "checkout_url": session.url}


def _current_subscription(db: sqlite3.Connection) -> dict | None:
    placeholders = ",".join("?" * len(CURRENT_STATUSES))
    row = db.execute(
        f"""SELECT id, amount_cents, status, created_at FROM subscriptions
            WHERE user_id = ? AND status IN ({placeholders})
            ORDER BY created_at DESC LIMIT 1""",
        (DEMO_USER_ID, *CURRENT_STATUSES),
    ).fetchone()
    return dict(row) if row else None


# --- Routes ------------------------------------------------------------------


class TopUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # StrictInt: "10", 10.5 and true are rejected rather than silently converted.
    amount_cents: StrictInt


@router.get("/card")
def card_options(db: sqlite3.Connection = Depends(get_db)):
    """Everything the Card screen needs to offer: top-up limits, the weekly plan, current subscription."""
    return {
        "currency": CURRENCY.upper(),
        "topup": {"min_cents": MIN_TOPUP_CENTS, "max_cents": MAX_TOPUP_CENTS, "presets_cents": list(TOPUP_PRESETS_CENTS)},
        "weekly_plan": {
            "amount_cents": WEEKLY_PLAN_CENTS,
            "interval": "week",
            "what_you_get": f"{euros(WEEKLY_PLAN_CENTS)} added to your simulated Leap+ balance every 7 days.",
            "why_this_price": f"{euros(WEEKLY_PLAN_CENTS)} is TFI's weekly fare cap for Student and Young Adult "
                              "Leap cards in Dublin, so one top-up covers a full week of capped travel.",
        },
        "subscription": _current_subscription(db),
    }


@router.get("/transactions")
def transactions(db: sqlite3.Connection = Depends(get_db)):
    return {"transactions": list_transactions(db, DEMO_USER_ID)}


@router.post("/topups", status_code=201)
def create_topup(body: TopUpRequest, db: sqlite3.Connection = Depends(get_db)):
    """Start a one-off top-up. Returns the Stripe Checkout URL to redirect to."""
    amount = body.amount_cents
    if amount < MIN_TOPUP_CENTS or amount > MAX_TOPUP_CENTS:
        raise ApiError(
            422, "amount_out_of_range",
            f"Top-ups must be between {euros(MIN_TOPUP_CENTS)} and {euros(MAX_TOPUP_CENTS)}.",
        )
    return _start_checkout(
        db, kind="topup", amount_cents=amount, mode="payment",
        line_item={"quantity": 1, "price_data": {
            "currency": CURRENCY,
            "unit_amount": amount,
            "product_data": {
                "name": "Leap+ top-up (simulated)",
                "description": f"Adds {euros(amount)} to your simulated Leap+ balance. Not a real Leap card top-up.",
            },
        }},
        extra={},
    )


@router.post("/subscription", status_code=201)
def create_subscription(db: sqlite3.Connection = Depends(get_db)):
    """Start the weekly auto top-up. Returns the Stripe Checkout URL to redirect to."""
    if _current_subscription(db):
        raise ApiError(409, "already_subscribed", "Weekly auto top-up is already active.")
    return _start_checkout(
        db, kind="auto_topup", amount_cents=WEEKLY_PLAN_CENTS, mode="subscription",
        line_item={"quantity": 1, "price_data": {
            "currency": CURRENCY,
            "unit_amount": WEEKLY_PLAN_CENTS,
            "recurring": {"interval": "week"},
            "product_data": {
                "name": "Leap+ weekly auto top-up (simulated)",
                "description": f"Adds {euros(WEEKLY_PLAN_CENTS)} to your simulated Leap+ balance every week. "
                               "Not a real Leap card top-up.",
            },
        }},
        extra={"subscription_data": {"metadata": {"leapplus_user_id": str(DEMO_USER_ID)}}},
    )


@router.delete("/subscription")
def cancel_subscription(db: sqlite3.Connection = Depends(get_db)):
    """Cancel the weekly auto top-up immediately. Money already credited stays."""
    current = _current_subscription(db)
    if not current:
        raise ApiError(404, "no_subscription", "There's no active weekly auto top-up to cancel.")
    stripe_client().v1.subscriptions.cancel(current["id"])
    with db:
        db.execute(
            "UPDATE subscriptions SET status = 'canceled', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (current["id"],),
        )
    return {"status": "canceled"}


@router.get("/checkout/{session_id}")
def checkout_status(session_id: str = Path(max_length=255), db: sqlite3.Connection = Depends(get_db)):
    """Has Stripe confirmed this checkout to us yet (via the webhook)? The UI polls this."""
    row = db.execute(
        "SELECT kind, amount_cents, stripe_subscription_id FROM checkout_sessions WHERE id = ? AND user_id = ?",
        (session_id, DEMO_USER_ID),
    ).fetchone()
    if row is None:
        raise ApiError(404, "unknown_checkout", "We don't have a record of that payment.")

    if row["kind"] == "topup":
        tx = db.execute("SELECT amount_cents FROM transactions WHERE stripe_object_id = ?", (session_id,)).fetchone()
        return {
            "kind": "topup",
            "status": "credited" if tx else "pending",
            "amount_cents": row["amount_cents"],
        }

    sub_id = row["stripe_subscription_id"]
    first_payment = sub_id and db.execute(
        "SELECT 1 FROM transactions WHERE stripe_subscription_id = ? LIMIT 1", (sub_id,)
    ).fetchone()
    return {
        "kind": "auto_topup",
        "status": "credited" if first_payment else ("subscription_created" if sub_id else "pending"),
        "subscription_created": bool(sub_id),
        "first_payment_credited": bool(first_payment),
        "amount_cents": row["amount_cents"],
    }
