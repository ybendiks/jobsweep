# jobsweep

A job-search agent that reads companies' **own career pages** — all of them,
without you maintaining a list — scores what it finds against your profile,
and gives you a ranked digest plus a local board to track what you applied to.

```
Senior Analytics Engineer
Acme Corp · Remote - United States · greenhouse · score 8 · 150,000–185,000 USD

Posted 2026-10-20. core match: analytics engineer; right level; Remote Solely; 5-10 yrs
```

## Two things make this different

### 1. No company list to maintain

Most job tooling asks you to name the companies to watch. That question is
unanswerable: **you cannot know in advance which company is about to post the
role you want.** A hand-picked list of 30–40 employers produces maybe a couple
of relevant senior postings a month, and you'd have heard about both anyway —
so the list quietly becomes the ceiling on your search.

jobsweep inverts it. It sweeps **~275,000 company career sites across 50+ ATS
platforms** and filters by *role*, not employer. You describe the job you want
once; every company that posts one is in scope, including the ones you'd never
have thought to add. A single sweep in testing returned postings from 14
different ATS platforms — Greenhouse, Workday, BambooHR, Ashby, Jobvite,
UltiPro, Rippling, Oracle Cloud, iCIMS and more.

### 2. Straight from the source, not the resale market

Job boards are a resale market: they copy listings from employers and show
them to everyone at once. jobsweep reads the employer's own board — the same
endpoint their careers page calls — so a posting reaches you on the company's
clock rather than the aggregator's.

Each sweep prints its source mix so you can see where the postings came from:

```
sources: greenhouse 12, ashby 5, icims 3, avature 3, workday 2,
         smartrecruiters 2, hibob 1, paylocity 1 (+4 more)
```

Those are applicant tracking systems — the software employers run their own
hiring on — not places that resell listings.

One thing that reads oddly at first: a posting can show **LinkedIn** as the
*employer* while its source is `smartrecruiters`. That is LinkedIn
Corporation's own career board, which happens to run on SmartRecruiters. It is
not linkedin.com/jobs.

Be clear-eyed about what that buys you: **it's latency, not exclusivity.**
Nothing stays unlisted forever. In spot-checks, one of three roles posted the
previous day had already been syndicated to third-party boards, and two had no
trace anywhere. The gap is measured in days, and it's widest on the platforms
few crawlers bother with — which is exactly where the least-contested roles
sit.

![review board](docs/board.png)

## Quickstart

**The short version:** download the repo and give it to your favorite LLM.
It will walk you through the install and use.

Open the folder in Claude Code, Codex, Cursor or any assistant that can run
commands, and say *"set this up for me."* If you only have a chat window,
paste `AGENTS.md` and `config.yaml` into it and say the same thing; it will
hand you the commands one at a time and ask for the output. Either way the
assistant follows [`AGENTS.md`](AGENTS.md): install, token, a dry run with
the example search, then an interview that turns the job you want into your
own search config, an audit of those terms, and a scheduled morning sweep.

**The manual version** is below.

**1. Install.** Python 3.9+, two dependencies.

```bash
pip install -r requirements.txt
```

