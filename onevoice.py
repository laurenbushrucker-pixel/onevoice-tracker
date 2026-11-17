"""Match bills to OneVoice agenda priorities and send email alerts."""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
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


TEXT_MIN_HITS = int(os.environ.get("BILLTRACKER_TEXT_MIN_HITS", "3"))


def _distinct_mentions(spans: set[tuple[int, int]]) -> int:
    """Number of non-overlapping places in the text where any keyword was found."""
    n, end = 0, -1
    for start, stop in sorted(spans):
        if start >= end:
            n += 1
            end = stop
        else:
            end = max(end, stop)
    return n


def match_bill(title: str, subjects: list[str], priorities: list[dict], text: str = "",
               min_hits: int = TEXT_MIN_HITS) -> list[tuple[str, list[str]]]:
    """[(priority_id, [evidence])] for every enabled priority the bill matches.

    A keyword in the title or subjects is a match on its own. In the full bill
    text, a priority's keywords must appear at least `min_hits` times in total,
    so a bill that mentions a topic once in passing isn't flagged. Evidence
    strings say where each word was found, e.g. "housing (title)" or
    "affordable housing (bill text, 4x)". Exclusion words apply to the title.
    """
    head = " ".join([title or ""] + list(subjects or []))
    out = []
    for p in priorities:
        if not p.get("enabled", True):
            continue
        kws = [k for k in p.get("keywords", []) if k.strip()]
        if match_text(head, p.get("exclude", [])):
            continue
        in_title = [k.strip() for k in kws if keyword_regex(k).search(head)]
        counts, spans = {}, set()
        if text:
            for k in kws:
                found = [m.span() for m in keyword_regex(k).finditer(text)]
                counts[k.strip()] = len(found)
                spans.update(found)
        body_total = _distinct_mentions(spans)  # "Indian tribe" and "tribe*" in the same place count once
        if not in_title and body_total < min_hits:
            continue
        evidence = [f"{k} (title)" for k in in_title]
        evidence += [f"{k} (bill text, {n}x)" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])
                     if n and k not in in_title]
        out.append((p["id"], evidence[:8]))
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
        "from_name": os.environ.get("BILLTRACKER_FROM_NAME", "ONEVOICE ALERT"),
        "security": os.environ.get("SMTP_SECURITY", "starttls").lower(),  # starttls | ssl | none
    }


def email_configured() -> bool:
    s = smtp_settings()
    return bool(s["host"] and s["sender"])


