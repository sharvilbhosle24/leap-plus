"""Stops from NTA static GTFS, and the user's favourite stops. No live times here (that's Stage 2)."""

import sqlite3

from fastapi import APIRouter, Depends, Path

from app.db import DEMO_USER_ID, get_db
from app.errors import ApiError

router = APIRouter(prefix="/api")

# Greater Dublin, roughly Balbriggan to Greystones, so InterCity stops elsewhere don't clutter the list.
DUBLIN_BBOX = (53.13, 53.62, -6.60, -6.00)  # min_lat, max_lat, min_lon, max_lon


def _stop_dict(row: sqlite3.Row) -> dict:
    return {"stop_id": row["stop_id"], "name": row["name"], "kind": row["kind"], "code": row["code"],
            "lat": row["lat"], "lon": row["lon"], "favourite": bool(row["favourite"])}


@router.get("/stations")
def stations(db: sqlite3.Connection = Depends(get_db)):
    """DART/rail and Luas stations in Dublin (one entry per name), plus every south Dublin bus stop."""
    rows = db.execute(
        """SELECT s.stop_id, s.name, s.kind, s.code, s.lat, s.lon,
                  EXISTS(SELECT 1 FROM favourite_stops f WHERE f.user_id = ? AND f.stop_id = s.stop_id) AS favourite
           FROM stops s
           WHERE s.kind IN ('rail', 'tram') AND s.lat BETWEEN ? AND ? AND s.lon BETWEEN ? AND ?
             AND s.stop_id = (SELECT MIN(s2.stop_id) FROM stops s2 WHERE s2.name = s.name AND s2.kind = s.kind)
           ORDER BY s.name""",
        (DEMO_USER_ID, *DUBLIN_BBOX),
    ).fetchall()
    bus = db.execute(
        """SELECT s.stop_id, s.name, s.kind, s.code, s.lat, s.lon,
                  EXISTS(SELECT 1 FROM favourite_stops f WHERE f.user_id = ? AND f.stop_id = s.stop_id) AS favourite
           FROM stops s
           WHERE s.kind = 'bus' AND EXISTS (SELECT 1 FROM stop_times st WHERE st.stop_id = s.stop_id)
           ORDER BY s.name, s.code""",
        (DEMO_USER_ID,),
    ).fetchall()
    rows = list(rows) + list(bus)
    if not rows and db.execute("SELECT COUNT(*) FROM stops").fetchone()[0] == 0:
        raise ApiError(503, "stops_not_imported", "Stop data isn't loaded yet. Run: python scripts/import_stops.py")
    return {"stations": [_stop_dict(r) for r in rows]}


@router.get("/favourites")
def favourites(db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute(
        """SELECT s.stop_id, s.name, s.kind, s.code, s.lat, s.lon, 1 AS favourite
           FROM favourite_stops f JOIN stops s ON s.stop_id = f.stop_id
           WHERE f.user_id = ? ORDER BY f.created_at""",
        (DEMO_USER_ID,),
    ).fetchall()
    return {"favourites": [_stop_dict(r) for r in rows]}


@router.put("/favourites/{stop_id}")
def add_favourite(stop_id: str = Path(max_length=64), db: sqlite3.Connection = Depends(get_db)):
    if not db.execute("SELECT 1 FROM stops WHERE stop_id = ?", (stop_id,)).fetchone():
        raise ApiError(404, "unknown_stop", "We don't know that stop.")
    with db:
        db.execute("INSERT OR IGNORE INTO favourite_stops (user_id, stop_id) VALUES (?, ?)", (DEMO_USER_ID, stop_id))
    return {"stop_id": stop_id, "favourite": True}


@router.delete("/favourites/{stop_id}")
def remove_favourite(stop_id: str = Path(max_length=64), db: sqlite3.Connection = Depends(get_db)):
    with db:
        db.execute("DELETE FROM favourite_stops WHERE user_id = ? AND stop_id = ?", (DEMO_USER_ID, stop_id))
    return {"stop_id": stop_id, "favourite": False}
