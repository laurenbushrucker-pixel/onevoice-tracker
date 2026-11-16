"""OneVoice Tracker: follows Oklahoma Legislature bills against the OneVoice
state agenda, using data from www.oklegislature.gov.

Run:  python app.py              (serves http://127.0.0.1:5000)
      python app.py index 2700   (build the search index for a session)
      python app.py refresh      (re-check every tracked bill)
      python app.py filings      (look for newly filed bills and send alerts)
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory

import oklegis as ok
import onevoice as ov

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("BILLTRACKER_DB", ROOT / "data" / "tracker.db"))
DEFAULT_SESSION = os.environ.get("BILLTRACKER_SESSION", "2700")
# Sessions whose new filings trigger email alerts (comma separated).
ALERT_SESSIONS = [s.strip().upper() for s in os.environ.get("BILLTRACKER_ALERT_SESSIONS", DEFAULT_SESSION).split(",") if s.strip()]
REFRESH_HOURS = float(os.environ.get("BILLTRACKER_REFRESH_HOURS", "6"))  # 0 = off
INDEX_HOURS = float(os.environ.get("BILLTRACKER_INDEX_HOURS", "24"))  # 0 = off
FILINGS_HOURS = float(os.environ.get("BILLTRACKER_FILINGS_HOURS", "1"))  # 0 = off
APP_URL = os.environ.get("BILLTRACKER_URL", "")  # public address, used for links in emails
# Default alert recipients until someone saves a list in the app (comma separated).
DEFAULT_RECIPIENTS = [a.strip() for a in os.environ.get("BILLTRACKER_ALERT_TO", "").split(",") if a.strip()]
DETAIL_MAX_AGE = timedelta(hours=6)

POSITIONS = ["support", "oppose", "monitor", "neutral"]
PRIORITIES = ["high", "medium", "low"]

# Bill types probed for new filings, with the number each type starts at in
# the first year of a legislature (House measures start at 1001).
FILING_TYPES = {"SB": 1, "HB": 1001, "SJR": 1, "HJR": 1001, "SCR": 1, "HCR": 1001, "SR": 1, "HR": 1001}
FILING_MISSES = 8  # stop probing a type after this many unused numbers in a row

app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")

# ------------------------------------------------------------------ database

SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
  session TEXT NOT NULL, bill TEXT NOT NULL,
  bill_type TEXT, bill_num INTEGER,
  title TEXT, last_action TEXT, last_action_date TEXT, stage TEXT,
  subjects TEXT DEFAULT '[]', indexed_at TEXT,
  first_seen TEXT, matched_at TEXT,
  PRIMARY KEY (session, bill));
CREATE TABLE IF NOT EXISTS details (
  session TEXT NOT NULL, bill TEXT NOT NULL, data TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (session, bill));
CREATE TABLE IF NOT EXISTS tracked (
  session TEXT NOT NULL, bill TEXT NOT NULL,
  position TEXT DEFAULT 'monitor', priority TEXT DEFAULT 'medium',
  notes TEXT DEFAULT '', labels TEXT DEFAULT '',
  added_at TEXT NOT NULL, last_checked TEXT,
  PRIMARY KEY (session, bill));
CREATE TABLE IF NOT EXISTS updates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session TEXT NOT NULL, bill TEXT NOT NULL,
  action TEXT, date TEXT, chamber TEXT,
  detected_at TEXT NOT NULL, seen INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS priorities (
  id TEXT PRIMARY KEY, task_force TEXT, number INTEGER, title TEXT, summary TEXT,
  keywords TEXT DEFAULT '[]', exclude TEXT DEFAULT '[]',
  enabled INTEGER DEFAULT 1, top INTEGER DEFAULT 0, sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS matches (
  session TEXT NOT NULL, bill TEXT NOT NULL, priority_id TEXT NOT NULL,
  keywords TEXT DEFAULT '[]', matched_at TEXT NOT NULL,
  alert TEXT DEFAULT 'baseline',   -- baseline | pending | sent | skipped
  alerted_at TEXT,
  PRIMARY KEY (session, bill, priority_id));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(bills)")}
        for col in ("first_seen", "matched_at"):
            if col not in cols:  # upgrade a database made by an earlier version
                c.execute(f"ALTER TABLE bills ADD COLUMN {col} TEXT")
        if not c.execute("SELECT 1 FROM priorities LIMIT 1").fetchone():
            seed_priorities(c)


def seed_priorities(c: sqlite3.Connection) -> None:
    c.execute("DELETE FROM priorities")
    for i, p in enumerate(ov.load_seed()["priorities"]):
        c.execute(
            "INSERT INTO priorities(id,task_force,number,title,summary,keywords,exclude,enabled,top,sort) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (p["id"], p["task_force"], p["number"], p["title"], p.get("summary", ""),
             json.dumps(p.get("keywords", [])), json.dumps(p.get("exclude", [])), 1, int(bool(p.get("top"))), i),
        )


def meta_get(key: str, default=None):
    with db() as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def meta_set(key: str, value) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, json.dumps(value)))


def settings() -> dict:
    s = meta_get("settings") or {}
    return {
        "recipients": s.get("recipients", DEFAULT_RECIPIENTS),
        "alerts_enabled": s.get("alerts_enabled", True),
        "app_url": s.get("app_url", APP_URL),
    }


def get_priorities() -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM priorities ORDER BY sort").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["keywords"] = json.loads(d["keywords"])
        d["exclude"] = json.loads(d["exclude"])
        d["enabled"] = bool(d["enabled"])
        d["top"] = bool(d["top"])
        out.append(d)
    return out


# ---------------------------------------------------------- agenda matching


def match_session(session: str, all_bills: bool = False) -> int:
    """Match bills against the agenda.

    By default only bills not matched before (i.e. newly seen) are checked.
    New matches on an alert session become 'pending' alerts once that
    session's baseline exists; everything else is recorded as 'baseline' so
    existing bills, or bills that only match after a keyword edit, never set
    off a flood of email.
    """
    priorities = get_priorities()
    baseline_done = bool(meta_get(f"baseline:{session}"))
    alertable = session in ALERT_SESSIONS and baseline_done and not all_bills
    ts = now_iso()
    created = 0
    with db() as c:
        q = "SELECT bill, title, subjects FROM bills WHERE session=?" + ("" if all_bills else " AND matched_at IS NULL")
        rows = c.execute(q, (session,)).fetchall()
        for r in rows:
            found = dict(ov.match_bill(r["title"] or "", json.loads(r["subjects"] or "[]"), priorities))
            existing = {m["priority_id"] for m in c.execute(
                "SELECT priority_id FROM matches WHERE session=? AND bill=?", (session, r["bill"]))}
            for pid in existing - set(found):
                c.execute("DELETE FROM matches WHERE session=? AND bill=? AND priority_id=? AND alert!='sent'",
                          (session, r["bill"], pid))
            for pid, kws in found.items():
                if pid in existing:
                    c.execute("UPDATE matches SET keywords=? WHERE session=? AND bill=? AND priority_id=?",
                              (json.dumps(kws), session, r["bill"], pid))
                else:
                    c.execute(
                        "INSERT INTO matches(session,bill,priority_id,keywords,matched_at,alert) VALUES(?,?,?,?,?,?)",
                        (session, r["bill"], pid, json.dumps(kws), ts, "pending" if alertable else "baseline"),
                    )
                    created += 1
            c.execute("UPDATE bills SET matched_at=? WHERE session=? AND bill=?", (ts, session, r["bill"]))
    if not baseline_done:
        meta_set(f"baseline:{session}", ts)
    return created


def rematch_all_sessions() -> None:
    with db() as c:
        sessions = [r["session"] for r in c.execute("SELECT DISTINCT session FROM bills")]
    for s in sessions:
        match_session(s, all_bills=True)


def session_name(code: str) -> str:
    for s in meta_get("sessions") or []:
        if s["code"] == code:
            return s["name"]
    return f"{ok.session_year(code)} session"


def send_alerts() -> dict:
    """Email one digest of every pending agenda match. Returns a summary."""
    st = settings()
    with db() as c:
        rows = c.execute(
            """SELECT m.session, m.bill, m.priority_id, m.keywords, b.title, b.last_action
               FROM matches m JOIN bills b ON b.session=m.session AND b.bill=m.bill
               WHERE m.alert='pending' ORDER BY m.session, b.bill_type, b.bill_num"""
        ).fetchall()
    if not rows:
        return {"sent": 0}
    if not st["alerts_enabled"]:
        with db() as c:
            c.execute("UPDATE matches SET alert='skipped' WHERE alert='pending'")
        return {"sent": 0, "skipped": len(rows)}

    pri = {p["id"]: p for p in get_priorities()}
    by_session: dict[str, dict[str, dict]] = {}
    for r in rows:
        bills = by_session.setdefault(r["session"], {})
        item = bills.setdefault(r["bill"], {
            "display": ok.display_bill(r["bill"]), "title": r["title"] or "",
            "url": ok.bill_url(r["bill"], r["session"]), "last_action": r["last_action"] or "", "priorities": [],
        })
        p = pri.get(r["priority_id"])
        if p:
            item["priorities"].append({"task_force": p["task_force"], "number": p["number"], "title": p["title"],
                                       "keywords": json.loads(r["keywords"])})
    sent = 0
    for session, bills in by_session.items():
        subject, text, html_body = ov.render_alert(list(bills.values()), st["app_url"], session_name(session))
        try:
            ov.send_email(st["recipients"], subject, text, html_body)
        except Exception as e:  # noqa: BLE001 - keep pending and report; retried next check
            meta_set("last_alert_error", {"at": now_iso(), "error": str(e)})
            return {"sent": sent, "error": str(e)}
        with db() as c:
            c.execute("UPDATE matches SET alert='sent', alerted_at=? WHERE alert='pending' AND session=?", (now_iso(), session))
        sent += len(bills)
        meta_set("last_alert", {"at": now_iso(), "bills": len(bills), "session": session})
    meta_set("last_alert_error", None)
    return {"sent": sent}


# ---------------------------------------------------------------- core logic


def upsert_index(session: str, entries: dict[str, ok.IndexEntry]) -> None:
    ts = now_iso()
    with db() as c:
        for e in entries.values():
            t, n = ok.split_bill(e.bill)
            stage = ok.stage_from_last_action(e.bill, e.last_action)
            c.execute(
                """INSERT INTO bills(session,bill,bill_type,bill_num,title,last_action,stage,subjects,indexed_at,first_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session,bill) DO UPDATE SET
                     title=excluded.title, indexed_at=excluded.indexed_at,
                     matched_at=CASE WHEN bills.subjects=excluded.subjects THEN bills.matched_at ELSE NULL END,
                     subjects=excluded.subjects,
                     last_action=CASE WHEN bills.last_action_date IS NULL THEN excluded.last_action ELSE bills.last_action END,
                     stage=CASE WHEN bills.last_action_date IS NULL THEN excluded.stage ELSE bills.stage END""",
                (session, e.bill, t, n, e.title, e.last_action, stage, json.dumps(e.subjects), ts, ts),
            )


def store_detail(d: ok.BillDetail) -> list[ok.Action]:
    """Save a fetched bill and return actions that are new since the last fetch."""
    with db() as c:
        prev = c.execute("SELECT data FROM details WHERE session=? AND bill=?", (d.session, d.bill)).fetchone()
        c.execute(
            "INSERT OR REPLACE INTO details(session,bill,data,fetched_at) VALUES(?,?,?,?)",
            (d.session, d.bill, json.dumps(d.to_dict()), now_iso()),
        )
        last = d.actions[-1] if d.actions else None
        stage = ok.stage_from_actions(d.bill, d.actions)
        t, n = ok.split_bill(d.bill)
        c.execute(
            """INSERT INTO bills(session,bill,bill_type,bill_num,title,last_action,last_action_date,stage,subjects,first_seen)
               VALUES(?,?,?,?,?,?,?,?,'[]',?)
               ON CONFLICT(session,bill) DO UPDATE SET
                 title=COALESCE(NULLIF(excluded.title,''),bills.title),
                 matched_at=CASE WHEN COALESCE(bills.title,'')=COALESCE(NULLIF(excluded.title,''),bills.title,'') THEN bills.matched_at ELSE NULL END,
                 last_action=excluded.last_action, last_action_date=excluded.last_action_date,
                 stage=excluded.stage""",
            (d.session, d.bill, t, n, d.title, last.action if last else "", last.date if last else None, stage, now_iso()),
        )
    if not prev:
        return []
    old = {(a["action"], a["date"], a["chamber"]) for a in json.loads(prev["data"]).get("actions", [])}
    return [a for a in d.actions if (a.action, a.date, a.chamber) not in old]


def get_detail(session: str, bill: str, refresh: bool = False) -> dict | None:
    with db() as c:
        row = c.execute("SELECT data, fetched_at FROM details WHERE session=? AND bill=?", (session, bill)).fetchone()
    if row and not refresh:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
        if age < DETAIL_MAX_AGE:
            return {**json.loads(row["data"]), "fetched_at": row["fetched_at"]}
    try:
        d = ok.fetch_bill(bill, session)
    except RuntimeError:
        if row:  # site unreachable: serve what we have
            return {**json.loads(row["data"]), "fetched_at": row["fetched_at"], "stale": True}
        raise
    if not d.found:
        return None
    record_updates(session, bill, store_detail(d))
    return {**d.to_dict(), "fetched_at": now_iso()}


def record_updates(session: str, bill: str, new_actions: list[ok.Action]) -> None:
    if not new_actions:
        return
    with db() as c:
        if not c.execute("SELECT 1 FROM tracked WHERE session=? AND bill=?", (session, bill)).fetchone():
            return
        for a in new_actions:
            c.execute(
                "INSERT INTO updates(session,bill,action,date,chamber,detected_at) VALUES(?,?,?,?,?,?)",
                (session, bill, a.action, a.date, a.chamber, now_iso()),
            )


def refresh_tracked(session: str | None = None, progress=None) -> int:
    with db() as c:
        q = "SELECT session, bill FROM tracked" + (" WHERE session=?" if session else "")
        rows = c.execute(q, (session,) if session else ()).fetchall()
    for i, r in enumerate(rows, 1):
        if progress:
            progress(i, len(rows), ok.display_bill(r["bill"]))
        try:
            get_detail(r["session"], r["bill"], refresh=True)
        except RuntimeError:
            continue
        with db() as c:
            c.execute("UPDATE tracked SET last_checked=? WHERE session=? AND bill=?", (now_iso(), r["session"], r["bill"]))
    return len(rows)


def build_index(session: str, progress=None) -> int:
    entries = ok.crawl_index(session, progress)
    upsert_index(session, entries)
    meta_set(f"indexed:{session}", {"at": now_iso(), "count": len(entries)})
    match_session(session)
    send_alerts()
    return len(entries)


def probe_filings(session: str, progress=None) -> list[str]:
    """Find newly filed measures by trying the next unused bill numbers."""
    with db() as c:
        top = {r["bill_type"]: r["n"] for r in c.execute(
            "SELECT bill_type, MAX(bill_num) n FROM bills WHERE session=? GROUP BY bill_type", (session,))}
    found: list[str] = []
    types = list(FILING_TYPES.items())
    for ti, (btype, start) in enumerate(types, 1):
        n = max(top.get(btype) or 0, start - 1) + 1
        misses = 0
        while misses < FILING_MISSES:
            if progress:
                progress(ti, len(types), f"{btype} {n}")
            bill = f"{btype}{n}"
            try:
                d = ok.fetch_bill(bill, session)
            except RuntimeError:
                break
            if d.found:
                store_detail(d)
                found.append(bill)
                misses = 0
            else:
                misses += 1
            n += 1
    return found


def check_filings(session: str, progress=None) -> dict:
    new = probe_filings(session, progress)
    created = match_session(session)
    result = {"new_bills": len(new), "new_matches": created, **send_alerts()}
    meta_set(f"filings:{session}", {"at": now_iso(), **result})
    return result


# --------------------------------------------------------- background jobs

_job_lock = threading.Lock()
job: dict = {"running": False}


def start_job(kind: str, session: str | None, fn) -> bool:
    if not _job_lock.acquire(blocking=False):
        return False

    def progress(i, total, label):
        job.update(done=i, total=total, label=label)

    def run():
        try:
            result = fn(progress)
            job.update(running=False, finished=now_iso(), result=result, error=None)
        except Exception as e:  # noqa: BLE001 - report any failure to the UI
            job.update(running=False, finished=now_iso(), error=str(e))
        finally:
            _job_lock.release()

    job.clear()
    job.update(running=True, kind=kind, session=session, done=0, total=0, label="", started=now_iso())
    threading.Thread(target=run, daemon=True).start()
    return True


def _due(key: str, hours: float) -> bool:
    if hours <= 0:
        return False
    last = meta_get(key)
    at = last.get("at") if isinstance(last, dict) else last
    return not at or datetime.now(timezone.utc) - datetime.fromisoformat(at) > timedelta(hours=hours)


def scheduler() -> None:
    """Look for new filings, re-check tracked bills and rebuild the index on a timer."""
    while True:
        time.sleep(60)
        try:
            for s in ALERT_SESSIONS:
                if _due(f"filings:{s}", FILINGS_HOURS):
                    start_job("filings", s, lambda p, s=s: check_filings(s, p))
                    break
            else:
                if _due("last_auto_refresh", REFRESH_HOURS):
                    if start_job("refresh", None, lambda p: refresh_tracked(None, p)):
                        meta_set("last_auto_refresh", now_iso())
                elif _due(f"indexed:{DEFAULT_SESSION}", INDEX_HOURS):
                    start_job("index", DEFAULT_SESSION, lambda p: build_index(DEFAULT_SESSION, p))
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            pass


# ---------------------------------------------------------------- helpers


def agenda_for(session: str, bills: list[str]) -> dict[str, list[dict]]:
    if not bills:
        return {}
    out: dict[str, list[dict]] = {}
    with db() as c:
        for i in range(0, len(bills), 500):
            chunk = bills[i:i + 500]
            q = f"SELECT bill, priority_id, keywords, alert, matched_at FROM matches WHERE session=? AND bill IN ({','.join('?' * len(chunk))})"
            for r in c.execute(q, [session] + chunk):
                out.setdefault(r["bill"], []).append(
                    {"id": r["priority_id"], "keywords": json.loads(r["keywords"]), "alert": r["alert"], "matched_at": r["matched_at"]})
    return out


def bill_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["subjects"] = json.loads(d.get("subjects") or "[]")
    d["display"] = ok.display_bill(d["bill"])
    d["stage_label"] = ok.STAGE_LABEL.get(d.get("stage") or "unknown", "Unknown")
    d["url"] = ok.bill_url(d["bill"], d["session"])
    return d


def with_agenda(session: str, rows: list[dict]) -> list[dict]:
    tags = agenda_for(session, [r["bill"] for r in rows])
    for r in rows:
        r["agenda"] = tags.get(r["bill"], [])
    return rows


def want_session() -> str:
    s = (request.args.get("session") or DEFAULT_SESSION).strip().upper()
    if not s.isalnum() or len(s) > 6:
        abort(400, "Invalid session")
    return s


def want_bill(text: str) -> str:
    b = ok.normalize_bill(text)
    if not b:
        abort(400, f"'{text}' is not a bill number. Use a form like HB 1234 or SJR 5.")
    return b


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(502)
def json_error(e):
    return jsonify(error=getattr(e, "description", str(e))), e.code


BUSY = "Another update is already running. Try again when it finishes."

# ------------------------------------------------------------------ routes


@app.get("/")
def index_page():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def api_config():
    sessions = meta_get("sessions")
    if not sessions:
        try:
            sessions = [s.__dict__ for s in ok.fetch_sessions()]
            meta_set("sessions", sessions)
        except RuntimeError:
            sessions = [{"code": DEFAULT_SESSION, "name": f"{ok.session_year(DEFAULT_SESSION)} Regular Session"}]
    return jsonify(
        default_session=DEFAULT_SESSION,
        alert_sessions=ALERT_SESSIONS,
        sessions=sessions,
        positions=POSITIONS,
        priorities=PRIORITIES,
        stages=[{"key": k, "label": v} for k, v in ok.STAGES] + [{"key": "vetoed", "label": "Vetoed"}, {"key": "failed", "label": "Failed"}],
        agenda=get_priorities(),
        agenda_name=ov.load_seed().get("name", "OneVoice agenda"),
        refresh_hours=REFRESH_HOURS,
        filings_hours=FILINGS_HOURS,
    )


@app.get("/api/status")
def api_status():
    s = want_session()
    with db() as c:
        n = c.execute("SELECT COUNT(*) n FROM bills WHERE session=?", (s,)).fetchone()["n"]
    return jsonify(
        session=s,
        indexed=meta_get(f"indexed:{s}"),
        filings=meta_get(f"filings:{s}"),
        bills=n,
        closed=ok.session_closed(s),
        alerts_watch=s in ALERT_SESSIONS,
        job=dict(job),
        last_auto_refresh=meta_get("last_auto_refresh"),
    )


@app.post("/api/index")
def api_index():
    s = want_session()
    if not start_job("index", s, lambda p: build_index(s, p)):
        return jsonify(error=BUSY), 409
    return jsonify(ok=True)


@app.post("/api/refresh")
def api_refresh():
    s = request.args.get("session")
    if not start_job("refresh", s, lambda p: refresh_tracked(s, p)):
        return jsonify(error=BUSY), 409
    return jsonify(ok=True)


@app.post("/api/filings")
def api_filings():
    s = want_session()
    if not start_job("filings", s, lambda p: check_filings(s, p)):
        return jsonify(error=BUSY), 409
    return jsonify(ok=True)


@app.get("/api/subjects")
def api_subjects():
    s = want_session()
    counts: dict[str, int] = {}
    with db() as c:
        for r in c.execute("SELECT subjects FROM bills WHERE session=?", (s,)):
            for subj in json.loads(r["subjects"] or "[]"):
                counts[subj] = counts.get(subj, 0) + 1
    return jsonify(sorted(({"name": k, "count": v} for k, v in counts.items()), key=lambda x: x["name"]))


@app.get("/api/search")
def api_search():
    s = want_session()
    q = (request.args.get("q") or "").strip()
    subject = request.args.get("subject") or ""
    chamber = request.args.get("chamber") or ""
    stage = request.args.get("stage") or ""
    agenda = request.args.get("agenda") or ""
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = max(int(request.args.get("offset", 0)), 0)

    where, args = ["b.session=?"], [s]
    bill = ok.normalize_bill(q) if q else None
    if bill:
        where.append("b.bill=?")
        args.append(bill)
    elif q:
        for word in q.split():
            where.append("(b.title LIKE ? OR b.bill LIKE ?)")
            args += [f"%{word}%", f"%{word}%"]
    if subject:
        where.append("b.subjects LIKE ?")
        args.append(f'%"{subject}"%')
    if chamber in ("H", "S"):
        where.append("substr(b.bill_type,1,1)=?")
        args.append(chamber)
    if stage:
        where.append("b.stage=?")
        args.append(stage)
    if agenda == "any":
        where.append("EXISTS (SELECT 1 FROM matches m WHERE m.session=b.session AND m.bill=b.bill)")
    elif agenda:
        where.append("EXISTS (SELECT 1 FROM matches m WHERE m.session=b.session AND m.bill=b.bill AND m.priority_id=?)")
        args.append(agenda)
    sql_where = " AND ".join(where)
    order = "b.first_seen DESC, b.bill_type, b.bill_num" if request.args.get("sort") == "newest" else "b.bill_type, b.bill_num"
    with db() as c:
        total = c.execute(f"SELECT COUNT(*) n FROM bills b WHERE {sql_where}", args).fetchone()["n"]
        rows = c.execute(
            f"""SELECT b.*, t.position AS tracked_position FROM bills b
                LEFT JOIN tracked t ON t.session=b.session AND t.bill=b.bill
                WHERE {sql_where} ORDER BY {order} LIMIT ? OFFSET ?""",
            args + [limit, offset],
        ).fetchall()

    # A bill number that isn't in the index yet: look it up on the site.
    if bill and not rows:
        try:
            d = get_detail(s, bill, refresh=True)
        except RuntimeError as e:
            abort(502, f"oklegislature.gov did not respond: {e}")
        if d:
            match_session(s)
            with db() as c:
                rows = c.execute(
                    """SELECT b.*, t.position AS tracked_position FROM bills b
                       LEFT JOIN tracked t ON t.session=b.session AND t.bill=b.bill
                       WHERE b.session=? AND b.bill=?""",
                    (s, bill),
                ).fetchall()
            total = len(rows)
    return jsonify(total=total, results=with_agenda(s, [bill_row(r) for r in rows]))


TRACKED_SQL = """SELECT t.*, b.title, b.last_action, b.last_action_date, b.stage, b.subjects,
                        b.bill_type, b.bill_num,
                        (SELECT COUNT(*) FROM updates u WHERE u.session=t.session AND u.bill=t.bill AND u.seen=0) AS unseen
                 FROM tracked t LEFT JOIN bills b ON b.session=t.session AND b.bill=t.bill"""


@app.get("/api/tracked")
def api_tracked():
    s = want_session()
    with db() as c:
        rows = c.execute(TRACKED_SQL + " WHERE t.session=? ORDER BY b.bill_type, b.bill_num", (s,)).fetchall()
    return jsonify(with_agenda(s, [bill_row(r) for r in rows]))


@app.post("/api/tracked")
def api_track():
    body = request.get_json(force=True) or {}
    s = (body.get("session") or DEFAULT_SESSION).upper()
    bill = want_bill(body.get("bill") or "")
    try:
        d = get_detail(s, bill, refresh=False)
    except RuntimeError as e:
        abort(502, f"oklegislature.gov did not respond: {e}")
    if not d:
        abort(404, f"{ok.display_bill(bill)} was not found in the {ok.session_year(s)} session.")
    match_session(s)
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO tracked(session,bill,position,priority,added_at,last_checked) VALUES(?,?,?,?,?,?)",
            (s, bill, body.get("position") or "monitor", body.get("priority") or "medium", now_iso(), now_iso()),
        )
        row = c.execute(TRACKED_SQL + " WHERE t.session=? AND t.bill=?", (s, bill)).fetchone()
    return jsonify(with_agenda(s, [bill_row(row)])[0])


@app.patch("/api/tracked/<session>/<bill>")
def api_update_tracked(session, bill):
    body = request.get_json(force=True) or {}
    fields, args = [], []
    if "position" in body and body["position"] in POSITIONS:
        fields.append("position=?"); args.append(body["position"])
    if "priority" in body and body["priority"] in PRIORITIES:
        fields.append("priority=?"); args.append(body["priority"])
    if "notes" in body:
        fields.append("notes=?"); args.append(str(body["notes"])[:20000])
    if "labels" in body:
        fields.append("labels=?"); args.append(str(body["labels"])[:500])
    if not fields:
        abort(400, "Nothing to update.")
    with db() as c:
        cur = c.execute(f"UPDATE tracked SET {', '.join(fields)} WHERE session=? AND bill=?", args + [session, bill])
        if not cur.rowcount:
            abort(404, "That bill is not on your tracking list.")
        row = c.execute(TRACKED_SQL + " WHERE t.session=? AND t.bill=?", (session, bill)).fetchone()
    return jsonify(with_agenda(session, [bill_row(row)])[0])


@app.delete("/api/tracked/<session>/<bill>")
def api_untrack(session, bill):
    with db() as c:
        c.execute("DELETE FROM tracked WHERE session=? AND bill=?", (session, bill))
        c.execute("DELETE FROM updates WHERE session=? AND bill=?", (session, bill))
    return jsonify(ok=True)


@app.get("/api/bill/<session>/<bill>")
def api_bill(session, bill):
    bill = want_bill(bill)
    session = session.upper()
    try:
        d = get_detail(session, bill, refresh=request.args.get("refresh") == "1")
    except RuntimeError as e:
        abort(502, f"oklegislature.gov did not respond: {e}")
    if not d:
        abort(404, f"{ok.display_bill(bill)} was not found in this session.")
    match_session(session)
    with db() as c:
        t = c.execute("SELECT * FROM tracked WHERE session=? AND bill=?", (session, bill)).fetchone()
        b = c.execute("SELECT subjects FROM bills WHERE session=? AND bill=?", (session, bill)).fetchone()
    d.update(
        display=ok.display_bill(bill),
        stage=ok.stage_from_actions(bill, [ok.Action(**a) for a in d["actions"]]),
        url=ok.bill_url(bill, session),
        lens_url=ok.lens_url(bill),
        tracked=dict(t) if t else None,
        subjects=json.loads(b["subjects"]) if b else [],
        agenda=agenda_for(session, [bill]).get(bill, []),
    )
    d["stage_label"] = ok.STAGE_LABEL.get(d["stage"], "Unknown")
    return jsonify(d)


@app.get("/api/updates")
def api_updates():
    s = want_session()
    with db() as c:
        rows = c.execute(
            """SELECT u.*, b.title FROM updates u LEFT JOIN bills b ON b.session=u.session AND b.bill=u.bill
               WHERE u.session=? ORDER BY u.date DESC, u.id DESC LIMIT 100""",
            (s,),
        ).fetchall()
    return jsonify([{**dict(r), "display": ok.display_bill(r["bill"])} for r in rows])


@app.post("/api/updates/seen")
def api_updates_seen():
    s = want_session()
    body = request.get_json(silent=True) or {}
    with db() as c:
        if body.get("bill"):
            c.execute("UPDATE updates SET seen=1 WHERE session=? AND bill=?", (s, body["bill"]))
        else:
            c.execute("UPDATE updates SET seen=1 WHERE session=?", (s,))
    return jsonify(ok=True)


# ---------------------------------------------------------------- agenda API


@app.get("/api/agenda")
def api_agenda():
    s = want_session()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with db() as c:
        counts = {r["priority_id"]: dict(r) for r in c.execute(
            """SELECT priority_id, COUNT(*) total,
                      SUM(CASE WHEN alert IN ('pending','sent','skipped') AND matched_at>=? THEN 1 ELSE 0 END) recent
               FROM matches WHERE session=? GROUP BY priority_id""", (week_ago, s))}
        tracked = {r["priority_id"]: r["n"] for r in c.execute(
            """SELECT m.priority_id, COUNT(*) n FROM matches m JOIN tracked t ON t.session=m.session AND t.bill=m.bill
               WHERE m.session=? GROUP BY m.priority_id""", (s,))}
        any_match = c.execute("SELECT COUNT(DISTINCT bill) n FROM matches WHERE session=?", (s,)).fetchone()["n"]
    out = []
    for p in get_priorities():
        cnt = counts.get(p["id"], {})
        out.append({**p, "matches": cnt.get("total", 0), "recent": cnt.get("recent", 0) or 0, "tracked": tracked.get(p["id"], 0)})
    return jsonify(priorities=out, bills_matching=any_match)


@app.put("/api/agenda/<pid>")
def api_agenda_update(pid):
    body = request.get_json(force=True) or {}

    def clean(lst):
        return [str(k).strip()[:80] for k in (lst or []) if str(k).strip()][:60]

    with db() as c:
        if not c.execute("SELECT 1 FROM priorities WHERE id=?", (pid,)).fetchone():
            abort(404, "Unknown priority.")
        if "keywords" in body:
            c.execute("UPDATE priorities SET keywords=? WHERE id=?", (json.dumps(clean(body["keywords"])), pid))
        if "exclude" in body:
            c.execute("UPDATE priorities SET exclude=? WHERE id=?", (json.dumps(clean(body["exclude"])), pid))
        if "enabled" in body:
            c.execute("UPDATE priorities SET enabled=? WHERE id=?", (int(bool(body["enabled"])), pid))
    rematch_all_sessions()
    return jsonify(next(p for p in get_priorities() if p["id"] == pid))


@app.post("/api/agenda/reset")
def api_agenda_reset():
    with db() as c:
        seed_priorities(c)
    rematch_all_sessions()
    return jsonify(ok=True)


@app.get("/api/agenda/preview")
def api_agenda_preview():
    """How many bills in a session a draft keyword list would match."""
    s = want_session()
    kws = [k for k in (request.args.get("keywords") or "").split("\n") if k.strip()]
    exc = [k for k in (request.args.get("exclude") or "").split("\n") if k.strip()]
    sample, total = [], 0
    with db() as c:
        for r in c.execute("SELECT bill, title, subjects FROM bills WHERE session=? ORDER BY bill_type, bill_num", (s,)):
            got = ov.match_bill(r["title"] or "", json.loads(r["subjects"] or "[]"),
                                [{"id": "draft", "keywords": kws, "exclude": exc}])
            if got:
                total += 1
                if len(sample) < 8:
                    sample.append({"display": ok.display_bill(r["bill"]), "title": r["title"], "keywords": got[0][1]})
    return jsonify(total=total, sample=sample)


# -------------------------------------------------------------- settings API


@app.get("/api/settings")
def api_settings():
    smtp = ov.smtp_settings()
    return jsonify(
        **settings(),
        email_configured=ov.email_configured(),
        smtp_host=smtp["host"],
        smtp_from=smtp["sender"],
        last_alert=meta_get("last_alert"),
        last_alert_error=meta_get("last_alert_error"),
        alert_sessions=[{"code": s, "name": session_name(s)} for s in ALERT_SESSIONS],
        filings_hours=FILINGS_HOURS,
    )


@app.put("/api/settings")
def api_settings_update():
    body = request.get_json(force=True) or {}
    s = settings()
    if "recipients" in body:
        addrs = [a.strip() for a in body["recipients"] if a and a.strip()]
        bad = [a for a in addrs if "@" not in a or " " in a]
        if bad:
            abort(400, f"These don't look like email addresses: {', '.join(bad)}")
        s["recipients"] = addrs[:50]
    if "alerts_enabled" in body:
        s["alerts_enabled"] = bool(body["alerts_enabled"])
    if "app_url" in body:
        s["app_url"] = str(body["app_url"]).strip()[:300]
    meta_set("settings", s)
    return api_settings()


@app.post("/api/settings/test-email")
def api_test_email():
    st = settings()
    sample = [{
        "display": "HB 0000", "title": "Example only: this is what an alert looks like. No bill was filed.",
        "url": ok.BASE, "last_action": "",
        "priorities": [{"task_force": "Workforce and Talent Strategies", "number": 1,
                        "title": "Strengthen Public Education and Early Literacy", "keywords": ["literacy"]}],
    }]
    subject, text, html_body = ov.render_alert(sample, st["app_url"], session_name(DEFAULT_SESSION))
    try:
        ov.send_email(st["recipients"], "[Test] " + subject, text, html_body)
    except Exception as e:  # noqa: BLE001 - show the reason to the user
        return jsonify(error=f"The test email was not sent: {e}"), 400
    return jsonify(ok=True, to=st["recipients"])


@app.get("/api/export.csv")
def api_export():
    s = want_session()
    pri = {p["id"]: p for p in get_priorities()}
    with db() as c:
        rows = c.execute(TRACKED_SQL + " WHERE t.session=? ORDER BY b.bill_type, b.bill_num", (s,)).fetchall()
    rows = with_agenda(s, [bill_row(r) for r in rows])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Bill", "Title", "OneVoice priorities", "Status", "Last action", "Last action date", "Position",
                "Priority", "Labels", "Notes", "Link"])
    for d in rows:
        tags = "; ".join(f"{pri[a['id']]['task_force']} #{pri[a['id']]['number']}" for a in d["agenda"] if a["id"] in pri)
        w.writerow([d["display"], d["title"], tags, d["stage_label"], d["last_action"], d["last_action_date"] or "",
                    d["position"], d["priority"], d["labels"], d["notes"], d["url"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=onevoice-tracked-bills-{s}.csv"})


# --------------------------------------------------------------------- main


def main(argv: list[str]) -> None:
    init_db()
    cmd = argv[1] if len(argv) > 1 else "serve"
    say = lambda i, t, label: print(f"[{i}/{t}] {label}", flush=True)  # noqa: E731
    if cmd == "index":
        s = argv[2] if len(argv) > 2 else DEFAULT_SESSION
        print(f"Indexed {build_index(s, say)} measures for session {s}.")
        return
    if cmd == "refresh":
        print(f"Checked {refresh_tracked(None, say)} tracked bills.")
        return
    if cmd == "filings":
        for s in ALERT_SESSIONS:
            print(s, check_filings(s, say))
        return
    threading.Thread(target=scheduler, daemon=True).start()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    try:
        from waitress import serve  # production server, if installed
        print(f"OneVoice Tracker on http://{host}:{port}", flush=True)
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main(sys.argv)
