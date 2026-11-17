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
from zoneinfo import ZoneInfo

import base64
import hmac
import secrets
import zlib

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
REFRESH_HOURS = float(os.environ.get("BILLTRACKER_REFRESH_HOURS", "1"))  # how often tracked bills are re-checked; 0 = off
CENTRAL = ZoneInfo("America/Chicago")
CHECKIN_HOUR = int(os.environ.get("BILLTRACKER_CHECKIN_HOUR", "7"))  # daily check-in email, Central time
BACKUPS_KEPT = 14
STARTED_AT = datetime.now(timezone.utc)
INDEX_HOURS = float(os.environ.get("BILLTRACKER_INDEX_HOURS", "6"))  # full index rebuild, the safety net; 0 = off
# How often to look for newly filed bills. Speed is the point, so the default is every 10 minutes.
FILINGS_MINUTES = float(os.environ.get("BILLTRACKER_FILINGS_MINUTES",
                                       str(float(os.environ.get("BILLTRACKER_FILINGS_HOURS", "0")) * 60 or 10)))
FILINGS_HOURS = FILINGS_MINUTES / 60  # 0 = off
NEW_BILL_DAYS = 7  # a bill seen for the first time within this many days can still trigger an alert
TEXT_BATCH = int(os.environ.get("BILLTRACKER_TEXT_BATCH", "60"))  # bill texts downloaded per background run
APP_URL = os.environ.get("BILLTRACKER_URL", "")  # public address, used for links in emails
# Default alert recipients until someone saves a list in the app (comma separated).
DEFAULT_RECIPIENTS = [a.strip() for a in os.environ.get("BILLTRACKER_ALERT_TO", "").split(",") if a.strip()]
DETAIL_MAX_AGE = timedelta(hours=6)
# Which bills get AI summaries automatically: "agenda" (agenda matches and tracked bills), "tracked", or "all".
SUMMARY_SCOPE = os.environ.get("BILLTRACKER_SUMMARY_SCOPE", "agenda")
SUMMARY_BATCH = int(os.environ.get("BILLTRACKER_SUMMARY_BATCH", "10"))  # per background run

POSITIONS = ["support", "oppose", "monitor", "neutral"]
PRIORITIES = ["high", "medium", "low"]

# Bill types probed for new filings, with the number each type starts at in
# the first year of a legislature (House measures start at 1001).
FILING_TYPES = {"SB": 1, "HB": 1001, "SJR": 1, "HJR": 1001, "SCR": 1, "HCR": 1001, "SR": 1, "HR": 1001}
FILING_MISSES = 4        # stop probing a type after this many unused numbers in a row...
FILING_MISSES_START = 8  # ...or this many, for a type with no bills yet this session

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
  created_at TEXT, checked_at TEXT, error TEXT,
  PRIMARY KEY (session, bill));
CREATE TABLE IF NOT EXISTS texts (
  session TEXT NOT NULL, bill TEXT NOT NULL,
  version TEXT, source_url TEXT, body BLOB, chars INTEGER, fetched_at TEXT, error TEXT,
  PRIMARY KEY (session, bill));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT, ok INTEGER, detail TEXT, data TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS reviews (
  session TEXT NOT NULL, bill TEXT NOT NULL, priority_id TEXT NOT NULL,
  verdict TEXT, keywords TEXT DEFAULT '[]', at TEXT,
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
        for col in ("first_seen", "matched_at", "author"):
            if col not in cols:  # upgrade a database made by an earlier version
                c.execute(f"ALTER TABLE bills ADD COLUMN {col} TEXT")
        ucols = {r["name"] for r in c.execute("PRAGMA table_info(updates)")}
        if "emailed" not in ucols:
            c.execute("ALTER TABLE updates ADD COLUMN emailed INTEGER DEFAULT 0")
            c.execute("UPDATE updates SET emailed=2")  # don't email actions found before this feature existed
        if "category" not in ucols:
            c.execute("ALTER TABLE updates ADD COLUMN category TEXT")
        if "checked_at" not in {r["name"] for r in c.execute("PRAGMA table_info(summaries)")}:
            c.execute("ALTER TABLE summaries ADD COLUMN checked_at TEXT")
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
        "checkin_enabled": s.get("checkin_enabled", True),
        "checkin_backup": s.get("checkin_backup", True),
        "movement_enabled": s.get("movement_enabled", True),
        "movement_categories": s.get("movement_categories", ok.DEFAULT_MOVEMENT_CATEGORIES),
    }


def log_event(kind: str, ok_: bool, detail: str = "", **data) -> None:
    """Record what the tracker did, for the daily check-in and problem alerts."""
    with db() as c:
        c.execute("INSERT INTO events(at,kind,ok,detail,data) VALUES(?,?,?,?,?)",
                  (now_iso(), kind, int(bool(ok_)), (detail or "")[:500], json.dumps(data)))


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


def match_session(session: str, all_bills: bool = False, fetch_text: bool = False) -> int:
    """Match bills against the agenda, reading each bill's title, subjects and full text.

    By default only bills not matched before (newly seen, or with new text)
    are checked. A new match becomes a 'pending' alert only on an alert
    session, after that session's starting point was recorded, and for a bill
    first seen within the last NEW_BILL_DAYS. Everything else is recorded as
    'baseline', so bills that already existed, or that only match after a
    keyword edit, never set off a flood of email.

    fetch_text downloads missing bill text first for recently seen bills, so a
    new filing is judged on its full text before anyone is alerted.
    """
    priorities = get_priorities()
    baseline = meta_get(f"baseline:{session}")
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=NEW_BILL_DAYS)).isoformat()
    ts = now_iso()
    created = 0
    with db() as c:
        q = "SELECT bill, title, subjects, first_seen FROM bills WHERE session=?" + ("" if all_bills else " AND matched_at IS NULL")
        rows = c.execute(q, (session,)).fetchall()
    for r in rows:
        recent = bool(baseline and r["first_seen"] and r["first_seen"] >= max(baseline, recent_cutoff))
        if fetch_text and recent:
            try:
                get_text(session, r["bill"])
            except RuntimeError:
                pass
        text = get_text(session, r["bill"], fetch=False)[2]
        found = dict(ov.match_bill(r["title"] or "", json.loads(r["subjects"] or "[]"), priorities, text))
        alert = "pending" if (session in ALERT_SESSIONS and recent and not all_bills) else "baseline"
        with db() as c:
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
                        (session, r["bill"], pid, json.dumps(kws), ts, alert),
                    )
                    created += 1
            c.execute("UPDATE bills SET matched_at=? WHERE session=? AND bill=?", (ts, session, r["bill"]))
    if not baseline:
        meta_set(f"baseline:{session}", ts)
    return created


