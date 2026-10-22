# Runbook for AI assistants

You are helping someone set up and use **jobsweep**, a job-search tool that
sweeps companies' own career pages, scores the postings against a profile,
and writes a daily digest. This file is written for you, the assistant. The
human may have handed you this folder, pasted this file into a chat, or asked
you to "set this up for me."

## How to work

- **If you can run commands** on the user's machine (Claude Code, Codex,
  Cursor, a connected desktop app), run each step yourself and show the
  result.
- **If you can only chat**, give the user one command at a time, in a code
  block, for their operating system, and ask them to paste the output back.
  Read the output before giving the next command. Never give a wall of
  commands to run blind.
- Ask what OS they are on first (Windows PowerShell, macOS, or Linux). Every
  step below has both forms where they differ.
- Keep the user's secrets out of files. The API token lives in an environment
  variable. Never write it into `config.yaml`, a script, or a commit.
- Never commit or share `config.local.yaml`, `profile.local.md`,
  `jobsweep.sqlite3`, `digests/` or `*.log`. They hold the user's real search,
  CV text, shortlist and application history. All are gitignored.
- Do not raise the per-track `limit` without saying what it costs: the
  index bills roughly one cent per row returned.

## Step 1 — Install

Python 3.9 or newer, two packages.

```
python --version
pip install -r requirements.txt
```

If `python` is not found on Windows, try `py`. If it is below 3.9, stop and
help them install a current Python before continuing.

## Step 2 — API token

The sweep runs through a hosted index of company career sites at apify.com.
Free tier: 500 runs a month; a daily two-track sweep uses about 60.

1. Create a free account at https://apify.com
2. Settings → API & Integrations → copy the token (it starts with `apify_api_`)
3. Set it for this terminal session:

```powershell
$env:APIFY_TOKEN = "apify_api_..."        # Windows PowerShell
```
```bash
export APIFY_TOKEN=apify_api_...          # macOS / Linux
```

Ask the user to paste the token into the command themselves. Do not ask them
to paste it into the chat.

## Step 3 — Dry run with the example search

`config.yaml` ships a working data-engineering search so the first run
returns real postings. `--dry-run` fetches and scores but writes nothing.
It costs up to 50 cents.

```
python jobsweep.py --dry-run
```

Expected shape of the output:

```
Sweeping via indexed source…
  track core: 25 rows
  track adjacent: 25 rows
  50 postings returned
  sources: greenhouse 12, workday 9, ashby 5, ... (+4 more)
  3 merged as duplicate reqs
  47 not seen before
  11 cleared threshold 5
    [8] Senior Analytics Engineer — Acme Corp (Remote - United States)
    ...

Dry run: nothing written.
```

If instead it prints `No API token found`, the variable is not set in this
terminal: go back to step 2. If it prints `Every track failed`, the token is
wrong or the network is blocked; the lines above it say which.

## Step 4 — Write the user's own search

This is the part that needs you. The search lives in **`config.local.yaml`**,
a gitignored file that merges over `config.yaml`. Overriding `queries` there
replaces the whole example search.

**Interview the user.** You need:

