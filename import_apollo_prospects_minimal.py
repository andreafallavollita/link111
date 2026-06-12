import argparse
import csv
import sqlite3
import sys
from pathlib import Path

DB_PATH = "linkedin_growth.db"


def get_table_columns(db_path: str):
    """Return list of column names in prospects table."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(prospects)")
        cols = [row[1] for row in cur.fetchall()]
        return cols
    finally:
        conn.close()


def map_csv_to_db(row, db_columns):
    """Map CSV row dict to DB column dict, focusing only on required fields."""
    mapped = {}

    # full_name (obbligatorio)
    if 'full_name' in db_columns:
        mapped['full_name'] = row.get('full_name', '').strip()
    else:
        # Se column mancante, usiamo notes (ma specifiche dicono di ignorare)
        pass

    # profile_url (obbligatorio)
    if 'profile_url' in db_columns:
        mapped['profile_url'] = row.get('profile_url', '').strip()
    else:
        # Se column mancante, row viene saltata (specifica richiede profile_url)
        return None

    # Dati opzionali ma desiderati se colonne esistono
    if 'headline' in db_columns:
        mapped['headline'] = row.get('title', '').strip()  # title -> headline
    if 'status' in db_columns:
        mapped['status'] = 'discovered'
    if 'source' in db_columns:
        mapped['source'] = 'apollo'
    if 'score' in db_columns:
        mapped['score'] = 70  # punteggio fissato a 70 per tutti Apollo

    return mapped

def main():
    parser = argparse.ArgumentParser(description='Import Apollo prospects (minimal set: full_name, profile_url)')
    parser.add_argument('--file', required=True, help='Path to Apollo CSV export')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be imported')
    args = parser.parse_args()

    csv_path = Path(args.file)
    if not csv_path.is_file():
        print(f"ERROR: File not found: {csv_path}")
        sys.exit(1)

    # Verifica esistenza DB e columns
    if not Path(DB_PATH).exists():
        print(f"ERROR: Database not found: {DB_PATH}")
        sys.exit(1)
    db_columns = get_table_columns(DB_PATH)

    # Verifica presenza colonne obbligatorie
    required_cols = ['full_name', 'profile_url']
    missing_cols = [col for col in required_cols if col not in db_columns]
    if missing_cols:
        print(f"ERROR: Missing required columns in DB: {', '.join(missing_cols)}")
        sys.exit(1)

    # Leggi CSV
    rows = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Filtra righe senza profile_url
            if not row.get('profile_url', '').strip():
                continue
            rows.append(row)

    total_rows = len(rows)
    print(f"Rows read from CSV: {total_rows}")

    # Deduplica per profile_url
    seen_profiles = set()
    valid_imports = 0
    duplicates = 0
    new_prospects = []  # per dry-run

    # Connessione DB per esistenti
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        if 'profile_url' in db_columns:
            cur.execute("SELECT profile_url FROM prospects WHERE profile_url IS NOT NULL AND profile_url <> ''")
            for (url,) in cur.fetchall():
                if url:
                    seen_profiles.add(url.strip().lower())
    finally:
        conn.close()

    # Processa righe
    for row in rows:
        profile_url = row.get('profile_url', '').strip().lower()

        # Se profile_url giÃ  in DB o visto
        if profile_url in seen_profiles:
            duplicates += 1
            continue
        seen_profiles.add(profile_url)

        # Mappa dati
        mapped = map_csv_to_db(row, db_columns)
        if mapped is None:  # row saltata per profile_url vuoto
            continue

        new_prospects.append(mapped)
        valid_imports += 1

    print(f"Valid imports: {valid_imports}")  # righe senza profile_url saltate non contate
    print(f"Duplicates skipped: {duplicates}")

    if args.dry_run:
        print("\n=== DRY-RUN OUTPUT ===")
        print(f"Total valid imports: {valid_imports}")
        print(f"Duplicates: {duplicates}")
        print("\nFirst 10 imported:")
        for i, rec in enumerate(new_prospects[:10], 1):
            print(f"{i}. full_name: {rec['full_name']}, profile_url: {rec['profile_url']}")
        if len(new_prospects) > 10:
            print(f"... and {len(new_prospects)-10} more")
        print("No database changes made in dry-run")
    else:
        # Insempio nella DB
        # (non eseguito in seca futuro per safety)
        print("Database insertion would be executed here")

if __name__ == '__main__':
    main()
