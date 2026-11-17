"""Scraper for the Oklahoma Legislature website (www.oklegislature.gov).

Two sources are used:

* The Subject Index (SubjectIndex.aspx) lists every measure in a session,
  grouped by subject, with its short title and last action. Crawling the
  ~100 main subjects gives a searchable index of the whole session.
* The Bill Information page (BillInfo.aspx) gives one measure's full
  history, authors, versions, amendments, summaries, fiscal impact
  statements and votes.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

BASE = "https://www.oklegislature.gov"
USER_AGENT = "ok-bill-tracker/1.0 (+bill status tracker)"
REQUEST_DELAY = 0.5  # seconds between requests, to be polite to the server

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})
_last_request = 0.0


def _get(url: str, retries: int = 3, timeout: int = 60) -> str:
    global _last_request
    wait = REQUEST_DELAY - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    err: Exception | None = None
    for attempt in range(retries):
        try:
            _last_request = time.time()
            r = _session.get(url, timeout=timeout)
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            return r.text
        except requests.RequestException as e:  # pragma: no cover - network
            err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}: {err}")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


# ---------------------------------------------------------------- bill ids

BILL_RE = re.compile(r"^\s*([A-Za-z]{2,4})\s*\.?\s*(\d{1,5})\s*$")


def normalize_bill(text: str) -> str | None:
    """'hb 1234', 'H.B. 1234', 'HB1234' -> 'HB1234'."""
    t = text.replace(".", "").replace(" ", "")
    m = BILL_RE.match(t)
    if not m:
        return None
    return f"{m.group(1).upper()}{int(m.group(2))}"


def split_bill(bill: str) -> tuple[str, int]:
    m = re.match(r"([A-Z]+)(\d+)", bill)
    if not m:
        return bill, 0
    return m.group(1), int(m.group(2))


def display_bill(bill: str) -> str:
    t, n = split_bill(bill)
    return f"{t} {n}"


def origin_chamber(bill: str) -> str:
    return "S" if bill.upper().startswith("S") else "H"


def bill_url(bill: str, session: str) -> str:
    return f"{BASE}/BillInfo.aspx?Bill={bill}&Session={session}"


def lens_url(bill: str) -> str:
    t, n = split_bill(bill)
    return f"{BASE}/EBillTrack/EBillTrack2.aspx?BillType={t}&BillNo={n}"


# ---------------------------------------------------------------- sessions


@dataclass
class SessionInfo:
    code: str
    name: str


def fetch_sessions() -> list[SessionInfo]:
    """Sessions offered in the bill page's session picker, newest first."""
    html = _get(f"{BASE}/BasicSearchForm.aspx")
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.find("select", id=re.compile("cbxSessionId"))
    out: list[SessionInfo] = []
    if sel:
        for opt in sel.find_all("option"):
            v = (opt.get("value") or "").strip()
            if v:
                out.append(SessionInfo(v, _clean(opt.text)))
    return out


def session_year(code: str) -> int:
    return 2000 + int(code[:2]) if code[:2].isdigit() else 0


# ---------------------------------------------------------- subject index


@dataclass
class IndexEntry:
    bill: str
    title: str
    last_action: str
    subjects: list[str] = field(default_factory=list)


def fetch_main_subjects(session: str) -> list[str]:
    html = _get(f"{BASE}/SubjectIndex.aspx?Alpha=All&Session={session}")
    names = re.findall(r"MainSubject=([^&'\"]+)&(?:amp;)?Session=" + re.escape(session), html)
    seen: list[str] = []
    for n in names:
        n = _clean(BeautifulSoup(n, "html.parser").text)
        if n and n not in seen:
            seen.append(n)
    return seen


def parse_subject_listing(html: str) -> list[IndexEntry]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[IndexEntry] = []
    for a in soup.find_all("a", href=re.compile(r"BillInfo\.aspx\?Bill=", re.I)):
        tr = a.find_parent("tr")
        if not tr:
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue
        bill = normalize_bill(_clean(a.text))
        if not bill:
            continue
        out.append(IndexEntry(bill, _clean(tds[1].text), _clean(tds[3].text)))
    return out


def fetch_subject_bills(session: str, main_subject: str) -> list[IndexEntry]:
    url = f"{BASE}/SubjectIndex.aspx?MainSubject={quote(main_subject)}&Session={session}"
    entries = parse_subject_listing(_get(url))
    for e in entries:
        e.subjects = [main_subject]
    return entries


