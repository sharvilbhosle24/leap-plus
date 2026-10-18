"""Stage 0 checkpoint: server, config, schema, seed and shell all work."""

import pytest

from app.config import ConfigError, check_stripe_key_is_test
from app.db import DEMO_USER_ID, connect, init_db


def test_health_ok_and_never_leaks_keys(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok" and body["db"] == "ok"
    # Integrations are reported as booleans only, never key values.
    assert all(isinstance(v, bool) for v in body["integrations"].values())
    assert "sk_" not in res.text and "whsec_" not in res.text


def test_unbuilt_features_are_reported_unavailable(client):
    features = client.get("/api/health").json()["features"]
    assert set(features) == {"topup", "live", "favourites", "journeys", "ask"}
    assert features["topup"] == {"available": True}  # built in Stage 1, keys set by conftest
    unbuilt = {k: v for k, v in features.items() if k not in ("topup", "ask", "live")}
    assert all(f["available"] is False and f["reason"] == "not_built" for f in unbuilt.values())


def test_demo_user_seeded_with_integer_cents(client):
    res = client.get("/api/me")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == DEMO_USER_ID
    assert body["balance_cents"] == 0 and isinstance(body["balance_cents"], int)
    assert body["currency"] == "EUR" and body["ledger"] == "simulated"


def test_seed_is_idempotent(client):
    init_db()
    init_db()
    conn = connect()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    assert count == 1


def test_negative_balance_rejected_by_schema(client):
    conn = connect()
    with pytest.raises(Exception):
        with conn:
            conn.execute("UPDATE users SET balance_cents = -1 WHERE id = ?", (DEMO_USER_ID,))
    conn.close()


def test_shell_served_with_disclaimer(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Simulated Leap ledger — no NTA integration" in res.text
    for tab in ("#home", "#live", "#card", "#ask"):
        assert f'href="{tab}"' in res.text


def test_unknown_route_returns_json_error_shape(client):
    res = client.get("/api/does-not-exist")
    assert res.status_code == 404
    assert res.json() == {"error": {"code": "not_found", "message": "Not found."}}


def test_dotenv_not_reachable_through_static(client):
    for path in ("/static/../.env", "/static/%2e%2e/.env", "/.env"):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("key", ["sk_live_abc", "rk_live_abc", "pk_test_abc", "garbage"])
def test_non_sandbox_stripe_keys_refused(key):
    with pytest.raises(ConfigError):
        check_stripe_key_is_test(key)


@pytest.mark.parametrize("key", [None, "sk_test_abc", "rk_test_abc"])
def test_sandbox_or_missing_stripe_keys_allowed(key):
    check_stripe_key_is_test(key)