# ---------------------------------------------------------------- bill text


def store_text(session: str, bill: str, version: str, url: str, text: str, error: str | None = None) -> None:
    with db() as c:
        prev = c.execute("SELECT source_url FROM texts WHERE session=? AND bill=?", (session, bill)).fetchone()
        c.execute(
            "INSERT OR REPLACE INTO texts(session,bill,version,source_url,body,chars,fetched_at,error) VALUES(?,?,?,?,?,?,?,?)",
            (session, bill, version, url, zlib.compress(text.encode("utf-8")), len(text), now_iso(), error),
        )
        if not prev or prev["source_url"] != url:
            c.execute("UPDATE bills SET matched_at=NULL WHERE session=? AND bill=?", (session, bill))  # re-match on new text


def get_text(session: str, bill: str, fetch: bool = True, detail: dict | None = None) -> tuple[str, str, str]:
    """(version, url, text) of the newest version of the bill. Downloads it
    when missing or when a newer version has been posted (if fetch is set)."""
    with db() as c:
        row = c.execute("SELECT version, source_url, body, error, fetched_at FROM texts WHERE session=? AND bill=?",
                        (session, bill)).fetchone()
    have = (row["version"] or "", row["source_url"] or "", zlib.decompress(row["body"]).decode("utf-8")) if row and row["body"] else None
    if not fetch:
        return have or ("", "", "")
    if detail is None:
        detail = get_detail(session, bill)
    if not detail:
        return have or ("", "", "")
    version, url = sm.pick_version(detail)
    if have and have[1] == url and (have[2] or not row["error"]):
        return have
    if not url:
        return have or ("", "", "")
    try:
        text = sm.bill_text(url)
        store_text(session, bill, version, url, text)
    except Exception as e:  # noqa: BLE001 - keep the title-only match; retry on a later run
        store_text(session, bill, version, url, "", error=str(e)[:300])
        text = ""
    return version, url, text


def fetch_texts(session: str, progress=None, limit: int = TEXT_BATCH) -> dict:
    """Background job: download text for bills that don't have it yet, newest first."""
    with db() as c:
        rows = c.execute(
            """SELECT b.bill FROM bills b LEFT JOIN texts t ON t.session=b.session AND t.bill=b.bill
               WHERE b.session=? AND t.bill IS NULL ORDER BY b.first_seen DESC, b.bill_type, b.bill_num LIMIT ?""",
            (session, limit)).fetchall()
    done = 0
    for i, r in enumerate(rows, 1):
        if progress:
            progress(i, len(rows), ok.display_bill(r["bill"]))
        try:
            get_text(session, r["bill"])
            done += 1
        except RuntimeError:
            continue
    match_session(session)
    meta_set(f"texts:{session}", {"at": now_iso(), "fetched": done})
    return {"fetched": done}


def texts_missing(session: str) -> int:
    with db() as c:
        return c.execute("""SELECT COUNT(*) n FROM bills b LEFT JOIN texts t ON t.session=b.session AND t.bill=b.bill
                            WHERE b.session=? AND t.bill IS NULL""", (session,)).fetchone()["n"]


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


def send_alerts(only: tuple[str, str] | None = None) -> dict:
    """Email each newly matched bill (one email per bill). `only` limits it to one (session, bill)."""
    st = settings()
    extra, args = ("AND m.session=? AND m.bill=?", list(only)) if only else ("", [])
    with db() as c:
        rows = c.execute(
            f"""SELECT m.session, m.bill, m.priority_id, m.keywords, b.title, b.last_action, b.author, s.summary, s.model AS summary_model
               FROM matches m JOIN bills b ON b.session=m.session AND b.bill=m.bill
               LEFT JOIN summaries s ON s.session=m.session AND s.bill=m.bill
               WHERE m.alert='pending' {extra} ORDER BY m.session, b.bill_type, b.bill_num""", args
        ).fetchall()
    if not rows:
        return {"sent": 0}
    if not st["alerts_enabled"]:
        with db() as c:
            for r in rows:
                c.execute("UPDATE matches SET alert='skipped' WHERE alert='pending' AND session=? AND bill=?", (r["session"], r["bill"]))
        return {"sent": 0, "skipped": len(rows)}

    pri = {p["id"]: p for p in get_priorities()}
    by_session: dict[str, dict[str, dict]] = {}
    for r in rows:
        bills = by_session.setdefault(r["session"], {})
        item = bills.setdefault(r["bill"], {
            "display": ok.display_bill(r["bill"]), "title": r["title"] or "",
            "url": ok.bill_url(r["bill"], r["session"]), "last_action": r["last_action"] or "", "priorities": [],
            "author": r["author"] or "", "summary": r["summary"] or "", "summary_model": r["summary_model"] or "",
            "progress": bill_progress(r["session"], r["bill"]),
        })
        p = pri.get(r["priority_id"])
        if p:
            item["priorities"].append({"id": p["id"], "task_force": p["task_force"], "number": p["number"], "title": p["title"],
                                       "keywords": json.loads(r["keywords"]),
                                       "review_links": review_links(st["app_url"], r["session"], r["bill"], p["id"])})
    sent = 0
    for session, bills in by_session.items():
        for bill, item in bills.items():  # one email per bill, titled with its policy area
            subject, text, html_body = ov.render_alert(item, st["app_url"], session_name(session))
            try:
                ov.send_email(st["recipients"], subject, text, html_body)
            except Exception as e:  # noqa: BLE001 - keep pending and report; retried next check
                meta_set("last_alert_error", {"at": now_iso(), "error": str(e)})
                log_event("alert_email", False, str(e), bill=bill)
                return {"sent": sent, "error": str(e)}
            log_event("alert_email", True, bill=bill)
            with db() as c:
                c.execute("UPDATE matches SET alert='sent', alerted_at=? WHERE alert='pending' AND session=? AND bill=?",
                          (now_iso(), session, bill))
            sent += 1
            meta_set("last_alert", {"at": now_iso(), "bills": sent, "session": session})
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
                "INSERT INTO updates(session,bill,action,date,chamber,detected_at,category) VALUES(?,?,?,?,?,?,?)",
                (session, bill, a.action, a.date, a.chamber, now_iso(), ok.classify_action(a.action, a.chamber, bill)),
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
    send_movement_emails()
    return len(rows)