def crawl_index(session: str, progress=None) -> dict[str, IndexEntry]:
    """Every measure listed in the session's subject index, keyed by bill."""
    subjects = fetch_main_subjects(session)
    found: dict[str, IndexEntry] = {}
    for i, subj in enumerate(subjects, 1):
        if progress:
            progress(i, len(subjects), subj)
        try:
            entries = fetch_subject_bills(session, subj)
        except RuntimeError:
            continue
        for e in entries:
            if e.bill in found:
                if subj not in found[e.bill].subjects:
                    found[e.bill].subjects.append(subj)
            else:
                found[e.bill] = e
    return found


# ------------------------------------------------------------- bill pages


@dataclass
class Action:
    action: str
    journal_page: str
    date: str  # ISO yyyy-mm-dd
    chamber: str  # "H" or "S"


@dataclass
class Document:
    group: str
    name: str
    url: str
    date: str  # ISO or "" when the site gives none


@dataclass
class BillDetail:
    bill: str
    session: str
    title: str
    author: str
    author_url: str
    other_author: str
    other_author_url: str
    coauthors: list[str]
    introduced_url: str
    actions: list[Action]
    documents: list[Document]
    found: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def _iso(date_text: str) -> str:
    date_text = _clean(date_text)
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(date_text, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def parse_bill_page(html: str, bill: str, session: str) -> BillDetail:
    soup = BeautifulSoup(html, "html.parser")

    def by_id(suffix: str):
        return soup.find(id=re.compile(re.escape(suffix) + "$"))

    title_el = by_id("ContentPlaceHolder1_txtST")
    author_el = by_id("ContentPlaceHolder1_lnkAuth")
    other_el = by_id("ContentPlaceHolder1_lnkOtherAuth")
    intro_el = by_id("ContentPlaceHolder1_lnkIntroduced")
    title = _clean(title_el.text) if title_el else ""

    actions: list[Action] = []
    tbl = soup.find(id=re.compile("tblHouseActions$"))
    if tbl:
        for tr in tbl.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) != 4:
                continue
            text = _clean(tds[0].text)
            date = _iso(tds[2].text)
            if not text or not date:
                continue
            actions.append(Action(text, _clean(tds[1].text), date, _clean(tds[3].text)))

    documents: list[Document] = []
    for panel_no in (2, 3, 4, 5):
        panel = soup.find("div", id=re.compile(f"TabContainer1_TabPanel{panel_no}$"))
        if not panel:
            continue
        group = ""
        for tr in panel.find_all("tr"):
            h = tr.find("h1")
            if h:
                group = _clean(h.text)
                continue
            a = tr.find("a", href=True)
            if not a:
                continue
            tds = tr.find_all("td", recursive=False)
            extra = _clean(tds[1].text) if len(tds) > 1 else ""
            date = _iso(extra)
            name = _clean(a.text)
            if not date and extra:  # e.g. votes: "All House Votes"
                name = extra
            documents.append(Document(group, name, a["href"].replace(" ", "%20"), date))

    coauthors: list[str] = []
    p6 = soup.find("div", id=re.compile("TabContainer1_TabPanel6$"))
    if p6:
        for td in p6.find_all("td"):
            t = _clean(td.text)
            if re.fullmatch(r"[A-Za-z .,'\-]+ \((H|S)\)", t):
                coauthors.append(t)

    found = bool(title or actions)
    return BillDetail(
        bill=bill,
        session=session,
        title=title,
        author=_clean(author_el.text) if author_el else "",
        author_url=(author_el.get("href") or "") if author_el else "",
        other_author=_clean(other_el.text) if other_el else "",
        other_author_url=(other_el.get("href") or "") if other_el else "",
        coauthors=coauthors,
        introduced_url=((intro_el.get("href") or "").replace(" ", "%20")) if intro_el else "",
        actions=actions,
        documents=documents,
        found=found,
    )


def fetch_bill(bill: str, session: str) -> BillDetail:
    return parse_bill_page(_get(bill_url(bill, session)), bill, session)


# ------------------------------------------------------------------ status

# Ordered stages; the index is used to compare progress.
STAGES = [
    ("introduced", "Introduced"),
    ("committee", "In committee"),
    ("floor", "On the floor"),
    ("crossed", "In second chamber"),
    ("passed", "Passed both chambers"),
    ("governor", "Sent to Governor"),
    ("law", "Enacted"),
]
STAGE_LABEL = dict(STAGES) | {"vetoed": "Vetoed", "failed": "Failed", "unknown": "Unknown"}
_STAGE_RANK = {k: i for i, (k, _) in enumerate(STAGES)}


