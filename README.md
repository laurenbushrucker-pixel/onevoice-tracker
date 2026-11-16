# OneVoice Tracker

Follows Oklahoma Legislature bills against the **2027 OneVoice state agenda**
and emails you when a newly filed bill matches one of the agenda's priorities.
All bill data comes from **www.oklegislature.gov**.

## What it does

- **Agenda home page.** All 21 state priorities, grouped by task force, each
  with a count of matching bills. Click a priority to see the list of bills that
  match it (each list has its own link, e.g. `/#bills-wts-1`).
- **Email alerts for new filings.** Every hour the tracker looks for newly
  filed bills, checks each title against the agenda keywords, and sends one
  email listing the bills that match, which priority each one matches and the
  words that triggered the match.
- **Editable keywords.** Each priority has a keyword list and an optional list
  of words to skip. Edit them on the Agenda page; a live preview shows how many
  bills the new list would match before you save.
- **Bill tracking.** Track any bill with a position (support, oppose, monitor,
  neutral), priority, labels and notes. Tracked bills are re-checked every 6
  hours, and new actions appear on the Updates tab.
- **Full bill record.** Action history with journal pages, authors, and links
  to every version, amendment, summary, fiscal impact statement, committee
  report and vote record.
- **Search** a whole session by keyword, bill number, agenda item, subject,
  chamber or status. **Export** tracked bills to CSV.

## AI summaries

Each bill is shown with its number, author, a short AI summary and its last
action. Summaries are written by Claude (`claude-opus-5-5`) from the newest
version of the bill text on oklegislature.gov, in 2–3 neutral sentences. Shell
bills (only a title and an effective date) are labeled as such. Summaries are
rewritten when a new version of the bill is posted.

- **Which bills:** by default, bills that match the agenda and bills you track
  (`BILLTRACKER_SUMMARY_SCOPE=agenda`). Use `tracked` for fewer or `all` for
  every bill. Any bill can be summarized on demand with **Summarize now**.
