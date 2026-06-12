import pandas as pd
import sqlite3
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent
EXCEL_FILE = SCRIPT_DIR / "lead_b2b_eu_martech_growth_2026-06-11 (1).xlsx"
DB_PATH = SCRIPT_DIR / "linkedin_growth.db"
TARGET = 500


def import_to_prospects():
    if not EXCEL_FILE.exists():
        print(f"[error] file not found {EXCEL_FILE}")
        return

    print(f"Reading {EXCEL_FILE.name}")
    df = pd.read_excel(EXCEL_FILE)
    df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]

    columns = {
        'full_name': 'nome_completo',
        'profile_url': 'url_linkedin',
        'company': 'azienda',
        'country': 'paese',
    }

    missing = [c for c in columns if c not in df.columns]
    if missing:
        print(f"[error] missing columns: {missing}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    new = 0
    skip = 0
    for _, row in df.iterrows():
        if new >= TARGET:
            break
        name = str(row.get(columns['full_name'], '')).strip()
        if not name:
            continue
        # dedup
        if conn.execute("SELECT 1 FROM existing_contacts WHERE full_name = ? LIMIT 1", (name,)).fetchone():
            skip += 1
            continue
        if conn.execute("SELECT 1 FROM prospects WHERE full_name = ? LIMIT 1", (name,)).fetchone():
            skip += 1
            continue
        conn.execute(
            "INSERT INTO prospects (full_name, full_name_normalized, profile_url, company, country, profile_id, source, score, status, discovered_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                name.lower().strip(),
                str(row.get(columns['profile_url'], '')),
                str(row.get(columns['company'], '')),
                str(row.get(columns['country'], '')),
                None,  # profile_id; not needed
                'excel_import',
                50,
                'discovered',
                datetime.now().isoformat(timespec='seconds'),
            ),
        )
        new += 1
    conn.commit()
    conn.close()
    print(f"Imported {new} prospects, skipped {skip} duplicates")

if __name__ == "__main__":
    import_to_prospects()
