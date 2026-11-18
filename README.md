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

## Bill summaries (free)

Each bill is shown with its number, author, a short summary and its last
action. Summaries are free and come from oklegislature.gov:

1. **Official staff summary.** House and Senate staff write a plain-English
   summary for most bills, usually within a few days of filing. The tracker
   uses the newest one.
2. **The bill's official description.** Until staff post a summary, the
   tracker shows the bill's own "An Act relating to ..." description, which
   lists what the bill does. Shell bills (only a title and an effective date)
   are labeled. Bills showing a description are re-checked every 12 hours for
   their first 45 days and switch to the staff summary once it's posted.

Each summary is labeled with its source on the website and in alert emails.

**Optional AI summaries.** If you set `ANTHROPIC_API_KEY`, Claude
(`claude-opus-5-5`) writes the summaries from the bill text instead, which
gives an AI summary on filing day. This costs roughly 2–4 cents per bill. If
the API call fails, the tracker falls back to the free summary.

## Phone app

The tracker installs on phones like an app (a "progressive web app"): it gets
a home-screen icon, opens full screen, and can send notifications. No app
store is involved and it's free.

- **iPhone (iOS 16.4 or later):** open the site in Safari, tap Share, then
  **Add to Home Screen**. Open OneVoice from the home screen, then go to
  Settings and tap **Turn on notifications**.
- **Android:** open the site in Chrome, tap ⋮, then **Install app**, and turn
  on notifications in Settings.
- **Home screen:** big buttons for OneVoice Bills, Calendar, Bill Search,
  Press Releases, My Bills, the Agenda and Settings, with counts. On phones a
  bottom tab bar keeps Home, Bills, Calendar and Press one tap away.
- **Notifications** (choose per device): new OneVoice bills, tracked bills
  that move, tracked or Relevant bills put on a committee agenda, and press
  releases about your bills. Tapping one opens the right screen. They're sent
  with standard Web Push (VAPID keys are created automatically and kept in
  the database).

## Committee calendar

Every 20 minutes (`BILLTRACKER_CALENDAR_MINUTES`) the tracker reads the
upcoming committee meetings that the House and Senate post to their meeting
video systems, opens each meeting's page for its agenda PDF on
oklegislature.gov, and reads the agenda to list the bills being heard
(including "revised to remove" notes). The Calendar shows one week at a time
with deadlines from the Senate's legislative calendar. Tracked bills (★) and
OneVoice bills are highlighted, and when a tracked bill or one you marked
Relevant is put on an agenda you get an email and a notification.

## Press releases

Every 15 minutes (`BILLTRACKER_PRESS_MINUTES`) the tracker collects press
releases from the Senate, House, Governor, Health Care Authority and Attorney
General, and from any oklahoma.gov newsroom you add under Settings. Each
release is tagged with the bills it mentions and the OneVoice priorities it
relates to. You can be emailed when a release mentions a bill you track (the
default), when it mentions your bills or a priority in the headline, or never.

## New bills list and keyword search

The **New bills** tab is a running list of every bill that matches the agenda,
newest first, with everything the alert emails show: bill number, author,
subject areas (with Relevant / Not relevant buttons), summary, a link to the
bill text PDF and the oklegislature.gov page, and current status with the
progress bar.

- **Chamber position**: choose Support, Oppose, Monitor or Neutral on any bill,
  with an optional "Why". Choosing a position adds the bill to My bills, so
  you're emailed when it moves; the position and reason appear in those
  emails and in the CSV export.
- **Keyword search**: the search box on New bills (agenda bills) and on Find
  bills (every bill in the session) searches titles, summaries and the full
  bill text. Words match their beginnings (`tax` finds "taxation"); put an
  exact phrase in quotes ("advanced air mobility"). Each result shows the
  passage where the words appear.
- Filter by agenda priority, relevance (not reviewed, relevant, not relevant)
  and position (including "No position yet").

## Session scoreboard