def stage_from_actions(bill: str, actions: list[Action]) -> str:
    """Furthest point a measure has reached, judged from its history."""
    if not actions:
        return "unknown"
    origin = origin_chamber(bill)
    stage = "introduced"
    crossed = False

    def bump(new: str):
        nonlocal stage
        if stage in ("law", "vetoed", "failed"):
            if new == "law":
                stage = "law"
            return
        if _STAGE_RANK.get(new, -1) > _STAGE_RANK.get(stage, -1):
            stage = new

    for a in actions:
        t = a.action.lower()
        if re.search(r"approved by governor|became law|veto overrid|filed with secretary of state", t):
            stage = "law"
            continue
        if re.search(r"\bvetoed\b|pocket veto", t) and stage != "law":
            stage = "vetoed"
            continue
        if "died in conference" in t and stage != "law":
            stage = "failed"
            continue
        if "sent to governor" in t:
            bump("governor")
            continue
        if re.search(r"\benrolled\b|referred for enrollment", t):
            bump("passed")
            continue
        if a.chamber and a.chamber != origin and re.search(r"first reading|referred|engrossed", t):
            crossed = True
        if re.search(r"engrossed.*to (house|senate)", t):
            bump("crossed")
            crossed = True
            continue
        if crossed:
            bump("crossed")
            continue
        if re.search(r"general order|placed on|do pass|reported|third reading|passed", t):
            bump("floor")
        elif re.search(r"referred|committee", t):
            bump("committee")
    return stage


def stage_from_last_action(bill: str, last_action: str) -> str:
    """Rough stage from the subject index's abbreviated last action."""
    t = last_action.upper()
    if not t:
        return "unknown"
    if re.search(r"APPROVED BY GOV|BECAME LAW|LAW W/O GOV|VETO OVERRID|CHAPTER|SEC OF STATE", t):
        return "law"
    if "VETO" in t:
        return "vetoed"
    if re.search(r"DIED IN CONF", t):
        return "failed"
    if re.search(r"SENT TO GOV|TO GOVERNOR", t):
        return "governor"
    if re.search(r"ENROLLED|ENR\b", t):
        return "passed"
    chamber = re.search(r"\((H|S)\)\s*$", t)
    if chamber and chamber.group(1) != origin_chamber(bill):
        return "crossed"
    if re.search(r"\bTO (SENATE|HOUSE)\b|HA'S|SA'S|CONFEREES|CONFERENCE", t):
        return "crossed"
    if re.search(r"GENERAL ORDER|CALENDAR|DO PASS|\bDP|CR FILED|THIRD READING|PASSED|FAILED", t):
        return "floor"
    if re.search(r"REF\b|REFERRED|COMM", t):
        return "committee"
    return "introduced"


def session_closed(session: str, today: datetime | None = None) -> bool:
    """True when the legislature that held this session has adjourned sine die.

    Oklahoma legislatures run two years (odd year + even year). Measures that
    have not become law die when the even-year session ends in late May;
    special sessions (codes ending in X) are treated as closed after their
    calendar year.
    """
    today = today or datetime.now()
    year = session_year(session)
    if not year:
        return False
    if session.upper().endswith("X"):
        return today.year > year
    end_year = year if year % 2 == 0 else year + 1
    return (today.year, today.month) > (end_year, 5)


# --------------------------------------------------------- action categories

ACTION_CATEGORIES = [
    ("committee_assignment", "Committee assignments"),
    ("committee_vote", "Committee votes and reports"),
    ("floor", "Floor action and votes"),
    ("other_chamber", "Sent to the other chamber"),
    ("governor", "Governor (sent, signed, vetoed)"),
    ("authors", "Authors added or removed"),
    ("other", "Other actions"),
]
DEFAULT_MOVEMENT_CATEGORIES = ["committee_assignment", "committee_vote", "floor", "other_chamber", "governor"]


def classify_action(action: str, chamber: str = "", bill: str = "") -> str:
    """Sort a history line into one of ACTION_CATEGORIES. A first reading in
    the bill's own chamber is just its introduction ("other")."""
    t = action.lower()
    if t.startswith("first reading") and bill and chamber == origin_chamber(bill):
        return "other"
    if re.search(r"governor|became law|veto|enrolled|signed, returned|filed with secretary of state", t):
        return "governor"
    if re.search(r"\b(co)?authored\b|author (removed|added|changed)|title (stricken|restored)|principal (house|senate) author", t):
        return "authors"
    if re.search(r"engrossed.*to (house|senate)|engrossed, signed, to|^first reading", t):
        return "other_chamber"
    if re.search(r"do pass|do not pass|reported|\bcr\b|cr filed|committee substitute|failed in committee|recommendation to the", t):
        return "committee_vote"
    if re.search(r"referred to|second reading referred|withdrawn from|recommitted|re-?referred", t):
        return "committee_assignment"
    if re.search(r"general order|third reading|passed|failed|calendar|considered|amended|ayes|reconsider|conference|ha's|sa's|has (read|adopted)", t):
        return "floor"
    return "other"


