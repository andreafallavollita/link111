"""
Import prospects from Excel (lead list) into the prospects table.

Usage:
  python import_excel_prospects.py path\to\file.xlsx

Maps columns:
  Nome completo -> full_name
  Ruolo -> title
  Azienda -> company
  Paese -> country
  URL LinkedIn -> profile_url (dedup key)
  Fit score -> score (Molto alto=85, Alto=70, Medio=50, Basso=30)

Skips duplicates by LinkedIn URL. Inserts with status='queued' and source='eu_martech_growth'.
"""

import sys
import pandas as pd
from pathlib import Path
import growth_db as db

SCORE_MAP = {
    "molto alto": 85,
    "alto": 70,
    "medio": 50,
    "basso": 30,
    "molto basso": 15,
}


def score_value(raw: str) -> int:
    if not raw:
        return 50
    raw_clean = raw.strip().lower()
    return SCORE_MAP.get(raw_clean, 50)


def main():
    if len(sys.argv) < 2:
        print("Usage: python import_excel_prospects.py <path_to_excel>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    df = pd.read_excel(path)
    total_rows = len(df)
    print(f"Read {total_rows} rows from {path.name}")

    db.init_db()
    new_count = 0
    skip_exists = 0
    skip_already_sent = 0

    with db.db_session() as conn:
        for _, row in df.iterrows():
            name = str(row.get("Nome completo", "")).strip()
            url = str(row.get("URL LinkedIn", "")).strip()
            if not name or not url:
                continue

            score_raw = str(row.get("Fit score", "")).strip()
            score = score_value(score_raw)
            title = str(row.get("Ruolo", "")) or None
            company = str(row.get("Azienda", "")) or None
            country = str(row.get("Paese", "")) or None

            result = db.insert_prospect(
                conn,
                full_name=name,
                profile_url=url,
                score=score,
                source="eu_martech_growth",
                headline=title,
                company=company,
                title=title,
                country=country,
                icp_segment="eu_martech_growth",
                source_detail="lead_b2b_eu_martech_growth_2026-06-11",
                language="en",
            )
            if result is None:
                skip_exists += 1
            else:
                new_count += 1

    print(f"  New prospects inserted:   {new_count}")
    print(f"  Duplicates skipped:       {skip_exists}")

    with db.db_session() as conn:
        all_p = conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
        queued = conn.execute(
            "SELECT COUNT(*) FROM prospects WHERE status IN ('discovered','queued')"
        ).fetchone()[0]
        sent = conn.execute(
            "SELECT COUNT(*) FROM prospects WHERE status IN ('sent','already_connected','already_pending')"
        ).fetchone()[0]

    print(f"  Total prospects in DB:    {all_p}")
    print(f"  In queue for sending:     {queued}")
    print(f"  Already sent/connected:   {sent}")


if __name__ == "__main__":
    main()
