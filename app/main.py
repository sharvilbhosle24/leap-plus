"""The FastAPI app: API routes under /api, and the static frontend at /."""

import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import DEMO_USER_ID, connect, get_db, init_db
from app.errors import ApiError, install_error_handlers
from app.payments import install_stripe_error_handlers
from app.payments import router as payments_router
from app.webhooks import router as webhooks_router
from app.ask import router as ask_router
from app.stops import router as stops_router
from app.realtime import router as realtime_router

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Which features are built so far. The UI only shows a feature when it is built
# AND its keys are configured (see /api/health), so it never advertises something broken.
BUILT = {"topup": True, "live": True, "favourites": False, "journeys": False, "ask": True}
# The integration whose keys each feature needs.
NEEDS = {"topup": "stripe", "live": "nta", "favourites": "nta", "journeys": "nta", "ask": "openrouter"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Leap+", lifespan=lifespan)
install_error_handlers(app)
install_stripe_error_handlers(app)
app.include_router(payments_router)
app.include_router(webhooks_router)
app.include_router(ask_router)
app.include_router(stops_router)
app.include_router(realtime_router)


@app.middleware("http")
async def no_stale_frontend(request: Request, call_next):
    # During a live hackathon we edit JS/CSS constantly: make the browser revalidate.
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.get("/api/health")
def health():
    """Is the server up, is the DB reachable, and which features can the UI show?"""
    try:
        conn = connect()
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except sqlite3.Error:
        db_ok = False

    integrations = settings.integrations()
    features = {}
    for name, built in BUILT.items():
        if not built:
            features[name] = {"available": False, "reason": "not_built"}
        elif not integrations[NEEDS[name]]:
            features[name] = {"available": False, "reason": "missing_keys", "needs": NEEDS[name]}
        else:
            features[name] = {"available": True}

    body = {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "integrations": integrations,
        "features": features,
        "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)


@app.get("/api/me")
def me(db: sqlite3.Connection = Depends(get_db)):
    """The demo user and their simulated Leap balance (integer cents)."""
    row = db.execute(
        "SELECT id, name, balance_cents FROM users WHERE id = ?", (DEMO_USER_ID,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "user_not_found", "Demo user is missing. Restart the server to re-seed it.")
    return {
        "id": row["id"],
        "name": row["name"],
        "balance_cents": row["balance_cents"],
        "currency": "EUR",
        "ledger": "simulated",
    }


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
