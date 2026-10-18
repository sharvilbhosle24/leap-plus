"""Test setup: a throwaway database and fake keys, set BEFORE the app is imported.

Real environment variables win over .env, so tests never touch your real keys
or make real Stripe calls (Stripe is replaced by a fake in the tests that need it).
"""

import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="leapplus-test-")
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ["STRIPE_SECRET_KEY"] = "sk_test_unit_tests_only"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_unit_tests_only"
os.environ["NTA_API_KEY"] = ""
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["OPENROUTER_MODEL"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402


def reset_data() -> None:
    conn = connect()
    with conn:
        for table in ("transactions", "stripe_events", "checkout_sessions", "subscriptions"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE users SET balance_cents = 0, stripe_customer_id = NULL")
    conn.close()


@pytest.fixture()
def client():
    # Using TestClient as a context manager runs startup (init_db + seed).
    with TestClient(app) as c:
        reset_data()
        yield c
