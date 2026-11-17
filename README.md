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
| `BILLTRACKER_REFRESH_HOURS` | `6` | How often tracked bills are re-checked (`0` = off) |
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
