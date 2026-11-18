"""Committee meetings (with agendas) and press releases.

Meetings: the House and Senate post every committee meeting to their video
systems (sliq Harmony, House = 00283, Senate = 00282). The "upcoming events"
page lists each meeting's committee, room, date and time; each meeting's own
page lists its handouts, which include the agenda PDF on oklegislature.gov.
The agenda lists the bills to be heard.

Press releases: the Senate's RSS feed, the House news page (its built-in
page data), and oklahoma.gov newsrooms (the Governor and agencies), which
expose a JSON listing.
"""

from __future__ import annotations

import html as htmllib
import json
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import requests

import oklegis as ok

UA = {"User-Agent": ok.USER_AGENT}
HARMONY = "https://sg001-harmony.sliq.net"
CHAMBERS = {"House": "00283", "Senate": "00282"}
_session = requests.Session()
_session.headers.update(UA)
_last = 0.0


def _get(url: str, timeout: int = 45) -> requests.Response:
    global _last
    wait = 0.4 - (time.time() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.time()
    r = _session.get(url, timeout=timeout)
    r.raise_for_status()
    return r


def _text(fragment: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment, flags=re.S)
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).strip()


# ------------------------------------------------------------- meetings

def upcoming_meetings(chamber: str) -> list[dict]:
    """Meetings listed on a chamber's upcoming-events page."""
    inst = CHAMBERS[chamber]
    s = _get(f"{HARMONY}/{inst}/Harmony/en/View/UpcomingEvents/").text
    out = []
    for blk in re.findall(r'<div class="divEvent"\s*>(.*?)</table>', s, re.S):
        m = re.search(r"PowerBrowserV3/(\d{8})/-1/(\d+)", blk)
        if not m:
            continue  # the page's template row
        def cls(name):
            mm = re.search(rf'class="{name}[^"]*"[^>]*>(.*?)</(?:div|span)>', blk, re.S)
            return _text(mm.group(1)) if mm else ""
        heading = _text((re.search(r'class="tdEventTitle">\s*<span>(.*?)</span>', blk, re.S) or [None, ""])[1]) or \
            htmllib.unescape((re.search(r'title="([^"]*)"', blk) or [None, ""])[1])
        loc = (re.search(r"\(([^()]*)\)\s*$", heading) or [None, ""])[1]
        title = re.sub(r"\s*\([^()]*\)\s*$", "", heading)
        status, desc, when, day = cls("eventStatus"), cls("eventDesc"), cls("eventTime"), cls("eventDate")
        start = None
        if day and when:
            try:
                start = datetime.strptime(f"{day} {when.split('-')[0].strip()}", "%a, %b %d, %Y %I:%M %p").isoformat()
            except ValueError:
                start = None
        out.append({
            "key": f"{chamber[0]}{m.group(2)}", "chamber": chamber, "harmony_id": int(m.group(2)),
            "title": title, "committee": committee_name(desc or title), "location": loc, "status": status,
            "time_text": when, "start": start,
            "page_url": f"{HARMONY}/{inst}/Harmony/en/PowerBrowser/PowerBrowserV3/{m.group(1)}/-1/{m.group(2)}",
        })
    return out


def committee_name(title: str) -> str:
    """'Health and Human Services 2ND REVISED' -> 'Health and Human Services'."""
    t = re.sub(r"\b(\d+(st|nd|rd|th)\s+)?REVIS(ED|ION)(\s+\d+)?\b.*$", "", title, flags=re.I)
    t = re.sub(r"\b(CANCEL+ED|POSTPONED|AMENDED|AGENDA ADDED|NOTICE)\b.*$", "", t, flags=re.I)
    return t.strip(" :-") or title


def meeting_details(page_url: str) -> dict:
    """Agenda PDF links and exact start time from a meeting's page."""
    s = _get(page_url).text
    handouts = []
    m = re.search(r"Handouts:\s*(\[.*?\])\s*,\s*\n", s, re.S)
    if m:
        try:
            for h in json.loads(m.group(1)):
                u = h.get("HandoutFileUrl") or h.get("Name") or ""
                if u:
                    handouts.append(u)
        except ValueError:
            handouts = re.findall(r'"HandoutFileUrl":"([^"]+)"', m.group(1))
    start = (re.search(r'"SCHEDULEDSTART":\{"id":null,"timestamp":"([^"]+)"', s) or [None, None])[1]
    title = htmllib.unescape((re.search(r'meetingTitle:"([^"]*)"', s) or [None, ""])[1])
    agenda = next((u for u in handouts if re.search(r"/CMN-|agenda", u, re.I)), handouts[0] if handouts else "")
    return {"handouts": handouts, "agenda_url": agenda, "start": start[:19] if start else None, "title": title}


BILL_RE = re.compile(r"\b([HS])\.?\s?(B|J\.?\s?R|C\.?\s?R|R)\.?\s?(\d{1,4})\b")
WORD_BILL_RE = re.compile(r"\b(House|Senate)\s+(Bill|Joint Resolution|Concurrent Resolution|Resolution)\s+(?:No\.\s*)?(\d{1,4})\b", re.I)


def bills_in(text: str) -> list[str]:
    """Bill numbers mentioned in text, normalized (HB1234, SJR5)."""
    found = []
    for a, b, n in BILL_RE.findall(text or ""):
        found.append(f"{a}{re.sub(r'[ .]', '', b)}{int(n)}")
    kinds = {"bill": "B", "joint resolution": "JR", "concurrent resolution": "CR", "resolution": "R"}
    for ch, kind, n in WORD_BILL_RE.findall(text or ""):
        found.append(f"{ch[0].upper()}{kinds[kind.lower()]}{int(n)}")
    return list(dict.fromkeys(found))


