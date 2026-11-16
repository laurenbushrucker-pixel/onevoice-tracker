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

import hmac
import secrets

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory
from flask import session as login_session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix

import oklegis as ok
import onevoice as ov
import summarize as sm

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
# Which bills get AI summaries automatically: "agenda" (agenda matches and tracked bills), "tracked", or "all".
SUMMARY_SCOPE = os.environ.get("BILLTRACKER_SUMMARY_SCOPE", "agenda")
SUMMARY_BATCH = int(os.environ.get("BILLTRACKER_SUMMARY_BATCH", "40"))  # per background run

POSITIONS = ["support", "oppose", "monitor", "neutral"]
PRIORITIES = ["high", "medium", "low"]

# Bill types probed for new filings, with the number each type starts at in
# the first year of a legislature (House measures start at 1001).
FILING_TYPES = {"SB": 1, "HB": 1001, "SJR": 1, "HJR": 1001, "SCR": 1, "HCR": 1001, "SR": 1, "HR": 1001}
FILING_MISSES = 8  # stop probing a type after this many unused numbers in a row

app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # for the development server; waitress handles this itself

# Sign-in. The password comes from the server's environment, never from the code.
PASSWORD = os.environ.get("BILLTRACKER_PASSWORD", "")
SESSION_DAYS = int(os.environ.get("BILLTRACKER_SESSION_DAYS", "30"))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_NAME="ovt_session",
    PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
)

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
CREATE TABLE IF NOT EXISTS summaries (
  session TEXT NOT NULL, bill TEXT NOT NULL,
  summary TEXT, version TEXT, source_url TEXT, model TEXT, truncated INTEGER DEFAULT 0,
  created_at TEXT, error TEXT,
  PRIMARY KEY (session, bill));
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
        for col in ("first_seen", "matched_at", "author"):
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
            """SELECT m.session, m.bill, m.priority_id, m.keywords, b.title, b.last_action, b.author, s.summary
               FROM matches m JOIN bills b ON b.session=m.session AND b.bill=m.bill
               LEFT JOIN summaries s ON s.session=m.session AND s.bill=m.bill
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
            "author": r["author"] or "", "summary": r["summary"] or "",
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
            """INSERT INTO bills(session,bill,bill_type,bill_num,title,last_action,last_action_date,stage,subjects,first_seen,author)
               VALUES(?,?,?,?,?,?,?,?,'[]',?,?)
               ON CONFLICT(session,bill) DO UPDATE SET
                 title=COALESCE(NULLIF(excluded.title,''),bills.title),
                 author=COALESCE(NULLIF(excluded.author,''),bills.author),
                 matched_at=CASE WHEN COALESCE(bills.title,'')=COALESCE(NULLIF(excluded.title,''),bills.title,'') THEN bills.matched_at ELSE NULL END,
                 last_action=excluded.last_action, last_action_date=excluded.last_action_date,
                 stage=excluded.stage""",
            (d.session, d.bill, t, n, d.title, last.action if last else "", last.date if last else None, stage, now_iso(),
             author_text(d.author, d.other_author)),
        )
    if not prev:
        return []
    old = {(a["action"], a["date"], a["chamber"]) for a in json.loads(prev["data"]).get("actions", [])}
    return [a for a in d.actions if (a.action, a.date, a.chamber) not in old]


def author_text(author: str, other: str) -> str:
    """'Woods (S) and Newton (H)' style: primary author, then principal author in the other chamber."""
    return " and ".join(a for a in (author, other) if a)


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


# ------------------------------------------------------------- AI summaries


def get_summary(session: str, bill: str) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM summaries WHERE session=? AND bill=?", (session, bill)).fetchone()
    return dict(r) if r else None


def summarize_bill(session: str, bill: str, force: bool = False) -> dict:
    """Summarize the latest version of a bill's text. Skips work when the
    stored summary already covers that version, unless force is set."""
    detail = get_detail(session, bill)
    if not detail:
        raise sm.SummaryUnavailable(f"{ok.display_bill(bill)} was not found.")
    version, url = sm.pick_version(detail)
    have = get_summary(session, bill)
    if have and have.get("summary") and have.get("source_url") == url and not force:
        return have
    try:
        text = sm.bill_text(url) if url else ""
    except Exception:  # noqa: BLE001 - fall back to the title when the PDF can't be read
        text = ""
    try:
        summary, truncated = sm.summarize(ok.display_bill(bill), detail.get("title", ""), version, text)
    except sm.SummaryUnavailable as e:
        with db() as c:
            c.execute("""INSERT INTO summaries(session,bill,error,created_at) VALUES(?,?,?,?)
                         ON CONFLICT(session,bill) DO UPDATE SET error=excluded.error""", (session, bill, str(e), now_iso()))
        raise
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO summaries(session,bill,summary,version,source_url,model,truncated,created_at,error) VALUES(?,?,?,?,?,?,?,?,NULL)",
            (session, bill, summary, version, url, sm.MODEL, int(truncated), now_iso()),
        )
    return get_summary(session, bill)


def summary_candidates(session: str, limit: int) -> list[str]:
    scope = {
        "tracked": "b.bill IN (SELECT bill FROM tracked WHERE session=b.session)",
        "all": "1=1",
    }.get(SUMMARY_SCOPE, "(b.bill IN (SELECT bill FROM tracked WHERE session=b.session) OR EXISTS (SELECT 1 FROM matches m WHERE m.session=b.session AND m.bill=b.bill))")
    with db() as c:
        rows = c.execute(
            f"""SELECT b.bill FROM bills b LEFT JOIN summaries s ON s.session=b.session AND s.bill=b.bill
                WHERE b.session=? AND {scope} AND (s.summary IS NULL) AND (s.error IS NULL OR s.created_at < ?)
                ORDER BY (b.bill IN (SELECT bill FROM tracked WHERE session=b.session)) DESC, b.first_seen DESC, b.bill_type, b.bill_num
                LIMIT ?""",
            (session, (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), limit),
        ).fetchall()
    return [r["bill"] for r in rows]


def summarize_pending(session: str, progress=None, limit: int = SUMMARY_BATCH, bills: list[str] | None = None) -> dict:
    if not sm.available():
        return {"summarized": 0, "skipped": "no API key"}
    todo = bills if bills is not None else summary_candidates(session, limit)
    done, failed = 0, 0
    for i, bill in enumerate(todo, 1):
        if progress:
            progress(i, len(todo), ok.display_bill(bill))
        try:
            summarize_bill(session, bill)
            done += 1
        except sm.SummaryUnavailable as e:
            failed += 1
            if "API key" in str(e) or "rate limit" in str(e):
                meta_set("summary_error", {"at": now_iso(), "error": str(e)})
                break
        except RuntimeError:
            failed += 1
    if done:
        meta_set("summary_error", None)
    meta_set(f"summaries:{session}", {"at": now_iso(), "summarized": done, "failed": failed})
    return {"summarized": done, "failed": failed}


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
    with db() as c:
        alert_bills = [r["bill"] for r in c.execute(
            "SELECT DISTINCT bill FROM matches WHERE session=? AND alert='pending'", (session,))]
    summarize_pending(session, progress, bills=alert_bills)  # so the alert email can include summaries
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
                elif sm.available() and _due(f"summaries:{DEFAULT_SESSION}", 0.25) and summary_candidates(DEFAULT_SESSION, 1):
                    start_job("summaries", DEFAULT_SESSION, lambda p: summarize_pending(DEFAULT_SESSION, p))
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


# ------------------------------------------------------------------- sign-in

_failures: dict[str, list[float]] = {}
FAIL_LIMIT, FAIL_WINDOW = 8, 15 * 60  # 8 wrong passwords in 15 minutes locks that address out for 15 minutes


def _client_ip() -> str:
    return request.remote_addr or "unknown"


def _locked_out(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < FAIL_WINDOW]
    _failures[ip] = recent
    return len(recent) >= FAIL_LIMIT


def _secret_key() -> str:
    key = os.environ.get("BILLTRACKER_SECRET_KEY") or meta_get("secret_key")
    if not key:
        key = secrets.token_hex(32)
        meta_set("secret_key", key)  # kept in the database so sign-ins survive restarts
    return key


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in · OneVoice Tracker</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700&family=Public+Sans:wght@400;600&display=swap">
<style>
:root{--bg:#f4f5f2;--card:#fff;--fg:#1e231d;--muted:#5d6659;--line:#d7dcd2;--brand:#4f8a1e;--bright:#76b82a;--err:#b3261e;--errbg:#f8dedb;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#121512;--card:#1a1f1a;--fg:#e7ece4;--muted:#9ea896;--line:#2f362d;--brand:#6aab2e;--err:#ff9a91;--errbg:#46201d;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:15px/1.5 "Public Sans",system-ui,sans-serif;padding:24px 16px}
form{background:var(--card);border:1px solid var(--line);border-top:5px solid var(--bright);border-radius:8px;padding:28px;width:100%;max-width:380px;display:flex;flex-direction:column;gap:14px}
h1{font:700 1.4rem Archivo,sans-serif;margin:0}p{margin:0;color:var(--muted);font-size:.9rem}
label{display:flex;flex-direction:column;gap:6px;font-weight:600;font-size:.88rem}
input{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px 12px}
input:focus-visible,button:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
button{font:inherit;font-weight:600;background:var(--brand);color:#fff;border:0;border-radius:6px;padding:10px;cursor:pointer}
.err{background:var(--errbg);color:var(--err);padding:8px 10px;border-radius:6px;font-size:.88rem}
</style></head><body>
<form method="post" action="/login">
<h1>OneVoice Tracker</h1><p>Enter the team password to continue.</p>
%ERROR%
<label for="password">Password<input id="password" name="password" type="password" autocomplete="current-password" required autofocus></label>
<input type="hidden" name="next" value="%NEXT%">
<button type="submit">Sign in</button>
</form></body></html>"""


