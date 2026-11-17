"""Short AI summaries of bills, written by Claude from the bill text on
oklegislature.gov."""

from __future__ import annotations

import io
import os
import re

import anthropic
import pypdf
import requests

import oklegis as ok

MODEL = os.environ.get("BILLTRACKER_SUMMARY_MODEL", "claude-opus-5-5")
MAX_TEXT_CHARS = 600_000  # ~150K tokens; longer bills are summarized from the start and flagged

SYSTEM = """You write short summaries of Oklahoma legislative bills for the policy team of a regional chamber of commerce.

Write 2 or 3 plain-language sentences, under 70 words in total, that say what the bill would do, who or what it affects, and any notable specifics: dollar amounts, programs or funds created or repealed, agencies given new duties, or an unusual effective date.

Keep it neutral. Do not take a position, judge the bill's merits, or predict whether it will pass. Do not repeat the bill number or author, and do not start with "This bill". Write for a reader who knows state government but has not read the bill.

Many Oklahoma bills are filed as "shell bills" that contain only a short title, a statement that the act will not be codified, and an effective date. If the text has no substantive provisions, say that it is a shell bill and name the subject from its title, in one sentence.

Reply with the summary only."""

_client: anthropic.Anthropic | None = None


class SummaryUnavailable(RuntimeError):
    pass


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def pick_version(detail: dict) -> tuple[str, str]:
    """The most recent version of the bill text: (version name, PDF url)."""
    versions = [d for d in detail.get("documents", []) if d.get("group", "").startswith("Versions of")]
    dated = [v for v in versions if v.get("date")]
    if dated:
        v = max(dated, key=lambda v: v["date"])
        return v["name"], v["url"]
    if versions:
        return versions[-1]["name"], versions[-1]["url"]
    if detail.get("introduced_url"):
        return "Introduced", detail["introduced_url"]
    return "", ""


_NOISE = [
    re.compile(r"^\s*\d{1,2}\s*$"),                      # margin line numbers
    re.compile(r"^\s*Req\. No\. \d+\s+Page \d+\s*$"),    # page header
    re.compile(r"^\s*(ENGR|ENR|SB|HB)\.? ?[A-Z]* ?\d+\s+Page \d+\s*$", re.I),
]


def bill_text(url: str) -> str:
    r = requests.get(url, timeout=90, headers={"User-Agent": ok.USER_AGENT})
    r.raise_for_status()
    reader = pypdf.PdfReader(io.BytesIO(r.content))
    lines = []
    for page in reader.pages:
        for line in (page.extract_text() or "").splitlines():
            if not any(p.match(line) for p in _NOISE):
                lines.append(line.rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text


def summarize(display: str, title: str, version: str, text: str) -> tuple[str, bool]:
    """Returns (summary, truncated)."""
    if not available():
        raise SummaryUnavailable("AI summaries are off: set ANTHROPIC_API_KEY on the server.")
    truncated = len(text) > MAX_TEXT_CHARS
    body = text[:MAX_TEXT_CHARS] if truncated else text
    note = "\n\n(The bill text was cut off here because it is very long; summarize what is shown and say the bill is long.)" if truncated else ""
    prompt = (
        f"Bill: {display}\nOfficial title: {title}\nVersion: {version or 'unknown'}\n\n"
        f"<bill_text>\n{body or '(No bill text is available yet; summarize from the official title, and say so.)'}{note}\n</bill_text>"
    )
    try:
        response = client().beta.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as e:
        raise SummaryUnavailable("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY on the server.") from e
    except anthropic.RateLimitError as e:
        raise SummaryUnavailable("The Anthropic API rate limit was reached. Summaries will resume at the next check.") from e
    except anthropic.APIStatusError as e:
        raise SummaryUnavailable(f"The Anthropic API returned an error ({e.status_code}).") from e
    except anthropic.APIConnectionError as e:
        raise SummaryUnavailable("Could not reach the Anthropic API.") from e
    if response.stop_reason == "refusal":
        raise SummaryUnavailable("The model declined to summarize this bill.")
    summary = " ".join(b.text.strip() for b in response.content if b.type == "text").strip()
    if not summary:
        raise SummaryUnavailable("The model returned an empty summary.")
    return summary, truncated


# ------------------------------------------------------- free summaries
# Without an API key, summaries come from oklegislature.gov itself: the
# House or Senate staff's official bill summary when one is posted, and until
# then the bill's own official description (its "An Act relating to ..." line).

def pick_official_summary(detail: dict) -> tuple[str, str]:
    """(label, url) of the newest staff bill summary, or ("", "")."""
    docs = [d for d in detail.get("documents", [])
            if d.get("group", "").startswith("Bill Summaries") and d.get("url")]
    if not docs:
        return "", ""
    d = max(docs, key=lambda d: d.get("date") or "")
    chamber = "House" if "(House)" in d["group"] else "Senate" if "(Senate)" in d["group"] else ""
    return f"{chamber} staff summary ({d['name']})".strip(), d["url"]


_SECTION_RE = re.compile(r"^\s*(Research Analysis|Bill Analysis|Analysis|Summary)\s*$", re.I | re.M)
_END_RE = re.compile(r"^\s*(Prepared [Bb]y:|Fiscal Analysis|Other Considerations|©)", re.M)


def official_summary_text(url: str) -> str:
    """The analysis paragraph(s) from a House or Senate staff bill summary."""
    text = bill_text(url)
    m = _SECTION_RE.search(text)
    body = text[m.end():] if m else text
    end = _END_RE.search(body)
    body = body[:end.start()] if end else body
    body = re.sub(r"-\n(?=[a-z])", "", body)          # re-join hyphenated words
    body = re.sub(r"\s*\n\s*", " ", body).strip()
    return body[:1500].rsplit(" ", 1)[0] + "…" if len(body) > 1500 else body


def caption_summary(text: str, title: str) -> str:
    """The bill's own description: its 'An Act relating to ...' clause."""
    m = re.search(r"\bAn Act\b(.*?)(BE IT ENACTED|$)", text or "", re.S | re.I)
    cap = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    cap = re.sub(r"-\s(?=[a-z])", "", cap)
    if not cap or len(cap) < 20:
        cap = title or ""
    else:
        cap = "Relating to " + re.sub(r"^relating to\s*", "", cap, flags=re.I)
    cap = cap[:1200].rsplit(" ", 1)[0] + "…" if len(cap) > 1200 else cap
    sections = len(re.findall(r"\bSECTION \d+\.", text or ""))
    if text and sections <= 2 and len(text) < 2500 and re.search(r"noncodif", text, re.I):
        cap += " (This looks like a shell bill: it names the act but has no detailed provisions yet.)"
    return cap