**2. Get an API token.** The sweep runs through a hosted index of company
career sites. Create a free account at [apify.com](https://apify.com), then
**Settings → API & Integrations** and copy the token. The free tier allows 500
runs a month; a daily two-track sweep uses about 60 (one run per track).

```bash
export APIFY_TOKEN=apify_api_...              # macOS / Linux
$env:APIFY_TOKEN = "apify_api_..."            # Windows PowerShell
```

**3. Try it before changing anything.** `config.yaml` ships a working
data-engineering search, so the first run returns real postings and you can
see the shape of the thing. `--dry-run` fetches and scores but writes nothing.

```bash
python jobsweep.py --dry-run
```

**4. Make it yours.** Edit the `titleSearch` terms and the scoring lists in
`config.yaml` for your own field — or, better, copy them into
**`config.local.yaml`**, which merges over `config.yaml` and is gitignored.
Overriding `queries` there swaps the whole search at once, so the committed
config stays shareable and your real search stays private.

Then audit what you wrote (see
[Editing the term lists safely](#editing-the-term-lists-safely)):

```bash
python jobsweep.py              # a real sweep, so there is data to audit
python jobsweep.py --explain    # what each of your terms actually matches
```

**5. Use it.**

```bash
python jobsweep.py              # each morning: sweep + write the digest
python jobsweep.py --serve      # the review board
python jobsweep.py --evaluate   # optional LLM fit scoring (ANTHROPIC_API_KEY)
python tests_regression.py      # offline checks
python tests_e2e.py             # full lifecycle, needs a token (a few cents)
```

`tests_regression.py` covers scoring, matching, storage and config handling
with no network. `tests_e2e.py` clones the repo into a temp directory, runs
two real sweeps, and checks that the digest, the database and the review board
agree with each other — then that a second run is idempotent and leaves your
application statuses untouched.

## Editing the term lists safely

The scoring terms match **whole words**: `intern` does not match
`International`. That matters more than it sounds — before this was fixed, an
`intern` entry in the exclude list was silently deleting `Director,
International SEO` from every digest, with nothing to show it had happened.

The flip side is no stemming: `engineer` does not match `engineering`. Add a
trailing `*` when you want a prefix — `engineer*`, `market*`.

Because a bad term fails *silently*, the tool audits itself:

```bash
python jobsweep.py --explain
```

It reports how many stored postings each term matches, with an example, and
flags terms that match nothing or that drop an unusual share of postings. Run
it after editing any list. Every sweep also prints what its local exclude list
removed, so a term that starts eating good roles shows up the same day.

**One blind spot, worth knowing about.** `titleExclusionSearch` filters at the
API, before anything is fetched or stored — so `--explain` cannot audit it and
no report can tell you what it dropped. That is the price of filtering at the
source, which is also what keeps the bill down, since you pay per row
returned. The practical rule: put *cheap, obviously-safe* exclusions in
`titleExclusionSearch` (`intern`, `account executive`), and anything you are
unsure about in the local `exclude` list, where its effect is visible and
costs only the row you already paid for.

```
title_strong_core  (scores +3 on its own)
  seo                             9 (25%)  e.g. "Manager of SEO"
  generative engine               0 ( 0%)  <- no match in this sample
```

## The review board

`python jobsweep.py --serve` runs a local page (standard library only — no
Node, no build step) listing everything swept, highest fit score first (then
keyword score). Set each role
to `new / shortlist / applied / interview / offer / rejected / silence /
skipped`, add notes, and it writes straight back to SQLite. A later sweep
never clobbers a status you set.

## Evaluate in ChatGPT or Claude without an AI API key

Write your background and job preferences to `profile.local.md`, then run a
normal sweep and export stored jobs together with that profile:

```bash
python jobsweep.py --export-evaluations
python jobsweep.py --export-evaluations --export-limit 20
```

The default batch has up to 10 jobs above your keyword threshold, excluding
skipped or already evaluated jobs. Upload the generated `evaluations/*.md`
file to your chat assistant. It contains evaluation instructions, complete
stored job descriptions and your profile, with a requested JSON result format.
No AI API key or network call is needed for export/import. Apify is still used for
normal indexed sweeps. Only upload the packet when you intend to share its
profile and job content with your chosen chat service.

Save the returned JSON in `evaluations/results.json`, then run:

```bash
python jobsweep.py --import-evaluations evaluations/results.json
python jobsweep.py --serve
```

Keep the adjacent `.manifest.json` locally: import uses it to check the batch
ID and every job ID. Malformed, duplicate, unknown or incomplete results are
rejected before writing scores. Existing evaluations, application statuses and
notes are preserved. Imported scores appear on the board, not in old digests.
The export does not reserve jobs; importing before the next export prevents
repeating the same unscored jobs. Set `output.evaluation_dir` to change the
private packet/results folder; ignore any custom folder in Git as well.
Descriptions missing from the upstream source remain missing; old truncated
descriptions need a new sweep to refresh them. No job data is fabricated.

## Optional LLM fit scoring

The built-in scorer is keyword-based: fast, free, runs on everything, and
blunt. `--evaluate` sends the postings that cleared that bar — plus your
background from `profile.local.md` — to Claude, and returns a 0–100 fit score
with strengths, gaps, risks and an apply/consider/skip call. Results are
cached in the database, so a posting is never paid for twice. It costs money
and is strictly optional; the sweep works fine without it.

## How it works

**Two tracks.** Track 1 matches titles that are unambiguously your field.
Track 2 takes a *broad* title set and ANDs it against a few *specific*
description terms, catching roles whose work is your field whatever the title
calls them. Track 2 is the portable idea here — useful in any discipline where
titles are inconsistent.

**Scoring** uses the index's structured classifications — work arrangement,
experience band, salary — rather than guessing them from strings, then adds
title tiers, seniority, and a location rule. Every digest line prints the
reasons behind its score, so a surprising rank is debuggable.

**Location.** Remote scores up. On-site and hybrid outside your commute range
are penalised *unless* the title is senior enough or the pay high enough to be
worth relocating for — thresholds you set.

**Ambiguous terms.** Some tokens mean different things by industry:
`discovery` is drug discovery in pharma, `marketing engineer` is a
semiconductor role. Those sit in a conditional tier that only scores strong
when no `domain_exclusions` term appears in the posting.

**Two sources** behind one interface: `indexed` sweeps nationally through a
hosted ATS index; `direct` hits company boards yourself (Greenhouse, Lever,
Ashby, Workday adapters included) for a free, narrower setup. Switching is a
config change.

## Findings

Things that cost real API credits to learn:

- **Matching is token-based, not substring.** `Search` does not match
  `Research`. Prefix matching needs explicit `:*` syntax.
- **`timeRange` filters on index time, not posted date.** An 8-day-old posting
  can arrive in a 24-hour window. Switching to `datePostedAfter` looks like
  the fix and is a trap: Workday and BambooHR lagged up to 4 days behind their
  own posted dates in testing, so a daily posted-date filter silently drops
  them — and those slow platforms hold the least-syndicated roles. Use
  `timeRange` and let the store dedupe.
- **A term that appears in most postings will crowd out the real matches.**
  Gating product roles on `LLM` returned 36 generic AI postings out of 40,
  because every tech posting name-drops LLMs now; the relevant ones were
  pushed past `limit`. Dropping that one term took 40 noisy rows to 16
  precise ones.
- **Never put a country in the commute list.** `united states` in
  `locations_ok` matches every US city, so an on-site role 2,000 miles away
  scores as "reachable".
- **Don't exclude `associate`.** It reads like a junior marker but kills
  `Associate Director`, a senior title.
- **Filter at the source.** You pay per row returned, so an exclusion term in
  the query is cheaper than one applied afterwards.
- **Expect noise at wide filters.** Before tuning, one sweep ran about 26%
  relevant; the dominant false positives were companies *building* search
  engines rather than optimising for them.

## Cost

Pay-per-row against an ATS index, roughly $0.01 per posting returned. With
tight filters a two-track daily sweep lands near **$4–6/month**. `--evaluate`
is billed separately by Anthropic and only runs on postings that already
cleared the keyword bar.

## Privacy

`config.local.yaml`, `profile.local.md`, `jobsweep.sqlite3`, `digests/` and
`sweep.log` are gitignored — they hold your real search terms, your CV text, your shortlist
and your application history. API keys are read from the environment; the tool
never writes them to a file. If you schedule the sweep, the token lives in
your own crontab or user environment, which only your account can read.

## License

MIT