def _login_page(error: str = "", next_url: str = "/", status: int = 200):
    import html as _html
    body = LOGIN_PAGE.replace("%ERROR%", f'<div class="err" role="alert">{_html.escape(error)}</div>' if error else "")
    body = body.replace("%NEXT%", _html.escape(next_url if next_url.startswith("/") and not next_url.startswith("//") else "/"))
    return Response(body, status=status, mimetype="text/html", headers={"Cache-Control": "no-store"})


class _SessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):  # Secure flag whenever the site is reached over HTTPS
        return request.is_secure or super().get_cookie_secure(app)


app.session_interface = _SessionInterface()


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp


@app.before_request
def require_sign_in():
    if not app.secret_key:
        app.secret_key = _secret_key()
    if request.path in ("/login", "/healthz"):
        return None
    if not PASSWORD:
        required = os.environ.get("BILLTRACKER_REQUIRE_PASSWORD", "false") == "true"
        if not required and os.environ.get("HOST", "127.0.0.1") in ("127.0.0.1", "localhost"):
            return None  # running only on this computer: no password needed
        msg = "Set BILLTRACKER_PASSWORD on the server to turn on sign-in. The tracker stays closed until then."
        if request.path.startswith("/api/"):
            return jsonify(error=msg), 503
        return Response(msg, status=503, mimetype="text/plain")
    if login_session.get("ok") and hmac.compare_digest(login_session.get("pw", ""), _pw_fingerprint()):
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="Your sign-in has expired. Reload the page to sign in again."), 401
    return redirect("/login?next=" + request.full_path.rstrip("?"))


