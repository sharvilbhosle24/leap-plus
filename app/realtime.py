"""Live departures for DART, Luas and south Dublin bus stops: timetable + NTA GTFS-Realtime TripUpdates.

Expected time = scheduled time + the live delay for that trip. The NTA feed mostly sends
delays (not absolute times), and a delay applies from its stop onwards (GTFS-R spec).
"""

import csv
import io
import logging
import sqlite3
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path as FilePath  # fastapi.Path is the route-parameter helper
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Path, Query
from google.transit import gtfs_realtime_pb2 as rt

from app.config import settings
from app.db import get_db
from app.errors import ApiError

log = logging.getLogger("leapplus.realtime")
router = APIRouter(prefix="/api")

FEED_URL = "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates"  # key goes in header x-api-key
REFRESH_SECONDS = 60   # conservative: NTA's limit is unpublished to us; one fetch a minute for everyone
STALE_SECONDS = 180    # older than this, we say so in the UI
MIN_PLAUSIBLE_DELAY = -300  # more than 5 min early is treated as bad data
TZ = ZoneInfo("Europe/Dublin")
SKIPPED = rt.TripUpdate.StopTimeUpdate.SKIPPED
CANCELED = rt.TripDescriptor.CANCELED


@dataclass
class Update:
    seq: int | None
    stop_id: str
    delay: int | None
    time: int | None
    skipped: bool


@dataclass
class TripRT:
    canceled: bool
    updates: list[Update] = field(default_factory=list)


_cache: dict = {"checked_at": 0.0, "feed_ts": None, "trips": {}, "error": None}
_lock = threading.Lock()


def refresh_feed() -> None:
    """Fetch TripUpdates at most once per REFRESH_SECONDS. On failure keep the last good copy."""
    with _lock:
        if time.time() - _cache["checked_at"] < REFRESH_SECONDS:
            return
        _cache["checked_at"] = time.time()  # also spaces out retries after an error or a 429
        if not settings.nta_api_key:
            _cache["error"] = "no NTA key"
            return
        try:
            res = httpx.get(FEED_URL, headers={"x-api-key": settings.nta_api_key}, timeout=15)
            if res.status_code == 429:
                raise RuntimeError("rate limited by NTA (429)")
            res.raise_for_status()
            feed = rt.FeedMessage()
            feed.ParseFromString(res.content)
        except Exception as exc:  # network, HTTP, protobuf decode: all mean "no fresh data"
            _cache["error"] = str(exc)
            log.warning("NTA TripUpdates fetch failed: %s", exc)
            return

        trips: dict[str, TripRT] = {}
        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue
            tu = entity.trip_update
            info = TripRT(canceled=tu.trip.schedule_relationship == CANCELED)
            for u in tu.stop_time_update:
                ev = u.departure if u.HasField("departure") else u.arrival
                info.updates.append(Update(
                    seq=u.stop_sequence if u.HasField("stop_sequence") else None,
                    stop_id=u.stop_id,
                    delay=ev.delay if ev.HasField("delay") else None,
                    time=ev.time if ev.HasField("time") and ev.time else None,
                    skipped=u.schedule_relationship == SKIPPED,
                ))
            info.updates.sort(key=lambda x: x.seq if x.seq is not None else 10**6)
            trips[tu.trip.trip_id] = info
        _cache.update(trips=trips, feed_ts=feed.header.timestamp, error=None)
        log.info("NTA TripUpdates refreshed: %d trips", len(trips))


