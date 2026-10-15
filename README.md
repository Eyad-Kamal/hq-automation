# MUST CPC Training HQ: automation setup guide

This folder is the engine behind the **MUST CPC Training HQ** page in Notion. It
runs on GitHub every 10 minutes and does the work Notion cannot do by itself:

- imports accepted solutions and rating changes from Codeforces, AtCoder and LeetCode;
- computes points, streaks, freezes, mastery tiers, goal progress, achievements and team totals;
- creates daily practice tasks, upsolve reminders and contest tasks;
- posts contest reminders (a Notion comment that mentions the participants, plus optional email);
- writes the Saturday weekly summaries, the MVP and the weekly report page.

You do not need to know how to program. You need a Notion account that is an admin of the
HQ page, a GitHub account, and about 45 minutes for the first setup.

---

## What you need

- A Notion account that owns or admins **MUST CPC Training HQ** (the teamspace is **MUST CPC**).
- A GitHub account (free). Create one at github.com if you do not have one.
- Optional, for email reminders: a Gmail account with 2-Step Verification turned on.

---

## Step 1: Create the Notion connection (an "integration")

An integration is a robot identity that lets this automation read and write your HQ
databases. You will keep its secret token in GitHub, never in these files.

1. Open **https://www.notion.so/profile/integrations** while signed in to Notion.
2. Click **New integration**.
3. Name it `HQ automation`. Choose the workspace that holds MUST CPC Training HQ.
4. Type: **Internal**.
5. Under **Capabilities**, tick all of these:
   - Read content
   - Update content
   - Insert content
   - Read comments
   - Insert comments
   - Read user information including email addresses
6. Click **Save**.
7. Open the **Configuration** tab and copy the **Internal Integration Secret**.
   Keep this copy somewhere safe for Step 4. Anyone holding it can act as the robot.

## Step 2: Connect the integration to the HQ page

The robot can only see pages you share with it.

1. Open the **MUST CPC Training HQ** page in Notion.
2. Click the **•••** menu at the top right of the page.
3. Choose **Connections** (in some versions: **Add connections**).
4. Search for `HQ automation` and select it. Confirm if asked.

The integration can then reach the HQ page and all of its databases.

## Step 3: Put the code on GitHub

### 3a. Create a repository

1. Sign in at **https://github.com** and click the **+** at the top right, then **New repository**.
2. Name it, for example, `hq-automation`.
3. **Visibility: choose Public.** Here is why. The workflow runs every 10 minutes, which is
   about 4,300 runs a month. GitHub's free plan gives private repositories only 2,000
   Actions minutes a month. If each run takes about a minute (an estimate, not measured),
   a private repository would run out of minutes in about two weeks. Standard runners are
   free on public repositories. The code is safe to publish: it contains **no secrets**. The Notion token is
   stored separately in GitHub's encrypted Secrets (Step 4), and reading the data needs that
   token. The file `notion-ids.json` holds only internal database ids.
4. Tick **Add a README file** only if you want; it does not matter. Click **Create repository**.

### 3b. Upload the files

Upload the **contents** of this `hq-automation` folder so that the repository's top level
contains `hq/`, `tests/`, `requirements.txt`, `notion-ids.json` and the hidden folder `.github/`.

- Easiest: on the new repository page click **Add file → Upload files**, then drag the files
  and folders from inside `hq-automation` (not the folder itself). Click **Commit changes**.
  Upload in two batches if the browser is slow.
- Important: the folder **`.github`** holds the schedule. Make sure it arrives. After uploading,
  open `.github/workflows/hq.yml` in the repository. If it is missing, upload `.github` on its
  own.

Your repository should now show `.github/workflows/hq.yml` and `.github/workflows/keepalive.yml`.

## Step 4: Add the secrets

1. In your repository, click **Settings** (top bar).
2. In the left column: **Secrets and variables → Actions**.
3. Click **New repository secret**.
4. Name: `NOTION_TOKEN`. Secret: paste the Internal Integration Secret from Step 1. Click **Add secret**.

**Email reminders are optional.** Without them, reminders are posted as Notion comments only.
To add email, add these secrets the same way:

| Name | Value |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASS` | a Gmail **App Password** (see below) |
| `SMTP_FROM` | the same Gmail address |

To create the App Password: Google Account → Security → turn on **2-Step Verification** → then
Security → **App passwords** → create one named `HQ` → copy the 16-character code into `SMTP_PASS`.
Do not use your normal Gmail password.

## Step 5: Turn it on and test it

1. Click the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. In the left list, click **HQ**.
3. Click **Run workflow**. Set **Job** to `onboard` and leave **force** unticked. Click **Run workflow**.
   This creates the learning-queue rows and default settings for every active trainee.
4. Wait for the run to turn green, then run again with **Job** set to `auto`.
5. Open the **Sync Log** database in the Admin section of the HQ page. A job writes a row when it
   changed something or did not finish cleanly (`Result` = `Warning` or `Error`). A quiet run
   with nothing to report writes no row.

From now on the **HQ** workflow runs by itself every 10 minutes. The first real import of a new
trainee looks back `IMPORT_BACKFILL_DAYS` days (120 by default). Later imports are incremental.

## Step 6: Add a trainee

1. In Notion, open the HQ page and **Share** it with the trainee's email as a **Guest** (see
   the Notion plan limits for your guest count).
2. Open **Members** and click **New**. Fill in:
   - **Name** (required)
   - **Person**: pick the trainee's Notion account (this is how they are @mentioned)
   - **Email** (used for reminder emails)
   - **CF Handle**, **AtCoder Handle**, **LeetCode Handle**: their usernames on each site
   - **Level**, **Team**
   - tick **Active**
3. Leave **Contest Subscriptions** empty. The next run fills it from the level (L1 gets Div. 4,
   Div. 3 and ABC; L2 adds Div. 2 and Educational; L3 and L4 add more). Edit it at any time.
4. Within about 10 minutes the trainee gets their learning-queue rows, a daily target and
   contest subscriptions. Their first import runs within about 25 minutes.
5. The first daily practice task is created once per Cairo day, so a trainee added during the
   day gets it the next day. To get it now, run the workflow with **Job** set to `daily`.
   Tasks for the same day are not duplicated.

To take someone out, untick **Active**. Their history stays.

## Step 7: Change the settings (Config)

Open **Admin → Config**. Change the **Number** or **Value** of a row, and the next run uses it.
Do not delete rows. The important ones:

| Key | Default | Meaning |
|---|---|---|
| `ALERT_MINUTES` | 30 | Send the contest reminder this many minutes before the start |
| `CONTEST_TASK_LEAD_HOURS` | 24 | Create contest tasks this many hours before the start |
| `AUTO_TASK_PROBLEMS` | 3 | Problems in each daily practice task |
| `DEFAULT_DAILY_TARGET` | 3 | Daily target for members who have none |
| `IMPORT_BACKFILL_DAYS` | 120 | How far back the first import of a member looks |
| `ATCODER_OFFSET` | 0 | Added to AtCoder difficulty estimates (tunable heuristic) |
| `LC_EASY` / `LC_MEDIUM` / `LC_HARD` | 1000 / 1400 / 1900 | Rating used for LeetCode difficulty |
| `MODE_UPSOLVE` / `MODE_EDITORIAL` | 0.7 / 0.5 | Score multipliers for upsolved and editorial-assisted problems |
| `IMPORT_MAX_CREATES` | 400 | Most new rows one import run may create. The rest is imported by later runs, so a large first backfill takes several runs |
| `UPSOLVE_WINDOW_DAYS` | 14 | Upsolve reminders only for contests that ended within this many days. Older open auto Upsolve tasks become Skipped |
| `DAILY_LOG_DAYS` | 60 | Daily Log days recomputed per member (back from today) |
| `WEEKLY_BACKFILL_WEEKS` | 12 | Complete past weeks whose Weekly Summary rows are kept filled (no reports or MVP for older weeks) |
| `DASHBOARDS_AUTO` | 1 | 1 = create a personal dashboard page for each active member with an empty Dashboard URL. 0 = off |
| `FREEZE_EVERY` / `FREEZE_MAX` | 7 / 2 | A freeze is earned per 7 active days, up to 2 |
| `TIER_*`, `BAND_*_FACTOR`, `MODE_*`, `GATE_L1..L4` | see the blueprint | Mastery, scoring and level gates |
| `ADMIN_USER_ID` | empty | Notion user id mentioned on each weekly report. Leave empty to skip the mention. Find your id with the Notion API (`GET /v1/users`, using the integration token). This path is not verified in this repository. |

**Important: some values are also written into Notion formulas.** `MODE_UPSOLVE`,
`MODE_EDITORIAL` and the `LC_EASY` / `LC_MEDIUM` / `LC_HARD` values are mirrored in the
Problems "Points" and "Effective Rating" formulas (they use 0.7, 0.5, 1000, 1400, 1900). The
Python code reads the Config values, but the Notion formulas do not. If a coach changes one of
these, edit the matching number in those two formulas too, or the Points column will disagree
with the stored totals.

The `STATE_*` rows record when each job last ran. Do not edit them. To force a job, use
**Run workflow** with **force** ticked.

---

## How it works

Every run (every 10 minutes) does, in order:

1. **onboard**: fills in new members (queue rows, targets, subscriptions).
2. **contests**: updates the contest list from all three platforms, and creates contest tasks.
3. **alerts**: posts reminders for contests starting within `ALERT_MINUTES` (+10 minutes of slack).
4. **imports + recompute**: only when the last import was more than 25 minutes ago. Each trainee's import starts 2 days before their last successful sync (or looks back `IMPORT_BACKFILL_DAYS` for a first import). A trainee whose source failed keeps the old sync date, so nothing is skipped.
5. **daily**: only once per Cairo day. Creates today's practice tasks and upsolve reminders, and
   marks yesterday's unfinished practice tasks as Skipped.
6. **weekly**: only after Saturday 00:10 (Cairo time), once per week.

Every job can run any number of times. Each record has a stable key, so reruns update or skip
instead of duplicating. Your own edits are kept: the import fills only empty fields and never
overwrites Notes, Key Idea, Time (min), or a Mode you changed.

## When something goes wrong

1. Open **Admin → Sync Log**. Look at the newest rows. `Result` **Error** means a job failed.
   `Warning` means part of it failed (for example, one trainee's Codeforces handle could not be read).
   The `Details` column says what happened.
2. Open the **Actions** tab, click the failed run, then the **Run HQ job** step for the full log.

Common messages:

| Message | Fix |
|---|---|
| `NOTION_TOKEN is not set` | The secret is missing or misspelled (Step 4). |
| `object_not_found` or HTTP 404 | The integration is not connected to the HQ page (Step 2). |
| HTTP 401 | The token is wrong or was regenerated. Update the `NOTION_TOKEN` secret. |
| HTTP 429 | Notion is rate-limiting. The code retries automatically. Nothing to do. |
| `No data source ids` | `notion-ids.json` is missing from the repository root. Upload it again. |
| Codeforces or AtCoder "failed" in the run log | The site was unreachable. The member's Last Sync is not moved, so the next run reads the same window again. Results are never duplicated. |
| `bad CF handles (skipped)` in the Sync Log | Codeforces does not know that handle. Fix the spelling in Members. |

## Known limits

- **Reminder timing.** GitHub starts scheduled runs 5 to 20 minutes late. A reminder therefore
  arrives roughly 20 to 40 minutes before the start, not exactly at `ALERT_MINUTES`.
- **Unofficial APIs.** LeetCode and AtCoder have no official data APIs for this. The code uses
  their public endpoints. They can change without notice. LeetCode shows only the last 50
  accepted submissions. Older LeetCode solves are not imported.
- **Contest names for imported live contests.** A solve does not carry the contest name. The
  import now reads the real name, start and duration from Codeforces `contest.list` (regular and
  gym). A placeholder such as `Codeforces contest 1985` is only left when that list does not
  know the contest; the import renames existing placeholders on each run.
- **Upsolve detection** needs both the live solve and the later practice solve to be seen by the same import run,
  or the live solve must already be recorded as a Contest Entry. An upsolve that happens long after the live contest,
  outside the import window, can be missed.
- **Sync Log volume.** A row is written only when a job changed something or ended with a Warning or Error, so
  the log stays small. A bad week can still produce many rows; use a filter in the Sync Log view if needed.
- **Keep-alive.** The `keepalive` workflow commits a timestamp on the 1st of every month, so
  GitHub does not switch off the schedule after 60 days of no activity.

---

## For developers: running it on your own computer

Requires Python 3.11 or newer.

```powershell
cd hq-automation
python -m pip install -r requirements.txt
python -m pytest -q                       # runs offline; no Notion or network calls
$env:NOTION_TOKEN = "<your token>"        # PowerShell; use `export` on macOS or Linux
python -m hq.main auto --dry-run          # reads Notion and the contest sites, writes nothing, prints the intended writes
python -m hq.main onboard                 # real run
```

Useful options: `--force` (ignore the due checks), `--member "Name"` (limit to one trainee).
Jobs: `auto`, `onboard`, `contests`, `alerts`, `imports`, `recompute`, `daily`, `weekly`.
Do not put the token inside any file in the repository.

`HQ_IDS_JSON` (optional) must be the flat `{"Database title": "data source id"}` object, the same
format as `hq-automation/notion-ids.json`. Do not paste `build/notion-ids.json` there: it has a
different, nested layout and will not load.
