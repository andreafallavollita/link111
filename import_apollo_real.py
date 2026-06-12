import csv
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

DB = "linkedin_growth.db"
CSV = "apollo_clean_import.csv"

def clean(v):
    return (v or "").strip()

def normalize_name(name):
    s = clean(name).lower()
    s = re.sub(r"[^a-z0-9àèéìòùäöüßñç\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def normalize_url(url):
    u = clean(url)
    u = u.replace("http://www.linkedin.com", "https://www.linkedin.com")
    u = u.replace("http://linkedin.com", "https://www.linkedin.com")
    return u.rstrip("/")

def profile_id_from_url(url):
    try:
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        if "in" in parts:
            i = parts.index("in")
            if i + 1 < len(parts):
                return parts[i + 1].strip()
        return parts[-1].strip() if parts else ""
    except Exception:
        return ""

if not Path(DB).exists():
    raise SystemExit(f"DB not found: {DB}")

if not Path(CSV).exists():
    raise SystemExit(f"CSV not found: {CSV}")

conn = sqlite3.connect(DB)
cur = conn.cursor()

cur.execute("SELECT profile_url FROM prospects WHERE profile_url IS NOT NULL AND profile_url <> ''")
existing_urls = {normalize_url(r[0]).lower() for r in cur.fetchall()}

cur.execute("SELECT profile_id FROM prospects WHERE profile_id IS NOT NULL AND profile_id <> ''")
existing_ids = {clean(r[0]).lower() for r in cur.fetchall()}

with open(CSV, newline="", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

inserted = 0
duplicates = 0
invalid = 0
seen_urls = set()
seen_ids = set()
now = datetime.now().isoformat(timespec="seconds")

for row in rows:
    full_name = clean(row.get("full_name"))
    profile_url = normalize_url(row.get("profile_url"))
    pid = profile_id_from_url(profile_url)

    if not full_name or not profile_url:
        invalid += 1
        continue

    url_key = profile_url.lower()
    pid_key = pid.lower()

    if url_key in existing_urls or url_key in seen_urls or (pid_key and (pid_key in existing_ids or pid_key in seen_ids)):
        duplicates += 1
        continue

    seen_urls.add(url_key)
    if pid_key:
        seen_ids.add(pid_key)

    title = clean(row.get("title"))
    company = clean(row.get("company"))
    country = clean(row.get("country"))

    notes = {
        "source": "apollo",
        "company": company,
        "title": title,
        "country": country,
        "company_website": clean(row.get("company_website")),
        "company_linkedin_url": clean(row.get("company_linkedin_url")),
        "industry": clean(row.get("industry")),
        "employees": clean(row.get("employees")),
        "original_notes": clean(row.get("notes")),
    }

    cur.execute("""
        INSERT INTO prospects (
            full_name,
            full_name_normalized,
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
            score,
            score_breakdown,
            status,
            discovered_at,
            notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        full_name,
        normalize_name(full_name),
        title,
        company,
        title,
        country,
        country,
        None,
        profile_url,
        pid,
        None,
        "apollo",
        "apollo",
        "apollo_clean_import.csv",
        70,
        json.dumps({"source": "apollo", "base_score": 70}, ensure_ascii=False),
        "discovered",
        now,
        json.dumps(notes, ensure_ascii=False),
    ))

    inserted += 1

conn.commit()

print(f"Rows read: {len(rows)}")
print(f"Inserted: {inserted}")
print(f"Duplicates skipped: {duplicates}")
print(f"Invalid skipped: {invalid}")

cur.execute("SELECT status, COUNT(*) FROM prospects GROUP BY status")
print("Status counts:")
for status, count in cur.fetchall():
    print(status, count)

conn.close()
