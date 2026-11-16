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