def agenda_bills(agenda_text: str) -> tuple[list[str], list[str]]:
    """(bills on the agenda, bills a revision removed)."""
    removed = []
    for line in agenda_text.splitlines():
        if re.search(r"\bremov", line, re.I):
            part = re.split(r"\b(?:and\s+)?to\s+add\b|\badd(?:ed|ing)?\b", line, flags=re.I)[0]
            removed += bills_in(part)
    bills = [b for b in bills_in(agenda_text) if b not in removed]
    return bills, removed


def legislative_deadlines() -> list[dict]:
    """Key dates from the Senate's legislative calendar (deadlines, session days)."""
    s = _get("https://oksenate.gov/calendar").text
    body = s[s.find('id="main-content"'):]
    m = re.search(r"(\d{4}) LEGISLATIVE CALENDAR(.*?)(Latest Senate Journals|</section>)", _text(body), re.S)
    if not m:
        return []
    year, rest = int(m.group(1)), m.group(2)
    out = []
    months = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
    for mon, day, label in re.findall(rf"\b({months})\s+(\d{{1,2}})\s+(.*?)(?=\s+(?:{months})\s+\d{{1,2}}\s|$)", rest):
        y = year - 1 if mon in ("Nov", "Dec") else year  # December deadlines belong to the year before session
        try:
            d = datetime.strptime(f"{mon} {day} {y}", "%b %d %Y").date().isoformat()
        except ValueError:
            continue
        out.append({"date": d, "label": label.strip(" |")})
    return out


# ------------------------------------------------------- press releases

def senate_press() -> list[dict]:
    s = _get("https://oksenate.gov/rss.xml").text
    out = []
    for it in re.findall(r"<item>(.*?)</item>", s, re.S):
        def g(tag):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", it, re.S)
            v = m.group(1) if m else ""
            v = re.sub(r"^<!\[CDATA\[|\]\]>$", "", v.strip())
            return htmllib.unescape(v)
        try:
            when = parsedate_to_datetime(g("pubDate")).isoformat()
        except (TypeError, ValueError):
            when = None
        out.append({"source": "Senate", "url": g("link").strip(), "title": _text(g("title")),
                    "published": when, "text": _text(g("description"))})
    return out


def house_press() -> list[dict]:
    s = _get("https://www.okhouse.gov/news").text
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', s, re.S)
    if not m:
        return []
    pp = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    posts = pp.get("initialPosts") or []
    posts = posts.get("data", []) if isinstance(posts, dict) else posts
    out = []
    for p in posts:
        a = p.get("attributes", p)
        slug = a.get("slug") or ""
        if not slug:
            continue
        out.append({"source": "House", "url": f"https://www.okhouse.gov/posts/{slug}", "title": _text(a.get("title") or ""),
                    "published": a.get("published") or a.get("publishedAt"), "text": _text(str(a.get("content") or ""))})
    return out


def aem_newsroom(page_url: str, source: str) -> list[dict]:
    """An oklahoma.gov newsroom (Governor, agencies): its JSON news listing."""
    page = _get(page_url).text
    api = re.search(r'data-newslistingapiurl="([^"]+)"', page)
    if not api:
        raise RuntimeError(f"No news listing found on {page_url}")
    data = _get(urljoin(page_url, api.group(1) + ".json")).json()
    out = []
    for it in data.get("items") or data.get("newsItems") or []:
        url = urljoin("https://oklahoma.gov", it.get("newsUrl") or "")
        if not url.endswith(".html") and "/content/sok-wcm/en/" in url:
            url = url.replace("/content/sok-wcm/en/", "/") + ".html"
        when = None
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
            try:
                when = datetime.strptime((it.get("lastModified") or it.get("date") or "").strip(), fmt).isoformat()
                break
            except ValueError:
                continue
        out.append({"source": source, "url": url, "title": _text(it.get("title") or ""), "published": when,
                    "text": _text(it.get("description") or "")})
    return out


def press_text(url: str) -> str:
    """The body text of a press release page (for releases listed without it)."""
    s = _get(url).text
    for pat in (r'<div[^>]+class="[^"]*cmp-text[^"]*"[^>]*>(.*?)</div>', r"<article[^>]*>(.*?)</article>",
                r'<main[^>]*>(.*?)</main>'):
        parts = re.findall(pat, s, re.S)
        if parts:
            t = _text(" ".join(parts))
            if len(t) > 200:
                return t[:20000]
    return _text(s)[:20000]


BUILTIN_PRESS = [
    {"id": "senate", "name": "Senate", "kind": "senate_rss", "url": "https://oksenate.gov/rss.xml"},
    {"id": "house", "name": "House", "kind": "house_next", "url": "https://www.okhouse.gov/news"},
    {"id": "governor", "name": "Governor", "kind": "aem", "url": "https://oklahoma.gov/governor/newsroom/newsroom.html"},
    {"id": "ohca", "name": "Health Care Authority", "kind": "aem", "url": "https://oklahoma.gov/ohca/about/newsroom.html"},
    {"id": "oag", "name": "Attorney General", "kind": "aem", "url": "https://oklahoma.gov/oag/news.html"},
]


def fetch_press(src: dict) -> list[dict]:
    if src["kind"] == "senate_rss":
        return senate_press()
    if src["kind"] == "house_next":
        return house_press()
    return aem_newsroom(src["url"], src["name"])
