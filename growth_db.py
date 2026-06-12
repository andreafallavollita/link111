"""LinkedIn growth agent: SQLite schema and DB helpers.

Single source of truth for:
- existing_contacts: people I already know / already contacted (dedup pool)
- prospects: profiles discovered by the discovery agent, scored and queued
- daily_quota: invitations sent per day with cap detection
- discovery_log: discovery runs (search/engagement) telemetry

All other modules import from here. Init is idempotent.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Iterable, Iterator, Optional

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "linkedin_growth.db"

ICP_SEGMENTS = {
    "cmo_head": "CMO / Head of Marketing / VP / Director / Head of Growth",
    "ops_automation": "Marketing Automation / MarTech / CRM / Marketing Ops / Growth Engineer",
    "founder_ai": "Founder / CEO / CTO of AI / martech / automation startup",
    "consultant_agency": "Marketing / AI consultant, agency owner, fractional CMO",
}

VALID_PROSPECT_STATUSES = {
    "discovered",
    "queued",
    "sending",
    "sent",
    "accepted",
    "declined",
    "withdrawn",
    "skipped",
    "error",
    "already_connected",
    "already_pending",
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS existing_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    full_name_normalized TEXT NOT NULL,
    profile_url TEXT,
    profile_id TEXT,
    source TEXT NOT NULL,
    status TEXT,
    company TEXT,
    headline TEXT,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_existing_name ON existing_contacts(full_name_normalized);
CREATE INDEX IF NOT EXISTS idx_existing_url ON existing_contacts(profile_url);
CREATE INDEX IF NOT EXISTS idx_existing_profile_id ON existing_contacts(profile_id);

CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    full_name_normalized TEXT NOT NULL,
    headline TEXT,
    company TEXT,
    title TEXT,
    location TEXT,
    country TEXT,
    language TEXT,
    profile_url TEXT NOT NULL UNIQUE,
    profile_id TEXT,
    connection_degree INTEGER,
    icp_segment TEXT,
    source TEXT NOT NULL,
    source_detail TEXT,
    score INTEGER NOT NULL,
    score_breakdown TEXT,
    status TEXT NOT NULL DEFAULT 'discovered',
    discovered_at TEXT NOT NULL,
    queued_at TEXT,
    sent_at TEXT,
    accepted_at TEXT,
    withdrawn_at TEXT,
    error_message TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
CREATE INDEX IF NOT EXISTS idx_prospects_score ON prospects(score DESC);
CREATE INDEX IF NOT EXISTS idx_prospects_name ON prospects(full_name_normalized);
CREATE INDEX IF NOT EXISTS idx_prospects_profile_id ON prospects(profile_id);
CREATE INDEX IF NOT EXISTS idx_prospects_icp ON prospects(icp_segment);

CREATE TABLE IF NOT EXISTS daily_quota (
    date TEXT PRIMARY KEY,
    target_count INTEGER NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    cap_reached INTEGER NOT NULL DEFAULT 0,
    cap_resume_date TEXT,
    captcha_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    ended_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS discovery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    query TEXT NOT NULL,
    page INTEGER,
    found_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS run_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    items_processed INTEGER DEFAULT 0,
    error_message TEXT,
    extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_health_component ON run_health(component, started_at DESC);
"""