CATEGORY_LABEL = dict(ok.ACTION_CATEGORIES)


def bill_progress(session: str, bill: str) -> dict | None:
    """Progress along the path to law, from the saved bill history."""
    with db() as c:
        row = c.execute("SELECT data FROM details WHERE session=? AND bill=?", (session, bill)).fetchone()
    if not row:
        return None
    return ok.progress(bill, json.loads(row["data"]).get("actions", []), closed=ok.session_closed(session))


def send_movement_emails() -> dict:
    """Email each tracked bill that has new actions of the kinds chosen in settings."""
    st = settings()
    with db() as c:
        rows = c.execute(
            """SELECT u.id, u.session, u.bill, u.action, u.date, u.chamber, u.category, u.detected_at,
                      t.position, b.title, b.author, b.stage, s.summary
               FROM updates u JOIN tracked t ON t.session=u.session AND t.bill=u.bill
               LEFT JOIN bills b ON b.session=u.session AND b.bill=u.bill
               LEFT JOIN summaries s ON s.session=u.session AND s.bill=u.bill
               WHERE u.emailed=0 ORDER BY u.session, u.bill, u.date, u.id""").fetchall()
    if not rows:
        return {"sent": 0}
    chosen = set(st["movement_categories"])
    skip, by_bill = [], {}
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    for r in rows:
        cat = r["category"] or ok.classify_action(r["action"], r["chamber"], r["bill"])
        if not st["movement_enabled"] or cat not in chosen or (not ov.email_configured() and r["detected_at"] < stale):
            skip.append(r["id"])
            continue
        by_bill.setdefault((r["session"], r["bill"]), []).append((r, cat))
    with db() as c:
        c.executemany("UPDATE updates SET emailed=2 WHERE id=?", [(i,) for i in skip])
    if not by_bill or not ov.email_configured() or not st["recipients"]:
        return {"sent": 0}
    sent = 0
    for (session, bill), items in by_bill.items():
        r0 = items[0][0]
        item = {
            "display": ok.display_bill(bill), "author": r0["author"] or "", "title": r0["title"] or "",
            "summary": r0["summary"] or "", "url": ok.bill_url(bill, session), "position": r0["position"] or "",
            "stage_label": ok.STAGE_LABEL.get(r0["stage"] or "unknown", ""),
            "actions": [{"date": r["date"], "chamber": r["chamber"], "action": r["action"], "category_label": CATEGORY_LABEL.get(cat, "")}
                        for r, cat in items],
            "progress": bill_progress(session, bill),
        }
        subject, text, html_body = ov.render_movement(item, st["app_url"])
        try:
            ov.send_email(st["recipients"], subject, text, html_body)
        except Exception as e:  # noqa: BLE001 - keep them queued; retried next check
            log_event("movement_email", False, str(e))
            meta_set("last_alert_error", {"at": now_iso(), "error": str(e)})
            break
        with db() as c:
            c.executemany("UPDATE updates SET emailed=1 WHERE id=?", [(r["id"],) for r, _ in items])
        log_event("movement_email", True, bill=bill)
        sent += 1
    return {"sent": sent}


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
    """Write or refresh a bill's summary.

    Free sources first (no API key needed), from oklegislature.gov:
      1. the House or Senate staff's official bill summary, when posted;
      2. until then, the bill's own official description ("An Act relating to ...").
    With an Anthropic API key, Claude writes the summary from the bill text instead.
    Skips work when the stored summary already covers the current source.
    """
    detail = get_detail(session, bill)
    if not detail:
        raise sm.SummaryUnavailable(f"{ok.display_bill(bill)} was not found.")
    have = get_summary(session, bill)
    with db() as c:
        c.execute("UPDATE summaries SET checked_at=? WHERE session=? AND bill=?", (now_iso(), session, bill))

    if sm.available():
        version, url = sm.pick_version(detail)
        if have and have.get("summary") and have.get("source_url") == url and (have.get("model") or "").startswith("claude") and not force:
            return have
        version, url, text = get_text(session, bill, detail=detail)
        summary = None
        try:
            summary, truncated = sm.summarize(ok.display_bill(bill), detail.get("title", ""), version, text)
        except sm.SummaryUnavailable as e:
            meta_set("summary_error", {"at": now_iso(), "error": str(e)})  # fall back to the free summary
        if summary:
            _save_summary(session, bill, summary, version, url, sm.MODEL, truncated)
            return get_summary(session, bill)

    # Free path.
    label, url = sm.pick_official_summary(detail)
    if url:
        if have and have.get("source_url") == url and have.get("summary") and not force:
            return have
        try:
            text = sm.official_summary_text(url)
        except Exception:  # noqa: BLE001 - fall back to the bill's description
            text = ""
        if len(text) > 40:
            _save_summary(session, bill, text, label, url, "official", False)
            return get_summary(session, bill)
    version, text_url, text = get_text(session, bill, detail=detail)
    if have and have.get("model") == "caption" and have.get("source_url") == text_url and have.get("summary") and not force:
        return have
    _save_summary(session, bill, sm.caption_summary(text, detail.get("title", "")),
                  f"the bill's official description ({version or 'latest version'})", text_url, "caption", False)
    return get_summary(session, bill)


def _save_summary(session, bill, summary, version, url, model, truncated) -> None:
    with db() as c:
        c.execute(
            """INSERT OR REPLACE INTO summaries(session,bill,summary,version,source_url,model,truncated,created_at,checked_at,error)
               VALUES(?,?,?,?,?,?,?,?,?,NULL)""",
            (session, bill, summary, version, url, model, int(truncated), now_iso(), now_iso()),
        )