def send_email(to: list[str], subject: str, text: str, html_body: str,
               attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    s = smtp_settings()
    if not (s["host"] and s["sender"]):
        raise EmailNotConfigured("Email is not set up. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_FROM on the server.")
    if not to:
        raise EmailNotConfigured("No alert recipients. Add at least one address in Settings.")
    msg = EmailMessage()
    msg["Subject"] = subject
    # Show a name instead of the bare address, e.g. "ONEVOICE ALERT <alerts@example.org>".
    name, addr = parseaddr(s["sender"])
    msg["From"] = formataddr((name or s["from_name"], addr or s["sender"]))
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    for filename, data, mimetype in attachments or []:
        maintype, subtype = mimetype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
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
    model = item.get("summary_model") or ""
    source = ("AI summary" if model.startswith("claude") else
              "Official staff summary" if model == "official" else
              "From the bill's official description" if model == "caption" else "Summary")
    matches = [f"{p['task_force']} #{p['number']}: {p['title']}" for p in item["priorities"]]

    lines = [
        f"A new bill matching the OneVoice {area_text} agenda was filed in the {session_name}.",
        "",
        f"Bill number: {item['display']}",
        f"Author: {author}",
        "Subject area: " + ("; ".join(matches) if matches else area_text),
        "",
        f"What it does ({source}):",
        summary or f"No summary available yet. Official title: {item['title']}",
        "",
        f"Official title: {item['title']}" if summary else "",
        f"Read the bill: {item['url']}",
    ]
    if app_url:
        lines.append(f"Open the tracker: {app_url}#agenda")
    lines += ["", "Matched on: " + ", ".join(w for p in item["priorities"] for w in p["keywords"][:4]),
              "Matches come from keywords in the bill's title and full text. Read the bill before taking a position.",
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
        '<div style="font-size:14px;color:#5d6659;font-style:italic">No summary available yet.</div>'
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
<h2 style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:#5d6659;margin:18px 0 6px">What it does &middot; {e(source)}</h2>
{summary_html}
<p style="font-size:12px;color:#707a6c;margin:10px 0 18px">Official title: {e(item['title'])}</p>
<div>{tracker_button}<a href="{e(item['url'])}" style="display:inline-block;border:1px solid #c9d1c2;color:#2f3a2c;padding:9px 15px;border-radius:6px;text-decoration:none;font-weight:600;margin:0 0 8px">Read the bill</a></div>
<p style="color:#8a9386;font-size:12px;margin-top:18px">Matched on: {e(", ".join(w for p in item["priorities"] for w in p["keywords"][:4]))}. Matches come from keywords in the bill's title and full text. Read the bill before taking a position. You are getting this because your address is on the OneVoice Tracker alert list.</p>
</td></tr></table></body></html>"""
    return subject, text, html_body


# ------------------------------------------------------------ other emails

_WRAP_TOP = """<!doctype html><html><body style="margin:0;background:#f4f6f2;padding:24px 12px;font-family:Arial,Helvetica,sans-serif">
<table role="presentation" width="100%" style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:8px;border-top:6px solid {color}">
<tr><td style="padding:24px 28px">
<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:{color};font-weight:700">OneVoice Tracker</div>"""
_WRAP_END = "</td></tr></table></body></html>"
_FOOT = '<p style="color:#8a9386;font-size:12px;margin-top:18px">You are getting this because your address is on the OneVoice Tracker alert list.</p>'


def _button(url: str, label: str, primary: bool = True) -> str:
    if not url:
        return ""
    style = ("background:#5a9a28;color:#fff;" if primary else "border:1px solid #c9d1c2;color:#2f3a2c;")
    return (f'<a href="{html.escape(url)}" style="display:inline-block;{style}padding:9px 15px;border-radius:6px;'
            f'text-decoration:none;font-weight:600;margin:0 8px 8px 0">{html.escape(label)}</a>')


def render_movement(item: dict, app_url: str) -> tuple[str, str, str]:
    """One email for one tracked bill that has new actions.

    item: {display, author, title, summary, url, position, stage_label,
           actions: [{date, chamber, action, category_label}]}
    """
    e = html.escape
    acts = item["actions"]
    headline = acts[-1]["action"]
    headline = headline if len(headline) <= 70 else headline[:67].rsplit(" ", 1)[0] + "..."
    subject = f"OneVoice Tracker: {item['display']} update: {headline}"
    chamber = {"H": "House", "S": "Senate"}
    lines = [f"{item['display']} has {len(acts)} new action{'s' if len(acts) != 1 else ''}.", ""]
    for a in acts:
        lines.append(f"{a['date']}  {chamber.get(a['chamber'], a['chamber'])}: {a['action']}")
    lines += ["", f"Bill number: {item['display']}", f"Author: {item.get('author') or 'Not listed'}",
              f"Your position: {item.get('position', '').capitalize()}", f"Status: {item.get('stage_label', '')}"]
    if item.get("summary"):
        lines += ["", f"What it does: {item['summary']}"]
    lines += ["", f"Read the bill: {item['url']}"] + ([f"Open the tracker: {app_url}#mine"] if app_url else [])
    lines += ["", "You are getting this because you track this bill in OneVoice Tracker."]
    rows = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#5d6659;font-size:13px;white-space:nowrap;vertical-align:top">{e(a["date"])}</td>'
        f'<td style="padding:6px 12px 6px 0;font-size:12px;color:#5d6659;vertical-align:top">{e(chamber.get(a["chamber"], a["chamber"]))}</td>'
        f'<td style="padding:6px 0;font-size:15px;color:#1d241b">{e(a["action"])}'
        f'<div style="font-size:11px;color:#8a9386;text-transform:uppercase;letter-spacing:.05em">{e(a.get("category_label", ""))}</div></td></tr>'
        for a in acts)
    pos = item.get("position", "")
    pos_color = {"support": "#1f7a4d", "oppose": "#b3261e", "monitor": "#8a5a00"}.get(pos, "#4d5866")
    html_body = (_WRAP_TOP.format(color="#5a9a28") +
        f'<h1 style="font-size:21px;color:#1d241b;margin:6px 0 4px">{e(item["display"])} moved</h1>'
        f'<p style="color:#4d574a;margin:0 0 12px;font-size:14px">{e(item.get("title", ""))}</p>'
        f'<table role="presentation" width="100%" style="border-collapse:collapse;border-top:1px solid #e3e7df;border-bottom:1px solid #e3e7df">{rows}</table>'
        f'<p style="font-size:14px;color:#1d241b;margin:14px 0 4px"><b>Author:</b> {e(item.get("author") or "Not listed")} &nbsp; '
        f'<b>Your position:</b> <span style="color:{pos_color};font-weight:700">{e(pos.capitalize())}</span> &nbsp; '
        f'<b>Status:</b> {e(item.get("stage_label", ""))}</p>'
        + (f'<p style="font-size:14px;color:#3d4a3a;margin:8px 0 16px"><b>What it does:</b> {e(item["summary"])}</p>' if item.get("summary") else "")
        + f'<div>{_button(app_url + "#mine" if app_url else "", "Open OneVoice Tracker")}{_button(item["url"], "Read the bill", False)}</div>'
        + '<p style="color:#8a9386;font-size:12px;margin-top:18px">You are getting this because you track this bill in OneVoice Tracker. '
          'Choose which kinds of actions send email under Alerts &amp; settings.</p>' + _WRAP_END)
    return subject, "\n".join(lines), html_body


def render_checkin(stats: dict, app_url: str) -> tuple[str, str, str]:
    """The daily check-in. stats: {date_label, ok, problems:[str], rows:[(label, value)], backup_note}"""
    e = html.escape
    ok = stats["ok"]
    subject = ("OneVoice Tracker daily check-in: all systems OK" if ok else
               f"OneVoice Tracker daily check-in: {len(stats['problems'])} problem{'s' if len(stats['problems']) != 1 else ''} need attention")
    lines = [f"Daily check-in for {stats['date_label']}", "", "Status: " + ("All systems OK" if ok else "Needs attention")]
    lines += [f"  - {p}" for p in stats["problems"]]
    lines += ["", "Last 24 hours:"] + [f"  {k}: {v}" for k, v in stats["rows"]]
    if stats.get("backup_note"):
        lines += ["", stats["backup_note"]]
    lines += ["", "No check-in by 8 a.m. Central means the tracker may be down. Open the website or the Google Cloud console to check."]
    if app_url:
        lines.append(f"Open the tracker: {app_url}")
    color = "#5a9a28" if ok else "#b4512b"
    status = ('<div style="background:#e3f0d6;color:#2f5f12;padding:10px 12px;border-radius:6px;font-weight:700">All systems OK</div>' if ok else
              '<div style="background:#f8dedb;color:#8c1d16;padding:10px 12px;border-radius:6px"><b>Needs attention</b><ul style="margin:6px 0 0;padding-left:18px">'
              + "".join(f"<li>{e(p)}</li>" for p in stats["problems"]) + "</ul></div>")
    rows = "".join(f'<tr><td style="padding:6px 12px 6px 0;color:#5d6659;font-size:14px">{e(k)}</td>'
                   f'<td style="padding:6px 0;font-size:15px;color:#1d241b;font-weight:600;text-align:right">{e(str(v))}</td></tr>' for k, v in stats["rows"])
    html_body = (_WRAP_TOP.format(color=color) +
        f'<h1 style="font-size:21px;color:#1d241b;margin:6px 0 10px">Daily check-in</h1>'
        f'<p style="color:#4d574a;margin:0 0 12px;font-size:14px">{e(stats["date_label"])}</p>{status}'
        f'<h2 style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:#5d6659;margin:18px 0 4px">Last 24 hours</h2>'
        f'<table role="presentation" width="100%" style="border-collapse:collapse">{rows}</table>'
        + (f'<p style="font-size:13px;color:#3d4a3a;margin:14px 0 0">{e(stats["backup_note"])}</p>' if stats.get("backup_note") else "")
        + f'<div style="margin-top:16px">{_button(app_url, "Open OneVoice Tracker")}</div>'
        '<p style="color:#8a9386;font-size:12px;margin-top:14px">No check-in by 8 a.m. Central means the tracker may be down. '
        'Open the website or the Google Cloud console to check.</p>' + _FOOT + _WRAP_END)
    return subject, "\n".join(lines), html_body


def render_problem(title: str, message: str, fix: str, resolved: bool, app_url: str) -> tuple[str, str, str]:
    e = html.escape
    subject = f"OneVoice Tracker {'all clear' if resolved else 'problem'}: {title}"
    parts = [("Fixed: " if resolved else "Problem: ") + title, "", message]
    if fix and not resolved:
        parts += ["", "What to do: " + fix]
    if app_url:
        parts += ["", f"Open the tracker: {app_url}"]
    text = "\n".join(parts)
    color = "#5a9a28" if resolved else "#b4512b"
    html_body = (_WRAP_TOP.format(color=color) +
        f'<h1 style="font-size:21px;color:#1d241b;margin:6px 0 8px">{e(title)}</h1>'
        f'<p style="font-size:15px;color:#1d241b;margin:0 0 12px">{e(message)}</p>'
        + (f'<p style="font-size:14px;color:#3d4a3a;margin:0 0 12px"><b>What to do:</b> {e(fix)}</p>' if fix and not resolved else "")
        + f'<div>{_button(app_url, "Open OneVoice Tracker")}</div>' + _FOOT + _WRAP_END)
    return subject, text, html_body