The Scoreboard screen shows the session at a glance: OneVoice bills filed, marked
Relevant, tracked, enacted, the share of supported bills that became law, and
wins (supported and passed, or opposed and stopped). Charts show bills by task
force, how far OneVoice bills got, and the Chamber's positions. "Show as tables"
gives the same numbers as tables.

## Hearings in Outlook

Settings › Hearings in your Outlook calendar gives a private calendar link
(`/cal/<secret>.ics`). Subscribe to it in Outlook ("Add calendar › Subscribe from
web"), Google Calendar or an iPhone. It lists every committee meeting with a
OneVoice, tracked or Relevant bill (or every meeting, with the box checked),
with the agenda link and bills, plus legislative deadlines as all-day events.
Canceled meetings update themselves. "Make a new link" retires the old one.

## What changed? (redlines)

When a tracked or Relevant bill gets a new version (committee substitute,
engrossed, enrolled), the tracker compares it word by word with the version
before and emails a redline: words taken out struck in red, words added in
green, and any OneVoice keywords the bill gained or lost. The bill's page in the
app lists each change and can compare any two posted versions on demand.

## Friday report for Members

The Friday Report screen builds the Bill Track spreadsheet (Track, Bill
Information, AI Summary, Last Action Date) for any week: every OneVoice and
tracked bill with action that week, newest first, grouped by track, with links
to each bill. Download it as Excel or email it. During session it emails itself
every Friday at noon Central (`BILLTRACKER_REPORT_HOUR`), but only in weeks when
bills moved. A tracked bill's track can be changed on its page (for example to
Criminal Justice or Tax Reform); otherwise it uses the OneVoice task force.

## 2027 goals and meetings

The Goals screen tracks the Government Affairs goals: a scorecard of the six
headline measures with pace markers from the quarterly targets, every
objective under Goals 1 to 3, and a quarterly milestone checklist. Bills passed
and tracking coverage fill in by themselves. The Meetings screen logs who met,
the date, the office, Chamber staff, issues, bills, an overview of the
discussion and follow-up; in-person meetings count toward the contact goals
(each person met is one contact, and distinct names are counted separately).
Donors and PAC activities are logged under Goals › Donors & activities.
Targets can be edited, and "Download (Excel)" exports the scorecard and all
logs. Set `BILLTRACKER_GOAL_YEAR` to change the default year (2027).

## Legislator directory

The Legislators screen lists all 149 House and Senate members from okhouse.gov
and oksenate.gov (refreshed weekly, or with the Refresh button): photo, party,
district, hometown, email or contact form, office, phone, assistant, committees,
and the counties and towns in their district. Filter to Tulsa County or the
seven-county Tulsa region. Each profile shows the OneVoice and tracked bills
they authored (taken from the author links on oklegislature.gov), their
upcoming committee meetings, and every meeting your team logged with them, with
a button to log a new one. Author names on a bill's page link to the profile.

## Staying reliable

- **Daily check-in** (7 a.m. Central, `BILLTRACKER_CHECKIN_HOUR`): an email
  with the last 24 hours of activity and "All systems OK" or what needs
  attention. If it doesn't arrive, the tracker may be down.
- **Problem alerts**: an email right away if checks for new bills stop for 45
  minutes, if the Legislature's pages stop reading correctly (checked hourly
  against a known bill), or if alert emails fail. A reminder follows every 12
  hours, and an "all clear" email when it's fixed. Problems also show as a red
  banner on the website.
- **Is everything working?** panel under Alerts & settings shows each part's
  status.

## Tracked bill updates

Tracked bills are re-checked every hour (`BILLTRACKER_REFRESH_HOURS`). When
one has new actions, you get one email for that bill listing them, with your
position and its status. Choose which kinds of actions send email under
Alerts & settings: committee assignments, committee votes, floor action, sent
to the other chamber, Governor, authors, other.

## Relevant / Not relevant

In a priority's bill list (and on each bill's page), mark each match
**Relevant** or **Not relevant**. Not-relevant bills drop out of the list and
the agenda counts but stay under "Marked not relevant". When you edit a
priority's keywords, the tracker shows which keywords are behind the
Not-relevant marks, so you know what to narrow.

