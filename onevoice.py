"""Match bills to OneVoice agenda priorities and send email alerts."""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from functools import lru_cache
from pathlib import Path

AGENDA_SEED = Path(__file__).resolve().parent / "agenda.json"


def load_seed() -> dict:
    return json.loads(AGENDA_SEED.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ matching


@lru_cache(maxsize=4096)
def keyword_regex(keyword: str) -> re.Pattern:
    """Whole-word, case-insensitive pattern. A trailing * matches any ending."""
    kw = keyword.strip()
    wildcard = kw.endswith("*")
    kw = kw.rstrip("*").strip()
    body = r"\s+".join(re.escape(part) for part in kw.split())
    tail = r"\w*" if wildcard else r"(?:'?s)?"  # allow simple plurals/possessives
    return re.compile(rf"(?<![\w-]){body}{tail}(?![\w-])", re.IGNORECASE)


def match_text(text: str, keywords: list[str]) -> list[str]:
    hits = []
    for kw in keywords:
        if kw.strip() and keyword_regex(kw).search(text):
            hits.append(kw.strip())
    return hits


def match_bill(title: str, subjects: list[str], priorities: list[dict]) -> list[tuple[str, list[str]]]:
    """[(priority_id, [matched keywords])] for every enabled priority the bill matches."""
    text = " ".join([title or ""] + list(subjects or []))
    out = []
    for p in priorities:
        if not p.get("enabled", True):
            continue
        hits = match_text(text, p.get("keywords", []))
        if hits and not match_text(text, p.get("exclude", [])):
            out.append((p["id"], hits))
    return out


# --------------------------------------------------------------------- email


class EmailNotConfigured(RuntimeError):
    pass


def smtp_settings() -> dict:
    return {
        "host": os.environ.get("SMTP_HOST", ""),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "sender": os.environ.get("SMTP_FROM", "") or os.environ.get("SMTP_USER", ""),
        "security": os.environ.get("SMTP_SECURITY", "starttls").lower(),  # starttls | ssl | none
    }


def email_configured() -> bool:
    s = smtp_settings()
    return bool(s["host"] and s["sender"])


def send_email(to: list[str], subject: str, text: str, html_body: str) -> None:
    s = smtp_settings()
    if not (s["host"] and s["sender"]):
        raise EmailNotConfigured("Email is not set up. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_FROM on the server.")
    if not to:
        raise EmailNotConfigured("No alert recipients. Add at least one address in Settings.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s["sender"]
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    if s["security"] == "ssl":
        server = smtplib.SMTP_SSL(s["host"], s["port"], context=ssl.create_default_context(), timeout=30)
    else:
        server = smtplib.SMTP(s["host"], s["port"], timeout=30)
        if s["security"] == "starttls":
            server.starttls(context=ssl.create_default_context())
    try:
        if s["user"] and s["password"]:
            server.login(s["user"], s["password"])
        server.send_message(msg)
    finally:
        server.quit()


def policy_areas(item: dict) -> list[str]:
    """Distinct task-force names (the agenda's policy areas) a bill matches, in agenda order."""
    out: list[str] = []
    for p in item["priorities"]:
        if p["task_force"] not in out:
            out.append(p["task_force"])
    return out


def render_alert(item: dict, app_url: str, session_name: str) -> tuple[str, str, str]:
    """One email for one newly filed bill.

    item: {display, title, author, summary, url, last_action,
           priorities: [{task_force, number, title, keywords}]}
    """
    areas = policy_areas(item) or ["Agenda"]
    area_text = " & ".join(areas)
    subject = f"OneVoice Tracker: New {area_text} Bill Filed"
    author = item.get("author") or "Not listed yet"
    summary = item.get("summary") or ""
    matches = [f"{p['task_force']} #{p['number']}: {p['title']}" for p in item["priorities"]]

    lines = [
        f"A new bill matching the OneVoice {area_text} agenda was filed in the {session_name}.",
        "",
        f"Bill number: {item['display']}",
        f"Author: {author}",
        "Subject area: " + ("; ".join(matches) if matches else area_text),
        "",
        "What it does (AI summary):",
        summary or f"No AI summary available. Official title: {item['title']}",
        "",
        f"Official title: {item['title']}" if summary else "",
        f"Read the bill: {item['url']}",
    ]
    if app_url:
        lines.append(f"Open the tracker: {app_url}#agenda")
    lines += ["", "AI summaries can be wrong; read the bill before taking a position.",
              "You are getting this because your address is on the OneVoice Tracker alert list."]
    text = "\n".join(l for l in lines if l is not None)

    e = html.escape
    label = 'style="padding:8px 12px 8px 0;color:#5d6659;font-size:13px;white-space:nowrap;vertical-align:top;width:110px"'
    value = 'style="padding:8px 0;color:#1d241b;font-size:15px;vertical-align:top"'
    area_rows = "".join(
        f'<div><b style="color:#4f8a1e">{e(p["task_force"])} #{p["number"]}</b> &middot; {e(p["title"])}</div>'
        for p in item["priorities"]
    ) or e(area_text)
    summary_html = (
        f'<div style="font-size:15px;line-height:1.5;color:#1d241b">{e(summary)}</div>'
        if summary else
        '<div style="font-size:14px;color:#5d6659;font-style:italic">No AI summary available.</div>'
    )
    tracker_button = (
        f'<a href="{e(app_url)}#agenda" style="display:inline-block;background:#5a9a28;color:#fff;padding:10px 16px;'
        f'border-radius:6px;text-decoration:none;font-weight:600;margin:0 8px 8px 0">Open OneVoice Tracker</a>' if app_url else ""
    )
    html_body = f"""<!doctype html><html><body style="margin:0;background:#f4f6f2;padding:24px 12px;font-family:Arial,Helvetica,sans-serif">
<table role="presentation" width="100%" style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:8px;border-top:6px solid #5a9a28">
<tr><td style="padding:24px 28px">
<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#5a9a28;font-weight:700">OneVoice Tracker</div>
<h1 style="font-size:21px;color:#1d241b;margin:6px 0 4px">New {e(area_text)} Bill Filed</h1>
<p style="color:#4d574a;margin:0 0 14px;font-size:14px">Filed in the {e(session_name)}.</p>
<table role="presentation" width="100%" style="border-collapse:collapse;border-top:1px solid #e3e7df;border-bottom:1px solid #e3e7df">
<tr><td {label}>Bill number</td><td {value}><a href="{e(item['url'])}" style="font:700 16px Menlo,Consolas,monospace;color:#2f3a2c;text-decoration:none">{e(item['display'])}</a></td></tr>
<tr><td {label}>Author</td><td {value}>{e(author)}</td></tr>
<tr><td {label}>Subject area</td><td {value}>{area_rows}</td></tr>
</table>
<h2 style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:#5d6659;margin:18px 0 6px">What it does &middot; AI summary</h2>
{summary_html}
<p style="font-size:12px;color:#707a6c;margin:10px 0 18px">Official title: {e(item['title'])}</p>
<div>{tracker_button}<a href="{e(item['url'])}" style="display:inline-block;border:1px solid #c9d1c2;color:#2f3a2c;padding:9px 15px;border-radius:6px;text-decoration:none;font-weight:600;margin:0 0 8px">Read the bill</a></div>
<p style="color:#8a9386;font-size:12px;margin-top:18px">AI summaries can be wrong; read the bill before taking a position. You are getting this because your address is on the OneVoice Tracker alert list.</p>
</td></tr></table></body></html>"""
    return subject, text, html_body
