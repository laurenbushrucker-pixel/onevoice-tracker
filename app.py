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
import re
import hmac
import secrets
import zlib

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory
from flask import session as login_session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix

import oklegis as ok
import onevoice as ov
import redline as rl
import sources as so
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
FTS_TABLE = True  # set False at startup when SQLite lacks FTS5
CALENDAR_MINUTES = float(os.environ.get("BILLTRACKER_CALENDAR_MINUTES", "20"))  # meeting notices and agendas
PRESS_MINUTES = float(os.environ.get("BILLTRACKER_PRESS_MINUTES", "15"))  # press releases
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
CREATE TABLE IF NOT EXISTS meetings (
  key TEXT PRIMARY KEY, chamber TEXT, harmony_id INTEGER, title TEXT, committee TEXT, location TEXT,
  status TEXT, start TEXT, time_text TEXT, page_url TEXT, agenda_url TEXT, handouts TEXT DEFAULT '[]',
  bills TEXT DEFAULT '[]', removed TEXT DEFAULT '[]', details_at TEXT, agenda_read_url TEXT,
  first_seen TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS deadlines (date TEXT NOT NULL, label TEXT NOT NULL, PRIMARY KEY (date, label));
CREATE TABLE IF NOT EXISTS press (
  url TEXT PRIMARY KEY, source TEXT, title TEXT, published TEXT, text TEXT,
  bills TEXT DEFAULT '[]', priorities TEXT DEFAULT '[]', first_seen TEXT, alerted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS hearing_alerts (
  session TEXT NOT NULL, bill TEXT NOT NULL, meeting TEXT NOT NULL, at TEXT, PRIMARY KEY (session, bill, meeting));
CREATE TABLE IF NOT EXISTS push_subs (
  endpoint TEXT PRIMARY KEY, sub TEXT NOT NULL, label TEXT, prefs TEXT, created TEXT, last_ok TEXT, fails INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS redlines (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, bill TEXT NOT NULL,
  old_version TEXT, old_url TEXT, new_version TEXT, new_url TEXT, old_body BLOB,
  headline TEXT, diff TEXT, keywords TEXT DEFAULT '{}', at TEXT NOT NULL, emailed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS legislators (
  key TEXT PRIMARY KEY, chamber TEXT, slug TEXT, first TEXT, last TEXT, name TEXT, district INTEGER, party TEXT, photo TEXT,
  email TEXT, contact_url TEXT, phone TEXT, office TEXT, assistant TEXT, hometown TEXT, term_limited TEXT, leadership TEXT,
  page_url TEXT, committees TEXT DEFAULT '[]', counties TEXT DEFAULT '[]', cities TEXT DEFAULT '[]', zips TEXT DEFAULT '[]', updated_at TEXT);
CREATE TABLE IF NOT EXISTS goal_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, date TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT);
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
        for col in ("first_seen", "matched_at", "author", "index_action", "moved_at"):
            if col not in cols:  # upgrade a database made by an earlier version
                c.execute(f"ALTER TABLE bills ADD COLUMN {col} TEXT")
        tcols = {r["name"] for r in c.execute("PRAGMA table_info(tracked)")}
        if "reason" not in tcols:
            c.execute("ALTER TABLE tracked ADD COLUMN reason TEXT DEFAULT ''")
        if "track" not in tcols:
            c.execute("ALTER TABLE tracked ADD COLUMN track TEXT DEFAULT ''")
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS bill_fts USING fts5(session UNINDEXED, bill UNINDEXED, title, summary, body)")
        except sqlite3.OperationalError:
            pass  # SQLite without FTS5: keyword search falls back to titles
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
    if FTS_OK() and not meta_get("fts_built"):
        rebuild_fts()
        meta_set("fts_built", now_iso())


def FTS_OK() -> bool:  # noqa: N802
    with db() as c:
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='bill_fts'").fetchone())


def fts_update(c: sqlite3.Connection, session: str, bill: str) -> None:
    """Refresh one bill in the keyword-search index (title, summary, full text)."""
    try:
        b = c.execute("SELECT title FROM bills WHERE session=? AND bill=?", (session, bill)).fetchone()
        sm_row = c.execute("SELECT summary FROM summaries WHERE session=? AND bill=?", (session, bill)).fetchone()
        t = c.execute("SELECT body FROM texts WHERE session=? AND bill=?", (session, bill)).fetchone()
        body = zlib.decompress(t["body"]).decode("utf-8") if t and t["body"] else ""
        c.execute("DELETE FROM bill_fts WHERE session=? AND bill=?", (session, bill))
        c.execute("INSERT INTO bill_fts(session,bill,title,summary,body) VALUES(?,?,?,?,?)",
                  (session, bill, (b["title"] if b else "") or "", (sm_row["summary"] if sm_row else "") or "", body))
    except sqlite3.OperationalError:
        pass


def rebuild_fts() -> None:
    with db() as c:
        bills = c.execute("SELECT session, bill FROM bills").fetchall()
        for r in bills:
            fts_update(c, r["session"], r["bill"])


def fts_query(text: str) -> str:
    """User words -> FTS5 query: every word must appear (as a word start); "quoted phrases" stay together."""
    parts = re.findall(r'"([^"]+)"|(\S+)', text)
    terms = []
    for phrase, word in parts:
        if phrase:
            terms.append('"' + phrase.replace('"', "") + '"')
        else:
            w = re.sub(r"[^\w'-]", "", word)
            if w:
                terms.append('"' + w.replace('"', "") + '"*')
    return " AND ".join(terms)


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
        "hearing_email": s.get("hearing_email", True),
        "press_email": s.get("press_email", "tracked"),  # tracked | agenda | off
        "redline_email": s.get("redline_email", True),
        "report_enabled": s.get("report_enabled", True),
        "report_scope": s.get("report_scope", "onevoice"),  # onevoice | watched
        "report_recipients": s.get("report_recipients", []),
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
        prev = c.execute("SELECT version, source_url, body, chars FROM texts WHERE session=? AND bill=?", (session, bill)).fetchone()
    if prev and text and prev["chars"] and prev["source_url"] and prev["source_url"] != url:
        try:
            record_redline(session, bill, prev["version"] or "Earlier version", prev["source_url"],
                           zlib.decompress(prev["body"]).decode("utf-8"), version, url, text)
        except Exception as e:  # noqa: BLE001 - a failed comparison never blocks saving the new text
            log_event("redline", False, str(e)[:200], bill=bill)
    if error and prev and prev["chars"]:
        return  # the new version didn't download: keep the old text (and compare later) rather than lose it
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO texts(session,bill,version,source_url,body,chars,fetched_at,error) VALUES(?,?,?,?,?,?,?,?)",
            (session, bill, version, url, zlib.compress(text.encode("utf-8")), len(text), now_iso(), error),
        )
        if not prev or prev["source_url"] != url:
            c.execute("UPDATE bills SET matched_at=NULL WHERE session=? AND bill=?", (session, bill))  # re-match on new text
        fts_update(c, session, bill)


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
            send_push("newbill", f"New {ov.policy_areas(item)[0] if ov.policy_areas(item) else 'OneVoice'} bill: {item['display']}",
                      (item.get("summary") or item["title"])[:180], f"/#bill-{session}-{bill}", tag=f"new-{bill}")
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
                """INSERT INTO bills(session,bill,bill_type,bill_num,title,last_action,stage,subjects,indexed_at,first_seen,index_action,moved_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session,bill) DO UPDATE SET
                     title=excluded.title, indexed_at=excluded.indexed_at,
                     moved_at=CASE WHEN bills.index_action IS NOT NULL AND bills.index_action!=excluded.index_action
                                   THEN excluded.moved_at ELSE bills.moved_at END,
                     index_action=excluded.index_action,
                     matched_at=CASE WHEN bills.subjects=excluded.subjects THEN bills.matched_at ELSE NULL END,
                     subjects=excluded.subjects,
                     last_action=CASE WHEN bills.last_action_date IS NULL THEN excluded.last_action ELSE bills.last_action END,
                     stage=CASE WHEN bills.last_action_date IS NULL THEN excluded.stage ELSE bills.stage END""",
                (session, e.bill, t, n, e.title, e.last_action, stage, json.dumps(e.subjects), ts, ts, e.last_action, ts),
            )
            if FTS_TABLE:
                fts_update(c, session, e.bill)


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
        if FTS_TABLE:
            fts_update(c, d.session, d.bill)
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
    if session in (None, DEFAULT_SESSION):
        check_watched_texts(progress)  # new versions of tracked or Relevant bills: "what changed?" redlines
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
                      t.position, t.reason, b.title, b.author, b.stage, s.summary
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
            "summary": r0["summary"] or "", "url": ok.bill_url(bill, session), "position": r0["position"] or "", "reason": r0["reason"] or "",
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
        send_push("moved", f"{item['display']} moved", item["actions"][-1]["action"][:180], f"/#bill-{session}-{bill}", tag=f"moved-{bill}")
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
        fts_update(c, session, bill)


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
                if _due("calendar", CALENDAR_MINUTES / 60):
                    start_job("calendar", None, refresh_calendar)
                elif _due("press", PRESS_MINUTES / 60):
                    start_job("press", None, refresh_press)
                elif _due("last_auto_refresh", REFRESH_HOURS):
                    if start_job("refresh", None, lambda p: refresh_tracked(None, p)):
                        meta_set("last_auto_refresh", now_iso())
                elif _due("legislators", 24 * 7):
                    start_job("legislators", None, refresh_legislators)
                elif _due(f"indexed:{DEFAULT_SESSION}", INDEX_HOURS):
                    start_job("index", DEFAULT_SESSION, lambda p: build_index(DEFAULT_SESSION, p))
                elif _due(f"summaries:{DEFAULT_SESSION}", 0.25) and summary_candidates(DEFAULT_SESSION, 1):
                    start_job("summaries", DEFAULT_SESSION, lambda p: summarize_pending(DEFAULT_SESSION, p))
                elif _due(f"ovdetails:{DEFAULT_SESSION}", 0.25) and onevoice_details_missing(DEFAULT_SESSION):
                    start_job("details", DEFAULT_SESSION, lambda p: fill_onevoice_details(DEFAULT_SESSION, p))
                elif _due(f"texts:{DEFAULT_SESSION}", 0.1) and texts_missing(DEFAULT_SESSION):
                    start_job("texts", DEFAULT_SESSION, lambda p: fetch_texts(DEFAULT_SESSION, p))
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            pass


# ------------------------------------------------------- push notifications


def vapid_signer():
    """The signing key as the push library wants it (RFC 8292 "vapid" scheme, which Apple requires)."""
    from py_vapid import Vapid02
    return Vapid02.from_pem(vapid()[0].encode())


def vapid() -> tuple[str, str]:
    """(private key PEM, public key for browsers). Made once and kept in the database."""
    keys = meta_get("vapid")
    if not keys:
        import base64 as b64
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid01
        v = Vapid01()
        v.generate_keys()
        pub = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        keys = {"private": v.private_pem().decode(), "public": b64.urlsafe_b64encode(pub).decode().rstrip("=")}
        meta_set("vapid", keys)
    return keys["private"], keys["public"]


PUSH_KINDS = {"newbill": "New OneVoice bills", "moved": "Tracked bill moves", "hearing": "Tracked bill on a committee agenda",
              "press": "Press releases about your bills", "changed": "Tracked bill rewritten (what changed)"}


def send_push(kind: str, title: str, body: str, url: str = "/", tag: str = "") -> int:
    """Notify every phone or computer that turned on notifications for this kind."""
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return 0
    with db() as c:
        subs = c.execute("SELECT endpoint, sub, prefs FROM push_subs").fetchall()
    if not subs:
        return 0
    signer = vapid_signer()
    st = settings()
    contact = st["app_url"] if (st["app_url"] or "").startswith("https://") else "mailto:" + (ov.smtp_settings()["sender"] or "alerts@example.org")
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or kind})
    sent = 0
    for r in subs:
        prefs = json.loads(r["prefs"] or "{}")
        if not prefs.get(kind, True):
            continue
        try:
            webpush(json.loads(r["sub"]), payload, vapid_private_key=signer, vapid_claims={"sub": contact}, ttl=86400, timeout=15)
            sent += 1
            with db() as c:
                c.execute("UPDATE push_subs SET last_ok=?, fails=0 WHERE endpoint=?", (now_iso(), r["endpoint"]))
        except WebPushException as e:
            code = getattr(e.response, "status_code", 0) if getattr(e, "response", None) is not None else 0
            with db() as c:
                if code in (404, 410):  # the browser unsubscribed or the phone was reset
                    c.execute("DELETE FROM push_subs WHERE endpoint=?", (r["endpoint"],))
                else:
                    c.execute("UPDATE push_subs SET fails=fails+1 WHERE endpoint=?", (r["endpoint"],))
            log_event("push", False, str(e)[:200], push_kind=kind)
        except Exception as e:  # noqa: BLE001 - one bad device never stops the others
            log_event("push", False, str(e)[:200], push_kind=kind)
    if sent:
        log_event("push", True, push_kind=kind, sent=sent)
    return sent


