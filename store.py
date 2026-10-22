"""SQLite store: postings, fit scores, and your application status.

Dedupes across sweeps, and holds the things you accumulate during a search:
what you applied to, what went quiet, and what a scoring pass thought of
each role.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("new", "shortlist", "applied", "interview", "offer",
            "rejected", "silence", "skipped")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid          TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    company      TEXT NOT NULL,
    location     TEXT,
    url          TEXT,
    ats          TEXT,
    track        TEXT,
    posted_at    TEXT,
    arrangement  TEXT,
    experience   TEXT,
    salary       TEXT,
    score        INTEGER,
    reasons      TEXT,
    description  TEXT,
    first_seen   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'new',
    notes        TEXT DEFAULT '',
    fit_score    INTEGER,
    fit_verdict  TEXT,
    fit_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(score DESC);
"""


# SQLite's default journal writes a side-file next to the database, which
# some filesystems refuse — network shares, OneDrive/Dropbox folders, and
# Windows folders mounted into a Linux VM all fail with "disk I/O error".
# TRUNCATE reuses one journal file instead of creating and deleting one, and
# works in all those places; MEMORY is a last resort (fast, but a crash
# mid-write can leave the database inconsistent).
_JOURNAL_MODES = ("TRUNCATE", "MEMORY")


@contextmanager
def connect(path: str | Path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        last = None
        for mode in _JOURNAL_MODES:
            try:
                conn.execute(f"PRAGMA journal_mode={mode}")
                conn.executescript(SCHEMA)
                last = None
                break
            except sqlite3.OperationalError as e:
                last = e
        if last is not None:
            raise sqlite3.OperationalError(
                f"could not initialise {path}: {last}. If it is on a synced "
                f"or network folder, set output.db to a local path."
            ) from last
        yield conn
        conn.commit()
    finally:
        conn.close()


def known_uids(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT uid FROM jobs")}


def upsert(conn, job, status: str = "new") -> bool:
    """Insert a posting. Returns True if it was new.

    An existing row keeps its status, notes and fit score — a re-sweep must
    never clobber the fact that you already applied to something.
    """
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute("SELECT 1 FROM jobs WHERE uid = ?", (job.uid,))
    if cur.fetchone():
        # Refresh the fields a later sweep can improve — notably description,
        # since a row first seen without one would otherwise keep an LLM fit
        # score computed from a title alone, and fit scores are never redone.
        conn.execute(
            """UPDATE jobs SET score=?, reasons=?, location=?, salary=?,
                   arrangement=COALESCE(NULLIF(?,''), arrangement),
                   experience=COALESCE(NULLIF(?,''), experience),
                   posted_at=COALESCE(NULLIF(?,''), posted_at),
                   description=COALESCE(NULLIF(?,''), description)
               WHERE uid=?""",
            (job.score, json.dumps(job.reasons), job.location, job.salary,
             job.arrangement, job.experience, job.posted_at,
             job.description, job.uid),
        )
        return False
    conn.execute(
        """INSERT INTO jobs (uid,title,company,location,url,ats,track,posted_at,
                             arrangement,experience,salary,score,reasons,
                             description,first_seen,status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job.uid, job.title, job.company, job.location, job.url, job.ats,
         job.track, job.posted_at, job.arrangement, job.experience, job.salary,
         job.score, json.dumps(job.reasons), job.description, now,
         status),
    )
    return True


def set_status(conn, uid: str, status: str,
               notes: str | None = None) -> int:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
    if notes is None:
        cur = conn.execute("UPDATE jobs SET status=? WHERE uid=?", (status, uid))
    else:
        cur = conn.execute("UPDATE jobs SET status=?, notes=? WHERE uid=?",
                           (status, notes, uid))
    return cur.rowcount


def save_fit(conn, uid: str, fit: dict) -> None:
    conn.execute(
        "UPDATE jobs SET fit_score=?, fit_verdict=?, fit_json=? WHERE uid=?",
        (fit.get("fit_score"), fit.get("recommendation"), json.dumps(fit), uid),
    )


def unscored(conn, min_score: int, limit: int) -> list[sqlite3.Row]:
    """Rows that cleared the cheap keyword bar but have no LLM fit score yet."""
    return list(conn.execute(
        """SELECT * FROM jobs
           WHERE fit_score IS NULL AND score >= ? AND status NOT IN ('skipped')
           ORDER BY score DESC LIMIT ?""", (min_score, limit)))


def all_jobs(conn, status: str | None = None) -> list[sqlite3.Row]:
    """Rows for display, best fit first.

    With no status, "skipped" rows are left out so they do not sit on the
    board beside the live postings. Ask for status="skipped" explicitly to
    see them.
    """
    if status:
        return list(conn.execute(
            "SELECT * FROM jobs WHERE status=? ORDER BY COALESCE(fit_score,-1) DESC,"
            " score DESC", (status,)))
    return list(conn.execute(
        "SELECT * FROM jobs WHERE status != 'skipped' "
        "ORDER BY COALESCE(fit_score,-1) DESC, score DESC"))


def corpus(conn, limit: int = 4000) -> list[sqlite3.Row]:
    """Rows for auditing term lists — every status, including skipped.

    all_jobs() is the display query and hides "skipped", which is a status
    the user can set on any row. Auditing through it meant marking postings
    skipped on the board silently shrank the audit corpus, and skipping them
    all made --explain exit as though nothing had ever been swept.
    """
    return list(conn.execute(
        "SELECT title, description, location FROM jobs "
        "ORDER BY first_seen DESC LIMIT ?", (limit,)))


def counts(conn) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