### One click from the email

New-bill alert emails have **✓ Relevant** and **✕ Not relevant** buttons for
each matching priority. One click records the answer with no sign-in; a small
tab confirms it and can be closed. The links are signed (they can't be forged
or edited), expire after 120 days, and only record when a real browser opens
them, so email security scanners that open links in advance (such as
Microsoft Defender) can't mark bills by accident.

## Progress bar

Alert emails and each bill's page show how far the bill has gone toward
becoming law. Senate bills: Filed, Senate committee, Senate floor, House
committee, House Oversight committee, House floor, Conference / final passage,
Governor, Law (House bills start in the House). Only Law is 100%. Vetoed bills
and bills left behind when the session ends are marked as such.

## Backups

- Every morning the server saves a full copy of the database in
  `data/backups/` and keeps 14 days.
- The daily check-in email has a small backup file attached
  (`onevoice-backup-YYYY-MM-DD.json`) with tracked bills, notes, positions,
  relevance marks, keyword edits and settings (never passwords or keys).
  **Download a backup** under Alerts & settings gets the same file any time.
- To restore from that file on a new server: upload it (SSH window > Upload
  file), then run
  `cd ~/onevoice-tracker && .venv/bin/python app.py restore ~/onevoice-backup-YYYY-MM-DD.json`
  and `sudo systemctl restart onevoice-tracker`.

## How alerts work

