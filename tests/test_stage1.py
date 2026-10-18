"""Stage 1 checkpoint: top-up validation, Stripe Checkout params, and the webhook ledger.

Stripe is faked here: these tests never call the real API. Webhook events are signed
with the same HMAC-SHA256 scheme Stripe uses, so signature checking runs for real.
"""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
import stripe

import app.payments as payments
import app.webhooks as webhooks
from app.db import DEMO_USER_ID, connect

SECRET = "whsec_unit_tests_only"  # matches conftest


# --- Helpers ---------------------------------------------------------------------


class FakeStripe:
    """Records what we send to Stripe and returns minimal fake objects."""

    def __init__(self, raise_on_create: Exception | None = None):
        self.sessions = []
        self.cancelled = []
        self._n = 0
        self.raise_on_create = raise_on_create
        self.v1 = SimpleNamespace(
            customers=SimpleNamespace(create=self._create_customer, retrieve=self._retrieve_customer),
            checkout=SimpleNamespace(sessions=SimpleNamespace(create=self._create_session)),
            subscriptions=SimpleNamespace(cancel=self._cancel),
        )

    def _create_customer(self, params):
        return SimpleNamespace(id="cus_fake")

    def _retrieve_customer(self, customer_id):
        return SimpleNamespace(id=customer_id, deleted=False)

    def _create_session(self, params):
        if self.raise_on_create:
            raise self.raise_on_create
        self._n += 1
        self.sessions.append(params)
        sid = f"cs_test_fake_{self._n}"
        return SimpleNamespace(id=sid, url=f"https://checkout.stripe.com/c/pay/{sid}")

    def _cancel(self, sub_id):
        self.cancelled.append(sub_id)
        return SimpleNamespace(id=sub_id, status="canceled")