def _pw_fingerprint() -> str:
    # Changing the password signs everyone out.
    return hmac.new(app.secret_key.encode(), PASSWORD.encode(), "sha256").hexdigest()[:16]


@app.get("/login")
def login_form():
    return _login_page(next_url=request.args.get("next", "/"))


@app.post("/login")
def login_submit():
    ip = _client_ip()
    nxt = request.form.get("next", "/")
    if _locked_out(ip):
        return _login_page("Too many wrong passwords. Wait 15 minutes and try again.", nxt, 429)
    if PASSWORD and hmac.compare_digest(request.form.get("password", "").encode(), PASSWORD.encode()):
        _failures.pop(ip, None)
        login_session.clear()
        login_session.permanent = True
        login_session["ok"] = True
        login_session["pw"] = _pw_fingerprint()
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
    _failures.setdefault(ip, []).append(time.time())
    return _login_page("That password isn't right. Check it and try again.", nxt, 401)


@app.get("/logout")
def logout():
    login_session.clear()
    return redirect("/login")


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


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
        ai_available=sm.available(),
        ai_model=sm.MODEL,
        summary_scope=SUMMARY_SCOPE,
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
        summaries=meta_get(f"summaries:{s}"),
        summary_error=meta_get("summary_error"),
        summaries_waiting=len(summary_candidates(s, 1000)) if sm.available() else None,
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
            f"""SELECT b.*, t.position AS tracked_position, s.summary, s.version AS summary_version FROM bills b
                LEFT JOIN tracked t ON t.session=b.session AND t.bill=b.bill
                LEFT JOIN summaries s ON s.session=b.session AND s.bill=b.bill
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
                    """SELECT b.*, t.position AS tracked_position, s.summary, s.version AS summary_version FROM bills b
                       LEFT JOIN tracked t ON t.session=b.session AND t.bill=b.bill
                       LEFT JOIN summaries s ON s.session=b.session AND s.bill=b.bill
                       WHERE b.session=? AND b.bill=?""",
                    (s, bill),
                ).fetchall()
            total = len(rows)
    return jsonify(total=total, results=with_agenda(s, [bill_row(r) for r in rows]))


TRACKED_SQL = """SELECT t.*, b.title, b.last_action, b.last_action_date, b.stage, b.subjects,
                        b.bill_type, b.bill_num, b.author,
                        (SELECT summary FROM summaries s WHERE s.session=t.session AND s.bill=t.bill) AS summary,
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
        author_text=author_text(d.get("author", ""), d.get("other_author", "")),
        ai=get_summary(session, bill),
        ai_available=sm.available(),
    )
    d["stage_label"] = ok.STAGE_LABEL.get(d["stage"], "Unknown")
    return jsonify(d)