- The job titles they want, including variants (e.g. "SEO Manager", "Head of
  Organic Search", "Growth Marketing Lead").
- Titles that look similar but are wrong (e.g. "Sales Engineer" for an
  engineer; "SEO Intern").
- Words that identify their field inside a description even when the title
  is vague — rare, specific terms, not ones every posting in the industry uses.
- Whether they want remote only, or on-site/hybrid within commuting distance
  of specific cities (and which cities).
- Seniority: are junior roles unwelcome?
- Industries they would never work in (these become `domain_exclusions`).

**Then write the file.** Model it on `config.yaml`, which has two tracks:
track 1 matches titles that are unambiguously their field; track 2 takes a
broad title list and ANDs it with specific description terms. Copy the
structure, replace the terms. Keep `limit` at 25 per track until the filters
are tuned. Rules that matter, from `config.yaml`:

- The API's `titleSearch` is token-based: `"Search"` does not match
  `"Research"`. Use `"Python:*"` syntax for prefixes.
- `titleSearch` and `descriptionSearch` are ANDed; terms within each are ORed.
- The local scoring lists (`title_strong_core`, `title_weak`, `exclude`, ...)
  match whole words. `intern` does not match `International`. Add `*` for a
  prefix: `engineer*`. `*` is not allowed in `exclude`.
- Never put a country in `locations_ok`; it matches every city in it.
- Do not exclude `associate`; it kills "Associate Director".

A minimal `config.local.yaml`:

```yaml
source:
  indexed:
    queries:
      - name: core
        input:
          timeRange: "24h"
          limit: 25
          descriptionType: "text"
          removeAgency: true
          titleSearch: ["seo manager", "head of seo", "organic search"]
          titleExclusionSearch: [intern, junior, "entry level"]
          locationSearch: ["United States"]

scoring:
  title_strong_core: [seo, "organic search"]
  title_strong_conditional: [search]
  domain_exclusions: [pharmaceutical, semiconductor]
  title_weak: [growth, marketing]
  seniority: [senior, staff, principal, lead, head, director, manager, sr]
  exclude: [intern, internship, "entry level"]
  locations_ok: []            # remote-only: leave empty
  threshold: 5
```

## Step 5 — First real sweep, then audit the terms

```
python jobsweep.py
python jobsweep.py --explain
```

The first writes `digests/<today>.md` and creates `jobsweep.sqlite3`. The
second reports how many stored postings each term matched, with an example,
and ends with either `Nothing looks wrong` or a `Worth a look:` list.

Work through the `Worth a look` list with the user: a term that never matched
may be a typo, a plural, or need a `*`; a term that drops more than a quarter
of postings is probably too broad. Edit `config.local.yaml`, rerun
`--explain`, repeat until clean. Then run the sweep once more.

## Step 6 — The review board

```
python jobsweep.py --serve
```

Opens http://127.0.0.1:8765/ in their browser (prints the URL if it cannot).
Each posting is a card with a status dropdown — new, shortlist, applied,
interview, offer, rejected, silence, skipped — and a notes field. Changes
save to the database immediately. Ctrl-C stops it. Use `--port` if 8765 is
taken.

## Step 7 — Schedule the daily sweep

The pitch is a morning digest, so set it up. The scheduler needs the token
in a persistent place; it goes in the user's own crontab or user environment,
which only they can read.

**Windows** (PowerShell). Store the token in the user environment, then
create the task:

```powershell
setx APIFY_TOKEN "apify_api_..."
schtasks /Create /SC DAILY /ST 07:30 /TN jobsweep /TR "cmd /c cd /d $PWD && python jobsweep.py >> sweep.log 2>&1"
```

Run it from inside the jobsweep folder so `$PWD` is right. Verify with
`schtasks /Run /TN jobsweep` and then check `sweep.log`.

**macOS / Linux** (cron):

```bash
crontab -e
```

Add, with the real path and token:

```
APIFY_TOKEN=apify_api_...
30 7 * * * cd /full/path/to/jobsweep && python3 jobsweep.py >> sweep.log 2>&1
```

Verify with `crontab -l`, and the next morning check `sweep.log` and
`digests/`. On macOS, if the log shows a permission error, give `cron` Full
Disk Access in System Settings → Privacy & Security.

## Optional — manual chat evaluation (no AI API key)

After a sweep, `python jobsweep.py --export-evaluations` writes a private
Markdown packet with instructions, up to ten qualifying unscored jobs and the
candidate profile. Upload that packet to the chosen chat assistant; save its
JSON reply in `evaluations/results.json`. Run
`python jobsweep.py --import-evaluations evaluations/results.json`, then open
the board with `--serve`. Keep the adjacent manifest locally for validation.
No AI API call is made. Do not commit evaluation packets, manifests or results.
Use `python tests_manual_evaluate.py` for offline round-trip checks.

## Optional — LLM fit scoring

`--evaluate` sends postings that cleared the keyword bar, plus the user's
background from `profile.local.md`, to Claude for a 0–100 fit score with
strengths, gaps and risks. Needs `ANTHROPIC_API_KEY` in the environment and
costs money per posting; results are cached so nothing is scored twice.

1. Help the user write `profile.local.md`: a few paragraphs on their
   experience, strengths, and what they are targeting. Plain text.
2. `python jobsweep.py --evaluate` (add `--evaluate-limit 5` to try it cheaply).
3. The board then shows a fit pill on each scored card.

## When something goes wrong

- **`No postings stored yet`** from `--explain`: run a sweep first.
- **`Nothing here yet — run a sweep`** on the board: same.
- **Second run the same day says `kept ... as it was`**: correct; nothing
  new cleared the bar, so the morning digest is left alone.
- **A good role is missing**: run `--explain` and look at `exclude` and
  `titleExclusionSearch`. `titleExclusionSearch` filters at the API and
  cannot be audited, so move anything doubtful to the local `exclude` list.
- **`disk I/O error`** on the database: the folder is synced (OneDrive,
  Dropbox) or a network share. Set `output.db` in `config.local.yaml` to a
  local path.
- **Offline self-check**: `python tests_regression.py` needs no token and
  should end with `0 failed`.