# ------------------------------------------------------------- progress bar

CHAMBER_NAME = {"H": "House", "S": "Senate"}


def progress(bill: str, actions: list, closed: bool = False) -> dict:
    """Where a measure is on its path to becoming law, as steps and a percent.

    Senate path: Filed, Senate committee, Senate floor, House committee, House
    Oversight committee, House floor, Conference / final passage, Governor, Law.
    House bills run the same steps with the chambers swapped. Only Law is 100%.
    Simple resolutions (HR/SR) stay in one chamber; concurrent resolutions skip
    the Governor. `actions` are oklegis.Action or dicts with action/chamber.
    """
    btype, _ = split_bill(bill)
    origin = origin_chamber(bill)
    other = "S" if origin == "H" else "H"

    def chamber_steps(ch):
        st = [(f"{ch}_committee", f"{CHAMBER_NAME[ch]} committee")]
        if ch == "H":
            st.append(("H_oversight", "House Oversight committee"))
        st.append((f"{ch}_floor", f"{CHAMBER_NAME[ch]} floor"))
        return st

    simple_res = btype in ("HR", "SR")
    concurrent = btype in ("HCR", "SCR")
    steps = [("filed", "Filed")] + chamber_steps(origin)
    if not simple_res:
        steps += chamber_steps(other)
    if simple_res or concurrent:
        steps += [("law", "Adopted")]
    else:
        steps += [("final", "Conference / final passage"), ("governor", "Governor"), ("law", "Law")]
    keys = [k for k, _ in steps]

    done: set[str] = {"filed"}
    status, conference = "active", False
    floors_passed: list[str] = []
    for a in actions:
        text = (a["action"] if isinstance(a, dict) else a.action)
        ch = (a["chamber"] if isinstance(a, dict) else a.chamber)
        t = text.lower()
        if "conference" in t:
            conference = True
        if ch in ("H", "S"):
            committee_report = re.search(r"do pass|reported|\bcr\b|cr filed|committee substitute", t)
            if ch == "H" and "policy recommendation" in t:
                done.add("H_committee")
            elif committee_report and "policy recommendation" not in t:
                done.add(f"{ch}_committee")
                if ch == "H":
                    done.add("H_oversight")
            passed = re.search(r"third reading|measure (and emergency )?passed|passed:\s*ayes|^adopted|resolution adopted", t)
            if (passed or re.search(r"engrossed.*to (house|senate)|engrossed, signed, to", t)) and ch not in floors_passed:
                floors_passed.append(ch)
                done.add(f"{ch}_floor")
                if simple_res:
                    done.add("law")
        if re.search(r"enrolled|referred for enrollment|sent to governor", t):
            done.add("final")
        if "sent to governor" in t:
            done.add("final")
        if re.search(r"approved by governor|became law|law w/?o|veto overrid|filed with secretary of state", t):
            done.update(keys)
            status = "law"
        elif re.search(r"\bvetoed\b|pocket veto", t) and status != "law":
            status = "vetoed"
        if "died in conference" in t and status == "active":
            status = "failed"

    furthest = max(keys.index(k) for k in done if k in keys)
    if status == "law":
        furthest = len(keys) - 1
    percent = round(100 * furthest / (len(keys) - 1))
    if status != "law":
        percent = min(percent, 99)
    if status == "active" and closed:
        status = "ended"
    nxt = steps[furthest + 1][1] if furthest + 1 < len(steps) else ""
    labels = {
        "law": "Became law" if not (simple_res or concurrent) else "Adopted",
        "vetoed": "Vetoed by the Governor",
        "failed": "Died in conference",
        "ended": "Session ended without final passage",
    }
    if status in labels:
        where = labels[status]
    elif nxt == "Governor":
        where = "Waiting on the Governor"
    elif nxt:
        where = f"Next: {nxt}" if furthest == 0 else f"In {nxt}" if "committee" in nxt else f"Next: {nxt}"
    else:
        where = ""
    return {
        "percent": percent,
        "status": status,
        "where": where,
        "steps": [{"key": k, "label": ("Conference committee / final passage" if k == "final" and conference else lbl),
                   "done": i <= furthest, "current": i == furthest + 1 and status == "active"}
                  for i, (k, lbl) in enumerate(steps)],
    }
