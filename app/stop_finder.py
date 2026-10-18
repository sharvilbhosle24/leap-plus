"""Find the stations and stops a question mentions, tolerating typos ("connolyy" -> Connolly).

Plain string matching (stdlib difflib), done in our code before the AI is called, so the
AI is handed real stop names and real times instead of guessing them.
"""

import difflib
import re
import sqlite3

from app.stops import DUBLIN_BBOX

MATCH_CUTOFF = 0.82  # how similar a phrase must be to a station name (0-1)
# Words that are never stop names, so "next train from" can't fuzzy-match a station.
STOPWORDS = {
    "next", "when", "what", "whats", "where", "train", "trains", "dart", "darts", "luas", "tram", "trams",
    "bus", "buses", "from", "stop", "stops", "station", "leave", "leaves", "leaving", "time", "times",
    "today", "please", "going", "there", "here", "does", "will", "the", "this", "that", "tell", "much",
    "balance", "money", "credit", "weekly", "card", "home", "work", "college", "my", "is", "to", "too", "at",
}


def _norm(text: str) -> str:
    text = text.lower().replace("&", " and ").replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text).split())


def _aliases(name: str) -> set[str]:
    full = _norm(name)
    aliases = {full, _norm(re.sub(r"\(.*?\)", "", name))}  # "Bray (Daly)" -> "bray"
    if full.startswith("dublin "):                        # "Dublin Pearse" -> "pearse"
        aliases.add(full[len("dublin "):])
    aliases |= {a.replace(" street", " st") for a in aliases}  # "Tara Street" -> "tara st"
    return {a for a in aliases if len(a) >= 4}


def find_stops(db: sqlite3.Connection, message: str, limit: int = 3) -> list[dict]:
    """Stations (DART/rail/Luas) named in the message, plus bus stops given by number, in the order mentioned."""
    stations = db.execute(
        """SELECT MIN(stop_id) AS stop_id, name, kind FROM stops
           WHERE kind IN ('rail', 'tram') AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
           GROUP BY name, kind""",
        DUBLIN_BBOX,
    ).fetchall()
    by_alias: dict[str, list[dict]] = {}
    for st in stations:
        for alias in _aliases(st["name"]):
            by_alias.setdefault(alias, []).append(dict(st))

    tokens = _norm(message).split()
    used: set[int] = set()
    found: dict[tuple[str, str], dict] = {}
    for size in (3, 2, 1):  # longest phrases first, so "tara street" beats "street"
        for i in range(len(tokens) - size + 1):
            span = range(i, i + size)
            phrase = " ".join(tokens[i:i + size])
            if any(j in used for j in span) or len(phrase) < 4 or phrase in STOPWORDS:
                continue
            match = difflib.get_close_matches(phrase, by_alias.keys(), n=1, cutoff=MATCH_CUTOFF)
            if match:
                used.update(span)
                for st in by_alias[match[0]]:
                    found.setdefault((st["name"], st["kind"]), {**st, "pos": i, "heard_as": phrase})

    for m in re.finditer(r"\b(\d{3,6})\b", message):  # bus stop numbers, e.g. "stop 768"
        bus = db.execute(
            """SELECT stop_id, name, kind, code FROM stops s
               WHERE code = ? AND kind = 'bus' AND EXISTS (SELECT 1 FROM stop_times st WHERE st.stop_id = s.stop_id)""",
            (m.group(1),),
        ).fetchone()
        if bus:
            found.setdefault((bus["name"], f"bus {bus['code']}"), {**dict(bus), "pos": 10_000 + m.start(), "heard_as": m.group(1)})

    return sorted(found.values(), key=lambda s: s["pos"])[: limit * 2]