@app.post("/api/bill/<session>/<bill>/summary")
def api_summarize(session, bill):
    bill = want_bill(bill)
    try:
        s = summarize_bill(session.upper(), bill, force=request.args.get("force") == "1")
    except sm.SummaryUnavailable as e:
        return jsonify(error=str(e)), 503
    except RuntimeError as e:
        abort(502, f"oklegislature.gov did not respond: {e}")
    return jsonify(s)


@app.post("/api/summaries")
def api_summaries():
    s = want_session()
    if not sm.available():
        return jsonify(error="AI summaries are off: set ANTHROPIC_API_KEY on the server."), 503
    if not start_job("summaries", s, lambda p: summarize_pending(s, p)):
        return jsonify(error=BUSY), 409
    return jsonify(ok=True)


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
    w.writerow(["Bill", "Author", "AI summary", "Title", "OneVoice priorities", "Status", "Last action", "Last action date", "Position",
                "Priority", "Labels", "Notes", "Link"])
    for d in rows:
        tags = "; ".join(f"{pri[a['id']]['task_force']} #{pri[a['id']]['number']}" for a in d["agenda"] if a["id"] in pri)
        w.writerow([d["display"], d.get("author") or "", d.get("summary") or "", d["title"], tags, d["stage_label"], d["last_action"], d["last_action_date"] or "",
                    d["position"], d["priority"], d["labels"], d["notes"], d["url"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=onevoice-tracked-bills-{s}.csv"})


# --------------------------------------------------------------------- main


def main(argv: list[str]) -> None:
    init_db()
    app.secret_key = _secret_key()
    cmd = argv[1] if len(argv) > 1 else "serve"
    say = lambda i, t, label: print(f"[{i}/{t}] {label}", flush=True)  # noqa: E731
    if cmd == "index":
        s = argv[2] if len(argv) > 2 else DEFAULT_SESSION
        print(f"Indexed {build_index(s, say)} measures for session {s}.")
        return
    if cmd == "refresh":
        print(f"Checked {refresh_tracked(None, say)} tracked bills.")
        return
    if cmd == "summaries":
        s = argv[2] if len(argv) > 2 else DEFAULT_SESSION
        n = int(argv[3]) if len(argv) > 3 else SUMMARY_BATCH
        print(summarize_pending(s, say, limit=n))
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
        if os.environ.get("BILLTRACKER_TRUST_PROXY", "true") == "true":
            # Behind a host like Render: take the visitor's address and HTTPS from the proxy's headers.
            serve(app, host=host, port=port, threads=8, trusted_proxy="*", trusted_proxy_count=1,
                  trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"},
                  clear_untrusted_proxy_headers=True)
        else:
            serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main(sys.argv)