def _estimate(trip: TripRT | None, stop_id: str, seq: int, scheduled: int) -> tuple[int, str, int | None] | None:
    """(expected_epoch, status, delay_seconds), or None if the vehicle won't call here."""
    if trip is None:
        return scheduled, "scheduled", None
    if trip.canceled:
        return scheduled, "cancelled", None
    best = None
    for u in trip.updates:  # the latest update at or before this stop applies here
        if (u.seq is not None and u.seq <= seq) or (u.seq is None and u.stop_id == stop_id):
            best = u
    if best is None:
        # Updates only for later stops: the vehicle has already passed this stop.
        return None if trip.updates else (scheduled, "scheduled", None)
    if best.skipped and (best.seq == seq or best.stop_id == stop_id):
        return None
    if best.time and (best.seq == seq or best.stop_id == stop_id):
        delay = best.time - scheduled
    elif best.delay is not None:
        delay = best.delay
    else:
        return scheduled, "scheduled", None
    # Observed 2026-10-10: some Luas predictions were up to 24 min *early*, which isn't
    # plausible for a scheduled tram/train. Don't trust those; show the timetable instead.
    if delay < MIN_PLAUSIBLE_DELAY:
        return scheduled, "scheduled", None
    return scheduled + delay, "live", delay


def _active_services(db: sqlite3.Connection, day: date) -> set[str]:
    ymd = day.strftime("%Y%m%d")
    ids = {r[0] for r in db.execute(
        "SELECT service_id FROM calendar WHERE start_date <= ? AND end_date >= ? AND substr(days, ?, 1) = '1'",
        (ymd, ymd, day.weekday() + 1))}
    for service_id, exception in db.execute("SELECT service_id, exception_type FROM calendar_dates WHERE date = ?", (ymd,)):
        if exception == 1:
            ids.add(service_id)
        elif exception == 2:
            ids.discard(service_id)
    return ids


def _route_label(short_name: str, long_name: str, route_type: int) -> str:
    if route_type == 0:
        return f"Luas {short_name}"
    if route_type == 3:
        return short_name or "Bus"
    return short_name if short_name and short_name.lower() != "rail" else "Train"


@router.get("/stops/{stop_id}/departures")
def departures(
    stop_id: str = Path(max_length=64),
    limit: int = Query(6, ge=1, le=20),
    db: sqlite3.Connection = Depends(get_db),
):
    return compute_departures(db, stop_id, limit)