# ------------------------------------------------------- committee calendar


def refresh_calendar(progress=None) -> dict:
    """Read upcoming committee meetings for both chambers, their agendas, and the bills on them."""
    found, errors, now = 0, [], now_iso()
    for chamber in so.CHAMBERS:
        try:
            meetings = so.upcoming_meetings(chamber)
        except Exception as e:  # noqa: BLE001 - one chamber's site being down doesn't stop the other
            errors.append(f"{chamber}: {e}")
            continue
        for i, m in enumerate(meetings, 1):
            if progress:
                progress(i, len(meetings), f"{chamber} {m['committee']}")
            found += 1
            with db() as c:
                prev = c.execute("SELECT * FROM meetings WHERE key=?", (m["key"],)).fetchone()
                c.execute(
                    """INSERT INTO meetings(key,chamber,harmony_id,title,committee,location,status,start,time_text,page_url,first_seen,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET title=excluded.title, committee=excluded.committee,
                         location=excluded.location, status=excluded.status, start=COALESCE(excluded.start, meetings.start),
                         time_text=excluded.time_text, page_url=excluded.page_url, updated_at=excluded.updated_at""",
                    (m["key"], chamber, m["harmony_id"], m["title"], m["committee"], m["location"], m["status"],
                     m["start"], m["time_text"], m["page_url"], now, now))
            stale = not prev or prev["title"] != m["title"] or not prev["agenda_url"] or not prev["details_at"] or \
                datetime.now(timezone.utc) - datetime.fromisoformat(prev["details_at"]) > timedelta(hours=2)
            if not stale:
                continue
            try:
                d = so.meeting_details(m["page_url"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"{m['key']}: {e}")
                continue
            with db() as c:
                c.execute("UPDATE meetings SET handouts=?, agenda_url=?, start=COALESCE(?, start), details_at=? WHERE key=?",
                          (json.dumps(d["handouts"]), d["agenda_url"], d["start"], now_iso(), m["key"]))
                read_url = c.execute("SELECT agenda_read_url FROM meetings WHERE key=?", (m["key"],)).fetchone()["agenda_read_url"]
            if d["agenda_url"] and (d["agenda_url"] != read_url or (prev and prev["title"] != m["title"])):
                try:
                    bills, removed = so.agenda_bills(sm.bill_text(d["agenda_url"]))
                    with db() as c:
                        c.execute("UPDATE meetings SET bills=?, removed=?, agenda_read_url=? WHERE key=?",
                                  (json.dumps(bills), json.dumps(removed), d["agenda_url"], m["key"]))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"agenda {m['key']}: {e}")
    if _due("deadlines_at", 24):
        try:
            dl = so.legislative_deadlines()
            if dl:
                with db() as c:
                    c.executemany("INSERT OR IGNORE INTO deadlines(date,label) VALUES(?,?)", [(x["date"], x["label"]) for x in dl])
                meta_set("deadlines_at", now_iso())
        except Exception as e:  # noqa: BLE001
            errors.append(f"deadlines: {e}")
    alerts = send_hearing_alerts()
    ok_ = len(errors) < 2
    meta_set("calendar", {"at": now_iso(), "meetings": found, "errors": errors[:5]})
    log_event("calendar", ok_, "; ".join(errors[:2]), meetings=found, alerts=alerts)
    return {"meetings": found, "hearing_alerts": alerts, "errors": len(errors)}


def watched_bills(session: str) -> dict[str, str]:
    """Bills whose hearings matter: tracked ones, and agenda matches marked Relevant. {bill: why}"""
    with db() as c:
        out = {r["bill"]: "tracked" for r in c.execute("SELECT bill FROM tracked WHERE session=?", (session,))}
        for r in c.execute("SELECT DISTINCT bill FROM reviews WHERE session=? AND verdict='relevant'", (session,)):
            out.setdefault(r["bill"], "relevant")
    return out


def send_hearing_alerts() -> int:
    """Email (and notify) when a tracked or Relevant bill is put on an upcoming committee agenda."""
    session = DEFAULT_SESSION
    watch = watched_bills(session)
    if not watch:
        return 0
    st = settings()
    sent = 0
    with db() as c:
        meetings = c.execute("SELECT * FROM meetings WHERE start >= ? ORDER BY start",
                             (datetime.now(CENTRAL).replace(tzinfo=None).isoformat(),)).fetchall()
    for m in meetings:
        hits = [b for b in json.loads(m["bills"] or "[]") if b in watch]
        with db() as c:
            done = {r["bill"] for r in c.execute("SELECT bill FROM hearing_alerts WHERE session=? AND meeting=?", (session, m["key"]))}
        new = [b for b in hits if b not in done]
        if not new:
            continue
        items = []
        with db() as c:
            for b in new:
                r = c.execute("""SELECT b.title, b.author, t.position FROM bills b LEFT JOIN tracked t ON t.session=b.session AND t.bill=b.bill
                                 WHERE b.session=? AND b.bill=?""", (session, b)).fetchone()
                items.append({"display": ok.display_bill(b), "title": (r["title"] if r else "") or "", "author": (r["author"] if r else "") or "",
                              "position": (r["position"] if r else "") or "", "url": ok.bill_url(b, session)})
        when = datetime.fromisoformat(m["start"]).strftime("%a, %b %-d at %-I:%M %p") if m["start"] else m["time_text"]
        meeting = {"chamber": m["chamber"], "committee": m["committee"], "when": when, "location": m["location"],
                   "agenda_url": m["agenda_url"], "page_url": m["page_url"], "status": m["status"]}
        delivered = False
        if st["hearing_email"]:
            delivered = _try_send(st, *ov.render_hearing(items, meeting, st["app_url"]))
        pushed = send_push("hearing", f"{', '.join(i['display'] for i in items)} on {m['chamber']} {m['committee']} agenda",
                           f"{when} · {m['location']}", "/#calendar", tag=f"hearing-{m['key']}")
        if delivered or pushed or not st["hearing_email"]:
            with db() as c:
                c.executemany("INSERT OR IGNORE INTO hearing_alerts(session,bill,meeting,at) VALUES(?,?,?,?)",
                              [(session, b, m["key"], now_iso()) for b in new])
            sent += 1
    return sent


# ---------------------------------------------------------- press releases


def press_sources() -> list[dict]:
    return so.BUILTIN_PRESS + (meta_get("press_sources_custom") or [])


def refresh_press(progress=None) -> dict:
    """Collect new press releases, tag them with bills and agenda priorities, and alert on your bills."""
    srcs = press_sources()
    priorities = get_priorities()
    new_total, errors = 0, []
    for i, src in enumerate(srcs, 1):
        if progress:
            progress(i, len(srcs), src["name"])
        try:
            items = so.fetch_press(src)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{src['name']}: {e}")
            continue
        first_run = not meta_get(f"press_seen:{src['id']}")
        fetched_text = 0
        for it in items:
            with db() as c:
                if c.execute("SELECT 1 FROM press WHERE url=?", (it["url"],)).fetchone():
                    continue
            text = it.get("text") or ""
            if len(text) < 300 and fetched_text < 10 and not first_run:
                try:
                    text = so.press_text(it["url"])
                    fetched_text += 1
                except Exception:  # noqa: BLE001
                    pass
            bills = so.bills_in(f"{it['title']} {text}")
            pr = ov.match_bill(it["title"], [], priorities, text, min_hits=3)
            with db() as c:
                c.execute("INSERT OR IGNORE INTO press(url,source,title,published,text,bills,priorities,first_seen,alerted) VALUES(?,?,?,?,?,?,?,?,?)",
                          (it["url"], it["source"], it["title"], it["published"], text[:20000], json.dumps(bills),
                           json.dumps([{"id": pid, "keywords": kw} for pid, kw in pr]), now_iso(), 2 if first_run else 0))
            new_total += 0 if first_run else 1
        meta_set(f"press_seen:{src['id']}", now_iso())
    alerts = send_press_alerts()
    meta_set("press", {"at": now_iso(), "new": new_total, "errors": errors[:5]})
    log_event("press", len(errors) < len(srcs), "; ".join(errors[:2]), new=new_total, alerts=alerts)
    return {"new": new_total, "alerts": alerts, "errors": len(errors)}