def summary_candidates(session: str, limit: int) -> list[str]:
    """Bills that need a summary, then bills showing the bill's description
    (re-checked every 12 hours for their first 45 days, until staff post a summary)."""
    scope = {
        "tracked": "b.bill IN (SELECT bill FROM tracked WHERE session=b.session)",
        "all": "1=1",
    }.get(SUMMARY_SCOPE, "(b.bill IN (SELECT bill FROM tracked WHERE session=b.session) OR EXISTS (SELECT 1 FROM matches m WHERE m.session=b.session AND m.bill=b.bill))")
    now = datetime.now(timezone.utc)
    with db() as c:
        rows = c.execute(
            f"""SELECT b.bill FROM bills b LEFT JOIN summaries s ON s.session=b.session AND s.bill=b.bill
                WHERE b.session=? AND {scope} AND (
                    (s.summary IS NULL AND (s.error IS NULL OR s.created_at < ?))
                 OR (s.model='caption' AND COALESCE(s.checked_at, s.created_at) < ? AND b.first_seen > ?))
                ORDER BY (s.summary IS NULL) DESC, (b.bill IN (SELECT bill FROM tracked WHERE session=b.session)) DESC,
                         b.first_seen DESC, b.bill_type, b.bill_num
                LIMIT ?""",
            (session, (now - timedelta(days=1)).isoformat(), (now - timedelta(hours=12)).isoformat(),
             (now - timedelta(days=45)).isoformat(), limit),
        ).fetchall()
    return [r["bill"] for r in rows]


def summarize_pending(session: str, progress=None, limit: int = SUMMARY_BATCH, bills: list[str] | None = None) -> dict:
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


def probe_filings(session: str, progress=None, errors: list | None = None) -> list[str]:
    """Find newly filed measures by trying the next unused bill numbers."""
    with db() as c:
        top = {r["bill_type"]: r["n"] for r in c.execute(
            "SELECT bill_type, MAX(bill_num) n FROM bills WHERE session=? GROUP BY bill_type", (session,))}
    found: list[str] = []
    types = list(FILING_TYPES.items())
    for ti, (btype, start) in enumerate(types, 1):
        n = max(top.get(btype) or 0, start - 1) + 1
        limit = FILING_MISSES if top.get(btype) else FILING_MISSES_START
        misses = 0
        while misses < limit:
            if progress:
                progress(ti, len(types), f"{btype} {n}")
            bill = f"{btype}{n}"
            try:
                d = ok.fetch_bill(bill, session)
            except RuntimeError as e:
                if errors is not None:
                    errors.append(str(e))
                break
            if d.found:
                store_detail(d)
                get_text(session, bill, detail=d.to_dict())  # read the full text right away
                found.append(bill)
                misses = 0
            else:
                misses += 1
            n += 1
    return found


def check_filings(session: str, progress=None) -> dict:
    """Find new filings, read their full text, match them, and email each match
    as soon as it's ready (summary first, when AI summaries are on)."""
    errors: list[str] = []
    try:
        new = probe_filings(session, progress, errors=errors)
    except TypeError:  # a stand-in probe without the errors argument (tests)
        new = probe_filings(session, progress)
    created = match_session(session, fetch_text=True)
    with db() as c:
        pending = [r["bill"] for r in c.execute(
            """SELECT DISTINCT m.bill FROM matches m JOIN bills b ON b.session=m.session AND b.bill=m.bill
               WHERE m.session=? AND m.alert='pending' ORDER BY b.first_seen, b.bill_type, b.bill_num""", (session,))]
    sent, error = 0, None
    for i, bill in enumerate(pending, 1):
        if progress:
            progress(i, len(pending), f"alert for {ok.display_bill(bill)}")
        summarize_pending(session, bills=[bill])  # quick; skipped when no API key
        r = send_alerts(only=(session, bill))
        sent += r.get("sent", 0)
        if r.get("error"):
            error = r["error"]
            break
    result = {"new_bills": len(new), "new_matches": created, "sent": sent}
    if error:
        result["error"] = error
    site_ok = len(errors) < len(FILING_TYPES)  # reached the site for at least some bill types
    result["site_errors"] = len(errors)
    meta_set(f"filings:{session}", {"at": now_iso(), **result})
    if site_ok:
        meta_set("last_filings_ok", now_iso())
    log_event("filings", site_ok, errors[0] if errors else "", new_bills=len(new), new_matches=created, sent=sent)
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
            log_event(kind, False, str(e))
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
            housekeeping()
        except Exception as e:  # noqa: BLE001 - keep the scheduler alive
            print("housekeeping:", e, flush=True)
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
                elif _due(f"summaries:{DEFAULT_SESSION}", 0.25) and summary_candidates(DEFAULT_SESSION, 1):
                    start_job("summaries", DEFAULT_SESSION, lambda p: summarize_pending(DEFAULT_SESSION, p))
                elif _due(f"texts:{DEFAULT_SESSION}", 0.1) and texts_missing(DEFAULT_SESSION):
                    start_job("texts", DEFAULT_SESSION, lambda p: fetch_texts(DEFAULT_SESSION, p))
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            pass


# ------------------------------------------------- health, check-in, backups


PROBLEMS = {
    "checks": ("Checks for new bills have stopped", "Checks for new bills are working again",
               "This usually means oklegislature.gov is down or very slow. The tracker keeps retrying on its own. "
               "If it lasts more than a few hours, open SSH in Google Cloud and run: sudo systemctl restart onevoice-tracker"),
    "site": ("The Legislature's website isn't reading correctly", "The Legislature's website is reading correctly again",
             "oklegislature.gov may be down, or its page layout may have changed. If this lasts more than a day, "
             "the part of the tracker that reads the website (oklegis.py) may need an update."),
    "email": ("Alert emails aren't sending", "Alert emails are sending again",
              "Check the mail settings on the server (run the setup script again and re-enter the mail server, username "
              "and app password), then use Send a test email under Alerts & settings."),
}


def run_canary() -> None:
    """Read one known bill to prove the tracker can still read oklegislature.gov."""
    with db() as c:
        r = c.execute("""SELECT bill FROM bills WHERE session=? AND last_action_date IS NOT NULL
                         ORDER BY bill_type DESC, bill_num LIMIT 1""", (DEFAULT_SESSION,)).fetchone()
    bill = r["bill"] if r else "SB1"
    try:
        d = ok.fetch_bill(bill, DEFAULT_SESSION)
        good = d.found and bool(d.title) and bool(d.actions)
        detail = "" if good else f"Could not read the title and history of {ok.display_bill(bill)}."
    except RuntimeError as e:
        good, detail = False, f"Could not reach oklegislature.gov: {e}"
    prev = meta_get("canary") or {}
    fails = 0 if good else prev.get("fails", 0) + 1
    meta_set("canary", {"at": now_iso(), "ok": good, "detail": detail, "fails": fails, "bill": bill})
    log_event("canary", good, detail, bill=bill)


