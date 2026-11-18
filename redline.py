"""'What changed?' redlines between two versions of a bill.

Bill PDFs carry boilerplate that differs between versions (the "AS
INTRODUCED" header, "ENGR. S. B. NO. 206 Page 3" running heads, the drafter
stamp, the signature blocks), so only the operative text is compared: from
"An Act" to the end of the last section. The comparison is word by word.
"""

from __future__ import annotations

import difflib
import html
import re

START_RE = re.compile(r"\b(An Act\s+(relat|creat|amend|making)|A Joint Resolution|A Concurrent Resolution|A Resolution)", re.I)
END_RE = re.compile(
    r"(Passed the (Senate|House)|Presiding Officer of the|\b\d{2}-\d-\d{2,5}\s+\w{1,4}\s+\d{1,2}/\d{1,2}/\d{2,4}|"
    r"OFFICE OF THE SECRETARY OF STATE|Approved by the Governor)", re.I)
HEADER_RE = re.compile(r"(?m)^.*\bPage \d+\s*$")
CONTEXT = 12        # words of unchanged text shown around a change
MAX_CHANGES = 12    # changes shown in an email


def operative(text: str) -> str:
    t = HEADER_RE.sub(" ", text or "")
    m = START_RE.search(t)
    if m:
        t = t[m.start():]
    t = re.sub(r"(?m)^\s*SUBJECT:.*$", " ", t)  # enrolled bills add a subject line
    e = END_RE.search(t, 200)
    if e:
        t = t[:e.start()]
    return t


def words(text: str) -> list[str]:
    return re.findall(r"\S+", operative(text))


def diff(old: str, new: str) -> dict:
    """{'added': n words, 'removed': n words, 'changes': [{'before','removed','added','after'}], 'similarity': 0..1}."""
    a, b = words(old), words(new)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    changes, added, removed = [], 0, 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        added += j2 - j1
        removed += i2 - i1
        changes.append({
            "before": " ".join(a[max(0, i1 - CONTEXT):i1]),
            "removed": " ".join(a[i1:i2]),
            "added": " ".join(b[j1:j2]),
            "after": " ".join(a[i2:i2 + CONTEXT]),
        })
    # merge changes whose context overlaps, so the reader sees one passage, not fragments
    return {"added": added, "removed": removed, "changes": changes, "similarity": round(sm.ratio(), 3),
            "old_words": len(a), "new_words": len(b)}


def headline(d: dict) -> str:
    if not d["changes"]:
        return "No change to the bill's wording (only headers or signatures differ)."
    if d["similarity"] < 0.35:
        return f"Largely rewritten: {d['new_words']:,} words now vs {d['old_words']:,} before."
    return f"{len(d['changes'])} change{'s' if len(d['changes']) != 1 else ''}: {d['added']:,} words added, {d['removed']:,} removed."


def _clip(s: str, n: int = 600) -> str:
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + " …"


def changes_html(d: dict, limit: int = MAX_CHANGES) -> str:
    """Redline passages: deletions struck through in red, insertions in green."""
    out = []
    for ch in d["changes"][:limit]:
        parts = [f'<span style="color:#666">…{html.escape(ch["before"])}</span> ' if ch["before"] else ""]
        if ch["removed"]:
            parts.append(f'<del style="background:#fde8e7;color:#b3261e;text-decoration:line-through">{html.escape(_clip(ch["removed"]))}</del> ')
        if ch["added"]:
            parts.append(f'<ins style="background:#e3f3ea;color:#1f7a4d;text-decoration:none;font-weight:600">{html.escape(_clip(ch["added"]))}</ins> ')
        if ch["after"]:
            parts.append(f'<span style="color:#666">{html.escape(ch["after"])}…</span>')
        out.append('<p style="margin:0 0 10px;padding:8px 10px;border-left:3px solid #ccc;background:#fafafa;line-height:1.5">'
                   + "".join(parts) + "</p>")
    if len(d["changes"]) > limit:
        out.append(f'<p style="color:#666">…and {len(d["changes"]) - limit} more change(s). Open the bill to read the full text.</p>')
    return "".join(out)


def changes_text(d: dict, limit: int = MAX_CHANGES) -> str:
    out = []
    for ch in d["changes"][:limit]:
        seg = []
        if ch["removed"]:
            seg.append(f"REMOVED: {_clip(ch['removed'], 400)}")
        if ch["added"]:
            seg.append(f"ADDED: {_clip(ch['added'], 400)}")
        out.append(f"...{ch['before'][-80:]} | " + " | ".join(seg))
    if len(d["changes"]) > limit:
        out.append(f"...and {len(d['changes']) - limit} more change(s).")
    return "\n".join(out)


def keyword_changes(old: str, new: str, keywords: list[str]) -> dict:
    """OneVoice keywords that appear in only one of the two versions."""
    lo, ln = operative(old).lower(), operative(new).lower()
    gained, lost = [], []
    for k in keywords:
        pat = re.compile(r"\b" + re.escape(k.lower()) + r"\b")
        a, b = bool(pat.search(lo)), bool(pat.search(ln))
        if b and not a:
            gained.append(k)
        elif a and not b:
            lost.append(k)
    return {"gained": sorted(set(gained)), "lost": sorted(set(lost))}