def compute_departures(db: sqlite3.Connection, stop_id: str, limit: int) -> dict:
    """Next departures for one stop. Also used by Ask Leap, so the AI gets our numbers."""
    stop = db.execute("SELECT stop_id, name, kind FROM stops WHERE stop_id = ?", (stop_id,)).fetchone()
    if stop is None:
        raise ApiError(404, "unknown_stop", "We don't know that stop.")
    if stop["kind"] == "bus" and not db.execute("SELECT 1 FROM stop_times WHERE stop_id = ? LIMIT 1", (stop_id,)).fetchone():
        raise ApiError(422, "outside_area", "Live bus times cover south Dublin stops for now.")

    refresh_feed()
    now = datetime.now(TZ)
    midnight = int(datetime(now.year, now.month, now.day, tzinfo=TZ).timestamp())
    now_epoch = int(now.timestamp())
    services = _active_services(db, now.date())
    if stop["kind"] == "bus":
        platform_ids = [stop_id]  # bus stops sharing a name are often on opposite sides of the road
    else:  # a station's platforms are separate stops with the same name
        platform_ids = [r[0] for r in db.execute("SELECT stop_id FROM stops WHERE name = ? AND kind = ?", (stop["name"], stop["kind"]))]

    q_stops = ",".join("?" * len(platform_ids))
    q_services = ",".join("?" * len(services)) or "''"
    rows = db.execute(
        f"""SELECT st.trip_id, st.stop_id, st.stop_sequence, st.departure_secs, t.headsign,
                   r.short_name, r.long_name, r.route_type
            FROM stop_times st JOIN trips t ON t.trip_id = st.trip_id JOIN routes r ON r.route_id = t.route_id
            WHERE st.stop_id IN ({q_stops}) AND t.service_id IN ({q_services})
              AND st.departure_secs BETWEEN ? AND ?
              AND st.stop_sequence < t.last_seq""",
        (*platform_ids, *services, now_epoch - midnight - 1800, now_epoch - midnight + 7200),
    ).fetchall()

    trips_rt = _cache["trips"]
    out = []
    for r in rows:
        scheduled = midnight + r["departure_secs"]
        est = _estimate(trips_rt.get(r["trip_id"]), r["stop_id"], r["stop_sequence"], scheduled)
        if est is None:
            continue
        expected, status, delay = est
        if expected < now_epoch - 30:
            continue
        out.append({
            "route": _route_label(r["short_name"], r["long_name"], r["route_type"]),
            "headsign": r["headsign"],
            "scheduled_time": datetime.fromtimestamp(scheduled, TZ).strftime("%H:%M"),
            "expected_time": datetime.fromtimestamp(expected, TZ).strftime("%H:%M"),
            "minutes": max(0, (expected - now_epoch) // 60),
            "status": status,
            "delay_minutes": round(delay / 60) if delay is not None else None,
        })
    out.sort(key=lambda d: (d["minutes"], d["expected_time"]))

    feed_ts = _cache["feed_ts"]
    age = now_epoch - feed_ts if feed_ts else None
    if feed_ts is None:
        realtime = {"status": "unavailable", "message": "Live feed unavailable. Showing the timetable only."}
    elif age > STALE_SECONDS:
        realtime = {"status": "stale", "message": f"Live data is {age // 60} min old. Times may be off."}
    else:
        realtime = {"status": "live", "message": None}
    realtime["feed_age_seconds"] = age

    return {"stop": {"stop_id": stop["stop_id"], "name": stop["name"], "kind": stop["kind"]},
            "departures": out[:limit], "realtime": realtime}


def compute_trips_between(db: sqlite3.Connection, origin_id: str, destination_id: str, limit: int = 3) -> list[dict]:
    """Next direct trips that call at the origin and LATER at the destination (no changes).

    Answers "train to Clonsilla from Connolly" even though those trains say "Maynooth" on the front.
    """
    refresh_feed()
    ids = {}
    for key, sid in (("o", origin_id), ("d", destination_id)):
        stop = db.execute("SELECT name, kind FROM stops WHERE stop_id = ?", (sid,)).fetchone()
        ids[key] = [sid] if stop["kind"] == "bus" else [
            r[0] for r in db.execute("SELECT stop_id FROM stops WHERE name = ? AND kind = ?", (stop["name"], stop["kind"]))]
    now = datetime.now(TZ)
    midnight = int(datetime(now.year, now.month, now.day, tzinfo=TZ).timestamp())
    now_epoch = int(now.timestamp())
    services = _active_services(db, now.date())
    q_o, q_d = ",".join("?" * len(ids["o"])), ",".join("?" * len(ids["d"]))
    q_s = ",".join("?" * len(services)) or "''"
    rows = db.execute(
        f"""SELECT a.trip_id, a.stop_id, a.stop_sequence, a.departure_secs, b.departure_secs AS arrive_secs,
                   t.headsign, r.short_name, r.long_name, r.route_type
            FROM stop_times a
            JOIN stop_times b ON b.trip_id = a.trip_id AND b.stop_sequence > a.stop_sequence
            JOIN trips t ON t.trip_id = a.trip_id JOIN routes r ON r.route_id = t.route_id
            WHERE a.stop_id IN ({q_o}) AND b.stop_id IN ({q_d}) AND t.service_id IN ({q_s})
              AND a.departure_secs BETWEEN ? AND ?""",
        (*ids["o"], *ids["d"], *services, now_epoch - midnight - 1800, now_epoch - midnight + 7200),
    ).fetchall()
    out = []
    for r in rows:
        scheduled = midnight + r["departure_secs"]
        est = _estimate(_cache["trips"].get(r["trip_id"]), r["stop_id"], r["stop_sequence"], scheduled)
        if est is None:
            continue
        expected, status, delay = est
        if expected < now_epoch - 30:
            continue
        out.append({
            "route": _route_label(r["short_name"], r["long_name"], r["route_type"]),
            "headsign": r["headsign"],
            "minutes": max(0, (expected - now_epoch) // 60),
            "expected_time": datetime.fromtimestamp(expected, TZ).strftime("%H:%M"),
            "arrive_time": datetime.fromtimestamp(midnight + r["arrive_secs"] + (delay or 0), TZ).strftime("%H:%M"),
            "status": status,
            "delay_minutes": round(delay / 60) if delay is not None else None,
        })
    out.sort(key=lambda d: d["minutes"])
    return out[:limit]


# --- Live vehicle positions (buses only: the NTA Vehicles feed has no DART/Luas) -------

VEHICLES_URL = "https://api.nationaltransport.ie/gtfsr/v2/Vehicles"
VEHICLE_MAX_AGE = 300  # hide buses that haven't reported for 5 min (we saw reports 18 min old)
DUBLIN_BBOX = (53.13, 53.62, -6.60, -6.00)
GTFS_ZIP = FilePath(__file__).resolve().parent.parent / "data" / "gtfs" / "GTFS_Realtime.zip"

_vcache: dict = {"checked_at": 0.0, "feed_ts": None, "vehicles": [], "error": None}
_vlock = threading.Lock()
_route_names: dict[str, str] | None = None


def _all_route_names() -> dict[str, str]:
    """route_id -> short name for every route (routes.txt is tiny; read once from the GTFS zip)."""
    global _route_names
    if _route_names is None:
        with zipfile.ZipFile(GTFS_ZIP) as zf, zf.open("routes.txt") as f:
            _route_names = {r["route_id"]: r["route_short_name"] for r in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))}
    return _route_names