- **Alert emails** include the summary for each new bill.
- **Setup:** set `ANTHROPIC_API_KEY` (from https://console.anthropic.com) on
  the server. Without it the tracker works normally and shows "AI summaries are
  off".
- **Cost:** roughly 2–4 cents per bill. Summarizing every agenda match over a
  two-year legislature (about 1,300 bills in 2025–26) comes to roughly $30–50.
- Requests ask for a fallback model if Claude declines a request
  (`fallbacks: "default"`), so a summary still gets written.

AI summaries can be wrong. Read the bill before taking a position.

## How alerts work

1. **Finding new filings.** Oklahoma numbers bills in order (Senate bills from
   1, House bills from 1001 in a legislature's first year). Each hour the
   tracker tries the next unused numbers for every bill type (SB, HB, SJR, HJR,
   SCR, HCR, SR, HR) on oklegislature.gov. It also rebuilds the full index from
   the Legislature's Subject Index once a day.
2. **Matching.** Each new bill's short title (and subjects, once the
   Legislature assigns them) is compared with every priority's keywords:
   whole words, case-insensitive, with `*` meaning "any ending".
3. **Emailing.** All new matches from one check go out in a single email. If
   sending fails, the matches stay queued and are retried at the next check.

The first check in a session records the bills already filed as a starting
point and sends nothing, and bills that match only because you edited keywords
are not emailed. Only genuinely new filings trigger email.

A match means a bill is worth a look. It doesn't mean the bill supports or
opposes the agenda, so read the bill before taking a position.

## Running it

Requires Python 3.10 or newer.

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python app.py
```

Open http://127.0.0.1:5000, go to **Alerts & settings**, add the addresses that
should get alerts, and click **Send a test email**.

Email alerts only go out while the tracker is running, so for real use run it
on an always-on server (see Hosting below).

## Password

Set `BILLTRACKER_PASSWORD` on the server. Everyone signs in with that one
team password, and stays signed in for 30 days (`BILLTRACKER_SESSION_DAYS`).
Changing the password signs everyone out. After 8 wrong passwords in 15
minutes, that address is locked out for 15 minutes.

When the tracker runs only on your own computer (`HOST` left at
`127.0.0.1`) no password is needed. On a server with no password set, the
tracker stays closed and says so, so it can never be left open by accident.

## Email setup

Set these environment variables before starting the app:

| Variable | Example | Meaning |
|---|---|---|
| `SMTP_HOST` | `smtp.gmail.com` | Mail server |
| `SMTP_PORT` | `587` | Mail server port |
| `SMTP_SECURITY` | `starttls` | `starttls` (port 587), `ssl` (port 465) or `none` |
| `SMTP_USER` | `alerts@yourorg.org` | Sign-in name for the mail server |
| `SMTP_PASSWORD` | | Password or app password. Keep it out of source control. |
| `SMTP_FROM` | `OneVoice Tracker <alerts@yourorg.org>` | Sender shown on the email |

With Gmail or Google Workspace, use an
[app password](https://support.google.com/accounts/answer/185833), not the
account's normal password. A transactional email service (Postmark, SendGrid,
Mailgun, Amazon SES) also works and is more reliable for an organization
account.

## Other settings

| Variable | Default | Meaning |
|---|---|---|
| `BILLTRACKER_SESSION` | `2700` | Session shown first and re-indexed daily (2700 = 2027 Regular Session) |
| `BILLTRACKER_ALERT_SESSIONS` | same as above | Sessions whose new filings trigger email, comma separated |
| `BILLTRACKER_FILINGS_HOURS` | `1` | How often to look for new filings (`0` = off) |
| `BILLTRACKER_REFRESH_HOURS` | `6` | How often tracked bills are re-checked (`0` = off) |
| `BILLTRACKER_INDEX_HOURS` | `24` | How often the search index is rebuilt (`0` = off) |
| `BILLTRACKER_PASSWORD` | | Team password for signing in (required on a server) |
| `ANTHROPIC_API_KEY` | | Turns on AI summaries |
| `BILLTRACKER_SUMMARY_SCOPE` | `agenda` | Which bills get summaries automatically: `agenda`, `tracked` or `all` |
| `BILLTRACKER_ALERT_TO` | | Alert recipients, comma separated. Used until a list is saved in Alerts & settings. |
| `BILLTRACKER_URL` | | Public address of the tracker, for links in emails (can also be set in the app) |
| `BILLTRACKER_DB` | `data/tracker.db` | SQLite database location |
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Where the web server listens |

Command-line tasks, useful with cron if you turn off the built-in schedule:

```bash
.venv/bin/python app.py filings
```

```bash
.venv/bin/python app.py refresh
```

```bash
.venv/bin/python app.py index 2700
```

## Hosting

The included `Dockerfile` runs the tracker on any container host (Render,
Railway, Fly.io, or a small cloud VM). Mount a persistent volume at
`/app/data` so tracked bills and settings survive restarts, and set the email
variables above as secrets.

```bash
docker build -t onevoice-tracker .
```

```bash
docker run -d -p 5000:5000 -v onevoice-data:/app/data --env-file .env onevoice-tracker
```

The app has no login of its own. Put it behind your organization's sign-in
(for example a reverse proxy with SSO, or your host's access controls) before
sharing the address.

## Files

| File | Role |
|---|---|
| `agenda.json` | The 21 OneVoice state priorities with their starting keywords. |
| `summarize.py` | Reads the bill PDF and asks Claude for the summary. |
| `onevoice.py` | Keyword matching and the alert email. |
| `oklegis.py` | Scraper for oklegislature.gov: the Subject Index, bill pages, and status from each bill's history. |
| `app.py` | Web server, database, scheduler (filings, tracked bills, index) and API. |
| `static/` | The web interface. |

The scraper waits half a second between requests to go easy on the
Legislature's server. If the site changes its page layout, `oklegis.py` is the
only file that needs updating. To load next year's agenda, edit
`agenda.json` and click **Restore default keywords** in Alerts & settings.

## Limits

- Matching uses bill titles and subjects, not the full bill text.
- Status labels are worked out from each bill's action history; the
  Legislature doesn't publish an official status.
- The federal priorities in the OneVoice agenda aren't included, since this
  tracker follows state legislation only.
