#!/usr/bin/env python3
"""
jobsweep — sweep company career pages (not aggregators) for relevant roles.

Two sources behind one interface:
  indexed  paid ATS index covering ~275k company career sites / 50+ ATS
  direct   your own slug list, hitting each company's public board API

Both return the same normalized Job records, so switching is a config change.

    pip install requests pyyaml
    export APIFY_TOKEN=...
    python jobsweep.py            # normal run
    python jobsweep.py --dry-run  # fetch + score, write nothing
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import html
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

import store

TIMEOUT = 45
UA = {"User-Agent": "jobsweep/1.0 (personal job search)"}


# ──────────────────────────────── model ────────────────────────────────


@dataclass
class Job:
    uid: str
    title: str
    company: str
    location: str
    url: str
    ats: str
    posted_at: str = ""
    description: str = ""
    arrangement: str = ""
    experience: str = ""
    salary: str = ""
    track: str = ""
    score: int = 0
    reasons: list[str] = field(default_factory=list)


def make_uid(company: str, title: str, url: str) -> str:
    """Stable across runs even if the vendor's own id churns."""
    basis = f"{company.lower().strip()}|{title.lower().strip()}|{url.strip()}"
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


# ─────────────────────────────── sources ───────────────────────────────


class IndexedSource:
    """Nationwide sweep via a hosted ATS index."""

    def __init__(self, cfg: dict):
        self.actor = cfg["actor"]
        # Either one `input` (legacy) or several named `queries` (tracks).
        q = cfg.get("queries")
        if q is None:
            q = [{"name": "default", "input": cfg.get("input", {})}]
        if not q:
            # An empty list would POST input={} — unbounded, and billed per row.
            sys.exit("source.indexed.queries is empty: nothing to sweep. "
                     "Remove the key to use the legacy single `input` block.")
        self.queries = q
        self.token = os.environ.get(cfg.get("token_env", "APIFY_TOKEN"), "")
        if not self.token:
            var = cfg.get("token_env", "APIFY_TOKEN")
            sys.exit(
                f"No API token found in ${var}.\n\n"
                f"  1. Create a free account at https://apify.com\n"
                f"  2. Settings -> API & Integrations -> copy your token\n"
                f"  3. macOS/Linux:  export {var}=apify_api_...\n"
                f"     Windows PS :  $env:{var} = \"apify_api_...\"\n\n"
                f"The free tier allows 500 runs/month; a daily two-track sweep uses ~60."
            )

    def fetch(self) -> list[Job]:
        # The token goes in a header, never the URL: requests embeds the full
        # URL in its exception text, so a timeout would print it to the console
        # and into any log or pasted traceback.
        url = (f"https://api.apify.com/v2/acts/{self.actor}"
               f"/run-sync-get-dataset-items")
        headers = {**UA, "Authorization": f"Bearer {self.token}"}
        out: list[Job] = []
        self.failed: list[str] = []
        for q in self.queries:
            name = q.get("name", "?")
            try:
                r = requests.post(url, json=q.get("input", {}), headers=headers,
                                  timeout=300)
            except requests.RequestException as e:
                print(f"  ! track {name}: request failed: {e}", file=sys.stderr)
                self.failed.append(name)
                continue
            if r.status_code >= 400:
                print(f"  ! track {name}: HTTP {r.status_code} {r.text[:200]}",
                      file=sys.stderr)
                self.failed.append(name)
                continue
            try:
                rows = r.json()
            except ValueError:
                # A 200 carrying HTML: proxy, captive portal, status page.
                print(f"  ! track {name}: response was not JSON "
                      f"({r.text[:120]!r})", file=sys.stderr)
                self.failed.append(name)
                continue
            if not isinstance(rows, list):
                print(f"  ! track {name}: unexpected shape", file=sys.stderr)
                self.failed.append(name)
                continue
            print(f"  track {name}: {len(rows)} rows")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    j = self._normalize(row)
                except Exception as e:
                    # One malformed row must not cost the whole paid run.
                    print(f"  ! track {name}: skipped a row ({e})",
                          file=sys.stderr)
                    continue
                j.track = name
                out.append(j)
        return out

    @staticmethod
    def _normalize(row: dict) -> Job:
        """Field names verified against a live run of the index."""
        title = str(row.get("title") or "")
        company = str(row.get("organization") or "")
        url = str(row.get("url") or "")

        locs = row.get("locations_derived") or row.get("locations") or []
        if isinstance(locs, str):
            locs = [locs]
        if not isinstance(locs, list):
            locs = []
        names = [str(x) for x in locs[:2] if x]
        location = "; ".join(names) if names else "—"

        # Salary values arrive as ints, floats, or strings like "150,000".
        lo, hi = row.get("ai_salary_min_value"), row.get("ai_salary_max_value")
        cur = row.get("ai_salary_currency") or ""
        salary = ""
        try:
            if lo and hi:
                lo_i = int(float(str(lo).replace(",", "")))
                hi_i = int(float(str(hi).replace(",", "")))
                salary = f"{lo_i:,}–{hi_i:,} {cur}".strip()
        except (TypeError, ValueError):
            salary = ""

        return Job(
            uid=make_uid(company, title, url),
            title=title,
            company=company,
            location=location,
            url=url,
            ats=str(row.get("source") or "indexed"),
            posted_at=str(row.get("date_posted") or ""),
            description=str(row.get("description_text") or "")[:4000],
            arrangement=str(row.get("ai_work_arrangement") or ""),
            experience=str(row.get("ai_experience_level") or ""),
            salary=salary,
        )