def refresh_vehicles() -> None:
    with _vlock:
        if time.time() - _vcache["checked_at"] < REFRESH_SECONDS:
            return
        _vcache["checked_at"] = time.time()
        if not settings.nta_api_key:
            _vcache["error"] = "no NTA key"
            return
        try:
            res = httpx.get(VEHICLES_URL, headers={"x-api-key": settings.nta_api_key}, timeout=15)
            if res.status_code == 429:
                raise RuntimeError("rate limited by NTA (429)")
            res.raise_for_status()
            feed = rt.FeedMessage()
            feed.ParseFromString(res.content)
        except Exception as exc:
            _vcache["error"] = str(exc)
            log.warning("NTA Vehicles fetch failed: %s", exc)
            return
        names = _all_route_names()
        vehicles = []
        for entity in feed.entity:
            if not entity.HasField("vehicle"):
                continue
            v = entity.vehicle
            vehicles.append({
                "id": v.vehicle.id or entity.id,
                "route": names.get(v.trip.route_id, "?"),
                "lat": round(v.position.latitude, 5),
                "lon": round(v.position.longitude, 5),
                "bearing": round(v.position.bearing) if v.position.HasField("bearing") else None,
                "reported_at": v.timestamp or feed.header.timestamp,
            })
        _vcache.update(vehicles=vehicles, feed_ts=feed.header.timestamp, error=None)
        log.info("NTA Vehicles refreshed: %d vehicles", len(vehicles))


@router.get("/vehicles")
def vehicles():
    """Live bus positions in Greater Dublin. Stale reports are hidden, and counted."""
    refresh_vehicles()
    now = int(time.time())
    min_lat, max_lat, min_lon, max_lon = DUBLIN_BBOX
    shown, stale = [], 0
    for v in _vcache["vehicles"]:
        if not (min_lat <= v["lat"] <= max_lat and min_lon <= v["lon"] <= max_lon):
            continue
        age = now - v["reported_at"]
        if age > VEHICLE_MAX_AGE:
            stale += 1
            continue
        shown.append({**v, "age_seconds": max(0, age)})

    feed_ts = _vcache["feed_ts"]
    age = now - feed_ts if feed_ts else None
    if feed_ts is None:
        realtime = {"status": "unavailable", "message": "Live bus positions are unavailable right now."}
    elif age > STALE_SECONDS:
        realtime = {"status": "stale", "message": f"Bus positions are {age // 60} min old."}
    else:
        realtime = {"status": "live", "message": None}
    realtime["feed_age_seconds"] = age
    return {"vehicles": shown, "hidden_stale": stale, "realtime": realtime}
