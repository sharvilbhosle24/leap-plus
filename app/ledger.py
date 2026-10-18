"""The simulated Leap ledger: money rules, the weekly plan, and the one function that credits.

All amounts are integer cents. Only the Stripe webhook (webhooks.py) calls credit().
"""

import sqlite3

CURRENCY = "eur"

# One-off top-up limits. These are Leap+'s own rules, not NTA rules.
MIN_TOPUP_CENTS = 300      # €3.00: TFI's daily fare cap for Student/Young Adult Leap in Dublin
MAX_TOPUP_CENTS = 10_000   # €100.00: our cap for a student wallet; stops a typo like €1,000
TOPUP_PRESETS_CENTS = (300, 1200, 2000)

# Weekly auto top-up. €12.00 is TFI's Dublin weekly fare cap (Mon–Sun) for Student and
# Young Adult Leap cards (transportforireland.ie, checked 2026-10-10): one top-up covers
# the most a student can be charged for capped travel in a week.
WEEKLY_PLAN_CENTS = 1200


def euros(cents: int) -> str:
    """Format cents for messages using integer maths only, e.g. 1250 -> '€12.50'."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}€{cents // 100}.{cents % 100:02d}"


def credit(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    amount_cents: int,
    kind: str,
    event_id: str,
    object_id: str,
    subscription_id: str | None = None,
) -> bool:
    """Add a confirmed Stripe payment to the user's balance.

    Must run inside the caller's transaction, so the ledger row, the balance and the
    processed-event record are saved together or not at all.
    Returns False (and changes nothing) if this Stripe payment was already credited.
    """
    if not isinstance(amount_cents, int) or amount_cents <= 0:
        raise ValueError("amount_cents must be a positive integer")
    already = conn.execute(
        "SELECT 1 FROM transactions WHERE stripe_object_id = ?", (object_id,)
    ).fetchone()
    if already:
        return False
    conn.execute(
        """INSERT INTO transactions
               (user_id, type, amount_cents, stripe_event_id, stripe_object_id, stripe_subscription_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, kind, amount_cents, event_id, object_id, subscription_id),
    )
    conn.execute(
        "UPDATE users SET balance_cents = balance_cents + ? WHERE id = ?", (amount_cents, user_id)
    )
    return True


def list_transactions(conn: sqlite3.Connection, user_id: int, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """SELECT id, type, amount_cents, created_at FROM transactions
           WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]