def parse_workday_url(career_url: str):
    """(tenant, pod, domain, locale, site) from a Workday careers URL, or None.

    Kept at module level so tests exercise this regex rather than a copy of it.
    """
    m = re.match(
        r"https://(?:www\.)?([^.]+)\.(wd\d+)\."
        r"(myworkdayjobs|myworkdaysite)\.com/"
        r"(?:([A-Za-z]{2}-[A-Za-z]{2})/)?([^/?#]+)",
        (career_url or "").strip(),
    )
    return m.groups() if m else None


def _workday_date(posted_on: str | None) -> str:
    """Workday's list endpoint has no date field — `postedOn` is display prose
    like "Posted Today" or "Posted 30+ Days Ago". Translate what we can into a
    real date and return "" rather than letting prose sit in a date column."""
    if not posted_on:
        return ""
    txt = str(posted_on).lower()
    today = datetime.now(timezone.utc).date()
    if "today" in txt:
        return today.isoformat()
    if "yesterday" in txt:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\s*\+?\s*day", txt)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s*\+?\s*month", txt)
    if m:
        return (today - timedelta(days=30 * int(m.group(1)))).isoformat()
    return ""


class DirectSource:
    """Free mode: hit each company's own public board endpoint."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        # Workday tenants can hold thousands of reqs; stopping silently at a
        # fixed number makes a partial sweep look complete.
        self.max_per_board = int(cfg.get("max_per_board", 400))

    def fetch(self) -> list[Job]:
        self.failed: list[str] = []
        jobs: list[Job] = []
        for slug in (self.cfg.get("greenhouse") or []):
            jobs += self._guard(self._greenhouse, slug)
        for slug in (self.cfg.get("lever") or []):
            jobs += self._guard(self._lever, slug)
        for slug in (self.cfg.get("ashby") or []):
            jobs += self._guard(self._ashby, slug)
        for url in (self.cfg.get("workday") or []):
            jobs += self._guard(self._workday, url)
        return jobs

    def _guard(self, fn, arg) -> list[Job]:
        try:
            return fn(arg)
        except Exception as e:  # one dead board must not kill the run
            print(f"  ! {fn.__name__} {arg}: {e}", file=sys.stderr)
            self.failed.append(f"{fn.__name__.lstrip('_')}:{arg}")
            return []

    def _greenhouse(self, slug: str) -> list[Job]:
        # content=true so descriptions arrive: without a body the scorer's
        # domain_exclusions check has nothing to read and cannot fire.
        u = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        r = requests.get(u, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        out = []
        for j in data.get("jobs", []):
            title = (j.get("title") or "").strip()
            url = j.get("absolute_url") or ""
            company = j.get("company_name") or slug
            out.append(Job(
                uid=make_uid(company, title, url), title=title, company=company,
                location=((j.get("location") or {}).get("name") or "—"),
                url=url, ats="greenhouse",
                posted_at=j.get("first_published", "") or j.get("updated_at", ""),
                # Greenhouse may HTML-escape the markup itself. Decode it
                # before stripping tags, then decode remaining text entities.
                description=html.unescape(re.sub(
                    r"<[^>]+>", " ", html.unescape(j.get("content") or "")
                ))[:4000],
            ))
        return out

    def _lever(self, slug: str) -> list[Job]:
        u = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        r = requests.get(u, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        out = []
        for j in r.json():
            title, url = j.get("text", ""), j.get("hostedUrl", "")
            cats = j.get("categories") or {}
            ts = j.get("createdAt")
            out.append(Job(
                uid=make_uid(slug, title, url), title=title, company=slug,
                location=cats.get("location", "—"), url=url, ats="lever",
                posted_at=(
                    datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()
                    if ts else ""
                ),
                description=(j.get("descriptionPlain") or "")[:4000],
            ))
        return out

    def _ashby(self, slug: str) -> list[Job]:
        u = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        r = requests.get(u, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        out = []
        for j in data.get("jobs", []):
            # Ashby titles routinely carry leading whitespace.
            title = (j.get("title") or "").strip()
            url = j.get("jobUrl", "")
            out.append(Job(
                uid=make_uid(slug, title, url), title=title, company=slug,
                location=j.get("location", "—"), url=url, ats="ashby",
                posted_at=j.get("publishedAt", ""),
                description=(j.get("descriptionPlain") or "")[:4000],
            ))
        return out

    def _workday(self, career_url: str) -> list[Job]:
        """Workday's CxS endpoint. Needs a POST, so it never worked from a
        sandbox with an egress allowlist — test this one on a real machine."""
        parsed = parse_workday_url(career_url)
        if not parsed:
            raise ValueError(f"unparseable Workday URL: {career_url!r}")
        tenant, pod, domain, locale, site = parsed
        api = (f"https://{tenant}.{pod}.{domain}.com/wday/cxs/"
               f"{tenant}/{site}/jobs")
        # Keep the locale segment: tenants that use one 404 without it.
        prefix = f"https://{tenant}.{pod}.{domain}.com/"
        if locale:
            prefix += f"{locale}/"
        prefix += site

        out, offset, page, total = [], 0, 20, None
        while offset < self.max_per_board:
            r = requests.post(
                api,
                json={"limit": page, "offset": offset, "searchText": "",
                      "appliedFacets": {}},
                headers={**UA, "Content-Type": "application/json",
                         "Accept": "application/json"},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            payload = r.json()
            if total is None:
                total = payload.get("total")
            posts = payload.get("jobPostings", [])
            if not posts:
                break
            for j in posts:
                title = (j.get("title") or "").strip()
                path = j.get("externalPath", "")
                out.append(Job(
                    uid=make_uid(tenant, title, prefix + path), title=title,
                    company=tenant, location=j.get("locationsText") or "—",
                    url=prefix + path, ats="workday",
                    posted_at=_workday_date(j.get("postedOn")),
                ))
            offset += page
            if len(posts) < page:
                break
        if total and len(out) < total:
            print(f"  ! workday {tenant}: returned {len(out)} of {total} "
                  f"postings (max_per_board={self.max_per_board})",
                  file=sys.stderr)
        return out


def make_source(cfg: dict):
    src = cfg.get("source") or {}
    mode = src.get("mode")
    if mode not in ("indexed", "direct"):
        sys.exit(f"source.mode must be 'indexed' or 'direct', got {mode!r}")
    block = src.get(mode)
    if not block:
        sys.exit(f"source.mode is {mode!r} but there is no source.{mode} "
                 f"block in the config.")
    return IndexedSource(block) if mode == "indexed" else DirectSource(block)


# ─────────────────────────────── scoring ───────────────────────────────


# Populated by score_job so a sweep can report what its own filters removed.
EXCLUDED_BY: dict[str, int] = {}
EXCLUDED_SAMPLE: dict[str, list[str]] = {}


@functools.lru_cache(maxsize=512)
def _pattern(term) -> "re.Pattern | None":
    """Compiled matcher for one term, or None when the term is unusable."""
    # YAML reads a bare number as an int; "2025" in a term list must not
    # crash the sweep on the first posting.
    term = str(term).strip().lower()
    if not term:
        return None
    if term.endswith("*"):
        stem = term[:-1].strip()
        if not stem or "*" in stem:
            return None
        return re.compile(rf"(?<!\w){re.escape(stem)}")
    if "*" in term:
        return None
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")


def _has(term: str, text: str) -> bool:
    """Whole-word containment, with an opt-in trailing-"*" prefix match.

    Plain substring matching silently destroys good roles: "intern" is inside
    "International", so an exclude list containing it drops
    "Director, International SEO". A trailing "*" opts back in to prefix
    matching ("engineer*" also matches "engineering"). Terms are lowercased,
    because the text they are matched against always is.
    """
    pat = _pattern(term)
    return bool(pat and pat.search(text))


def reset_exclusions() -> None:
    """Clear the per-run exclusion counters.

    They are module-level, so a second sweep in one process would otherwise
    report the first sweep's numbers too.
    """
    EXCLUDED_BY.clear()
    EXCLUDED_SAMPLE.clear()


def score_job(job: Job, s: dict) -> Job:
    t = (job.title or "").lower()
    # A posting can arrive with location null; keep one safe string and use it
    # everywhere below rather than slicing job.location directly.
    loc_text = job.location or "—"
    loc = loc_text.lower()
    pts, why = 0, []

    for bad in s.get("exclude", []):
        bad = str(bad)
        if bad.strip().endswith("*"):
            # Prefix matching in an exclude list is how "intern" came to eat
            # "International SEO". Refuse it rather than silently dropping
            # good roles; put the wildcard in a positive list instead.
            print(f"  ! ignoring exclude term {bad!r}: '*' is not allowed in "
                  f"`exclude` because it silently drops near-matches",
                  file=sys.stderr)
            continue
        if _has(bad, t):
            job.score, job.reasons = -99, [f"excluded: {bad}"]
            EXCLUDED_BY[bad] = EXCLUDED_BY.get(bad, 0) + 1
            sample = EXCLUDED_SAMPLE.setdefault(bad, [])
            if len(sample) < 3:        # only the first is ever displayed
                sample.append(job.title)
            return job

    # Core terms stand on their own. Conditional terms (genai, geo, llm,
    # marketing engineer) are domain-ambiguous: the same word means drug
    # discovery in pharma and chip marketing in semiconductors, so they only
    # count as strong when nothing marks the role as another industry.
    core_terms = s.get("title_strong_core") or s.get("title_strong", [])
    core = [k for k in core_terms if _has(k, t)]
    cond = [k for k in s.get("title_strong_conditional", []) if _has(k, t)]
    if core:
        pts += 3
        why.append(f"core match: {', '.join(core[:3])}")
    elif cond:
        blob_all = f"{t} {job.description.lower()}"
        off = [d for d in s.get("domain_exclusions", []) if _has(d, blob_all)]
        if off:
            why.append(f"{cond[0]} but {off[0]} domain")
        else:
            pts += 3
            why.append(f"core match: {', '.join(cond[:3])}")

    scored_strong = pts > 0
    weak = [k for k in s.get("title_weak", []) if _has(k, t)]
    if weak:
        pts += 1
        if not scored_strong:
            why.append(f"adjacent: {weak[0]}")

    if any(_has(k, t) for k in s.get("seniority", [])):
        pts += 1
        why.append("right level")

    # Work arrangement: the index classifies this, so trust it over the string.
    arr = (job.arrangement or "").lower()
    if "remote" in arr:
        pts += 2
        why.append(job.arrangement)
    elif arr in ("on-site", "hybrid"):
        if any(_has(k, loc) for k in s.get("locations_ok", [])):
            pts += 1
            why.append(f"{job.arrangement}, reachable")
        else:
            # Waive the relocation penalty when the role is senior enough or
            # pays enough to be worth the conversation.
            top_title = any(_has(k, t) for k in s.get("waive_location_titles", []))
            big_money = False
            m = re.findall(r"[\d,]{4,}", job.salary or "")
            if m:
                try:
                    big_money = max(int(x.replace(",", "")) for x in m) >= int(
                        s.get("waive_location_salary", 150000))
                except ValueError:
                    pass
            if top_title or big_money:
                why.append(f"{job.arrangement} {loc_text[:28]} — "
                           f"{'senior' if top_title else 'comp'} waiver")
            else:
                pts -= 2
                why.append(f"{job.arrangement} in {loc_text[:36]}")
    elif any(_has(k, loc) for k in s.get("locations_ok", [])):
        pts += 1
        why.append("location works")

    # Experience band, as classified by the index.
    if job.experience in ("10+", "5-10"):
        pts += 1
        why.append(f"{job.experience} yrs")
    elif job.experience in ("0-2",):
        pts -= 3
        why.append("too junior")

    # Optional +1 when the posting mentions whatever differentiates you.
    # Configured, not hardcoded — see scoring.bonus_terms.
    bonus = s.get("bonus_terms") or {}
    terms = bonus.get("terms") or []
    if terms:
        blob = f"{t} {job.description.lower()}"
        if any(_has(k, blob) for k in terms):
            pts += 1
            why.append(bonus.get("label", "bonus"))

    job.score, job.reasons = pts, why
    return job


# ──────────────────────────────── digest ───────────────────────────────


def _digest_entries(jobs: list[Job]) -> list[str]:
    lines: list[str] = []
    for j in jobs:
        lines += [
            f"## {j.title}",
            f"**{j.company}** · {j.location} · `{j.ats}` · score {j.score}"
            + (f" · {j.track}" if j.track else "")
            + (f" · {j.salary}" if j.salary else ""),
            "",
            (f"Posted {j.posted_at[:10]}. " if j.posted_at else "")
            + ("; ".join(j.reasons) if j.reasons else ""),
            "",
            f"[Apply]({j.url})" if j.url else "_no link_",
            "",
            "---",
            "",
        ]
    return lines


def write_digest(jobs: list[Job], outdir: Path, swept: int) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = outdir / f"{today}.md"

    # A later run on the same day can find postings the morning run did not.
    # Those are added to the existing digest; rewriting it from scratch threw
    # the morning's entries away.
    if path.exists() and not jobs:
        # main() already guards this; guard here too so no caller can replace
        # a real digest with "nothing new".
        return path
    if jobs and path.exists():
        prior = path.read_text(encoding="utf-8")
        m = re.search(r"\*\*(\d+) new\*\*", prior)
        if m:
            n = int(m.group(1)) + len(jobs)
            prior = prior[:m.start()] + f"**{n} new**" + prior[m.end():]
            stamp = datetime.now().strftime("%H:%M")
            extra = [f"_Added by a later run at {stamp}:_", ""]
            path.write_text(prior.rstrip("\n") + "\n\n"
                            + "\n".join(extra + _digest_entries(jobs)),
                            encoding="utf-8")
            return path
        # Otherwise the existing file says nothing cleared the bar — replace it.

    lines = [f"# New roles — {today}", ""]
    if not jobs:
        lines += [f"Nothing new cleared the bar. Swept {swept} postings.", ""]
    else:
        lines += [f"**{len(jobs)} new** out of {swept} postings swept.", ""]
        lines += _digest_entries(jobs)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_evaluate(cfg: dict, db: str, limit: int) -> None:
    """Fit-score cleared postings with an LLM, caching every result."""
    import evaluate

    key = evaluate.api_key_or_none()
    if not key:
        sys.exit("Set ANTHROPIC_API_KEY to use --evaluate.")

    prof_path = Path(cfg.get("profile_file", "profile.local.md"))
    if not prof_path.exists():
        sys.exit(f"Write your background to {prof_path} first — the fit score "
                 f"is only as good as the profile it compares against.")
    profile = prof_path.read_text(encoding="utf-8")

    with store.connect(db) as conn:
        rows = store.unscored(conn, cfg["scoring"]["threshold"], limit)
    print(f"{len(rows)} postings to assess")

    done = 0
    for r in rows:
        fit = evaluate.score_row(r, profile, key)
        if not fit:
            continue
        done += 1
        # Each assessment is a paid API call, so commit it immediately. Holding
        # them all in one transaction meant a Ctrl-C after ten results had
        # printed discarded all ten and they would be billed again.
        with store.connect(db) as conn:
            store.save_fit(conn, r["uid"], fit)
        print(f"  {fit['fit_score']:>3} {fit['recommendation']:<9}"
              f"{r['title'][:44]} — {r['company'][:22]}")

    # A bad key fails every row identically. Exiting 0 there would read as
    # "nothing needed scoring" rather than "the key is wrong".
    if rows and not done:
        sys.exit(f"All {len(rows)} assessments failed — see the errors "
                 f"above. Check ANTHROPIC_API_KEY and your network.")


def run_explain(cfg: dict, db: str) -> None:
    """Audit every term against the postings already swept.

    Editing term lists is the main way to break this tool, and a term that
    matches nothing — or silently eats everything — is otherwise invisible.
    Run this after any edit to config.yaml or config.local.yaml.
    """
    sc = cfg["scoring"]
    with store.connect(db) as conn:
        rows = store.corpus(conn)
    if not rows:
        sys.exit("No postings stored yet — run a sweep first, then --explain.")
    titles = [(r["title"] or "").lower() for r in rows]
    bodies = [f"{(r['title'] or '').lower()} {(r['description'] or '').lower()}"
              for r in rows]
    locs = [(r["location"] or "").lower() for r in rows]
    n = len(rows)
    print(f"Auditing term lists against {n} stored postings.\n")

    lists = [("title_strong_core", titles, "scores +3 on its own", True),
             ("title_strong_conditional", titles,
              "scores +3 unless a domain word appears", True),
             ("title_weak", titles, "scores +1", True),
             ("seniority", titles, "scores +1", True),
             ("exclude", titles, "DROPS the posting entirely", False),
             ("waive_location_titles", titles,
              "waives the relocation penalty", False),
             ("domain_exclusions", bodies, "demotes a conditional match", False),
             ("locations_ok", locs, "counts as commutable", False)]
    # score_job falls back to the legacy `title_strong` key, and separately
    # scores bonus_terms — a typo in either was invisible to the audit.
    if not sc.get("title_strong_core") and sc.get("title_strong"):
        lists.insert(0, ("title_strong", titles, "scores +3 on its own", True))
    bonus = (sc.get("bonus_terms") or {}).get("terms") or []
    if bonus:
        lists.append(("bonus_terms.terms", bodies,
                      f"scores +1 ({(sc.get('bonus_terms') or {}).get('label','bonus')})",
                      True))
    warned = []
    for name, corpus, effect, expect_hits in lists:
        terms = ((sc.get("bonus_terms") or {}).get("terms")
                 if name == "bonus_terms.terms" else sc.get(name)) or []
        if not terms:
            continue
        print(f"{name}  ({effect})")
        for term in terms:
            pat = _pattern(str(term))
            hits = ([i for i, text in enumerate(corpus) if pat.search(text)]
                    if pat else [])
            pct = 100 * len(hits) // n if n else 0
            flag = ""
            if not hits and expect_hits:
                flag = "  <- no match in this sample"
                warned.append((name, term, "never matched"))
            elif name == "exclude" and str(term).strip().endswith("*"):
                flag = "  <- '*' is ignored in exclude (drops near-matches)"
                warned.append((name, term, "'*' not allowed in exclude"))
            elif name == "exclude" and pct > 25:
                flag = "  <- dropping a lot, check it"
                warned.append((name, term, f"drops {pct}% of postings"))
            elif pct > 60 and expect_hits:
                flag = "  <- matches almost everything"
                warned.append((name, term, f"matches {pct}%"))
            eg = f'  e.g. "{rows[hits[0]]["title"][:42]}"' if hits else ""
            print(f"  {str(term):<28} {len(hits):>4} ({pct:>2}%){eg}{flag}")
        print()

    if warned:
        print("Worth a look:")
        for name, term, why in warned:
            print(f"  {name}.{term}: {why}")
        print(f"\nThis is {n} postings, so a term that never matched may simply\n"
              f"not have come up yet. If it should have: check for a typo, a\n"
              f"plural, or a word needing a '*' suffix — \"engineer*\" also\n"
              f"matches \"engineering\", while plain \"engineer\" does not.")
    else:
        print("Nothing looks wrong: every search term matched something, and\n"
              "no exclusion is removing an unusual share of postings.")


def deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge `over` into `base`. Non-dict values are replaced."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ───────────────────────────────── main ────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and score, but write neither the database nor "
                         "the digest")
    ap.add_argument("--local", help="override file merged over --config "
                                    "(default: config.local.yaml if present)")
    ap.add_argument("--serve", action="store_true",
                    help="open the local review board instead of sweeping")
    ap.add_argument("--port", type=int, default=8765, help="port for --serve")
    ap.add_argument("--evaluate", action="store_true",
                    help="LLM fit-score the postings that cleared the keyword "
                         "bar (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--explain", action="store_true",
                    help="audit your term lists against the postings already "
                         "swept; run this after editing any term list")
    ap.add_argument("--evaluate-limit", type=int, default=15,
                    help="max postings to fit-score in one pass")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    # config.yaml is the shareable template. Anything personal — the cities
    # you can commute to, your comp floor — goes in config.local.yaml, which
    # merges on top and is never meant to be shared or committed.
    # Resolve the overlay next to the config file, NOT the working directory:
    # a cron entry or a run from another folder would otherwise silently fall
    # back to the shipped example config and score against the wrong field.
    cfg_path = Path(args.config)
    local = Path(args.local) if args.local else (
        cfg_path.parent / (cfg_path.stem + ".local.yaml"))
    if local.exists():
        overlay = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
        cfg = deep_merge(cfg, overlay)
        print(f"merged overrides from {local}")

    db = (cfg.get("output") or {}).get("db", "jobsweep.sqlite3")

    if args.serve:
        import dashboard
        dashboard.serve(db, args.port)
        return

    s = cfg["scoring"]

    if args.explain:
        run_explain(cfg, db)
        return

    if args.evaluate:
        run_evaluate(cfg, db, args.evaluate_limit)
        return

    source = make_source(cfg)
    print(f"Sweeping via {cfg['source']['mode']} source…")
    raw = source.fetch()
    swept_total = len(raw)      # before filtering/dedupe, for the digest header
    print(f"  {swept_total} postings returned")

    # A failed sweep and a quiet day both return zero rows. Without this, an
    # expired token on a schedule reports "0 new" every morning forever.
    failed = getattr(source, "failed", [])
    if failed and not raw:
        sys.exit(f"Every track failed ({', '.join(failed)}) — no results are "
                 f"not the same as no new jobs. Check the token and network.")
    if failed:
        print(f"  ! {len(failed)} track(s) failed: {', '.join(failed)}",
              file=sys.stderr)

    # The source mix is the evidence for the premise, so print it every run.
    mix = {}
    for j in raw:
        mix[j.ats or "?"] = mix.get(j.ats or "?", 0) + 1
    if mix:
        top = sorted(mix.items(), key=lambda kv: -kv[1])
        print("  sources: " + ", ".join(f"{k} {v}" for k, v in top[:8])
              + (f" (+{len(top) - 8} more)" if len(top) > 8 else ""))

    # Same req posted under several locations arrives as several rows.
    by_role, deduped = {}, []
    for j in raw:
        key = (j.company.lower().strip(), j.title.lower().strip())
        if key in by_role:
            # Same req under a different location — keep the extra location,
            # but only when it actually differs.
            prior = by_role[key]
            # location can be null from Lever/Ashby; the first row of a
            # duplicated req arriving that way must not crash the sweep.
            parts = [x.strip() for x in (prior.location or "").split(" / ")]
            if j.location and j.location not in parts:
                prior.location = (f"{prior.location} / {j.location}"
                                  if prior.location else j.location)
            continue
        by_role[key] = j
        deduped.append(j)
    if len(deduped) < len(raw):
        print(f"  {len(raw) - len(deduped)} merged as duplicate reqs")
    raw = deduped

    reset_exclusions()
    for j in raw:
        score_job(j, s)

    with store.connect(db) as conn:
        known = store.known_uids(conn)
        fresh = [j for j in raw if j.uid not in known]
        print(f"  {len(fresh)} not seen before")
        if not args.dry_run:
            for j in raw:
                store.upsert(conn, j)

    scored = sorted((j for j in fresh if j.score >= s["threshold"]),
                    key=lambda j: -j.score)
    print(f"  {len(scored)} cleared threshold {s['threshold']}")

    # Show what the exclude list removed. A term quietly eating good roles is
    # the single easiest way to break this tool, and it is otherwise invisible.
    if EXCLUDED_BY:
        parts = ", ".join(f"{k} x{v}" for k, v in
                          sorted(EXCLUDED_BY.items(), key=lambda kv: -kv[1]))
        print(f"  excluded by your local `exclude` list "
              f"(all {len(raw)} swept, not just new): {parts}")
        for term, titles in sorted(EXCLUDED_SAMPLE.items(),
                                   key=lambda kv: -len(kv[1]))[:3]:
            print(f"    {term!r} dropped e.g. {titles[0][:58]!r}")

    if args.dry_run:
        for j in scored[:20]:
            print(f"    [{j.score}] {j.title} — {j.company} ({j.location})")
        print("\nDry run: nothing written.")
        return

    # A second run on the same day that finds nothing must not replace the
    # morning's digest with "nothing new cleared the bar". One that finds
    # something appends to it (see write_digest).
    digest_dir = Path(cfg["output"].get("digest_dir", "digests"))
    today_file = digest_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md"
    if not scored and today_file.exists():
        print(f"\nNothing new — kept {today_file} as it was.")
        return
    digest = write_digest(scored, digest_dir, swept_total)
    print(f"\nDigest: {digest}")


if __name__ == "__main__":
    main()