@pytest.fixture()
def fake_stripe(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(payments, "stripe_client", lambda: fake)
    return fake


def signed_headers(body: str, secret: str = SECRET, timestamp: int | None = None) -> dict:
    t = int(time.time()) if timestamp is None else timestamp
    sig = hmac.new(secret.encode(), f"{t}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={t},v1={sig}", "Content-Type": "application/json"}


def send_event(client, event: dict, **sign_kwargs):
    body = json.dumps(event)
    return client.post("/api/stripe/webhook", content=body, headers=signed_headers(body, **sign_kwargs))


def event(event_id: str, event_type: str, obj: dict) -> dict:
    return {"id": event_id, "type": event_type, "api_version": "2026-09-30.endive", "data": {"object": obj}}


def paid_session(session_id: str, amount: int = 1200, **overrides) -> dict:
    obj = {
        "id": session_id, "object": "checkout.session", "mode": "payment", "payment_status": "paid",
        "currency": "eur", "amount_total": amount, "metadata": {"leapplus_user_id": str(DEMO_USER_ID)},
    }
    obj.update(overrides)
    return obj


def balance(client) -> int:
    return client.get("/api/me").json()["balance_cents"]


def ledger_consistent() -> bool:
    conn = connect()
    stored = conn.execute("SELECT balance_cents FROM users WHERE id = ?", (DEMO_USER_ID,)).fetchone()[0]
    summed = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM transactions WHERE user_id = ?", (DEMO_USER_ID,)).fetchone()[0]
    conn.close()
    return stored == summed


# --- Top-up input validation ---------------------------------------------------------


@pytest.mark.parametrize("amount", [0, -500, 299, 10_001, 10**12])
def test_topup_amount_out_of_range_rejected(client, fake_stripe, amount):
    res = client.post("/api/topups", json={"amount_cents": amount})
    assert res.status_code == 422
    assert res.json()["error"] == {"code": "amount_out_of_range", "message": "Top-ups must be between €3.00 and €100.00."}
    assert fake_stripe.sessions == []  # never reached Stripe


@pytest.mark.parametrize("body", [
    {"amount_cents": "abc"}, {"amount_cents": "1000"}, {"amount_cents": 10.5}, {"amount_cents": True},
    {"amount_cents": None}, {}, {"amount_cents": 1000, "user_id": 2},
])
def test_topup_non_integer_or_malformed_rejected(client, fake_stripe, body):
    res = client.post("/api/topups", json=body)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_input"
    assert fake_stripe.sessions == []


@pytest.mark.parametrize("amount", [300, 10_000])
def test_topup_boundaries_accepted_and_sent_to_stripe(client, fake_stripe, amount):
    res = client.post("/api/topups", json={"amount_cents": amount})
    assert res.status_code == 201
    assert res.json()["checkout_url"].startswith("https://checkout.stripe.com/")
    params = fake_stripe.sessions[-1]
    assert params["mode"] == "payment"
    assert params["line_items"][0]["price_data"]["unit_amount"] == amount
    assert params["line_items"][0]["price_data"]["currency"] == "eur"
    assert "{CHECKOUT_SESSION_ID}" in params["success_url"]
    assert params["metadata"]["leapplus_user_id"] == str(DEMO_USER_ID)
    assert balance(client) == 0  # creating a checkout never credits


def test_subscription_checkout_is_weekly_1200_eur(client, fake_stripe):
    res = client.post("/api/subscription")
    assert res.status_code == 201
    price = fake_stripe.sessions[-1]["line_items"][0]["price_data"]
    assert fake_stripe.sessions[-1]["mode"] == "subscription"
    assert price["unit_amount"] == 1200 and price["currency"] == "eur"
    assert price["recurring"] == {"interval": "week"}


def test_card_options_describe_plan(client):
    body = client.get("/api/card").json()
    assert body["weekly_plan"]["amount_cents"] == 1200
    assert body["weekly_plan"]["interval"] == "week"
    assert body["topup"] == {"min_cents": 300, "max_cents": 10_000, "presets_cents": [300, 1200, 2000]}
    assert body["subscription"] is None


@pytest.mark.parametrize("exc, status, code", [
    (stripe.APIConnectionError("network down"), 502, "stripe_unreachable"),
    (stripe.AuthenticationError("bad key"), 503, "stripe_auth_failed"),
])
def test_stripe_failures_become_clear_errors(client, monkeypatch, exc, status, code):
    monkeypatch.setattr(payments, "stripe_client", lambda: FakeStripe(raise_on_create=exc))
    res = client.post("/api/topups", json={"amount_cents": 1200})
    assert res.status_code == status
    assert res.json()["error"]["code"] == code


# --- Webhook: signature ------------------------------------------------------------


def test_webhook_rejects_missing_bad_tampered_and_stale_signatures(client):
    body = json.dumps(event("evt_sig", "checkout.session.completed", paid_session("cs_x")))
    url = "/api/stripe/webhook"
    assert client.post(url, content=body).status_code == 400
    assert client.post(url, content=body, headers=signed_headers(body, secret="whsec_wrong")).status_code == 400
    tampered = body.replace("1200", "99999")
    assert client.post(url, content=tampered, headers=signed_headers(body)).status_code == 400
    stale = signed_headers(body, timestamp=int(time.time()) - 600)
    assert client.post(url, content=body, headers=stale).status_code == 400
    garbage = b"\xff\xfe\x00"
    assert client.post(url, content=garbage, headers={"Stripe-Signature": "t=1,v1=00"}).status_code == 400
    assert balance(client) == 0


def test_webhook_rejects_signed_non_event(client):
    body = json.dumps({"hello": "world"})
    res = client.post("/api/stripe/webhook", content=body, headers=signed_headers(body))
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_event"


def test_webhook_without_secret_configured_is_503(client, monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(webhooks, "settings", replace(webhooks.settings, stripe_webhook_secret=None))
    res = send_event(client, event("evt_nosecret", "checkout.session.completed", paid_session("cs_x")))
    assert res.status_code == 503


# --- Webhook: one-off top-up --------------------------------------------------------


def test_topup_credited_only_by_webhook_and_replay_ignored(client, fake_stripe):
    session_id = client.post("/api/topups", json={"amount_cents": 1200}).json()["session_id"]
    assert client.get(f"/api/checkout/{session_id}").json()["status"] == "pending"

    evt = event("evt_topup_1", "checkout.session.completed", paid_session(session_id, 1200))
    assert send_event(client, evt).json()["outcome"] == "credited"
    assert balance(client) == 1200
    assert client.get(f"/api/checkout/{session_id}").json()["status"] == "credited"

    # Same event delivered again (Stripe retries, or `stripe events resend`).
    assert send_event(client, evt).json()["outcome"] == "duplicate_event"
    # A different event about the same payment.
    evt2 = event("evt_topup_2", "checkout.session.completed", paid_session(session_id, 1200))
    assert send_event(client, evt2).json()["outcome"] == "already_credited"

    assert balance(client) == 1200
    txs = client.get("/api/transactions").json()["transactions"]
    assert len(txs) == 1 and txs[0]["type"] == "topup" and txs[0]["amount_cents"] == 1200
    assert ledger_consistent()


@pytest.mark.parametrize("overrides, outcome", [
    ({"payment_status": "unpaid"}, "ignored_unpaid"),
    ({"currency": "usd"}, "ignored_wrong_currency"),
    ({"amount_total": 0}, "ignored_bad_amount"),
    ({"metadata": {}}, "ignored_not_ours"),
    ({"metadata": {"leapplus_user_id": "999"}}, "ignored_not_ours"),
    ({"metadata": {"leapplus_user_id": "1; DROP TABLE users"}}, "ignored_not_ours"),
])
def test_unpayable_or_foreign_sessions_never_credit(client, overrides, outcome):
    res = send_event(client, event("evt_bad", "checkout.session.completed", paid_session("cs_bad", **overrides)))
    assert res.status_code == 200 and res.json()["outcome"] == outcome
    assert balance(client) == 0


def test_unknown_event_type_is_acknowledged_and_ignored(client):
    res = send_event(client, event("evt_other", "charge.refunded", {"id": "ch_1"}))
    assert res.json()["outcome"] == "ignored_event_type"


def test_failure_mid_event_rolls_back_so_stripe_can_retry(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(webhooks, "credit", boom)
    evt = event("evt_retry", "checkout.session.completed", paid_session("cs_retry"))
    from fastapi.testclient import TestClient
    real_server = TestClient(client.app, raise_server_exceptions=False)  # return the 500 like production
    res = send_event(real_server, evt)
    assert res.status_code == 500 and res.json()["error"]["code"] == "internal_error"
    monkeypatch.undo()
    # Nothing was recorded, so Stripe's retry is processed normally.
    assert send_event(client, evt).json()["outcome"] == "credited"
    assert balance(client) == 1200 and ledger_consistent()


# --- Webhook: weekly subscription ---------------------------------------------------


def subscription_session(session_id: str, sub_id: str) -> dict:
    return paid_session(session_id, 1200, mode="subscription", subscription=sub_id)


def invoice(invoice_id: str, sub_id: str | None, legacy_shape: bool = False, customer: str = "cus_fake") -> dict:
    obj = {"id": invoice_id, "object": "invoice", "customer": customer, "currency": "eur",
           "amount_paid": 1200, "billing_reason": "subscription_create"}
    if legacy_shape:
        obj["subscription"] = sub_id
    elif sub_id:
        obj["parent"] = {"type": "subscription_details", "subscription_details": {
            "subscription": sub_id, "metadata": {"leapplus_user_id": str(DEMO_USER_ID)}}}
    return obj


@pytest.mark.parametrize("invoice_first", [False, True])
def test_subscription_creation_then_first_payment(client, fake_stripe, invoice_first):
    session_id = client.post("/api/subscription").json()["session_id"]
    created = event("evt_sub_created", "checkout.session.completed", subscription_session(session_id, "sub_1"))
    paid = event("evt_inv_paid", "invoice.paid", invoice("in_1", "sub_1"))
    # Stripe doesn't guarantee event order, so test both orders.
    first, second = (paid, created) if invoice_first else (created, paid)

    send_event(client, first)
    send_event(client, second)

    status = client.get(f"/api/checkout/{session_id}").json()
    assert status["subscription_created"] and status["first_payment_credited"] and status["status"] == "credited"
    assert balance(client) == 1200  # credited once, by invoice.paid only
    assert client.get("/api/card").json()["subscription"]["id"] == "sub_1"
    assert client.get("/api/transactions").json()["transactions"][0]["type"] == "auto_topup"
    assert ledger_consistent()


def test_subscription_created_is_not_credited_until_invoice_paid(client, fake_stripe):
    session_id = client.post("/api/subscription").json()["session_id"]
    out = send_event(client, event("evt_c", "checkout.session.completed", subscription_session(session_id, "sub_2")))
    assert out.json()["outcome"] == "subscription_recorded"
    assert balance(client) == 0
    assert client.get(f"/api/checkout/{session_id}").json()["status"] == "subscription_created"


def test_legacy_invoice_shape_mapped_by_customer(client, fake_stripe):
    client.post("/api/subscription")  # creates the demo user's Stripe customer (cus_fake)
    out = send_event(client, event("evt_legacy", "invoice.paid", invoice("in_legacy", "sub_3", legacy_shape=True)))
    assert out.json()["outcome"] == "credited"
    assert balance(client) == 1200


def test_invoice_without_subscription_or_unknown_customer_ignored(client):
    assert send_event(client, event("e1", "invoice.paid", invoice("in_x", None))).json()["outcome"] == "ignored_not_subscription"
    foreign = invoice("in_y", "sub_9", legacy_shape=True, customer="cus_someone_else")
    assert send_event(client, event("e2", "invoice.paid", foreign)).json()["outcome"] == "ignored_not_ours"
    assert balance(client) == 0


def test_second_subscription_blocked_and_cancel_works(client, fake_stripe):
    send_event(client, event("evt_p", "invoice.paid", invoice("in_p", "sub_4")))
    res = client.post("/api/subscription")
    assert res.status_code == 409 and res.json()["error"]["code"] == "already_subscribed"

    assert client.delete("/api/subscription").json() == {"status": "canceled"}
    assert fake_stripe.cancelled == ["sub_4"]
    assert client.get("/api/card").json()["subscription"] is None
    assert balance(client) == 1200  # money already credited stays
    assert client.delete("/api/subscription").status_code == 404


def test_subscription_deleted_event_marks_canceled(client):
    send_event(client, event("evt_p2", "invoice.paid", invoice("in_p2", "sub_5")))
    out = send_event(client, event("evt_del", "customer.subscription.deleted", {"id": "sub_5", "status": "canceled"}))
    assert out.json()["outcome"] == "subscription_status_updated"
    assert client.get("/api/card").json()["subscription"] is None


def test_unknown_checkout_session_is_404(client):
    res = client.get("/api/checkout/cs_test_never_made")
    assert res.status_code == 404 and res.json()["error"]["code"] == "unknown_checkout"
