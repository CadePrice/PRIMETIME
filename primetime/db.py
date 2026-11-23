import os
import sqlite3
import time
from contextlib import closing

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# events.db lives next to this file, no matter where the app is started from
DB_PATH = os.path.join(BASE_DIR, "events.db")

# Snapshot paths in the database are relative to static/, e.g. "snaps/1732400000.jpg",
# so the template can use url_for('static', filename=snapshot).
STATIC_DIR = os.path.join(BASE_DIR, "static")
SNAPS_DIR = os.path.join(STATIC_DIR, "snaps")

MAX_EVENTS = 20   # keep only the newest 20 events

# How each event type is shown on the notifications page.
# Only "type" is stored in the database; everything here is looked up from it.
EVENT_INFO = {
    "motion": {
        "icon": "lightbulb",
        "title": "Night Motion Detected",
        "desc": "Activating floodlight",
    },
    "doorbell": {
        "icon": "button",
        "title": "Doorbell Pressed",
        "desc": "Someone's at your door",
    },
    "human": {
        "icon": "human",
        "title": "Person Detected",
        "desc": "Someone's near your house",
    },
    "cupcake": {
        "icon": "cupcake",
        "title": "EDP ALERT",
        "desc": "Cupcake detected",
    },
}


def _connect():
    """Open a connection that is closed when the `with closing(...)` block ends.

    (sqlite3's own `with conn:` only commits; it doesn't close the connection.)
    """
    return closing(sqlite3.connect(DB_PATH, timeout=5))


def init_db():
    """Create the events table if it doesn't exist yet. Safe to call on every start."""
    with _connect() as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                type     TEXT    NOT NULL,           -- key into EVENT_INFO
                ts       INTEGER NOT NULL,           -- Unix time (seconds)
                snapshot TEXT,                       -- e.g. "snaps/1732400000.jpg"; NULL if no photo
                read     INTEGER NOT NULL DEFAULT 0  -- 0 = unread, 1 = read
            )
        """)


def add_event(event_type, ts=None, snapshot=None):
    """Save one event, then prune so only the newest MAX_EVENTS remain.

    event_type: a key of EVENT_INFO ("motion", "doorbell", "human", "cupcake")
    ts:         Unix time in seconds (defaults to now)
    snapshot:   path relative to static/, e.g. "snaps/1732400000.jpg", or None
    Returns the new event's id.
    """
    if event_type not in EVENT_INFO:
        raise ValueError(f"unknown event type: {event_type!r}")
    if ts is None:
        ts = int(time.time())

    with _connect() as conn, conn:   # one transaction: insert + prune together
        cur = conn.execute(
            "INSERT INTO events (type, ts, snapshot) VALUES (?, ?, ?)",
            (event_type, int(ts), snapshot),
        )
        new_id = cur.lastrowid

        # Everything except the newest MAX_EVENTS rows.
        # LIMIT -1 means "no limit", so this is "skip the newest 20, take the rest".
        old_rows = conn.execute(
            "SELECT id, snapshot FROM events ORDER BY id DESC LIMIT -1 OFFSET ?",
            (MAX_EVENTS,),
        ).fetchall()

        conn.executemany("DELETE FROM events WHERE id = ?", [(row_id,) for row_id, _ in old_rows])

    # Delete the pruned photos only after the rows are gone (commit succeeded).
    for _, snap in old_rows:
        _delete_snapshot(snap)

    return new_id


def get_latest(limit=MAX_EVENTS):
    """Newest events first, each as a dict ready for the template.

    Each dict has the stored columns (id, type, ts, snapshot, read)
    plus icon, title and desc from EVENT_INFO.
    """
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, type, ts, snapshot, read FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    events = []
    for row in rows:
        event = dict(row)
        event["read"] = bool(event["read"])
        # Unknown types (e.g. renamed later) still render instead of crashing
        event.update(EVENT_INFO.get(event["type"], {
            "icon": "unknown", "title": event["type"], "desc": "",
        }))
        events.append(event)
    return events


def unread_count():
    """Number of unread events, for the bell badge."""
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM events WHERE read = 0").fetchone()[0]


def mark_all_read():
    """Mark every event read (called when the notifications page opens)."""
    with _connect() as conn, conn:
        conn.execute("UPDATE events SET read = 1 WHERE read = 0")


def _delete_snapshot(snapshot):
    """Remove a snapshot JPEG, but only if it really is inside static/snaps/."""
    if not snapshot:
        return
    path = os.path.realpath(os.path.join(STATIC_DIR, snapshot))
    if os.path.dirname(path) != os.path.realpath(SNAPS_DIR):
        print("[db] not deleting file outside snaps/:", snapshot)
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass                      # already gone: fine
    except OSError as e:
        print("[db] could not delete snapshot:", snapshot, e)


if __name__ == "__main__":
    init_db()
    with _connect() as conn:
        print(conn.execute("SELECT sql FROM sqlite_master WHERE name = 'events'").fetchone()[0])
    print("OK:", DB_PATH)
