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


def render_alert(items: list[dict], app_url: str, session_name: str) -> tuple[str, str, str]:
    """items: [{display, title, url, last_action, priorities: [{task_force, number, title, keywords}]}]"""
    n = len(items)
    verb = "match" if n != 1 else "matches"
    subject = f"OneVoice Tracker: {n} new bill{'s' if n != 1 else ''} {verb} the agenda"

    lines = [f"{n} newly filed bill{'s' if n != 1 else ''} in the {session_name} match{'' if n != 1 else 'es'} the OneVoice state agenda.", ""]
    for it in items:
        lines.append(f"{it['display']}: {it['title']}")
        for p in it["priorities"]:
            lines.append(f"  - {p['task_force']} #{p['number']}: {p['title']} (matched: {', '.join(p['keywords'])})")
        lines.append(f"  {it['url']}")
        lines.append("")
    if app_url:
        lines.append(f"Open the tracker: {app_url}#agenda")
    lines.append("You are getting this because your address is on the OneVoice Tracker alert list.")
    text = "\n".join(lines)

    e = html.escape
    rows = []
    for it in items:
        pr = "".join(
            f'<div style="margin:4px 0 0;font-size:13px;color:#3d4a3a">'
            f'<b style="color:#4f8a1e">{e(p["task_force"])} #{p["number"]}</b> &middot; {e(p["title"])}'
            f'<span style="color:#707a6c"> (matched: {e(", ".join(p["keywords"]))})</span></div>'
            for p in it["priorities"]
        )
        rows.append(
            f'<tr><td style="padding:14px 0;border-bottom:1px solid #e3e7df">'
            f'<a href="{e(it["url"])}" style="font:600 15px Menlo,Consolas,monospace;color:#2f3a2c;text-decoration:none">{e(it["display"])}</a>'
            f'<div style="font-size:15px;color:#1d241b;margin-top:2px">{e(it["title"])}</div>{pr}'
            + (f'<div style="font-size:12px;color:#707a6c;margin-top:4px">Latest: {e(it["last_action"])}</div>' if it.get("last_action") else "")
            + "</td></tr>"
        )
    button = (
        f'<p style="margin:20px 0"><a href="{e(app_url)}#agenda" style="background:#5a9a28;color:#fff;padding:10px 16px;'
        f'border-radius:6px;text-decoration:none;font-weight:600">Open OneVoice Tracker</a></p>' if app_url else ""
    )
    html_body = f"""<!doctype html><html><body style="margin:0;background:#f4f6f2;padding:24px 12px;font-family:Arial,Helvetica,sans-serif">
<table role="presentation" width="100%" style="max-width:640px;margin:0 auto;background:#fff;border-radius:8px;border-top:6px solid #5a9a28">
<tr><td style="padding:24px 28px">
<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#5a9a28;font-weight:700">OneVoice Tracker</div>
<h1 style="font-size:20px;color:#1d241b;margin:6px 0 4px">{n} new bill{'s' if n != 1 else ''} {verb} the agenda</h1>
<p style="color:#4d574a;margin:0 0 8px;font-size:14px">Newly filed in the {e(session_name)}, matched against the OneVoice state priorities.</p>
<table role="presentation" width="100%" style="border-collapse:collapse">{''.join(rows)}</table>
{button}
<p style="color:#8a9386;font-size:12px;margin-top:18px">Matches come from keywords in each bill's title. Review the bill before taking a position. You are getting this because your address is on the OneVoice Tracker alert list.</p>
</td></tr></table></body></html>"""
    return subject, text, html_body