def active_problems() -> dict[str, str]:
    """{kind: message} for problems happening right now."""
    out: dict[str, str] = {}
    now = datetime.now(timezone.utc)
    if FILINGS_MINUTES > 0:
        window = timedelta(minutes=max(45, 3 * FILINGS_MINUTES))
        last = meta_get("last_filings_ok")
        last_dt = datetime.fromisoformat(last) if last else STARTED_AT
        if now - max(last_dt, STARTED_AT) > window:
            when = last_dt.astimezone(CENTRAL).strftime("%-I:%M %p on %b %-d") if last else "the tracker started"
            out["checks"] = f"The tracker hasn't been able to check for new bills since {when} (Central)."
    canary = meta_get("canary") or {}
    if canary.get("fails", 0) >= 2:
        out["site"] = canary.get("detail") or "Bill pages on oklegislature.gov aren't reading correctly."
    err = meta_get("last_alert_error")
    if err and now - datetime.fromisoformat(err["at"]) < timedelta(hours=3):
        out["email"] = f"The last alert email failed to send: {err['error']}"
    return out


def watch_problems() -> None:
    """Email when a problem starts (reminder every 12 hours) and when it clears."""
    state = meta_get("problems") or {}
    current = active_problems()
    st = settings()
    now = datetime.now(timezone.utc)
    changed = False
    for kind, message in current.items():
        p = state.get(kind) or {"since": now_iso()}
        last = p.get("emailed_at")
        if not last or now - datetime.fromisoformat(last) > timedelta(hours=12):
            title, _, fix = PROBLEMS[kind]
            if _try_send(st, *ov.render_problem(title, message, fix, False, st["app_url"])):
                p["emailed_at"] = now_iso()
        p["message"] = message
        state[kind] = p
        changed = True
    for kind in [k for k in state if k not in current]:
        p = state.pop(kind)
        changed = True
        if p.get("emailed_at"):
            _, fixed_title, _ = PROBLEMS[kind]
            _try_send(st, *ov.render_problem(fixed_title, "No action is needed.", "", True, st["app_url"]))
    if changed:
        meta_set("problems", state)


def _try_send(st: dict, subject: str, text: str, html_body: str, attachments=None) -> bool:
    if not ov.email_configured() or not st["recipients"]:
        return False
    try:
        ov.send_email(st["recipients"], subject, text, html_body, attachments)
        return True
    except Exception as e:  # noqa: BLE001
        log_event("system_email", False, str(e))
        return False


def export_backup() -> dict:
    """Everything people entered: tracked bills, notes, positions, relevance marks,
    keyword edits and settings (never passwords or keys)."""
    with db() as c:
        tracked = [dict(r) for r in c.execute("SELECT * FROM tracked ORDER BY session, bill")]
        reviews = [dict(r) for r in c.execute("SELECT * FROM reviews ORDER BY session, bill, priority_id")]
    st = settings()
    return {"app": "OneVoice Tracker", "format": 1, "exported_at": now_iso(),
            "tracked": tracked, "reviews": reviews, "priorities": get_priorities(),
            "settings": {k: st[k] for k in ("recipients", "alerts_enabled", "app_url", "checkin_enabled",
                                             "checkin_backup", "movement_enabled", "movement_categories")}}