def _last_id(cursor: sqlite3.Cursor) -> int:
    rowid = cursor.lastrowid
    if rowid is None:
        raise RuntimeError("INSERT did not return a rowid")
    return int(rowid)


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def db_session(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with db_session(db_path) as conn:
        conn.executescript(SCHEMA)


def normalize_name(name: Optional[str]) -> str:
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().strip()
    text = re.sub(r"[^\w\s\-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def extract_profile_id(profile_url: Optional[str]) -> Optional[str]:
    if not profile_url:
        return None
    match = re.search(r"/in/([^/?#]+)", profile_url)
    if not match:
        return None
    return match.group(1).lower().rstrip("-")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


def upsert_existing_contact(
    conn: sqlite3.Connection,
    *,
    full_name: str,
    source: str,
    profile_url: Optional[str] = None,
    profile_id: Optional[str] = None,
    status: Optional[str] = None,
    company: Optional[str] = None,
    headline: Optional[str] = None,
) -> int:
    norm = normalize_name(full_name)
    if not profile_id and profile_url:
        profile_id = extract_profile_id(profile_url)

    if profile_id:
        row = conn.execute(
            "SELECT id FROM existing_contacts WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
    elif profile_url:
        row = conn.execute(
            "SELECT id FROM existing_contacts WHERE profile_url = ?",
            (profile_url,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM existing_contacts WHERE full_name_normalized = ? AND source = ?",
            (norm, source),
        ).fetchone()

    if row:
        conn.execute(
            """
            UPDATE existing_contacts
               SET full_name = COALESCE(?, full_name),
                   profile_url = COALESCE(?, profile_url),
                   profile_id = COALESCE(?, profile_id),
                   status = COALESCE(?, status),
                   company = COALESCE(?, company),
                   headline = COALESCE(?, headline)
             WHERE id = ?
            """,
            (full_name, profile_url, profile_id, status, company, headline, row["id"]),
        )
        return int(row["id"])

    cursor = conn.execute(
        """
        INSERT INTO existing_contacts
            (full_name, full_name_normalized, profile_url, profile_id,
             source, status, company, headline, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            full_name,
            norm,
            profile_url,
            profile_id,
            source,
            status,
            company,
            headline,
            now_iso(),
        ),
    )
    return _last_id(cursor)


def is_existing_contact(
    conn: sqlite3.Connection,
    *,
    full_name: Optional[str] = None,
    profile_url: Optional[str] = None,
    profile_id: Optional[str] = None,
    require_url_match: bool = False,
) -> bool:
    """Check whether a contact is already in the dedup pool.

    When `require_url_match=True`, only an exact profile_id or profile_url
    match counts. Use this for new prospects whose name alone may collide
    with a name-only entry in the pool (e.g. a row in Elenco linkedin.xlsx
    with no profile URL).
    """
    if profile_id:
        row = conn.execute(
            "SELECT 1 FROM existing_contacts WHERE profile_id = ? LIMIT 1",
            (profile_id,),
        ).fetchone()
        if row:
            return True
    if profile_url and not profile_id:
        pid = extract_profile_id(profile_url)
        if pid:
            row = conn.execute(
                "SELECT 1 FROM existing_contacts WHERE profile_id = ? LIMIT 1",
                (pid,),
            ).fetchone()
            if row:
                return True
        row = conn.execute(
            "SELECT 1 FROM existing_contacts WHERE profile_url = ? LIMIT 1",
            (profile_url,),
        ).fetchone()
        if row:
            return True
    if require_url_match:
        return False
    if full_name:
        norm = normalize_name(full_name)
        if norm:
            row = conn.execute(
                "SELECT 1 FROM existing_contacts WHERE full_name_normalized = ? LIMIT 1",
                (norm,),
            ).fetchone()
            if row:
                return True
    return False


def insert_prospect(
    conn: sqlite3.Connection,
    *,
    full_name: str,
    profile_url: str,
    score: int,
    source: str,
    headline: Optional[str] = None,
    company: Optional[str] = None,
    title: Optional[str] = None,
    location: Optional[str] = None,
    country: Optional[str] = None,
    language: Optional[str] = None,
    connection_degree: Optional[int] = None,
    icp_segment: Optional[str] = None,
    source_detail: Optional[str] = None,
    score_breakdown: Optional[dict] = None,
) -> Optional[int]:
    """Insert a new prospect. Returns the new id, or None if it already existed
    (either as prospect or already in existing_contacts)."""
    profile_id = extract_profile_id(profile_url)
    if is_existing_contact(
        conn,
        full_name=full_name,
        profile_url=profile_url,
        profile_id=profile_id,
        require_url_match=True,
    ):
        return None

    norm = normalize_name(full_name)
    existing = conn.execute(
        "SELECT id FROM prospects WHERE profile_url = ? LIMIT 1",
        (profile_url,),
    ).fetchone()
    if existing:
        return None

    try:
        cursor = conn.execute(
            """
            INSERT INTO prospects
                (full_name, full_name_normalized, headline, company, title,
                 location, country, language, profile_url, profile_id,
                 connection_degree, icp_segment, source, source_detail,
                 score, score_breakdown, status, discovered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?)
            """,
            (
                full_name,
                norm,
                headline,
                company,
                title,
                location,
                country,
                language,
                profile_url,
                profile_id,
                connection_degree,
                icp_segment,
                source,
                source_detail,
                int(score),
                json.dumps(score_breakdown) if score_breakdown else None,
                now_iso(),
            ),
        )
        return _last_id(cursor)
    except sqlite3.IntegrityError:
        return None


def get_top_prospects(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    min_score: int = 0,
    statuses: Iterable[str] = ("discovered", "queued"),
    prefer_languages: Optional[list[str]] = None,
) -> list[sqlite3.Row]:
    status_list = list(statuses)
    placeholders = ",".join("?" * len(status_list))
    lang_order = ""
    params: list = [min_score] + status_list
    if prefer_languages:
        cases = []
        for i, lang in enumerate(prefer_languages):
            cases.append(f"WHEN language = ? THEN {i}")
            params.append(lang)
        lang_order = f"CASE {' '.join(cases)} ELSE {len(prefer_languages)} END,"
    sql = f"""
        SELECT * FROM prospects
         WHERE score >= ?
           AND status IN ({placeholders})
         ORDER BY {lang_order} score DESC, discovered_at ASC
         LIMIT ?
    """
    params.append(int(limit))
    return list(conn.execute(sql, params).fetchall())


def update_prospect_status(
    conn: sqlite3.Connection,
    prospect_id: int,
    status: str,
    *,
    error_message: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    if status not in VALID_PROSPECT_STATUSES:
        raise ValueError(f"Invalid status: {status}")
    timestamp = now_iso()
    set_clauses = ["status = ?"]
    params: list = [status]
    if status == "queued":
        set_clauses.append("queued_at = ?")
        params.append(timestamp)
    elif status == "sent":
        set_clauses.append("sent_at = ?")
        params.append(timestamp)
    elif status == "accepted":
        set_clauses.append("accepted_at = ?")
        params.append(timestamp)
    elif status == "withdrawn":
        set_clauses.append("withdrawn_at = ?")
        params.append(timestamp)
    if error_message is not None:
        set_clauses.append("error_message = ?")
        params.append(error_message)
    if notes is not None:
        set_clauses.append("notes = ?")
        params.append(notes)
    params.append(prospect_id)
    conn.execute(
        f"UPDATE prospects SET {', '.join(set_clauses)} WHERE id = ?",
        params,
    )


def ensure_today_quota(conn: sqlite3.Connection, target: int) -> sqlite3.Row:
    today = today_iso()
    row = conn.execute(
        "SELECT * FROM daily_quota WHERE date = ?", (today,)
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO daily_quota
                (date, target_count, sent_count, skipped_count, error_count,
                 cap_reached, captcha_count, started_at)
            VALUES (?, ?, 0, 0, 0, 0, 0, ?)
            """,
            (today, int(target), now_iso()),
        )
        row = conn.execute(
            "SELECT * FROM daily_quota WHERE date = ?", (today,)
        ).fetchone()
    return row


def increment_today_quota(
    conn: sqlite3.Connection,
    *,
    sent: int = 0,
    skipped: int = 0,
    error: int = 0,
    captcha: int = 0,
) -> None:
    today = today_iso()
    conn.execute(
        """
        UPDATE daily_quota
           SET sent_count = sent_count + ?,
               skipped_count = skipped_count + ?,
               error_count = error_count + ?,
               captcha_count = captcha_count + ?
         WHERE date = ?
        """,
        (sent, skipped, error, captcha, today),
    )


def mark_cap_reached(conn: sqlite3.Connection, resume_date: str, note: str = "") -> None:
    today = today_iso()
    conn.execute(
        """
        UPDATE daily_quota
           SET cap_reached = 1,
               cap_resume_date = ?,
               ended_at = ?,
               notes = COALESCE(notes, '') || ?
         WHERE date = ?
        """,
        (resume_date, now_iso(), f"\n[{now_iso()}] {note}" if note else "", today),
    )


def is_cap_active(conn: sqlite3.Connection) -> tuple[bool, Optional[str]]:
    """Returns (cap_active, resume_date). Cap is active if the most recent cap
    record has a resume_date in the future or today."""
    row = conn.execute(
        """
        SELECT cap_resume_date
          FROM daily_quota
         WHERE cap_reached = 1
           AND cap_resume_date IS NOT NULL
         ORDER BY date DESC
         LIMIT 1
        """
    ).fetchone()
    if not row or not row["cap_resume_date"]:
        return False, None
    resume = row["cap_resume_date"]
    today = today_iso()
    return today < resume, resume


def end_today_quota(conn: sqlite3.Connection, note: str = "") -> None:
    today = today_iso()
    conn.execute(
        """
        UPDATE daily_quota
           SET ended_at = ?,
               notes = COALESCE(notes, '') || ?
         WHERE date = ?
        """,
        (now_iso(), f"\n[{now_iso()}] {note}" if note else "", today),
    )


def log_discovery_run(
    conn: sqlite3.Connection,
    *,
    source: str,
    query: str,
    page: Optional[int],
    found_count: int,
    new_count: int,
    status: str,
    started_at: str,
    error_message: Optional[str] = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO discovery_log
            (source, query, page, found_count, new_count,
             started_at, ended_at, status, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            query,
            page,
            int(found_count),
            int(new_count),
            started_at,
            now_iso(),
            status,
            error_message,
        ),
    )
    return _last_id(cursor)


def log_run_health(
    conn: sqlite3.Connection,
    *,
    component: str,
    started_at: str,
    status: str,
    items_processed: int = 0,
    error_message: Optional[str] = None,
    extra: Optional[dict] = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO run_health
            (component, started_at, ended_at, status,
             items_processed, error_message, extra_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            component,
            started_at,
            now_iso(),
            status,
            int(items_processed),
            error_message,
            json.dumps(extra) if extra else None,
        ),
    )
    return _last_id(cursor)


def stats_overview(conn: sqlite3.Connection) -> dict:
    out: dict = {}
    out["existing_contacts_total"] = conn.execute(
        "SELECT COUNT(*) AS c FROM existing_contacts"
    ).fetchone()["c"]
    out["existing_by_source"] = {
        row["source"]: row["c"]
        for row in conn.execute(
            "SELECT source, COUNT(*) AS c FROM existing_contacts GROUP BY source"
        )
    }
    out["prospects_total"] = conn.execute(
        "SELECT COUNT(*) AS c FROM prospects"
    ).fetchone()["c"]
    out["prospects_by_status"] = {
        row["status"]: row["c"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS c FROM prospects GROUP BY status"
        )
    }
    out["prospects_by_icp"] = {
        row["icp_segment"] or "unknown": row["c"]
        for row in conn.execute(
            "SELECT icp_segment, COUNT(*) AS c FROM prospects GROUP BY icp_segment"
        )
    }
    out["last_7_days_sent"] = conn.execute(
        """
        SELECT COALESCE(SUM(sent_count), 0) AS c
          FROM daily_quota
         WHERE date >= date('now', '-7 days')
        """
    ).fetchone()["c"]
    return out


def main() -> None:
    print(f"Initializing DB at {DB_PATH}")
    init_db()
    with db_session() as conn:
        stats = stats_overview(conn)
    print("OK. Current stats:")
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