def send_press_alerts() -> int:
    st = settings()
    watch = watched_bills(DEFAULT_SESSION)
    pri = {p["id"]: p for p in get_priorities()}
    with db() as c:
        rows = c.execute("SELECT * FROM press WHERE alerted=0 ORDER BY published").fetchall()
    sent = 0
    for r in rows:
        bills = [b for b in json.loads(r["bills"] or "[]") if b in watch]
        prs = [p for p in json.loads(r["priorities"] or "[]") if any("(title)" in k for k in p["keywords"])]
        mode = st["press_email"]
        relevant = bool(bills) or (mode == "agenda" and prs)
        delivered = False
        if relevant and mode != "off":
            item = {"source": r["source"], "title": r["title"], "url": r["url"], "published": r["published"],
                    "excerpt": (r["text"] or "")[:400], "bills": [ok.display_bill(b) for b in bills],
                    "priorities": [f"{pri[p['id']]['task_force']} #{pri[p['id']]['number']}" for p in prs if p["id"] in pri]}
            delivered = _try_send(st, *ov.render_press(item, st["app_url"]))
        if bills or prs:
            send_push("press", f"{r['source']}: {r['title'][:90]}",
                      ("Mentions " + ", ".join(ok.display_bill(b) for b in bills)) if bills else "Related to the OneVoice agenda",
                      "/#press", tag="press-" + str(abs(hash(r["url"])) % 10**8))
        with db() as c:
            c.execute("UPDATE press SET alerted=1 WHERE url=?", (r["url"],))
        sent += int(delivered)
    return sent


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
        goal_log = [dict(r) for r in c.execute("SELECT kind, date, data, created_at, updated_at FROM goal_log ORDER BY id")]
        goal_meta = {r["key"]: json.loads(r["value"]) for r in c.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'goal_%'")}
    st = settings()
    return {"app": "OneVoice Tracker", "format": 1, "exported_at": now_iso(),
            "tracked": tracked, "reviews": reviews, "priorities": get_priorities(), "goal_log": goal_log, "goal_meta": goal_meta,
            "settings": {k: st[k] for k in ("recipients", "alerts_enabled", "app_url", "checkin_enabled",
                                             "checkin_backup", "movement_enabled", "movement_categories",
                                             "hearing_email", "press_email", "redline_email", "report_enabled",
                                             "report_scope", "report_recipients")}}


def restore_backup(data: dict) -> dict:
    if data.get("app") != "OneVoice Tracker":
        raise ValueError("This file isn't a OneVoice Tracker backup.")
    with db() as c:
        for t in data.get("tracked", []):
            c.execute("""INSERT OR REPLACE INTO tracked(session,bill,position,priority,notes,labels,added_at,last_checked,reason,track)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (t["session"], t["bill"], t.get("position"), t.get("priority"), t.get("notes", ""),
                       t.get("labels", ""), t.get("added_at") or now_iso(), t.get("last_checked"), t.get("reason", ""), t.get("track", "")))
        if data.get("goal_log") and not c.execute("SELECT 1 FROM goal_log LIMIT 1").fetchone():
            c.executemany("INSERT INTO goal_log(kind,date,data,created_at,updated_at) VALUES(?,?,?,?,?)",
                          [(g["kind"], g["date"], g["data"], g["created_at"], g.get("updated_at")) for g in data["goal_log"]])
        for k, v in (data.get("goal_meta") or {}).items():
            if k.startswith("goal_"):
                c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, json.dumps(v)))
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
        ("Committee agenda alerts", total("calendar", "alerts")),
        ("New press releases", total("press", "new")),
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
    try:
        weekly_report_due()
    except Exception as e:  # noqa: BLE001
        log_event("weekly_report", False, str(e))
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
    if request.path in ("/login", "/healthz", "/sw.js", "/manifest.webmanifest") or request.path.startswith(("/r/", "/cal/", "/static/icons/")):
        return None  # /r/ and /cal/ links carry their own secret; app files hold nothing private
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
    elif q and FTS_TABLE and fts_query(q):
        where.append("b.bill IN (SELECT bill FROM bill_fts WHERE session=? AND bill_fts MATCH ?)")
        args += [s, fts_query(q)]
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
    results = with_agenda(s, [bill_row(r) for r in rows])
    if q and not bill:
        add_snippets(s, q, results)
    return jsonify(total=total, results=results)


def add_snippets(session: str, q: str, rows: list[dict]) -> None:
    """Show where the search words appear in each bill."""
    if not FTS_TABLE or not rows or not fts_query(q):
        return
    with db() as c:
        try:
            found = {}
            for r in c.execute(
                f"""SELECT bill, snippet(bill_fts, 4, '[[', ']]', '…', 14) AS body,
                           snippet(bill_fts, 3, '[[', ']]', '…', 14) AS summary,
                           snippet(bill_fts, 2, '[[', ']]', '…', 14) AS title FROM bill_fts
                    WHERE session=? AND bill_fts MATCH ? AND bill IN ({','.join('?' * len(rows))})""",
                    [session, fts_query(q)] + [r["bill"] for r in rows]):
                # Prefer the bill text, then the summary, then the title: whichever actually contains the words.
                found[r["bill"]] = next((x for x in (r["body"], r["summary"], r["title"]) if x and "[[" in x), r["title"])
        except sqlite3.OperationalError:
            return
    for r in rows:
        if r["bill"] in found:
            r["snippet"] = re.sub(r"\s+", " ", found[r["bill"]]).strip()


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
            "INSERT OR IGNORE INTO tracked(session,bill,position,priority,reason,added_at,last_checked) VALUES(?,?,?,?,?,?,?)",
            (s, bill, body.get("position") if body.get("position") in POSITIONS else "monitor",
             body.get("priority") or "medium", str(body.get("reason") or "")[:1000], now_iso(), now_iso()),
        )
        if body.get("position") in POSITIONS:  # choosing a position on an already-tracked bill
            c.execute("UPDATE tracked SET position=? WHERE session=? AND bill=?", (body["position"], s, bill))
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
    if "reason" in body:
        fields.append("reason=?"); args.append(str(body["reason"])[:1000])
    if "track" in body:
        fields.append("track=?"); args.append(str(body["track"]).strip()[:100])
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
        reds = [{"id": r["id"], "old": r["old_version"], "new": r["new_version"], "headline": r["headline"], "at": r["at"]}
                for r in c.execute("SELECT id, old_version, new_version, headline, at FROM redlines WHERE session=? AND bill=? ORDER BY id DESC",
                                   (session, bill))]
    d.update(
        redlines=reds,
        versions=[{"name": v["name"], "url": v["url"]} for v in d.get("documents", []) if v.get("group", "").startswith("Versions of")],
        track_options=list(dict.fromkeys([p["task_force"] for p in get_priorities()] + EXTRA_TRACKS)),
        display=ok.display_bill(bill),
        stage=ok.stage_from_actions(bill, [ok.Action(**a) for a in d["actions"]]),
        url=ok.bill_url(bill, session),
        lens_url=ok.lens_url(bill),
        tracked=dict(t) if t else None,
        subjects=json.loads(b["subjects"]) if b else [],
        agenda=agenda_for(session, [bill]).get(bill, []),
        author_text=author_text(d.get("author", ""), d.get("other_author", "")),
        author_key=slug_from_url(d.get("author_url")), other_author_key=slug_from_url(d.get("other_author_url")),
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


# ------------------------------------------------------------ new bills list


@app.get("/api/newbills")
def api_newbills():
    """Every bill that matches the agenda, newest first, with what the alert
    emails show: author, subject areas, summary, bill text link, status, and
    the Chamber's position."""
    s = want_session()
    q = (request.args.get("q") or "").strip()
    pid = request.args.get("priority") or ""
    review = request.args.get("review") or ""      # "" = to review + relevant, todo, relevant, not_relevant, all
    position = request.args.get("position") or ""  # "" any, none, support, oppose, monitor, neutral
    limit = min(int(request.args.get("limit", 30)), 100)
    offset = max(int(request.args.get("offset", 0)), 0)

    m_where, m_args = ["m.session=b.session", "m.bill=b.bill"], []
    if pid:
        m_where.append("m.priority_id=?"); m_args.append(pid)
    rv = {"todo": "rv.verdict IS NULL", "relevant": "rv.verdict='relevant'", "not_relevant": "rv.verdict='not_relevant'",
          "all": "1=1"}.get(review, "COALESCE(rv.verdict,'')!='not_relevant'")
    m_where.append(rv)
    where = [f"""EXISTS (SELECT 1 FROM matches m LEFT JOIN reviews rv ON rv.session=m.session AND rv.bill=m.bill
                          AND rv.priority_id=m.priority_id WHERE {' AND '.join(m_where)})""", "b.session=?"]
    args = m_args + [s]
    bill = ok.normalize_bill(q) if q else None
    if bill:
        where.append("b.bill=?"); args.append(bill)
    elif q and FTS_TABLE and fts_query(q):
        where.append("b.bill IN (SELECT bill FROM bill_fts WHERE session=? AND bill_fts MATCH ?)"); args += [s, fts_query(q)]
    elif q:
        where.append("b.title LIKE ?"); args.append(f"%{q}%")
    if position == "none":
        where.append("t.bill IS NULL")
    elif position in POSITIONS:
        where.append("t.position=?"); args.append(position)
    sql_where = " AND ".join(where)
    base = f"""FROM bills b LEFT JOIN tracked t ON t.session=b.session AND t.bill=b.bill
               LEFT JOIN summaries su ON su.session=b.session AND su.bill=b.bill
               LEFT JOIN texts tx ON tx.session=b.session AND tx.bill=b.bill WHERE {sql_where}"""
    with db() as c:
        total = c.execute(f"SELECT COUNT(*) n {base}", args).fetchone()["n"]
        rows = c.execute(
            f"""SELECT b.*, t.position AS tracked_position, t.reason AS position_reason, su.summary, su.model AS summary_model,
                       tx.source_url AS text_url, tx.version AS text_version,
                       (SELECT MAX(alerted_at) FROM matches m2 WHERE m2.session=b.session AND m2.bill=b.bill AND m2.alert='sent') AS emailed_at
                {base} ORDER BY b.first_seen DESC, b.bill_type, b.bill_num LIMIT ? OFFSET ?""", args + [limit, offset]).fetchall()
    out = with_agenda(s, [bill_row(r) for r in rows])
    for r in out:
        r["progress"] = bill_progress(s, r["bill"])
    if q and not bill:
        add_snippets(s, q, out)
    return jsonify(total=total, results=out)


# ------------------------------------------------------- phone app (PWA)


@app.get("/manifest.webmanifest")
def manifest():
    data = {
        "name": "OneVoice Tracker", "short_name": "OneVoice", "start_url": "/#home", "scope": "/",
        "display": "standalone", "background_color": "#33393a", "theme_color": "#33393a",
        "description": "Oklahoma bills, committee meetings and press releases for the OneVoice agenda.",
        "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    }
    return Response(json.dumps(data), mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/push/key")
def api_push_key():
    return jsonify(key=vapid()[1], kinds=PUSH_KINDS)


@app.post("/api/push/subscribe")
def api_push_subscribe():
    body = request.get_json(force=True) or {}
    sub = body.get("subscription") or {}
    if not sub.get("endpoint") or not (sub.get("keys") or {}).get("p256dh"):
        abort(400, "That browser didn't provide a notification subscription.")
    prefs = {k: bool((body.get("prefs") or {}).get(k, True)) for k in PUSH_KINDS}
    with db() as c:
        c.execute("INSERT OR REPLACE INTO push_subs(endpoint,sub,label,prefs,created,last_ok,fails) VALUES(?,?,?,?,?,NULL,0)",
                  (sub["endpoint"], json.dumps(sub), str(body.get("label") or "")[:80], json.dumps(prefs), now_iso()))
    return jsonify(ok=True, prefs=prefs)


@app.post("/api/push/prefs")
def api_push_prefs():
    body = request.get_json(force=True) or {}
    prefs = {k: bool((body.get("prefs") or {}).get(k, True)) for k in PUSH_KINDS}
    with db() as c:
        cur = c.execute("UPDATE push_subs SET prefs=? WHERE endpoint=?", (json.dumps(prefs), body.get("endpoint", "")))
    if not cur.rowcount:
        abort(404, "Notifications aren't turned on for this device.")
    return jsonify(ok=True, prefs=prefs)


@app.post("/api/push/status")
def api_push_status():
    body = request.get_json(force=True) or {}
    with db() as c:
        r = c.execute("SELECT prefs FROM push_subs WHERE endpoint=?", (body.get("endpoint", ""),)).fetchone()
        n = c.execute("SELECT COUNT(*) n FROM push_subs").fetchone()["n"]
    return jsonify(subscribed=bool(r), prefs=json.loads(r["prefs"]) if r else None, devices=n)


@app.post("/api/push/unsubscribe")
def api_push_unsubscribe():
    body = request.get_json(force=True) or {}
    with db() as c:
        c.execute("DELETE FROM push_subs WHERE endpoint=?", (body.get("endpoint", ""),))
    return jsonify(ok=True)


@app.post("/api/push/test")
def api_push_test():
    body = request.get_json(force=True) or {}
    with db() as c:
        r = c.execute("SELECT sub FROM push_subs WHERE endpoint=?", (body.get("endpoint", ""),)).fetchone()
    if not r:
        abort(404, "Notifications aren't turned on for this device.")
    try:
        from pywebpush import webpush
        st = settings()
        contact = st["app_url"] if (st["app_url"] or "").startswith("https://") else "mailto:alerts@example.org"
        webpush(json.loads(r["sub"]), json.dumps({"title": "OneVoice Tracker", "body": "Notifications are working on this device.",
                                                  "url": "/#home", "tag": "test"}),
                vapid_private_key=vapid_signer(), vapid_claims={"sub": contact}, ttl=600, timeout=15)
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"The test notification couldn't be sent: {e}"), 400
    return jsonify(ok=True)


# ------------------------------------------------- home, calendar, press APIs


@app.get("/api/home")
def api_home():
    s = want_session()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    now_local = datetime.now(CENTRAL).replace(tzinfo=None)
    monday = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    with db() as c:
        new_bills = c.execute("""SELECT COUNT(DISTINCT m.bill) n FROM matches m JOIN bills b ON b.session=m.session AND b.bill=m.bill
                                 LEFT JOIN reviews rv ON rv.session=m.session AND rv.bill=m.bill AND rv.priority_id=m.priority_id
                                 WHERE m.session=? AND b.first_seen>=? AND COALESCE(rv.verdict,'')!='not_relevant'""", (s, week_ago)).fetchone()["n"]
        to_review = c.execute("""SELECT COUNT(DISTINCT m.bill) n FROM matches m LEFT JOIN reviews rv
                                 ON rv.session=m.session AND rv.bill=m.bill AND rv.priority_id=m.priority_id
                                 WHERE m.session=? AND m.alert IN ('pending','sent') AND rv.verdict IS NULL""", (s,)).fetchone()["n"]
        meetings = c.execute("SELECT COUNT(*) n FROM meetings WHERE start>=? AND start<?",
                             (monday.isoformat(), (monday + timedelta(days=7)).isoformat())).fetchone()["n"]
        press_new = c.execute("SELECT COUNT(*) n FROM press WHERE first_seen>=? AND alerted!=2", (week_ago,)).fetchone()["n"]
        tracked = c.execute("SELECT COUNT(*) n FROM tracked WHERE session=?", (s,)).fetchone()["n"]
        upcoming = [dict(r) for r in c.execute(
            "SELECT key, chamber, committee, start, time_text, location, bills FROM meetings WHERE start>=? ORDER BY start LIMIT 40",
            (now_local.isoformat(),))]
        unseen_updates = c.execute("SELECT COUNT(*) n FROM updates WHERE session=? AND seen=0", (s,)).fetchone()["n"]
    watch = watched_bills(s)
    mine = [m for m in upcoming if any(b in watch for b in json.loads(m["bills"] or "[]"))][:3]
    for m in mine:
        m["your_bills"] = [ok.display_bill(b) for b in json.loads(m["bills"]) if b in watch]
        m.pop("bills")
    try:
        g = compute_goals(GOAL_YEAR)
        heads = [m for m in g["metrics"] if m["headline"]]
        goals_line = (f"{sum(1 for m in heads if m['status'] in ('met', 'on_track'))} of {len(heads)} on pace"
                      if any(m["status"] in ("met", "on_track", "behind") for m in heads) else "")
        logged = g["counts"]["meetings"]
    except Exception:  # noqa: BLE001 - the home screen never fails over the goals card
        goals_line, logged = "", 0
    return jsonify(new_bills=new_bills, to_review=to_review, meetings_this_week=meetings, press_new=press_new,
                   tracked=tracked, updates=unseen_updates, your_hearings=mine, goals_line=goals_line,
                   meetings_logged=logged, goal_year=GOAL_YEAR)


@app.get("/api/calendar")
def api_calendar():
    """One week of committee meetings (Monday to Sunday) with agendas and the bills on them."""
    s = want_session()
    try:
        start = datetime.fromisoformat(request.args.get("week") or "")
    except ValueError:
        today = datetime.now(CENTRAL).replace(tzinfo=None)
        start = today - timedelta(days=today.weekday())
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    watch = watched_bills(s)
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM meetings WHERE start>=? AND start<? ORDER BY start",
                                           (start.isoformat(), end.isoformat()))]
        dls = [dict(r) for r in c.execute("SELECT date, label FROM deadlines WHERE date>=? AND date<? ORDER BY date",
                                          (start.date().isoformat(), end.date().isoformat()))]
        matched = {r["bill"] for r in c.execute(
            """SELECT DISTINCT m.bill FROM matches m LEFT JOIN reviews rv ON rv.session=m.session AND rv.bill=m.bill
               AND rv.priority_id=m.priority_id WHERE m.session=? AND COALESCE(rv.verdict,'')!='not_relevant'""", (s,))}
        titles = {}
        allb = sorted({b for r in rows for b in json.loads(r["bills"] or "[]")})
        for i in range(0, len(allb), 500):
            chunk = allb[i:i + 500]
            for r in c.execute(f"SELECT bill, title FROM bills WHERE session=? AND bill IN ({','.join('?' * len(chunk))})", [s] + chunk):
                titles[r["bill"]] = r["title"]
    for r in rows:
        r["bills"] = [{"bill": b, "display": ok.display_bill(b), "title": titles.get(b, ""),
                       "watch": watch.get(b), "onevoice": b in matched} for b in json.loads(r["bills"] or "[]")]
        r["removed"] = [ok.display_bill(b) for b in json.loads(r["removed"] or "[]")]
        r.pop("handouts", None)
    return jsonify(week=start.date().isoformat(), meetings=rows, deadlines=dls, updated=(meta_get("calendar") or {}).get("at"))


@app.get("/api/press")
def api_press():
    s = want_session()
    source, show, q = request.args.get("source") or "", request.args.get("show") or "", (request.args.get("q") or "").strip()
    limit, offset = min(int(request.args.get("limit", 30)), 100), max(int(request.args.get("offset", 0)), 0)
    where, args = ["1=1"], []
    if source:
        where.append("source=?"); args.append(source)
    if q:
        for w in q.split():
            where.append("(title LIKE ? OR text LIKE ?)"); args += [f"%{w}%", f"%{w}%"]
    if show == "agenda":
        where.append("(priorities!='[]' OR bills!='[]')")
    with db() as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT url, source, title, published, substr(text,1,400) AS excerpt, bills, priorities, first_seen FROM press "
            f"WHERE {' AND '.join(where)} ORDER BY COALESCE(published, first_seen) DESC", args)]
    watch = watched_bills(s)
    pri = {p["id"]: p for p in get_priorities()}
    out = []
    for r in rows:
        bills = json.loads(r["bills"] or "[]")
        prs = json.loads(r["priorities"] or "[]")
        r["bills"] = [{"bill": b, "display": ok.display_bill(b), "watch": watch.get(b)} for b in bills]
        r["priorities"] = [{"id": p["id"], "label": f"{pri[p['id']]['task_force']} #{pri[p['id']]['number']}"} for p in prs if p["id"] in pri]
        if show == "mine" and not any(b["watch"] for b in r["bills"]):
            continue
        out.append(r)
    sources = sorted({x["name"] for x in press_sources()})
    return jsonify(total=len(out), results=out[offset:offset + limit], sources=sources, updated=(meta_get("press") or {}).get("at"))


