"""Stripe webhook: the ONLY code path that credits the simulated ledger.

Each event is (1) checked against the Stripe-Signature header, (2) looked up by event id
so a replay is ignored, and (3) handled inside one SQLite transaction: everything about
the event is saved, or nothing is and Stripe retries later.
"""

import json
import logging
import sqlite3

import stripe
from fastapi import APIRouter, Request

from app.config import settings
from app.db import connect
from app.errors import ApiError
from app.ledger import CURRENCY, credit

log = logging.getLogger("leapplus.webhooks")
router = APIRouter()

SIGNATURE_TOLERANCE_SECONDS = 300  # reject events signed more than 5 minutes ago


@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    if not settings.stripe_webhook_secret:
        raise ApiError(503, "webhook_not_configured", "STRIPE_WEBHOOK_SECRET is not set on the server.")

    payload = await request.body()
    try:
        stripe.WebhookSignature.verify_header(
            payload, request.headers.get("stripe-signature"), settings.stripe_webhook_secret,
            tolerance=SIGNATURE_TOLERANCE_SECONDS,
        )
    except (stripe.SignatureVerificationError, UnicodeDecodeError):
        # The SDK decodes the body as UTF-8 before checking, so garbage bytes land here too.
        log.warning("Rejected webhook with a bad or missing Stripe-Signature")
        raise ApiError(400, "invalid_signature", "Webhook signature verification failed.")

    try:
        event = json.loads(payload)
        event["id"], event["type"], event["data"]["object"]
    except (ValueError, KeyError, TypeError):
        raise ApiError(400, "invalid_event", "Webhook body is not a Stripe event.")

    conn = connect()
    try:
        outcome = process_event(conn, event)
    finally:
        conn.close()
    log.info("Stripe %s %s -> %s", event["type"], event["id"], outcome)
    return {"received": True, "outcome": outcome}


def process_event(conn: sqlite3.Connection, event: dict) -> str:
    """Apply one verified event exactly once. Returns a short outcome label."""
    # BEGIN IMMEDIATE takes SQLite's write lock up front, so two deliveries of the same
    # event can't both pass the "seen it before?" check at the same time.
    conn.execute("BEGIN IMMEDIATE")
    try:
        seen = conn.execute("SELECT 1 FROM stripe_events WHERE event_id = ?", (event["id"],)).fetchone()
        if seen:
            conn.rollback()
            return "duplicate_event"
        handler = HANDLERS.get(event["type"])
        outcome = handler(conn, event) if handler else "ignored_event_type"
        conn.execute(
            "INSERT INTO stripe_events (event_id, type, api_version, outcome) VALUES (?, ?, ?, ?)",
            (event["id"], event["type"], event.get("api_version"), outcome),
        )
        conn.commit()
        return outcome
    except Exception:
        conn.rollback()
        raise


# --- Helpers -------------------------------------------------------------------


def _user_from_metadata(conn: sqlite3.Connection, metadata: dict | None) -> int | None:
    raw = (metadata or {}).get("leapplus_user_id")
    if not isinstance(raw, str) or not raw.isdigit():
        return None
    row = conn.execute("SELECT id FROM users WHERE id = ?", (int(raw),)).fetchone()
    return row["id"] if row else None


def _user_from_customer(conn: sqlite3.Connection, customer: str | dict | None) -> int | None:
    if isinstance(customer, dict):
        customer = customer.get("id")
    if not customer:
        return None
    row = conn.execute("SELECT id FROM users WHERE stripe_customer_id = ?", (customer,)).fetchone()
    return row["id"] if row else None


def _invoice_subscription_id(invoice: dict) -> str | None:
    # Current API versions: invoice.parent.subscription_details.subscription.
    # Older versions put it at invoice.subscription. Events arrive in the account's
    # default API version, so accept both.
    details = (invoice.get("parent") or {}).get("subscription_details") or {}
    sub = details.get("subscription") or invoice.get("subscription")
    return sub.get("id") if isinstance(sub, dict) else sub


def _upsert_subscription(conn: sqlite3.Connection, sub_id: str, user_id: int, amount_cents: int, status: str) -> None:
    conn.execute(
        """INSERT INTO subscriptions (id, user_id, amount_cents, status) VALUES (?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               status = CASE WHEN subscriptions.status = 'canceled' THEN 'canceled' ELSE excluded.status END,
               updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')""",
        (sub_id, user_id, amount_cents, status),
    )


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


# --- Event handlers ----------------------------------------------------------------


def on_checkout_completed(conn: sqlite3.Connection, event: dict) -> str:
    session = event["data"]["object"]
    user_id = _user_from_metadata(conn, session.get("metadata"))
    if user_id is None:
        return "ignored_not_ours"

    if session.get("mode") == "subscription":
        # Subscription created. Its first payment is credited by invoice.paid, not here.
        sub_id = session.get("subscription")
        if isinstance(sub_id, dict):
            sub_id = sub_id.get("id")
        if not sub_id:
            return "ignored_no_subscription"
        amount = session.get("amount_total") if _positive_int(session.get("amount_total")) else 0
        status = "active" if session.get("payment_status") == "paid" else "incomplete"
        if amount:
            _upsert_subscription(conn, sub_id, user_id, amount, status)
        conn.execute("UPDATE checkout_sessions SET stripe_subscription_id = ? WHERE id = ?", (sub_id, session["id"]))
        return "subscription_recorded"

    if session.get("payment_status") != "paid":
        return "ignored_unpaid"
    if session.get("currency") != CURRENCY:
        return "ignored_wrong_currency"
    if not _positive_int(session.get("amount_total")):
        return "ignored_bad_amount"
    credited = credit(
        conn, user_id=user_id, amount_cents=session["amount_total"], kind="topup",
        event_id=event["id"], object_id=session["id"],
    )
    return "credited" if credited else "already_credited"


def on_invoice_paid(conn: sqlite3.Connection, event: dict) -> str:
    invoice = event["data"]["object"]
    sub_id = _invoice_subscription_id(invoice)
    if not sub_id:
        return "ignored_not_subscription"
    details = (invoice.get("parent") or {}).get("subscription_details") or {}
    user_id = _user_from_metadata(conn, details.get("metadata")) or _user_from_customer(conn, invoice.get("customer"))
    if user_id is None:
        return "ignored_not_ours"
    if invoice.get("currency") != CURRENCY:
        return "ignored_wrong_currency"
    if not _positive_int(invoice.get("amount_paid")):
        return "ignored_zero_amount"
    _upsert_subscription(conn, sub_id, user_id, invoice["amount_paid"], "active")
    credited = credit(
        conn, user_id=user_id, amount_cents=invoice["amount_paid"], kind="auto_topup",
        event_id=event["id"], object_id=invoice["id"], subscription_id=sub_id,
    )
    return "credited" if credited else "already_credited"


def on_subscription_changed(conn: sqlite3.Connection, event: dict) -> str:
    sub = event["data"]["object"]
    status = "canceled" if event["type"] == "customer.subscription.deleted" else sub.get("status")
    if not status:
        return "ignored_no_status"
    cur = conn.execute(
        "UPDATE subscriptions SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
        (status, sub.get("id")),
    )
    return "subscription_status_updated" if cur.rowcount else "ignored_not_ours"


HANDLERS = {
    "checkout.session.completed": on_checkout_completed,
    "invoice.paid": on_invoice_paid,
    "customer.subscription.updated": on_subscription_changed,
    "customer.subscription.deleted": on_subscription_changed,
}

# The events to forward with `stripe listen --events ...` (see README).
FORWARDED_EVENTS = ",".join(HANDLERS)
