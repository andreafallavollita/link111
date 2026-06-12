import pandas as pd
import sqlite3
from pathlib import Path
from datetime import datetime

# Paths
EXCEL_FILE = Path(r"C:\Users\andrea.fallavollita\OneDrive - Marketing Multimedia\File di chat di Microsoft Teams\Documenti\richieste linkedln\lead_b2b_eu_martech_growth_2026-06-11 (1).xlsx")
DB_PATH = Path(r"C:\Users\andrea.fallavollita\OneDrive - Marketing Multimedia\File di chat di Microsoft Teams\Documenti\richieste linkedln\linkedin_growth.db")

# Map normalized Excel columns to DB columns
COLUMNS = {
    'nome_completo': 'full_name',
    'url_linkedin': 'profile_url',
    'azienda': 'company',
    'paese': 'country'
}


def import_all():
    if not EXCEL_FILE.exists():
        print('[error] Excel file not found')
        return

    df = pd.read_excel(EXCEL_FILE)
    df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]

    for col in COLUMNS:
        if col not in df.columns:
            print(f'[error] missing column: {col}')
            return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    new = 0
    skip = 0

    for _, row in df.iterrows():
        name = str(row.get(COLUMNS['nome_completo'], '')).strip()
        if not name:
            continue

        # Skip if already in prospects
        if conn.execute('SELECT 1 FROM prospects WHERE full_name = ? LIMIT 1', (name,)).fetchone():
            skip += 1
            continue

        conn.execute(
            'INSERT INTO prospects (full_name, profile_url, company, country, status, discovered_at) VALUES (?,?,?,?,?,?)',
            (
                name,
                str(row.get(COLUMNS['url_linkedin'], '')),
                str(row.get(COLUMNS['azienda'], '')),
                str(row.get(COLUMNS['paese'], '')),
                'discovered',
                datetime.now().isoformat(timespec='seconds')
            )
        )
        new += 1

    conn.commit()
    conn.close()
    print(f'Inserted {new} prospects, skipped {skip} duplicates.')

if __name__ == '__main__':
    import_all()