def restore_backup(data: dict) -> dict:
    if data.get("app") != "OneVoice Tracker":
        raise ValueError("This file isn't a OneVoice Tracker backup.")
    with db() as c:
        for t in data.get("tracked", []):
            c.execute("""INSERT OR REPLACE INTO tracked(session,bill,position,priority,notes,labels,added_at,last_checked)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (t["session"], t["bill"], t.get("position"), t.get("priority"), t.get("notes", ""),
                       t.get("labels", ""), t.get("added_at") or now_iso(), t.get("last_checked")))
        for r in data.get("reviews", []):
            c.execute("INSERT OR REPLACE INTO reviews(session,bill,priority_id,verdict,keywords,at) VALUES(?,?,?,?,?,?)",
                      (r["session"], r["bill"], r["priority_id"], r.get("verdict"), r.get("keywords", "[]"), r.get("at")))
        for p in data.get("priorities", []):
            c.execute("UPDATE priorities SET keywords=?, exclude=?, enabled=? WHERE id=?",
                      (json.dumps(p.get("keywords", [])), json.dumps(p.get("exclude", [])), int(bool(p.get("enabled", True))), p["id"]))
    if data.get("settings"):
        meta_set("settings", {**(meta_get("settings") or {}), **data["settings"]})
    rematch_all_sessions()
    return {"tracked": len(data.get("tracked", [])), "reviews": len(data.get("reviews", [])),
            "priorities": len(data.get("priorities", []))}


def make_local_backup() -> str:
    """A full copy of the database on the server, one per day, newest 14 kept."""
    folder = DB_PATH.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"tracker-{datetime.now(CENTRAL).date().isoformat()}.db"
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    for old in sorted(folder.glob("tracker-*.db"))[:-BACKUPS_KEPT]:
        old.unlink()
    meta_set("last_backup", {"at": now_iso(), "file": target.name})
    return str(target)


def checkin_stats() -> dict:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with db() as c:
        ev = c.execute("SELECT kind, ok, data FROM events WHERE at >= ?", (since,)).fetchall()
        tracked = c.execute("SELECT COUNT(*) n FROM tracked").fetchone()["n"]
    def n(kind, ok_=None):
        return sum(1 for e in ev if e["kind"] == kind and (ok_ is None or e["ok"] == ok_))
    def total(kind, field):
        return sum(json.loads(e["data"] or "{}").get(field, 0) for e in ev if e["kind"] == kind)
    last = meta_get("last_filings_ok")
    last_txt = datetime.fromisoformat(last).astimezone(CENTRAL).strftime("%-I:%M %p") if last else "never"
    problems = list(active_problems().values())
    failed = n("filings", 0)
    if failed and "checks" not in active_problems():
        problems_info = f"{failed} of {n('filings')} checks couldn't reach the site (it recovered)"
    else:
        problems_info = None
    rows = [
        ("Checks for new bills", f"{n('filings', 1)} completed" + (f", {failed} failed" if failed else "")),
        ("Last successful check", last_txt + " Central"),
        ("New bills found", total("filings", "new_bills")),
        ("New bills matching the agenda", total("filings", "new_matches")),
        ("New-bill alerts emailed", n("alert_email", 1)),
        ("Tracked-bill updates emailed", n("movement_email", 1)),
        ("Bills you're tracking", tracked),
    ]
    if problems_info:
        rows.append(("Note", problems_info))
    return {"date_label": datetime.now(CENTRAL).strftime("%A, %B %-d, %Y"), "ok": not problems,
            "problems": problems, "rows": rows}


def send_checkin() -> bool:
    st = settings()
    stats = checkin_stats()
    attachments = None
    if st["checkin_backup"]:
        data = json.dumps(export_backup(), indent=1).encode("utf-8")
        name = f"onevoice-backup-{datetime.now(CENTRAL).date().isoformat()}.json"
        attachments = [(name, data, "application/json")]
        stats["backup_note"] = (f"Attached: {name}, a backup of your tracked bills, notes, positions, relevance marks and "
                                "keyword edits. Keep these emails; any one of them can restore the tracker.")
    sent = _try_send(st, *ov.render_checkin(stats, st["app_url"]), attachments=attachments)
    log_event("checkin", sent)
    meta_set("last_checkin", {"at": now_iso(), "sent": sent, "ok": stats["ok"]})
    return sent


def housekeeping() -> None:
    """Runs every minute next to the scheduled jobs: site check, problem alerts,
    and once a day the backup and the check-in email."""
    canary = meta_get("canary") or {}
    if _due("canary", 1 if canary.get("ok", True) else 1 / 6):  # re-check after 10 minutes when it failed
        run_canary()
    watch_problems()
    now_local = datetime.now(CENTRAL)
    today = now_local.date().isoformat()
    if now_local.hour >= CHECKIN_HOUR and meta_get("last_checkin_date") != today:
        meta_set("last_checkin_date", today)
        try:
            make_local_backup()
        except Exception as e:  # noqa: BLE001
            log_event("backup", False, str(e))
        with db() as c:
            c.execute("DELETE FROM events WHERE at < ?", ((datetime.now(timezone.utc) - timedelta(days=45)).isoformat(),))
        if settings()["checkin_enabled"]:
            send_checkin()


# ---------------------------------------------------------------- helpers


def agenda_for(session: str, bills: list[str]) -> dict[str, list[dict]]:
    if not bills:
        return {}
    out: dict[str, list[dict]] = {}
    with db() as c:
        for i in range(0, len(bills), 500):
            chunk = bills[i:i + 500]
            q = f"""SELECT m.bill, m.priority_id, m.keywords, m.alert, m.matched_at, rv.verdict FROM matches m
                    LEFT JOIN reviews rv ON rv.session=m.session AND rv.bill=m.bill AND rv.priority_id=m.priority_id
                    WHERE m.session=? AND m.bill IN ({','.join('?' * len(chunk))})"""
            for r in c.execute(q, [session] + chunk):
                out.setdefault(r["bill"], []).append(
                    {"id": r["priority_id"], "keywords": json.loads(r["keywords"]), "alert": r["alert"],
                     "matched_at": r["matched_at"], "review": r["verdict"]})
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


# ------------------------------------------------- one-click review links

REVIEW_LINK_DAYS = 120


def _sign(payload: bytes) -> str:
    if not app.secret_key:
        app.secret_key = _secret_key()
    return hmac.new(app.secret_key.encode(), payload, "sha256").hexdigest()[:32]


def review_token(session: str, bill: str, pid: str, verdict: str) -> str:
    exp = int((datetime.now(timezone.utc) + timedelta(days=REVIEW_LINK_DAYS)).timestamp())
    payload = base64.urlsafe_b64encode(json.dumps([session, bill, pid, verdict, exp], separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload.encode())}"


def read_review_token(token: str) -> tuple[str, str, str, str] | None:
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload.encode())):
            return None
        session, bill, pid, verdict, exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if exp < datetime.now(timezone.utc).timestamp() or verdict not in ("relevant", "not_relevant"):
            return None
        return session, bill, pid, verdict
    except Exception:  # noqa: BLE001 - any malformed token is simply invalid
        return None


def review_links(app_url: str, session: str, bill: str, pid: str) -> dict:
    if not app_url:
        return {}
    base = app_url.rstrip("/")
    return {v: f"{base}/r/{review_token(session, bill, pid, v)}" for v in ("relevant", "not_relevant")}


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
    if request.path in ("/login", "/healthz") or request.path.startswith("/r/"):
        return None  # /r/ links carry their own signature
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
        ai_available=True,
        claude_available=sm.available(),
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
        texts_missing=texts_missing(s),
        filings_minutes=FILINGS_MINUTES,
        summary_error=meta_get("summary_error"),
        summaries_waiting=len(summary_candidates(s, 1000)),
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
    dismissed = request.args.get("dismissed") or ""  # "" = hide not-relevant, "only" = show only them, "all"
    review_join = ("LEFT JOIN reviews rv ON rv.session=m.session AND rv.bill=m.bill AND rv.priority_id=m.priority_id")
    review_cond = {"only": "AND rv.verdict='not_relevant'", "all": ""}.get(dismissed, "AND COALESCE(rv.verdict,'')!='not_relevant'")
    if agenda == "any":
        where.append(f"EXISTS (SELECT 1 FROM matches m {review_join} WHERE m.session=b.session AND m.bill=b.bill {review_cond})")
    elif agenda:
        where.append(f"EXISTS (SELECT 1 FROM matches m {review_join} WHERE m.session=b.session AND m.bill=b.bill AND m.priority_id=? {review_cond})")
        args.append(agenda)
    sql_where = " AND ".join(where)
    order = "b.first_seen DESC, b.bill_type, b.bill_num" if request.args.get("sort") == "newest" else "b.bill_type, b.bill_num"
    with db() as c:
        total = c.execute(f"SELECT COUNT(*) n FROM bills b WHERE {sql_where}", args).fetchone()["n"]
        rows = c.execute(
            f"""SELECT b.*, t.position AS tracked_position, s.summary, s.version AS summary_version, s.model AS summary_model FROM bills b
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
                    """SELECT b.*, t.position AS tracked_position, s.summary, s.version AS summary_version, s.model AS summary_model FROM bills b
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
                        (SELECT model FROM summaries s WHERE s.session=t.session AND s.bill=t.bill) AS summary_model,
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
        progress=ok.progress(bill, d.get("actions", []), closed=ok.session_closed(session)),
        ai=get_summary(session, bill),
        ai_available=True,
        claude_available=sm.available(),
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
            """SELECT m.priority_id,
                      SUM(CASE WHEN COALESCE(rv.verdict,'')!='not_relevant' THEN 1 ELSE 0 END) total,
                      SUM(CASE WHEN rv.verdict='not_relevant' THEN 1 ELSE 0 END) dismissed,
                      SUM(CASE WHEN rv.verdict='relevant' THEN 1 ELSE 0 END) relevant,
                      SUM(CASE WHEN m.alert IN ('pending','sent','skipped') AND m.matched_at>=?
                               AND COALESCE(rv.verdict,'')!='not_relevant' THEN 1 ELSE 0 END) recent
               FROM matches m LEFT JOIN reviews rv ON rv.session=m.session AND rv.bill=m.bill AND rv.priority_id=m.priority_id
               WHERE m.session=? GROUP BY m.priority_id""", (week_ago, s))}
        tracked = {r["priority_id"]: r["n"] for r in c.execute(
            """SELECT m.priority_id, COUNT(*) n FROM matches m JOIN tracked t ON t.session=m.session AND t.bill=m.bill
               WHERE m.session=? GROUP BY m.priority_id""", (s,))}
        any_match = c.execute("SELECT COUNT(DISTINCT bill) n FROM matches WHERE session=?", (s,)).fetchone()["n"]
    out = []
    for p in get_priorities():
        cnt = counts.get(p["id"], {})
        out.append({**p, "matches": cnt.get("total", 0) or 0, "recent": cnt.get("recent", 0) or 0,
                    "dismissed": cnt.get("dismissed", 0) or 0, "relevant": cnt.get("relevant", 0) or 0,
                    "tracked": tracked.get(p["id"], 0)})
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
                                [{"id": "draft", "keywords": kws, "exclude": exc}],
                                get_text(s, r["bill"], fetch=False)[2])
            if got:
                total += 1
                if len(sample) < 8:
                    sample.append({"display": ok.display_bill(r["bill"]), "title": r["title"], "keywords": got[0][1]})
    return jsonify(total=total, sample=sample)


@app.post("/api/review")
def api_review():
    """Mark a bill Relevant or Not relevant for one agenda priority (verdict null clears it)."""
    body = request.get_json(force=True) or {}
    session, bill, pid, verdict = (body.get("session") or DEFAULT_SESSION).upper(), want_bill(body.get("bill") or ""), body.get("priority_id"), body.get("verdict")
    if verdict not in ("relevant", "not_relevant", None):
        abort(400, "verdict must be relevant, not_relevant or null")
    with db() as c:
        m = c.execute("SELECT keywords FROM matches WHERE session=? AND bill=? AND priority_id=?", (session, bill, pid)).fetchone()
        if not m:
            abort(404, "That bill doesn't match this priority.")
        if verdict is None:
            c.execute("DELETE FROM reviews WHERE session=? AND bill=? AND priority_id=?", (session, bill, pid))
        else:
            c.execute("INSERT OR REPLACE INTO reviews(session,bill,priority_id,verdict,keywords,at) VALUES(?,?,?,?,?,?)",
                      (session, bill, pid, verdict, m["keywords"], now_iso()))
    return jsonify(ok=True, verdict=verdict)


REVIEW_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>OneVoice Tracker</title>
<style>
:root{--bg:#f4f5f2;--card:#fff;--fg:#1e231d;--muted:#5d6659;--line:#d7dcd2;--yes:#1f7a4d;--no:#b3261e;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#121512;--card:#1a1f1a;--fg:#e7ece4;--muted:#9ea896;--line:#2f362d;--yes:#6fd3a0;--no:#ff9a91;color-scheme:dark}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:24px 16px}
.card{background:var(--card);border:1px solid var(--line);border-top:5px solid #76b82a;border-radius:8px;padding:28px;max-width:420px;width:100%}
.big{font-size:1.4rem;font-weight:700;margin:0 0 6px} .yes{color:var(--yes)} .no{color:var(--no)} p{margin:0;color:var(--muted)}
button{font:inherit;font-weight:600;border:0;border-radius:6px;padding:10px 16px;background:#4f8a1e;color:#fff;cursor:pointer;margin-top:14px}
</style></head><body><div class="card" id="card">
<p class="big">Saving your answer…</p><p>%BILL% · %PRIORITY%</p>
<noscript><form method="post"><button type="submit">Confirm: %LABEL%</button></form></noscript>
</div>
<script>
// Recorded by the browser, not by the page load, so email security scanners that
// open links in advance can't mark bills by accident.
fetch(location.pathname, {method: "POST"}).then(r => r.json()).then(d => {
  document.getElementById("card").innerHTML = d.ok
    ? (d.test
        ? '<p class="big yes">✓ The button works</p><p>This was the test email, so no bill was marked. In a real alert, this click marks the bill ' + (d.verdict === "relevant" ? "Relevant" : "Not relevant") + '.</p><p style="margin-top:10px">You can close this tab.</p>'
        : '<p class="big ' + (d.verdict === "relevant" ? 'yes">✓ Marked relevant' : 'no">✕ Marked not relevant') + '</p><p>%BILL% · %PRIORITY%</p><p style="margin-top:10px">You can close this tab. Clicked the wrong one? Use the other button in the email.</p>')
    : '<p class="big">This link has expired</p><p>Open OneVoice Tracker to mark this bill.</p>';
}).catch(() => { document.getElementById("card").innerHTML = '<p class="big">Couldn’t save</p><p>Check your connection and click the link again.</p>'; });
</script></body></html>"""


def _review_target(token: str):
    t = read_review_token(token)
    if not t:
        return None, None
    session, bill, pid, verdict = t
    p = next((x for x in get_priorities() if x["id"] == pid), None)
    label = f"{p['task_force']} #{p['number']}" if p else pid
    return t, label


@app.get("/r/<token>")
def review_link_page(token):
    import html as _html
    t, label = _review_target(token)
    if not t:
        return Response("<p style='font-family:sans-serif;padding:24px'>This link has expired or isn't valid. "
                        "Open OneVoice Tracker to mark this bill.</p>", status=404, mimetype="text/html")
    session, bill, pid, verdict = t
    body = (REVIEW_PAGE.replace("%BILL%", _html.escape(ok.display_bill(bill)))
            .replace("%PRIORITY%", _html.escape(label))
            .replace("%LABEL%", "Relevant" if verdict == "relevant" else "Not relevant"))
    return Response(body, mimetype="text/html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.post("/r/<token>")
def review_link_save(token):
    t, _ = _review_target(token)
    if not t:
        return jsonify(ok=False), 404
    session, bill, pid, verdict = t
    if session == "TEST":  # buttons in the test email: prove the link works, mark nothing
        return jsonify(ok=True, verdict=verdict, test=True)
    with db() as c:
        m = c.execute("SELECT keywords FROM matches WHERE session=? AND bill=? AND priority_id=?", (session, bill, pid)).fetchone()
        c.execute("INSERT OR REPLACE INTO reviews(session,bill,priority_id,verdict,keywords,at) VALUES(?,?,?,?,?,?)",
                  (session, bill, pid, verdict, m["keywords"] if m else "[]", now_iso()))
    log_event("review_link", True, bill=bill, priority=pid, verdict=verdict)
    if request.headers.get("Accept", "").startswith("text/html") or request.form:
        return redirect(request.path)  # no-script form submit: show the page again
    return jsonify(ok=True, verdict=verdict)


@app.get("/api/agenda/<pid>/noise")
def api_agenda_noise(pid):
    """Which keywords are behind bills marked Not relevant (and Relevant) for a priority."""
    stats: dict[str, dict[str, int]] = {}
    with db() as c:
        for r in c.execute("SELECT verdict, keywords FROM reviews WHERE priority_id=?", (pid,)):
            for ev in json.loads(r["keywords"] or "[]"):
                kw = ev.split(" (")[0]
                stats.setdefault(kw, {"not_relevant": 0, "relevant": 0})[r["verdict"]] += 1
    out = sorted(({"keyword": k, **v} for k, v in stats.items()), key=lambda x: (-x["not_relevant"], x["relevant"]))
    return jsonify(out)


@app.get("/api/health")
def api_health():
    def local(iso):
        return datetime.fromisoformat(iso).astimezone(CENTRAL).strftime("%b %-d, %-I:%M %p") if iso else None
    canary, last_ok = meta_get("canary") or {}, meta_get("last_filings_ok")
    backup, checkin = meta_get("last_backup") or {}, meta_get("last_checkin") or {}
    alert, err = meta_get("last_alert"), meta_get("last_alert_error")
    return jsonify(
        problems=[{"kind": k, "title": PROBLEMS[k][0], "message": v, "fix": PROBLEMS[k][2]} for k, v in active_problems().items()],
        checks={"last_ok": local(last_ok), "every_minutes": FILINGS_MINUTES},
        site={"ok": canary.get("ok"), "at": local(canary.get("at")), "detail": canary.get("detail")},
        email={"configured": ov.email_configured(), "last_sent": local(alert["at"]) if alert else None,
               "last_error": err["error"] if err else None, "last_error_at": local(err["at"]) if err else None},
        checkin={"at": local(checkin.get("at")), "sent": checkin.get("sent"), "hour": CHECKIN_HOUR},
        backup={"at": local(backup.get("at")), "file": backup.get("file")},
        refresh_hours=REFRESH_HOURS,
    )


@app.get("/api/backup.json")
def api_backup():
    data = json.dumps(export_backup(), indent=1)
    name = f"onevoice-backup-{datetime.now(CENTRAL).date().isoformat()}.json"
    return Response(data, mimetype="application/json", headers={"Content-Disposition": f"attachment; filename={name}"})


@app.post("/api/checkin/send")
def api_checkin_send():
    if not send_checkin():
        return jsonify(error="The check-in email couldn't be sent. Check the mail settings and the alert address."), 400
    return jsonify(ok=True)


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
        filings_minutes=FILINGS_MINUTES,
        action_categories=[{"key": k, "label": v} for k, v in ok.ACTION_CATEGORIES],
        checkin_hour=CHECKIN_HOUR,
        refresh_hours=REFRESH_HOURS,
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
    for key in ("checkin_enabled", "checkin_backup", "movement_enabled"):
        if key in body:
            s[key] = bool(body[key])
    if "movement_categories" in body:
        valid = dict(ok.ACTION_CATEGORIES)
        s["movement_categories"] = [k for k in body["movement_categories"] if k in valid]
    meta_set("settings", s)
    return api_settings()


@app.post("/api/settings/test-email")
def api_test_email():
    st = settings()
    sample = {
        "display": "HB 0000", "author": "Example Author (H)",
        "title": "Schools; example title for a test alert; effective date.",
        "summary": "Example only: no bill was filed. A real alert shows the House or Senate staff's official summary here, or the bill's own official description until staff post one.",
        "summary_model": "official",
        "progress": ok.progress("HB1000", [{"action": "First Reading", "chamber": "H"},
                                           {"action": "Second Reading referred to Common Education", "chamber": "H"}]),
        "url": ok.BASE, "last_action": "",
        "priorities": [{"task_force": "Workforce and Talent Strategies", "number": 1,
                        "title": "Strengthen Public Education and Early Literacy", "keywords": ["schools (title)"],
                        "review_links": review_links(st["app_url"], "TEST", "HB0", "wts-1")}],
    }
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
    if cmd == "restore":
        if len(argv) < 3:
            print("Usage: python app.py restore onevoice-backup-YYYY-MM-DD.json")
            return
        with open(argv[2], encoding="utf-8") as f:
            print("Restored:", restore_backup(json.load(f)))
        return
    if cmd == "checkin":
        print("Check-in sent." if send_checkin() else "Check-in NOT sent (check mail settings).")
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