@app.post("/api/press/sources")
def api_press_add_source():
    body = request.get_json(force=True) or {}
    url, name = (body.get("url") or "").strip(), (body.get("name") or "").strip()[:60]
    if not re.match(r"^https://(www\.)?oklahoma\.gov/.+", url):
        abort(400, "Paste the link to an oklahoma.gov newsroom page, like https://oklahoma.gov/ohca/about/newsroom.html")
    try:
        items = so.aem_newsroom(url, name or "Agency")
    except Exception as e:  # noqa: BLE001
        abort(400, f"That page doesn't have a news list the tracker can read ({e}).")
    custom = meta_get("press_sources_custom") or []
    sid = "c" + str(abs(hash(url)) % 10**8)
    if not any(x["url"] == url for x in custom):
        custom.append({"id": sid, "name": name or url.split("/")[3].upper(), "kind": "aem", "url": url})
        meta_set("press_sources_custom", custom)
    return jsonify(ok=True, found=len(items), sources=press_sources())


@app.delete("/api/press/sources/<sid>")
def api_press_remove_source(sid):
    meta_set("press_sources_custom", [x for x in (meta_get("press_sources_custom") or []) if x["id"] != sid])
    return jsonify(ok=True, sources=press_sources())


@app.get("/api/press/sources")
def api_press_list_sources():
    return jsonify(sources=press_sources())


@app.post("/api/calendar/refresh")
def api_calendar_refresh():
    if not start_job("calendar", None, refresh_calendar):
        return jsonify(error=BUSY), 409
    return jsonify(ok=True)


@app.post("/api/press/refresh")
def api_press_refresh():
    if not start_job("press", None, refresh_press):
        return jsonify(error=BUSY), 409
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
        calendar={"at": local((meta_get("calendar") or {}).get("at")), "errors": (meta_get("calendar") or {}).get("errors", []),
                  "every_minutes": CALENDAR_MINUTES},
        press={"at": local((meta_get("press") or {}).get("at")), "errors": (meta_get("press") or {}).get("errors", []),
               "every_minutes": PRESS_MINUTES},
        push_devices=_count("push_subs"),
        refresh_hours=REFRESH_HOURS,
    )


def _count(table: str) -> int:
    with db() as c:
        return c.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]


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
        report_hour=REPORT_HOUR,
        ical=ical_urls(),
    )


@app.put("/api/settings")
def api_settings_update():
    body = request.get_json(force=True) or {}
    s = settings()
    if "recipients" in body:
        addrs, bad = parse_addresses(body["recipients"])
        if bad:
            abort(400, f"These don't look like email addresses: {', '.join(bad)}")
        s["recipients"] = addrs[:50]
    if "alerts_enabled" in body:
        s["alerts_enabled"] = bool(body["alerts_enabled"])
    if "app_url" in body:
        s["app_url"] = str(body["app_url"]).strip()[:300]
    if body.get("press_email") in ("tracked", "agenda", "off"):
        s["press_email"] = body["press_email"]
    if body.get("report_scope") in ("onevoice", "watched"):
        s["report_scope"] = body["report_scope"]
    if "report_recipients" in body:
        addrs, bad = parse_addresses(body["report_recipients"])
        if bad:
            abort(400, f"These don't look like email addresses: {', '.join(bad)}")
        s["report_recipients"] = addrs[:50]
    for key in ("checkin_enabled", "checkin_backup", "movement_enabled", "hearing_email", "redline_email", "report_enabled"):
        if key in body:
            s[key] = bool(body[key])
    if "movement_categories" in body:
        valid = dict(ok.ACTION_CATEGORIES)
        s["movement_categories"] = [k for k in body["movement_categories"] if k in valid]
    meta_set("settings", s)
    return api_settings()


EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def parse_addresses(raw) -> tuple[list[str], list[str]]:
    """Split addresses on new lines, commas, semicolons or spaces; return (good, bad)."""
    items = raw if isinstance(raw, list) else [raw or ""]
    addrs = [a.strip() for chunk in items for a in re.split(r"[\s,;]+", str(chunk)) if a.strip()]
    good = list(dict.fromkeys(a for a in addrs if EMAIL_RE.match(a)))
    return good, [a for a in addrs if not EMAIL_RE.match(a)]


@app.post("/api/settings/test-email")
def api_test_email():
    st = settings()
    body = request.get_json(silent=True) or {}
    if "to" in body:  # send to what's typed in the box, saved or not
        to, bad = parse_addresses(body["to"])
        if bad:
            return jsonify(error=f"These don't look like email addresses: {', '.join(bad)}"), 400
        if not to:
            return jsonify(error="Type at least one email address in the box first."), 400
    else:
        to = st["recipients"]
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
        ov.send_email(to, "[Test] " + subject, text, html_body)
    except Exception as e:  # noqa: BLE001 - show the reason to the user
        return jsonify(error=f"The test email was not sent: {e}"), 400
    return jsonify(ok=True, to=to, unsaved=[a for a in to if a not in st["recipients"]])