1. **Finding new filings, fast.** Every 10 minutes (`BILLTRACKER_FILINGS_MINUTES`). Oklahoma numbers bills in order (Senate bills from
   1, House bills from 1001 in a legislature's first year). The
   tracker tries the next unused numbers for every bill type (SB, HB, SJR, HJR,
   SCR, HCR, SR, HR) on oklegislature.gov. It also rebuilds the full index from
   the Legislature's Subject Index every 6 hours as a safety net.
2. **Matching.** The tracker downloads each new bill's full text from
   oklegislature.gov and compares its title, subjects and text with every
   priority's keywords (whole words, case-insensitive, `*` means "any
   ending"). A keyword in the title counts on its own. In the bill text, a
   priority's keywords must appear at least 3 times in total
   (`BILLTRACKER_TEXT_MIN_HITS`), so a passing mention doesn't trigger an
   alert. Each match records where the words were found, e.g.
   "housing (title)" or "aircraft (bill text, 6x)".
3. **Emailing.** Each matching bill is emailed the moment it's ready, with the subject "OneVoice Tracker: New [Policy Area] Bill Filed". If
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
| `BILLTRACKER_FILINGS_MINUTES` | `10` | How often to look for new filings (`0` = off) |
| `BILLTRACKER_TEXT_MIN_HITS` | `3` | Keyword mentions needed in the bill text (title matches always count) |
| `BILLTRACKER_REFRESH_HOURS` | `1` | How often tracked bills are re-checked (`0` = off) |
| `BILLTRACKER_CHECKIN_HOUR` | `7` | Hour (Central) of the daily check-in email |
| `BILLTRACKER_INDEX_HOURS` | `6` | How often the search index is rebuilt (`0` = off) |
| `BILLTRACKER_PASSWORD` | | Team password for signing in (required on a server) |
| `ANTHROPIC_API_KEY` | | Optional: AI summaries instead of the free ones (paid) |
| `BILLTRACKER_FROM_NAME` | `ONEVOICE ALERT` | Sender name shown on alert emails |
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

### Google Cloud free tier ($0)

Google Cloud includes one small e2-micro server free each month in
us-central1 (Iowa), us-west1 (Oregon) or us-east1 (South Carolina). The
`deploy/gcp-setup.sh` script installs the tracker there with free HTTPS (via
sslip.io and Let's Encrypt), starts it on every reboot, and updates it from
GitHub each night at 4:30 a.m. Central.

**1. Create the server** (console.cloud.google.com > Compute Engine > VM instances > Create instance)

| Setting | Choose |
|---|---|
| Region | `us-central1`, `us-west1` or `us-east1` |
| Machine type | `e2-micro` |
| Boot disk | Debian 12, **Standard persistent disk**, 30 GB (Balanced and SSD disks are not free) |
| Firewall | tick **Allow HTTP traffic** and **Allow HTTPS traffic** |

Leave the external IP as the default (ephemeral). Reserving a static IP can
cost money.

**2. Install.** Click **SSH** next to the server, paste this whole block, and
press Enter. It shows a key to add on GitHub (repository **Settings > Deploy
keys > Add deploy key**, leave "Allow write access" unticked), then asks for
your settings.

```bash
{
sudo apt-get update -qq && sudo apt-get install -y -qq git >/dev/null
[ -f ~/.ssh/onevoice ] || ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/onevoice -C onevoice-server
echo; echo "Add this key on GitHub: onevoice-tracker > Settings > Deploy keys > Add deploy key"; echo; cat ~/.ssh/onevoice.pub; echo
read -r -p "Press Enter after you've added the key on GitHub... " _ </dev/tty
[ -d ~/onevoice-tracker ] || GIT_SSH_COMMAND="ssh -i ~/.ssh/onevoice -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" git clone -q -c core.sshCommand="ssh -i ~/.ssh/onevoice -o IdentitiesOnly=yes" git@github.com:laurenbushrucker-pixel/onevoice-tracker.git ~/onevoice-tracker
bash ~/onevoice-tracker/deploy/gcp-setup.sh
}
```

**Afterwards**

- Change settings: `sudo nano /etc/onevoice-tracker.env`, then `sudo systemctl restart onevoice-tracker`. Or run the setup script again.
- Update right away: `bash ~/onevoice-tracker/deploy/update.sh`
- See the logs: `sudo journalctl -u onevoice-tracker -n 50`
- If you stop and start the server, Google may give it a new IP address. Run `bash ~/onevoice-tracker/deploy/gcp-setup.sh` again and keep your settings; it moves the web address to the new IP.
- Set a budget alert (Billing > Budgets & alerts, $1) so you'd hear about any charge.

### Render (paid, simplest)

The repository includes `render.yaml`, which sets everything up on
[Render](https://render.com): an always-on server in Render's Ohio region, a
1 GB disk for the database, and a health check.

1. Sign in to Render with GitHub and allow it to see the `onevoice-tracker`
   repository.
2. Click **New > Blueprint**, pick the repository, and click **Apply**.
3. Fill in the values Render asks for (password, alert address, email
   settings, Anthropic API key). Leave `BILLTRACKER_URL` blank for now.
4. When the deploy finishes, copy the address Render shows (like
   `https://onevoice-tracker.onrender.com`), paste it into `BILLTRACKER_URL`
   under **Environment**, and save. That makes the links in alert emails work.
5. Open the address, sign in, go to **Alerts & settings**, and click **Send a
   test email**.

Cost: about $7/month for the Starter server plus about $0.25/month for the
1 GB disk (Render's prices as of late 2026). The free tier won't work: it
sleeps when idle, can't keep a disk, and blocks outgoing email.

### Docker (any host)

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
| `sources.py` | Committee meetings and agendas (House and Senate meeting systems) and press releases. |
| `summarize.py` | Bill summaries: official staff summaries, the bill's description, or (optional) Claude. |
| `onevoice.py` | Keyword matching and the alert email. |
| `oklegis.py` | Scraper for oklegislature.gov: the Subject Index, bill pages, and status from each bill's history. |
| `app.py` | Web server, database, scheduler (filings, tracked bills, index) and API. |
| `static/` | The web interface. |

The scraper waits half a second between requests to go easy on the
Legislature's server. If the site changes its page layout, `oklegis.py` is the
only file that needs updating. To load next year's agenda, edit
`agenda.json` and click **Restore default keywords** in Alerts & settings.

## Limits

- Status labels are worked out from each bill's action history; the
  Legislature doesn't publish an official status.
- The federal priorities in the OneVoice agenda aren't included, since this
  tracker follows state legislation only.
