"""Import stops and the timetable from the NTA static GTFS zip into SQLite.

- All stops, tagged bus / rail (DART, commuter) / tram (Luas).
- Timetable rows for every rail and Luas stop, plus every bus stop in south Dublin.
A stop counts as rail/tram if any trip on a route_type 2 (rail) or 0 (tram) route calls there.

Run from the project folder:  python scripts/import_stops.py
"""

import csv
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import connect, init_db  # noqa: E402

ZIP = Path(__file__).resolve().parent.parent / "data" / "gtfs" / "GTFS_Realtime.zip"
KINDS = {"2": "rail", "0": "tram"}  # GTFS route_type -> our label
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# "South Dublin" = south of the Liffey (approximated as latitude 53.345, the river in the city
# centre), from Lucan/Saggart in the west to Dalkey in the east and down to Bray.
SOUTH_DUBLIN = (53.19, 53.345, -6.50, -6.04)  # min_lat, max_lat, min_lon, max_lon
BATCH = 50_000


def rows(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def in_south_dublin(lat: float, lon: float) -> bool:
    min_lat, max_lat, min_lon, max_lon = SOUTH_DUBLIN
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def main() -> None:
    zf = zipfile.ZipFile(ZIP)
    all_routes = {r["route_id"]: r for r in rows(zf, "routes.txt")}
    rail_routes = {rid for rid, r in all_routes.items() if r["route_type"] in KINDS}
    all_trips = {t["trip_id"]: t for t in rows(zf, "trips.txt")}
    stops = list(rows(zf, "stops.txt"))
    south_stops = {s["stop_id"] for s in stops if in_south_dublin(float(s["stop_lat"]), float(s["stop_lon"]))}

    conn = connect()
    conn.execute("PRAGMA synchronous = OFF")  # bulk import: speed over crash-safety; just re-run if it fails
    conn.executescript("DROP TABLE IF EXISTS stop_times; DROP TABLE IF EXISTS trips; DROP TABLE IF EXISTS routes;")
    conn.close()
    init_db()  # recreates the dropped tables with the current schema
    conn = connect()
    conn.execute("PRAGMA synchronous = OFF")

    stop_kind: dict[str, str] = {}
    kept_trips: set[str] = set()
    last_seq: dict[str, int] = {}
    batch: list[tuple] = []
    kept = 0
    with zf.open("stop_times.txt") as f:  # 500+ MB: stream it, keep only the rows we need
        lines = io.TextIOWrapper(f, encoding="utf-8-sig")
        header = next(lines).strip().split(",")
        trip_col, stop_col = header.index("trip_id"), header.index("stop_id")
        seq_col, dep_col = header.index("stop_sequence"), header.index("departure_time")
        with conn:
            for line in lines:
                cols = line.split(",")
                trip_id, stop_id, seq = cols[trip_col], cols[stop_col], int(cols[seq_col])
                if seq > last_seq.get(trip_id, -1):
                    last_seq[trip_id] = seq
                trip = all_trips.get(trip_id)
                if trip is None:
                    continue
                is_rail = trip["route_id"] in rail_routes
                if is_rail:
                    stop_kind[stop_id] = KINDS[all_routes[trip["route_id"]]["route_type"]]
                elif stop_id not in south_stops:
                    continue
                h, m, s = cols[dep_col].split(":")
                batch.append((trip_id, stop_id, seq, int(h) * 3600 + int(m) * 60 + int(s)))
                kept_trips.add(trip_id)
                if len(batch) >= BATCH:
                    conn.executemany("INSERT INTO stop_times VALUES (?, ?, ?, ?)", batch)
                    kept += len(batch)
                    batch.clear()
            conn.executemany("INSERT INTO stop_times VALUES (?, ?, ?, ?)", batch)
            kept += len(batch)

    with conn:
        # Upsert, not delete: favourite_stops references these rows.
        conn.executemany(
            """INSERT INTO stops (stop_id, code, name, lat, lon, kind) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(stop_id) DO UPDATE SET code = excluded.code, name = excluded.name,
                   lat = excluded.lat, lon = excluded.lon, kind = excluded.kind""",
            [(s["stop_id"], s["stop_code"], s["stop_name"], float(s["stop_lat"]), float(s["stop_lon"]),
              stop_kind.get(s["stop_id"], "bus")) for s in stops],
        )
        conn.executemany("INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)", [
            (tid, all_trips[tid]["route_id"], all_trips[tid]["service_id"], all_trips[tid]["trip_headsign"],
             int(all_trips[tid]["direction_id"] or 0), last_seq[tid]) for tid in kept_trips])
        route_ids = {all_trips[tid]["route_id"] for tid in kept_trips}
        conn.executemany("INSERT INTO routes VALUES (?, ?, ?, ?)", [
            (rid, all_routes[rid]["route_short_name"], all_routes[rid]["route_long_name"], int(all_routes[rid]["route_type"]))
            for rid in route_ids])
        conn.execute("DELETE FROM calendar")
        conn.executemany("INSERT INTO calendar VALUES (?, ?, ?, ?)", [
            (c["service_id"], "".join(c[d] for d in DAYS), c["start_date"], c["end_date"]) for c in rows(zf, "calendar.txt")])
        conn.execute("DELETE FROM calendar_dates")
        conn.executemany("INSERT INTO calendar_dates VALUES (?, ?, ?)", [
            (c["service_id"], c["date"], int(c["exception_type"])) for c in rows(zf, "calendar_dates.txt")])

    counts = dict(conn.execute("SELECT kind, COUNT(*) FROM stops GROUP BY kind").fetchall())
    south_bus = conn.execute(
        "SELECT COUNT(*) FROM stops s WHERE kind = 'bus' AND EXISTS (SELECT 1 FROM stop_times st WHERE st.stop_id = s.stop_id)"
    ).fetchone()[0]
    conn.close()
    print(f"Stops: {counts}. South Dublin bus stops with times: {south_bus}. "
          f"Timetable rows: {kept:,}. Trips: {len(kept_trips):,}.")


if __name__ == "__main__":
    main()