@app.get("/api/export.csv")
def api_export():
    s = want_session()
    pri = {p["id"]: p for p in get_priorities()}
    with db() as c:
        rows = c.execute(TRACKED_SQL + " WHERE t.session=? ORDER BY b.bill_type, b.bill_num", (s,)).fetchall()
    rows = with_agenda(s, [bill_row(r) for r in rows])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Bill", "Author", "Summary", "Title", "OneVoice priorities", "Status", "Last action", "Last action date", "Position", "Why",
                "Priority", "Labels", "Notes", "Link"])
    for d in rows:
        tags = "; ".join(f"{pri[a['id']]['task_force']} #{pri[a['id']]['number']}" for a in d["agenda"] if a["id"] in pri)
        w.writerow([d["display"], d.get("author") or "", d.get("summary") or "", d["title"], tags, d["stage_label"], d["last_action"], d["last_action_date"] or "",
                    d["position"], d.get("reason") or "", d["priority"], d["labels"], d["notes"], d["url"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=onevoice-tracked-bills-{s}.csv"})


# ----------------------------------------------------- "what changed?" redlines


def bill_keywords(session: str, bill: str) -> list[str]:
    """Keywords of the OneVoice priorities a bill matches (for 'now mentions / no longer mentions')."""
    pri = {p["id"]: p for p in get_priorities()}
    with db() as c:
        ids = [r["priority_id"] for r in c.execute("SELECT priority_id FROM matches WHERE session=? AND bill=?", (session, bill))]
    return sorted({k for i in ids if i in pri for k in pri[i]["keywords"]})


def record_redline(session, bill, old_version, old_url, old_text, new_version, new_url, new_text) -> None:
    d = rl.diff(old_text, new_text)
    if not d["changes"]:
        return
    kc = rl.keyword_changes(old_text, new_text, bill_keywords(session, bill))
    d["changes"] = d["changes"][:80]
    watched = bill in watched_bills(session)
    with db() as c:
        c.execute("""INSERT INTO redlines(session,bill,old_version,old_url,new_version,new_url,old_body,headline,diff,keywords,at,emailed)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (session, bill, old_version, old_url, new_version, new_url, zlib.compress(old_text.encode("utf-8")),
                   rl.headline(d), json.dumps(d), json.dumps(kc), now_iso(), 0 if watched else 2))
    log_event("redline", True, bill=bill, watched=watched)


def send_redline_emails() -> int:
    st = settings()
    with db() as c:
        rows = c.execute("""SELECT r.*, b.title FROM redlines r LEFT JOIN bills b ON b.session=r.session AND b.bill=r.bill
                            WHERE r.emailed=0 ORDER BY r.id""").fetchall()
    sent = 0
    for r in rows:
        d, kc = json.loads(r["diff"]), json.loads(r["keywords"] or "{}")
        item = {"session": r["session"], "bill": r["bill"], "display": ok.display_bill(r["bill"]), "title": r["title"] or "",
                "url": ok.bill_url(r["bill"], r["session"]), "link": r["new_url"], "old_version": r["old_version"] or "Earlier version",
                "new_version": r["new_version"] or "New version", "headline": r["headline"],
                "changes_html": rl.changes_html(d), "changes_text": rl.changes_text(d),
                "gained": kc.get("gained", []), "lost": kc.get("lost", [])}
        delivered = _try_send(st, *ov.render_redline(item, st["app_url"])) if st.get("redline_email", True) else False
        pushed = send_push("changed", f"{item['display']} was rewritten", f"{item['new_version']}: {r['headline']}"[:180],
                           f"/#bill-{r['session']}-{r['bill']}", tag=f"changed-{r['bill']}")
        if delivered or pushed or not ov.email_configured() or not st.get("redline_email", True):
            with db() as c:
                c.execute("UPDATE redlines SET emailed=1 WHERE id=?", (r["id"],))
            sent += 1
    return sent


def check_watched_texts(progress=None) -> int:
    """Download the newest text of every tracked or Relevant bill whose version changed (redlines follow)."""
    n = 0
    watch = watched_bills(DEFAULT_SESSION)
    for i, bill in enumerate(sorted(watch), 1):
        if progress:
            progress(i, len(watch), ok.display_bill(bill) + " text")
        try:
            get_text(DEFAULT_SESSION, bill)
            n += 1
        except Exception:  # noqa: BLE001 - try again on the next run
            continue
    send_redline_emails()
    return n


# ------------------------------------------------------------ session scoreboard


def onevoice_bills(session: str) -> dict[str, list[str]]:
    """{bill: [priority ids]} for agenda matches not marked Not relevant."""
    out: dict[str, list[str]] = {}
    with db() as c:
        for r in c.execute("""SELECT m.bill, m.priority_id FROM matches m LEFT JOIN reviews rv ON rv.session=m.session
                              AND rv.bill=m.bill AND rv.priority_id=m.priority_id
                              WHERE m.session=? AND COALESCE(rv.verdict,'')!='not_relevant'""", (session,)):
            out.setdefault(r["bill"], []).append(r["priority_id"])
    return out


def scoreboard(session: str) -> dict:
    pri = {p["id"]: p for p in get_priorities()}
    ovb = onevoice_bills(session)
    with db() as c:
        bills = {r["bill"]: dict(r) for r in c.execute(
            "SELECT bill, title, stage, last_action, last_action_date, author FROM bills WHERE session=?", (session,))}
        tracked = {r["bill"]: dict(r) for r in c.execute("SELECT bill, position, reason FROM tracked WHERE session=?", (session,))}
        relevant = {r["bill"] for r in c.execute("SELECT DISTINCT bill FROM reviews WHERE session=? AND verdict='relevant'", (session,))}
    closed = ok.session_closed(session)
    tf_order = list(dict.fromkeys(p["task_force"] for p in pri.values()))
    by_tf = {tf: set() for tf in tf_order}
    for b, ids in ovb.items():
        for i in ids:
            if i in pri:
                by_tf[pri[i]["task_force"]].add(b)
    rank = {k: i for i, (k, _) in enumerate(ok.STAGES)}
    reached = [0] * len(ok.STAGES)
    for b in ovb:
        st = (bills.get(b) or {}).get("stage") or "introduced"
        r = rank.get(st, rank["governor"] if st == "vetoed" else 0)
        for k in range(r + 1):
            reached[k] += 1
    positions = {p: 0 for p in ("support", "neutral", "monitor", "oppose")}
    for t in tracked.values():
        if t["position"] in positions:
            positions[t["position"]] += 1
    wins, losses = [], []
    for b, t in tracked.items():
        st = (bills.get(b) or {}).get("stage") or "unknown"
        row = {"bill": b, "display": ok.display_bill(b), "title": (bills.get(b) or {}).get("title") or "",
               "position": t["position"], "stage_label": ok.STAGE_LABEL.get(st, st), "onevoice": b in ovb}
        if t["position"] == "support" and st == "law":
            wins.append({**row, "why": "Supported and signed into law"})
        elif t["position"] == "oppose" and (st in ("vetoed", "failed") or (closed and st != "law")):
            wins.append({**row, "why": "Opposed and stopped"})
        elif t["position"] == "oppose" and st == "law":
            losses.append({**row, "why": "Opposed but became law"})
        elif t["position"] == "support" and (st in ("vetoed", "failed") or (closed and st != "law")):
            losses.append({**row, "why": "Supported but did not pass"})
    supported = [b for b, t in tracked.items() if t["position"] == "support"]
    enacted_support = sum(1 for b in supported if (bills.get(b) or {}).get("stage") == "law")
    return {
        "session": session, "session_name": session_name(session), "closed": closed,
        "kpis": {"onevoice": len(ovb), "relevant": len(relevant), "tracked": len(tracked),
                 "with_position": sum(1 for t in tracked.values() if t["position"] != "monitor"),
                 "enacted": sum(1 for b in ovb if (bills.get(b) or {}).get("stage") == "law"),
                 "supported": len(supported), "supported_enacted": enacted_support,
                 "win_rate": round(100 * enacted_support / len(supported)) if supported else None},
        "by_task_force": [{"task_force": tf, "count": len(by_tf[tf])} for tf in tf_order],
        "funnel": [{"stage": k, "label": lbl, "count": reached[i]} for i, (k, lbl) in enumerate(ok.STAGES)],
        "positions": [{"position": p, "count": n} for p, n in positions.items()],
        "wins": sorted(wins, key=lambda w: w["bill"]), "losses": sorted(losses, key=lambda w: w["bill"]),
    }


# ------------------------------------------------------ calendar feed (Outlook)


def ical_token(new: bool = False) -> str:
    tok = meta_get("ical_token")
    if new or not tok:
        tok = secrets.token_urlsafe(18)
        meta_set("ical_token", tok)
    return tok


def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _ics_fold(line: str) -> str:
    out, b = [], line.encode("utf-8")
    while len(b) > 74:
        cut = 74
        while cut > 0 and (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b" " + b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/Chicago
BEGIN:DAYLIGHT
TZOFFSETFROM:-0600
TZOFFSETTO:-0500
TZNAME:CDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0500
TZOFFSETTO:-0600
TZNAME:CST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


def meeting_end(start: datetime, time_text: str) -> datetime:
    m = re.search(r"-\s*(\d{1,2}:\d{2}\s*[AP]M)", time_text or "", re.I)
    if m:
        try:
            t = datetime.strptime(m.group(1).upper().replace(" ", ""), "%I:%M%p")
            end = start.replace(hour=t.hour, minute=t.minute)
            if end > start:
                return end
        except ValueError:
            pass
    return start + timedelta(hours=1)


def build_ics(session: str, everything: bool) -> str:
    watch = watched_bills(session)
    ovb = onevoice_bills(session)
    lo = (datetime.now(CENTRAL) - timedelta(days=60)).replace(tzinfo=None).isoformat()
    with db() as c:
        meetings = [dict(r) for r in c.execute("SELECT * FROM meetings WHERE start>=? ORDER BY start", (lo,))]
        dls = [dict(r) for r in c.execute("SELECT date, label FROM deadlines WHERE date>=?", (lo[:10],))]
        titles = {r["bill"]: r["title"] for r in c.execute("SELECT bill, title FROM bills WHERE session=?", (session,))}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Tulsa Regional Chamber//OneVoice Tracker//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:" + ("OneVoice: all committee meetings" if everything else "OneVoice hearings"),
             "X-WR-TIMEZONE:America/Chicago", "X-PUBLISHED-TTL:PT1H", "REFRESH-INTERVAL;VALUE=DURATION:PT1H"] + VTIMEZONE.split("\n")
    for m in meetings:
        if not m["start"]:
            continue
        bills = json.loads(m["bills"] or "[]")
        mine = [b for b in bills if b in watch or b in ovb]
        if not everything and not mine:
            continue
        start = datetime.fromisoformat(m["start"])
        end = meeting_end(start, m["time_text"])
        cancelled = bool(re.search(r"cancel", f"{m['status']} {m['title']}", re.I))
        summary = f"{m['chamber']} {m['committee']}"
        if mine:
            summary += ": " + ", ".join(ok.display_bill(b) for b in mine[:6]) + (" +" if len(mine) > 6 else "")
        if cancelled:
            summary = "CANCELED: " + summary
        desc = []
        for b in bills:
            tag = " [tracked]" if watch.get(b) == "tracked" else " [Relevant]" if b in watch else " [OneVoice]" if b in ovb else ""
            desc.append(f"{ok.display_bill(b)}{tag}: {titles.get(b) or ''}".strip())
        if m["agenda_url"]:
            desc.append("")
            desc.append("Agenda: " + m["agenda_url"])
        if m["page_url"]:
            desc.append("Meeting page and video: " + m["page_url"])
        ev = ["BEGIN:VEVENT", f"UID:{m['key']}@onevoice-tracker", f"DTSTAMP:{stamp}",
              f"DTSTART;TZID=America/Chicago:{start.strftime('%Y%m%dT%H%M%S')}",
              f"DTEND;TZID=America/Chicago:{end.strftime('%Y%m%dT%H%M%S')}",
              "SUMMARY:" + _ics_escape(summary), "LOCATION:" + _ics_escape(m["location"] or "Oklahoma State Capitol"),
              "DESCRIPTION:" + _ics_escape("\n".join(desc) or "No agenda posted yet."),
              f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}", "TRANSP:OPAQUE"]
        if m["agenda_url"] or m["page_url"]:
            ev.append("URL:" + (m["agenda_url"] or m["page_url"]))
        lines += ev + ["END:VEVENT"]
    for d in dls:
        day = d["date"].replace("-", "")
        nxt = (datetime.fromisoformat(d["date"]) + timedelta(days=1)).strftime("%Y%m%d")
        uid = hashlib_hex(f"{d['date']}{d['label']}")
        lines += ["BEGIN:VEVENT", f"UID:deadline-{uid}@onevoice-tracker", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{day}",
                  f"DTEND;VALUE=DATE:{nxt}", "SUMMARY:" + _ics_escape("Legislature: " + d["label"]), "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(x) for x in lines) + "\r\n"


def hashlib_hex(s: str) -> str:
    import hashlib
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------- weekly Members report

REPORT_HOUR = int(os.environ.get("BILLTRACKER_REPORT_HOUR", "12"))  # Friday report email, Central time
EXTRA_TRACKS = ["Criminal Justice", "Tax Reform", "Education", "Other"]


def week_bounds(day: str | None) -> tuple[datetime, datetime]:
    """Monday 00:00 and next Monday for the week containing `day` (default: this week)."""
    try:
        d = datetime.fromisoformat(day) if day else datetime.now(CENTRAL).replace(tzinfo=None)
    except ValueError:
        d = datetime.now(CENTRAL).replace(tzinfo=None)
    mon = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return mon, mon + timedelta(days=7)


def report_rows(session: str, week: str | None, scope: str = "onevoice") -> tuple[datetime, list[dict]]:
    """Bills with action in the week: OneVoice matches (not marked Not relevant) and tracked bills,
    or only tracked and Relevant bills (scope='watched')."""
    mon, nxt = week_bounds(week)
    pri = get_priorities()
    order = {p["id"]: i for i, p in enumerate(pri)}
    tf = {p["id"]: p["task_force"] for p in pri}
    ovb = onevoice_bills(session)
    watch = watched_bills(session)
    with db() as c:
        tracks = {r["bill"]: r["track"] for r in c.execute("SELECT bill, track FROM tracked WHERE session=?", (session,))}
        rows = c.execute("""SELECT b.bill, b.bill_num, b.title, b.last_action, b.last_action_date, b.moved_at, b.author, s.summary, d.data AS detail
                            FROM bills b LEFT JOIN summaries s ON s.session=b.session AND s.bill=b.bill
                            LEFT JOIN details d ON d.session=b.session AND d.bill=b.bill
                            WHERE b.session=? AND (b.last_action_date>=? OR b.moved_at>=?)""",
                         (session, mon.date().isoformat(), (mon - timedelta(days=1)).date().isoformat())).fetchall()
    lo, hi = mon.date().isoformat(), nxt.date().isoformat()
    out = []
    for r in rows:
        b = r["bill"]
        if not (b in watch or b in tracks if scope == "watched" else b in ovb or b in tracks):
            continue
        when, what = r["last_action_date"], r["last_action"]
        if not (lo <= (when or "") < hi):
            # moved again since: use the bill's saved history to find its last action within the week
            acts = [a for a in (json.loads(r["detail"]).get("actions", []) if r["detail"] else []) if lo <= (a.get("date") or "") < hi]
            if not acts:
                moved = r["moved_at"] and datetime.fromisoformat(r["moved_at"]).astimezone(CENTRAL).date().isoformat()
                if not moved or not (lo <= moved < hi) or (when or "") > moved:
                    continue
                acts = [{"date": moved, "action": what}]  # the index saw it move this week; dates firm up when the bill is re-read
            last = sorted(acts, key=lambda a: a["date"])[-1]
            when, what = last["date"], last["action"]
        ids = sorted(ovb.get(b, []), key=lambda i: order.get(i, 99))
        track = (tracks.get(b) or "").strip() or (tf.get(ids[0]) if ids else "") or "Other"
        out.append({"bill": b, "display": ok.display_bill(b), "num": r["bill_num"] or 0, "track": track,
                    "summary": r["summary"] or r["title"] or "", "date": when, "last_action": what or "",
                    "author": r["author"] or "", "url": ok.bill_url(b, session)})
    out.sort(key=lambda x: (-int(x["date"].replace("-", "")), x["track"], x["num"]))
    return mon, out


def prepare_report(session: str, week: str | None, progress=None) -> dict:
    """Re-read every bill on the week's report from oklegislature.gov so its last action and date are exact."""
    _, rows = report_rows(session, week, "onevoice")
    for i, r in enumerate(rows, 1):
        if progress:
            progress(i, len(rows), r["display"])
        try:
            get_detail(session, r["bill"], refresh=True)
        except RuntimeError:
            continue
    meta_set("report_prepared", {"at": now_iso(), "week": week_bounds(week)[0].date().isoformat(), "count": len(rows)})
    return {"bills": len(rows)}


def report_label(mon: datetime) -> str:
    return f"Week of {mon.month}/{mon.day}/{mon.year}"


def report_filename(mon: datetime) -> str:
    return f"Bill_Track_Week_of_{mon.month}.{mon.day}.xlsx"


def report_xlsx(rows: list[dict]) -> bytes:
    """Same layout as the team's weekly sheet: Track, Bill Information, AI Summary, Last Action Date."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    wb = Workbook()
    ws = wb.active
    ws.title = "Bill Track"
    head = ["Track", "Bill Information", "AI Summary", "Last Action Date"]
    ws.append(head)
    thin = Side(style="thin", color="D0D5CC")
    for i, _ in enumerate(head, 1):
        cell = ws.cell(1, i)
        cell.font = Font(bold=True, size=12)
        cell.fill = PatternFill("solid", fgColor="FFFFFF")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.border = Border(bottom=Side(style="medium", color="5A9A28"))
    for r in rows:
        ws.append([r["track"], r["display"], r["summary"], datetime.fromisoformat(r["date"])])
        n = ws.max_row
        link = ws.cell(n, 2)
        link.hyperlink = r["url"]
        link.font = Font(color="0563C1", underline="single")
        ws.cell(n, 4).number_format = "mm-dd-yy"
        for col in range(1, 5):
            c = ws.cell(n, col)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            c.border = Border(bottom=thin)
    for col, w in zip("ABCD", (41.27, 22.18, 78.82, 21.45)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:D{max(ws.max_row, 1)}"
    ws.print_title_rows = "1:1"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def send_weekly_report(week: str | None = None, to: list[str] | None = None, force: bool = False) -> dict:
    st = settings()
    mon, rows = report_rows(DEFAULT_SESSION, week, st.get("report_scope", "onevoice"))
    if not rows and not force:
        return {"sent": False, "count": 0, "reason": "No bills had action this week."}
    recips = to or st.get("report_recipients") or st["recipients"]
    if not recips or not ov.email_configured():
        return {"sent": False, "count": len(rows), "reason": "Email isn't set up, or there is no one to send to."}
    subject, text, html_body = ov.render_weekly_report(report_label(mon), len(rows), st["app_url"])
    att = [(report_filename(mon), report_xlsx(rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")]
    try:
        ov.send_email(recips, subject, text, html_body, attachments=att)
    except Exception as e:  # noqa: BLE001
        log_event("weekly_report", False, str(e))
        return {"sent": False, "count": len(rows), "reason": f"The email didn't send: {e}"}
    log_event("weekly_report", True, count=len(rows))
    return {"sent": True, "count": len(rows), "to": recips}


def weekly_report_due() -> None:
    """Fridays at REPORT_HOUR Central: email the week's report (only when bills moved)."""
    now_local = datetime.now(CENTRAL)
    if now_local.weekday() != 4 or now_local.hour < REPORT_HOUR or not settings().get("report_enabled", True):
        return
    mon, _ = week_bounds(None)
    if meta_get("last_weekly_report") == mon.date().isoformat():
        return
    meta_set("last_weekly_report", mon.date().isoformat())
    prepare_report(DEFAULT_SESSION, None)
    send_weekly_report()


# ---------------------------------------------------------------- 2027 goals

GOAL_OWNER = "Katie Henke"
GOALS = [
    {"n": 1, "title": "Advance the OneVoice agenda", "text": "Advance OneVoice legislative priorities at the state and federal levels."},
    {"n": 2, "title": "Strengthen relationships with policy leaders", "text": "Grow face-to-face engagement with elected officials and policy leaders at every level of government."},
    {"n": 3, "title": "Grow the Chamber's political action committees", "text": "Grow donors and board engagement for OklahomaBizPac and TulsaBizPac and prepare for 2028."},
]
ACTIVITY_TYPES = {
    "roundtable": "Policy briefing or roundtable",
    "coalition": "Coalition update or call to action",
    "federal": "Federal advocacy initiative (e.g., DC Fly-In)",
    "ballot": "Ballot initiative analysis",
    "quarterly_report": "Quarterly policy progress report",
    "pac_board": "PAC board meeting",
    "forum": "PAC forum or candidate event",
    "pac_review": "PAC fundraising goals review",
}
PACS = ["OklahomaBizPac", "TulsaBizPac"]
LEVELS = {"local": "Municipal / county", "state": "State", "federal": "Federal", "other": "Other policy leader"}
DEFAULT_TARGETS = {
    "bills_passed": 10, "coverage": 100, "coalition": 4, "federal": 2, "ballot_designated": 0, "quarterly_report": 4,
    "contacts": 150, "contacts_local": 45, "contacts_state": 75, "contacts_federal": 30, "distinct": 50, "roundtable": 6,
    "donors": 75, "pac_board": 4, "attendance": 80, "forum": 2,
}
QUARTERLY = {"contacts": [40, 85, 120, 150], "donors": [15, 35, 55, 75]}  # cumulative by quarter end
MILESTONES = [
    ("g1q1a", 1, 1, "Finalize the OneVoice agenda"), ("g1q1b", 1, 1, "Bill tracking and monitoring in place"),
    ("g1q1c", 1, 1, "Launch state advocacy for the session"),
    ("g1q2a", 1, 2, "Complete session advocacy"), ("g1q2b", 1, 2, "Document legislative outcomes"),
    ("g1q2c", 1, 2, "Advance federal priorities (DC Fly-In)"), ("g1q2d", 1, 2, "Assess local initiatives"),
    ("g1q3a", 1, 3, "Prepare the next OneVoice cycle"), ("g1q3b", 1, 3, "Interim engagement with legislators"),
    ("g1q3c", 1, 3, "Develop positions on emerging issues"),
    ("g1q4a", 1, 4, "Finalize 2028 OneVoice priorities"), ("g1q4b", 1, 4, "Annual report"), ("g1q4c", 1, 4, "Set next year's strategy"),
    ("g2q1", 2, 1, "Q1 contact review"), ("g2q2", 2, 2, "Q2 contact review"), ("g2q3", 2, 3, "Q3 contact review"), ("g2q4", 2, 4, "Q4 contact review"),
    ("g3q1", 3, 1, "Q1 PAC fundraising review"), ("g3q2", 3, 2, "Q2 PAC fundraising review"),
    ("g3plan", 3, 3, "2028 election readiness plan (due Sept. 30)"), ("g3q3", 3, 3, "Q3 PAC fundraising review"),
    ("g3q4", 3, 4, "Q4 PAC fundraising review"),
]
GOAL_YEAR = int(os.environ.get("BILLTRACKER_GOAL_YEAR", "2027"))


def goal_targets() -> dict:
    return {**DEFAULT_TARGETS, **(meta_get("goal_targets") or {})}


def year_fraction(year: int, quarterly: list[int] | None, target: float) -> float:
    """How much of `target` should be done by today, from quarterly targets or evenly through the year."""
    today = datetime.now(CENTRAL).date()
    start, end = datetime(year, 1, 1).date(), datetime(year + 1, 1, 1).date()
    if today < start:
        return 0
    if today >= end:
        return target
    if quarterly:
        q = (today.month - 1) // 3
        qs = datetime(year, q * 3 + 1, 1).date()
        qe = datetime(year + (q == 3), (q * 3 + 3) % 12 + 1, 1).date()
        prev = quarterly[q - 1] if q else 0
        return prev + (quarterly[q] - prev) * (today - qs).days / (qe - qs).days
    return target * (today - start).days / (end - start).days


def goal_entries(year: int, kind: str | None = None) -> list[dict]:
    with db() as c:
        q = "SELECT * FROM goal_log WHERE date>=? AND date<?" + (" AND kind=?" if kind else "") + " ORDER BY date DESC, id DESC"
        args = [f"{year}-01-01", f"{year + 1}-01-01"] + ([kind] if kind else [])
        rows = c.execute(q, args).fetchall()
    return [{"id": r["id"], "kind": r["kind"], "date": r["date"], **json.loads(r["data"] or "{}")} for r in rows]


def people_of(entry: dict) -> list[str]:
    return [p.strip() for p in re.split(r"[\n;]+", entry.get("people") or "") if p.strip()]


def goal_bills(session: str) -> list[dict]:
    """OneVoice-aligned bills that became law: matched (not marked Not relevant), supported or marked Relevant,
    plus any added by hand, minus any taken out by hand."""
    ovb = onevoice_bills(session)
    over = meta_get(f"goal_bills:{session}") or {}
    with db() as c:
        law = {r["bill"]: r["title"] for r in c.execute("SELECT bill, title FROM bills WHERE session=? AND stage='law'", (session,))}
        support = {r["bill"] for r in c.execute("SELECT bill FROM tracked WHERE session=? AND position='support'", (session,))}
        relevant = {r["bill"] for r in c.execute("SELECT DISTINCT bill FROM reviews WHERE session=? AND verdict='relevant'", (session,))}
    out = []
    for b, title in law.items():
        auto = b in ovb and (b in support or b in relevant)
        choice = over.get(b)
        if choice is True or (auto and choice is not False):
            out.append({"bill": b, "display": ok.display_bill(b), "title": title or "", "auto": auto, "added": choice is True and not auto})
    return sorted(out, key=lambda x: x["bill"])


def compute_goals(year: int) -> dict:
    t = goal_targets()
    session = f"{str(year)[2:]}00"
    entries = goal_entries(year)
    meets = [e for e in entries if e["kind"] == "meeting" and e.get("in_person", True)]
    donors = [e for e in entries if e["kind"] == "donor"]
    acts = [e for e in entries if e["kind"] == "activity"]
    contacts = sum(len(people_of(m)) or 1 for m in meets)
    by_level = {k: sum(len(people_of(m)) or 1 for m in meets if m.get("level") == k) for k in LEVELS}
    distinct = {p.lower() for m in meets for p in people_of(m)}
    nact = lambda typ: sum(1 for a in acts if a.get("type") == typ)  # noqa: E731
    board = {p: sum(1 for a in acts if a.get("type") == "pac_board" and a.get("pac") == p) for p in PACS}
    att = [float(a["attendance"]) for a in acts if a.get("type") == "pac_board" and str(a.get("attendance") or "").strip()]
    att_avg = round(sum(att) / len(att)) if att else None
    gb = goal_bills(session)
    with db() as c:
        relevant = {r["bill"] for r in c.execute("SELECT DISTINCT bill FROM reviews WHERE session=? AND verdict='relevant'", (session,))}
        tracked = {r["bill"] for r in c.execute("SELECT bill FROM tracked WHERE session=?", (session,))}
    coverage = round(100 * len(relevant & tracked) / len(relevant)) if relevant else None
    done = meta_get(f"goal_milestones:{year}") or {}

    def metric(mid, goal, label, value, target, headline=False, quarterly=None, unit="", note="", pace=True):
        exp = year_fraction(year, quarterly, target) if pace and target else None
        if target in (None, 0) or value is None:
            status = "info"
        elif value >= target:
            status = "met"
        elif exp is None:
            status = "open"
        elif exp == 0:
            status = "upcoming"
        elif value >= exp * 0.9:
            status = "on_track"
        else:
            status = "behind"
        return {"id": mid, "goal": goal, "label": label, "value": value, "target": target, "unit": unit, "headline": headline,
                "expected": round(exp, 1) if exp is not None else None, "status": status, "note": note,
                "quarterly": quarterly}

    m = [
        metric("bills_passed", 1, "OneVoice-aligned bills passed", len(gb), t["bills_passed"], True, pace=False,
               note="Bills that match the agenda, that you supported or marked Relevant, and that became law."),
        metric("coverage", 1, "Priority bills tracked", coverage, t["coverage"], unit="%", pace=False,
               note=f"{len(relevant & tracked)} of {len(relevant)} bills marked Relevant are on the tracking list." if relevant else "Mark bills Relevant to measure this."),
        metric("coalition", 1, "Coalition updates / calls to action", nact("coalition"), t["coalition"]),
        metric("federal", 1, "Federal advocacy initiatives", nact("federal"), t["federal"]),
        metric("ballot", 1, "Ballot initiative analyses", nact("ballot"), t["ballot_designated"] or None, pace=False,
               note="Set the number of designated initiatives under Edit targets." if not t["ballot_designated"] else ""),
        metric("quarterly_report", 1, "Quarterly advocacy reports", nact("quarterly_report"), t["quarterly_report"], True,
               quarterly=[1, 2, 3, 4]),
        metric("contacts", 2, "Face-to-face contacts", contacts, t["contacts"], True, QUARTERLY["contacts"]),
        metric("contacts_local", 2, "Municipal and county", by_level["local"], t["contacts_local"]),
        metric("contacts_state", 2, "State", by_level["state"], t["contacts_state"]),
        metric("contacts_federal", 2, "Federal", by_level["federal"], t["contacts_federal"]),
        metric("distinct", 2, "Distinct policy leaders", len(distinct), t["distinct"], True),
        metric("roundtable", 2, "Policy briefings and roundtables", nact("roundtable"), t["roundtable"], True),
        metric("donors", 3, "PAC donors, new or reactivated", len(donors), t["donors"], True, QUARTERLY["donors"],
               note=" · ".join(f"{p}: {sum(1 for d in donors if d.get('pac') == p)}" for p in PACS)),
        metric("pac_board_ok", 3, "OklahomaBizPac board meetings", board["OklahomaBizPac"], t["pac_board"]),
        metric("pac_board_tu", 3, "TulsaBizPac board meetings", board["TulsaBizPac"], t["pac_board"]),
        metric("attendance", 3, "Average PAC board attendance", att_avg, t["attendance"], unit="%", pace=False),
        metric("forum", 3, "PAC forums", nact("forum"), t["forum"]),
    ]
    today = datetime.now(CENTRAL).date()
    ms = []
    for mid, goal, q, label in MILESTONES:
        due = datetime(year, q * 3, 30 if q * 3 in (6, 9) else 31).date()
        if mid == "g3plan":
            due = datetime(year, 9, 30).date()
        ms.append({"id": mid, "goal": goal, "quarter": q, "label": label, "due": due.isoformat(),
                   "done": done.get(mid), "late": not done.get(mid) and today > due})
    return {"year": year, "owner": GOAL_OWNER, "goals": GOALS, "metrics": m, "milestones": ms, "targets": t,
            "bills": gb, "session": session, "activity_types": ACTIVITY_TYPES, "pacs": PACS, "levels": LEVELS,
            "counts": {"meetings": sum(1 for e in entries if e["kind"] == "meeting"), "donors": len(donors), "activities": len(acts)}}


def clean_entry(kind: str, body: dict) -> tuple[str, dict]:
    date = str(body.get("date") or "")[:10]
    try:
        datetime.fromisoformat(date)
    except ValueError:
        abort(400, "Pick a date.")
    s = lambda k, n=2000: str(body.get(k) or "").strip()[:n]  # noqa: E731
    if kind == "meeting":
        if not s("people") and not s("entity"):
            abort(400, "Add who you met with.")
        data = {"people": s("people", 1000), "entity": s("entity", 300), "level": body.get("level") if body.get("level") in LEVELS else "state",
                "staff": s("staff", 300), "issues": s("issues", 500), "overview": s("overview", 8000), "follow_up": s("follow_up", 2000),
                "in_person": bool(body.get("in_person", True)), "bills": [b for b in (ok.normalize_bill(x) for x in re.split(r"[,;\n]+", s("bills", 300)) if x.strip()) if b]}
    elif kind == "donor":
        if not s("name"):
            abort(400, "Add the donor's name.")
        data = {"name": s("name", 200), "pac": body.get("pac") if body.get("pac") in PACS else PACS[0],
                "status": "reactivated" if body.get("status") == "reactivated" else "new", "amount": s("amount", 30), "notes": s("notes", 1000)}
    elif kind == "activity":
        if body.get("type") not in ACTIVITY_TYPES:
            abort(400, "Pick what kind of activity this was.")
        data = {"type": body["type"], "title": s("title", 300), "pac": body.get("pac") if body.get("pac") in PACS else "",
                "attendance": s("attendance", 5), "notes": s("notes", 4000)}
    else:
        abort(400, "Unknown kind.")
    return date, data


def goals_xlsx(year: int) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    g = compute_goals(year)
    wb = Workbook()
    ws = wb.active
    ws.title = "Scorecard"
    ws.append([f"{year} Government Affairs goals", "", "", f"Owner: {g['owner']}", f"As of {datetime.now(CENTRAL):%b %-d, %Y}"])
    ws.append([])
    ws.append(["Goal", "Measure", "Result", "Target", "Status"])
    for m in g["metrics"]:
        ws.append([m["goal"], m["label"], "" if m["value"] is None else f"{m['value']}{m['unit']}",
                   "" if not m["target"] else f"{m['target']}{m['unit']}", m["status"].replace("_", " ")])
    ws.append([])
    ws.append(["Goal", "Milestone", "Quarter", "Due", "Done"])
    for x in g["milestones"]:
        ws.append([x["goal"], x["label"], f"Q{x['quarter']}", x["due"], (x["done"] or {}).get("at", "")[:10] if x["done"] else ""])
    sheets = [
        ("Meetings", "meeting", ["Date", "Who met", "Organization / office", "Level", "Chamber staff", "Issues", "Overview", "Follow-up", "In person", "Bills"],
         lambda e: [e["date"], e.get("people"), e.get("entity"), LEVELS.get(e.get("level"), ""), e.get("staff"), e.get("issues"),
                    e.get("overview"), e.get("follow_up"), "Yes" if e.get("in_person", True) else "No", ", ".join(ok.display_bill(b) for b in e.get("bills", []))]),
        ("PAC donors", "donor", ["Date", "Donor", "PAC", "New or reactivated", "Amount", "Notes"],
         lambda e: [e["date"], e.get("name"), e.get("pac"), e.get("status"), e.get("amount"), e.get("notes")]),
        ("Activities", "activity", ["Date", "Type", "Title", "PAC", "Board attendance %", "Notes"],
         lambda e: [e["date"], ACTIVITY_TYPES.get(e.get("type"), ""), e.get("title"), e.get("pac"), e.get("attendance"), e.get("notes")]),
    ]
    for title, kind, head, fn in sheets:
        s = wb.create_sheet(title)
        s.append(head)
        for e in reversed(goal_entries(year, kind)):
            s.append(fn(e))
        s.freeze_panes = "A2"
        for col in s.columns:
            s.column_dimensions[col[0].column_letter].width = 18
    for s in wb.worksheets:
        for cell in s[3 if s.title == "Scorecard" else 1]:
            cell.font = Font(bold=True)
        for row in s.iter_rows():
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["B"].width = 44
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------- routes for the features above


@app.get("/api/scoreboard")
def api_scoreboard():
    return jsonify(scoreboard(want_session()))


@app.get("/cal/<token>.ics")
def ical_feed(token):
    if not hmac.compare_digest(token, ical_token()):
        abort(404)
    body = build_ics(DEFAULT_SESSION, request.args.get("all") == "1")
    return Response(body, mimetype="text/calendar", headers={"Cache-Control": "no-cache",
                                                             "Content-Disposition": "inline; filename=onevoice-hearings.ics"})


def ical_urls(new: bool = False) -> dict:
    base = (settings()["app_url"] or request.host_url).rstrip("/")
    tok = ical_token(new)
    url = f"{base}/cal/{tok}.ics"
    return {"hearings": url, "all": url + "?all=1",
            "webcal": url.replace("https://", "webcal://").replace("http://", "webcal://")}


@app.get("/api/ical")
def api_ical():
    return jsonify(ical_urls())


@app.post("/api/ical/reset")
def api_ical_reset():
    return jsonify(ical_urls(new=True))


@app.get("/api/bill/<session>/<bill>/compare")
def api_compare(session, bill):
    """Redline between any two posted versions of a bill."""
    bill = want_bill(bill)
    session = session.upper()
    d = get_detail(session, bill)
    if not d:
        abort(404, "Bill not found.")
    versions = {v["url"]: v["name"] for v in d.get("documents", []) if v.get("group", "").startswith("Versions of")}
    a, b = request.args.get("a", ""), request.args.get("b", "")
    if a not in versions or b not in versions:
        abort(400, "Pick two versions of this bill.")
    try:
        ta, tb = sm.bill_text(a), sm.bill_text(b)
    except Exception as e:  # noqa: BLE001
        abort(502, f"Couldn't download the bill text: {e}")
    r = rl.diff(ta, tb)
    kc = rl.keyword_changes(ta, tb, bill_keywords(session, bill))
    return jsonify(old=versions[a], new=versions[b], headline=rl.headline(r), html=rl.changes_html(r, limit=60),
                   count=len(r["changes"]), gained=kc["gained"], lost=kc["lost"])


@app.get("/api/redline/<int:rid>")
def api_redline(rid):
    with db() as c:
        r = c.execute("SELECT * FROM redlines WHERE id=?", (rid,)).fetchone()
    if not r:
        abort(404, "Not found.")
    d, kc = json.loads(r["diff"]), json.loads(r["keywords"] or "{}")
    return jsonify(old=r["old_version"], new=r["new_version"], headline=r["headline"], html=rl.changes_html(d, limit=80),
                   count=len(d["changes"]), gained=kc.get("gained", []), lost=kc.get("lost", []), at=r["at"], link=r["new_url"])


@app.get("/api/report")
def api_report():
    s = want_session()
    scope = request.args.get("scope") or settings().get("report_scope", "onevoice")
    mon, rows = report_rows(s, request.args.get("week"), scope)
    return jsonify(week=mon.date().isoformat(), label=report_label(mon), filename=report_filename(mon), scope=scope, rows=rows,
                   last_sent=meta_get("last_weekly_report"))


@app.post("/api/report/prepare")
def api_report_prepare():
    body = request.get_json(force=True) or {}
    s = (body.get("session") or DEFAULT_SESSION).upper()
    if not start_job("report", s, lambda p: prepare_report(s, body.get("week"), p)):
        return jsonify(error="Another update is running. Try again in a minute."), 409
    return jsonify(ok=True)


@app.get("/api/report.xlsx")
def api_report_xlsx():
    s = want_session()
    mon, rows = report_rows(s, request.args.get("week"), request.args.get("scope") or settings().get("report_scope", "onevoice"))
    return Response(report_xlsx(rows), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={report_filename(mon)}"})


@app.post("/api/report/send")
def api_report_send():
    body = request.get_json(force=True) or {}
    to = None
    if body.get("to"):
        to, bad = parse_addresses(body["to"])
        if bad:
            abort(400, f"These don't look like email addresses: {', '.join(bad)}")
    r = send_weekly_report(body.get("week"), to, force=True)
    if not r["sent"]:
        return jsonify(error=r["reason"]), 400
    return jsonify(r)


def want_year() -> int:
    try:
        y = int(request.args.get("year") or GOAL_YEAR)
    except ValueError:
        abort(400, "Invalid year")
    return max(2020, min(2100, y))


@app.get("/api/goals")
def api_goals():
    return jsonify(compute_goals(want_year()))


@app.get("/api/goals/log")
def api_goals_log():
    kind = request.args.get("kind")
    rows = goal_entries(want_year(), kind if kind in ("meeting", "donor", "activity") else None)
    q = (request.args.get("q") or "").strip().lower()
    if q:
        rows = [r for r in rows if q in json.dumps(r).lower()]
    for r in rows:
        r["bills_display"] = [ok.display_bill(b) for b in r.get("bills", [])]
    return jsonify(rows)


@app.post("/api/goals/log")
def api_goals_add():
    body = request.get_json(force=True) or {}
    kind = body.get("kind")
    date, data = clean_entry(kind, body)
    with db() as c:
        cur = c.execute("INSERT INTO goal_log(kind,date,data,created_at) VALUES(?,?,?,?)", (kind, date, json.dumps(data), now_iso()))
    return jsonify(id=cur.lastrowid, kind=kind, date=date, **data)


@app.put("/api/goals/log/<int:eid>")
def api_goals_edit(eid):
    body = request.get_json(force=True) or {}
    with db() as c:
        r = c.execute("SELECT kind FROM goal_log WHERE id=?", (eid,)).fetchone()
    if not r:
        abort(404, "That entry was deleted.")
    date, data = clean_entry(r["kind"], body)
    with db() as c:
        c.execute("UPDATE goal_log SET date=?, data=?, updated_at=? WHERE id=?", (date, json.dumps(data), now_iso(), eid))
    return jsonify(id=eid, kind=r["kind"], date=date, **data)


@app.delete("/api/goals/log/<int:eid>")
def api_goals_delete(eid):
    with db() as c:
        c.execute("DELETE FROM goal_log WHERE id=?", (eid,))
    return jsonify(ok=True)


@app.post("/api/goals/milestone")
def api_goals_milestone():
    body = request.get_json(force=True) or {}
    year = int(body.get("year") or GOAL_YEAR)
    if body.get("id") not in {m[0] for m in MILESTONES}:
        abort(400, "Unknown milestone.")
    done = meta_get(f"goal_milestones:{year}") or {}
    if body.get("done"):
        done[body["id"]] = {"at": now_iso()}
    else:
        done.pop(body["id"], None)
    meta_set(f"goal_milestones:{year}", done)
    return jsonify(compute_goals(year))


@app.put("/api/goals/targets")
def api_goals_targets():
    body = request.get_json(force=True) or {}
    cur = meta_get("goal_targets") or {}
    for k, v in body.items():
        if k in DEFAULT_TARGETS:
            try:
                cur[k] = max(0, int(v))
            except (TypeError, ValueError):
                abort(400, f"'{v}' isn't a number.")
    meta_set("goal_targets", cur)
    return jsonify(compute_goals(want_year()))


@app.post("/api/goals/bill")
def api_goals_bill():
    """Count or don't count a bill toward 'OneVoice-aligned bills passed'."""
    body = request.get_json(force=True) or {}
    year = int(body.get("year") or GOAL_YEAR)
    session = f"{str(year)[2:]}00"
    bill = want_bill(body.get("bill") or "")
    over = meta_get(f"goal_bills:{session}") or {}
    if body.get("count") is None:
        over.pop(bill, None)
    else:
        over[bill] = bool(body["count"])
    meta_set(f"goal_bills:{session}", over)
    return jsonify(compute_goals(year))


@app.get("/api/goals/export.xlsx")
def api_goals_export():
    y = want_year()
    return Response(goals_xlsx(y), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=Government_Affairs_Goals_{y}.xlsx"})


# --------------------------------------------------------- legislator directory

TULSA_REGION = ["Tulsa", "Rogers", "Wagoner", "Osage", "Creek", "Okmulgee", "Pawnee"]
LEG_FIELDS = ("chamber", "slug", "first", "last", "name", "district", "party", "photo", "email", "contact_url", "phone", "office",
              "assistant", "hometown", "term_limited", "leadership", "page_url")


def refresh_legislators(progress=None) -> dict:
    """Read every member's page on okhouse.gov and oksenate.gov (contact, committees, district)."""
    roster = [("House", m) for m in so.house_members()] + [("Senate", m) for m in so.senate_members()]
    if len(roster) < 100:
        raise RuntimeError(f"Only {len(roster)} members found; the House or Senate site may have changed.")
    seen, failed = [], 0
    for i, (chamber, m) in enumerate(roster, 1):
        if progress:
            progress(i, len(roster), m["slug"])
        try:
            d = (so.house_member if chamber == "House" else so.senate_member)(m["slug"])
        except Exception:  # noqa: BLE001 - keep the saved copy of that member
            failed += 1
            seen.append(f"{chamber[0]}:{m['slug'].lower()}")
            continue
        d["leadership"] = m.get("leadership", "")
        key = f"{chamber[0]}:{m['slug'].lower()}"
        seen.append(key)
        with db() as c:
            c.execute(f"""INSERT OR REPLACE INTO legislators(key,{','.join(LEG_FIELDS)},committees,counties,cities,zips,updated_at)
                          VALUES(?,{','.join('?' * len(LEG_FIELDS))},?,?,?,?,?)""",
                      [key] + [d.get(f) for f in LEG_FIELDS] +
                      [json.dumps(d.get(k, [])) for k in ("committees", "counties", "cities", "zips")] + [now_iso()])
    with db() as c:  # members who left office
        c.execute(f"DELETE FROM legislators WHERE key NOT IN ({','.join('?' * len(seen))})", seen)
    meta_set("legislators", {"at": now_iso(), "count": len(seen), "failed": failed})
    return {"members": len(seen), "failed": failed}


def slug_from_url(url: str) -> str | None:
    """'https://www.okhouse.gov/representatives/Jacob-rosecrants' -> 'H:jacob-rosecrants'."""
    m = re.search(r"(okhouse\.gov/representatives|oksenate\.gov/senators)/([^/?#]+)", url or "", re.I)
    if not m:
        return None
    return ("H:" if "okhouse" in m.group(1).lower() else "S:") + m.group(2).lower()


def authored_bills(session: str) -> dict[str, list[tuple[str, str]]]:
    """{legislator key: [(bill, 'primary'|'principal')]} from saved bill pages."""
    out: dict[str, list[tuple[str, str]]] = {}
    with db() as c:
        rows = c.execute("SELECT bill, data FROM details WHERE session=?", (session,)).fetchall()
    for r in rows:
        try:
            d = json.loads(r["data"])
        except ValueError:
            continue
        for field, role in (("author_url", "primary"), ("other_author_url", "principal")):
            k = slug_from_url(d.get(field))
            if k:
                out.setdefault(k, []).append((r["bill"], role))
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower().replace("&", " and ")).strip()


def meetings_with(leg: dict, entries: list[dict]) -> list[dict]:
    """Logged meetings that name this legislator."""
    last, first = _norm(leg["last"]), _norm(leg["first"]).split(" ")[0] if leg["first"] else ""
    title = "rep" if leg["chamber"] == "House" else "sen"
    hits = []
    for e in entries:
        for line in people_of(e):
            n = _norm(line)
            if re.search(rf"\b{re.escape(last)}\b", n) and ((first and re.search(rf"\b{re.escape(first)}\b", n)) or re.match(rf"{title}\b", n)):
                hits.append(e)
                break
    return hits


def legislator_rows() -> list[dict]:
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM legislators ORDER BY last, first")]
    for r in rows:
        for k in ("committees", "counties", "cities", "zips"):
            r[k] = json.loads(r[k] or "[]")
    return rows


def legislator_counts(session: str, rows: list[dict]) -> None:
    ovb = onevoice_bills(session)
    auth = authored_bills(session)
    meetings = goal_log_all("meeting")
    for r in rows:
        mine = auth.get(r["key"], [])
        r["bills_count"] = len(mine)
        r["onevoice_count"] = sum(1 for b, _ in mine if b in ovb)
        r["meetings_count"] = len(meetings_with(r, meetings))
        r["tulsa"] = "Tulsa" in r["counties"]
        r["region"] = any(x in TULSA_REGION for x in r["counties"])


def goal_log_all(kind: str) -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM goal_log WHERE kind=? ORDER BY date DESC, id DESC", (kind,)).fetchall()
    return [{"id": r["id"], "kind": r["kind"], "date": r["date"], **json.loads(r["data"] or "{}")} for r in rows]


@app.get("/api/legislators")
def api_legislators():
    s = want_session()
    rows = legislator_rows()
    legislator_counts(s, rows)
    for r in rows:
        r.pop("zips", None)
    return jsonify(members=rows, updated=(meta_get("legislators") or {}).get("at"), region=TULSA_REGION)


@app.get("/api/legislators/<chamber>/<slug>")
def api_legislator(chamber, slug):
    s = want_session()
    key = f"{chamber[:1].upper()}:{slug.lower()}"
    rows = [r for r in legislator_rows() if r["key"] == key]
    if not rows:
        abort(404, "That legislator isn't in the directory. Refresh the directory from the Legislators screen.")
    leg = rows[0]
    ovb = onevoice_bills(s)
    pri = {p["id"]: p for p in get_priorities()}
    mine = authored_bills(s).get(key, [])
    with db() as c:
        info = {r["bill"]: dict(r) for r in c.execute(
            "SELECT b.bill, b.title, b.stage, b.last_action_date, t.position FROM bills b LEFT JOIN tracked t ON t.session=b.session AND t.bill=b.bill WHERE b.session=?", (s,))}
        upcoming = [dict(r) for r in c.execute("SELECT key, chamber, committee, start, time_text, location, agenda_url, bills FROM meetings WHERE start>=? ORDER BY start",
                                                (datetime.now(CENTRAL).replace(tzinfo=None).isoformat(),))]
    bills = []
    for b, role in mine:
        i = info.get(b, {})
        bills.append({"bill": b, "display": ok.display_bill(b), "role": role, "title": i.get("title") or "",
                      "stage_label": ok.STAGE_LABEL.get(i.get("stage") or "unknown", ""), "position": i.get("position"),
                      "onevoice": [f"{TF_SHORT_PY.get(pri[x]['task_force'], pri[x]['task_force'])} #{pri[x]['number']}" for x in ovb.get(b, []) if x in pri]})
    bills.sort(key=lambda x: (not x["onevoice"], x["role"] != "primary", x["bill"]))
    comms = {_norm(x) for x in leg["committees"]}
    hearings = [m for m in upcoming if m["chamber"] == leg["chamber"] and _norm(m["committee"]) in comms][:10]
    for m in hearings:
        m["bills"] = [ok.display_bill(b) for b in json.loads(m["bills"] or "[]")]
    meets = meetings_with(leg, goal_log_all("meeting"))
    for e in meets:
        e["bills_display"] = [ok.display_bill(b) for b in e.get("bills", [])]
    return jsonify(**leg, bills=bills, meetings=meets, hearings=hearings, onevoice_count=sum(1 for b in bills if b["onevoice"]))


@app.post("/api/legislators/refresh")
def api_legislators_refresh():
    if not start_job("legislators", None, refresh_legislators):
        return jsonify(error="Another update is running. Try again in a minute."), 409
    return jsonify(ok=True)


TF_SHORT_PY = {"Energy and Natural Resources": "Energy", "General Government and Legal Reform": "Gov't",
               "Health and Human Services": "Health", "Economic Development": "Econ Dev", "Tourism and Quality of Life": "Tourism",
               "Transportation, Infrastructure and Aerospace": "Transport", "Workforce and Talent Strategies": "Workforce"}


def fill_onevoice_details(session: str, progress=None, limit: int = 60) -> dict:
    """Read the bill page (authors, history) of OneVoice bills not read yet, so the directory knows who wrote them."""
    with db() as c:
        have = {r["bill"] for r in c.execute("SELECT bill FROM details WHERE session=?", (session,))}
    todo = [b for b in sorted(onevoice_bills(session)) if b not in have][:limit]
    for i, b in enumerate(todo, 1):
        if progress:
            progress(i, len(todo), ok.display_bill(b))
        try:
            get_detail(session, b)
        except RuntimeError:
            continue
    meta_set(f"ovdetails:{session}", {"at": now_iso(), "fetched": len(todo)})
    return {"fetched": len(todo)}


def onevoice_details_missing(session: str) -> int:
    with db() as c:
        have = {r["bill"] for r in c.execute("SELECT bill FROM details WHERE session=?", (session,))}
    return sum(1 for b in onevoice_bills(session) if b not in have)


# --------------------------------------------------------------------- main


def main(argv: list[str]) -> None:
    global FTS_TABLE
    init_db()
    FTS_TABLE = FTS_OK()
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
