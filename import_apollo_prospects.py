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
    """Map CSV row dict to DB column dict, using notes fallback when column missing."""
    mapped = {}
    notes_parts = []

    # full_name -> full_name
    if 'full_name' in db_columns:
        mapped['full_name'] = row.get('full_name', '').strip()
    else:
        notes_parts.append(f"full_name: {row.get('full_name', '')}")

    # title -> headline
    if 'headline' in db_columns:
        mapped['headline'] = row.get('title', '').strip()
    else:
        notes_parts.append(f"title: {row.get('title', '')}")

    # country -> country
    if 'country' in db_columns:
        mapped['country'] = row.get('country', '').strip()
    else:
        notes_parts.append(f"country: {row.get('country', '')}")

    # source -> source (hardcoded apollo)
    if 'source' in db_columns:
        mapped['source'] = 'apollo'
    else:
        notes_parts.append("source: apollo")

    # profile_url -> profile_url
    if 'profile_url' in db_columns:
        mapped['profile_url'] = row.get('profile_url', '').strip()
    else:
        notes_parts.append(f"profile_url: {row.get('profile_url', '')}")

    # email -> email (if column exists, else notes)
    if 'email' in db_columns:
        mapped['email'] = row.get('email', '').strip()
    else:
        notes_parts.append(f"email: {row.get('email', '')}")

    # company -> company (if column exists)
    if 'company' in db_columns:
        mapped['company'] = row.get('company', '').strip()
    else:
        notes_parts.append(f"company: {row.get('company', '')}")

    # company_website -> company_website (if column exists)
    if 'company_website' in db_columns:
        mapped['company_website'] = row.get('company_website', '').strip()
    else:
        notes_parts.append(f"company_website: {row.get('company_website', '')}")

    # company_linkedin_url -> company_linkedin_url (if column exists)
    if 'company_linkedin_url' in db_columns:
        mapped['company_linkedin_url'] = row.get('company_linkedin_url', '').strip()
    else:
        notes_parts.append(f"company_linkedin_url: {row.get('company_linkedin_url', '')}")

    # industry -> industry (if column exists)
    if 'industry' in db_columns:
        mapped['industry'] = row.get('industry', '').strip()
    else:
        notes_parts.append(f"industry: {row.get('industry', '')}")

    # employees -> employees (if column exists)
    if 'employees' in db_columns:
        # try to keep as string; could be integer but store as text
        mapped['employees'] = str(row.get('employees', '')).strip()
    else:
        notes_parts.append(f"employees: {row.get('employees', '')}")

    # status -> status
    if 'status' in db_columns:
        mapped['status'] = 'discovered'
    else:
        notes_parts.append("status: discovered")

    # notes: combine original notes (if any) and fallback parts
    original_notes = row.get('notes', '').strip()
    if original_notes:
        notes_parts.append(original_notes)
    if 'notes' in db_columns:
        mapped['notes'] = " | ".join(notes_parts).strip()
    else:
        # If notes column missing, we ignore extra info (could also put into another column but spec says leave empty)
        pass

    # score: assign 70 for Apollo leads with email verified and LinkedIn URL present
    # email_status column in CSV may indicate verification
    email_status = row.get('email_status', '').strip().lower()
    has_linkedin = bool(row.get('profile_url', '').strip())
    score_val = 70 if email_status == 'verified' and has_linkedin else 0
    if 'score' in db_columns:
        mapped['score'] = score_val
    else:
        # If score column missing, we could store in notes but spec says assign default 70; we ignore for now.
        pass

    return mapped

def main():
    parser = argparse.ArgumentParser(description='Import Apollo prospects into linkedin_growth.db (dry-run by default).')
    parser.add_argument('--file', required=True, help='Path to Apollo CSV export')
    parser.add_argument('--dry-run', action='store_true', help='Only show what would be imported, do not write to DB')
    args = parser.parse_args()

    csv_path = Path(args.file)
    if not csv_path.is_file():
        print(f"ERROR: File not found: {csv_path}")
        sys.exit(1)

    if not Path(DB_PATH).exists():
        print(f"ERROR: Database not found: {DB_PATH}")
        sys.exit(1)

    db_columns = get_table_columns(DB_PATH)

    # Read CSV
    rows = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    total_rows = len(rows)
    print(f"Rows read from CSV: {total_rows}")

    # Determine dedup keys from existing DB
    existing_keys = set()
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        if 'profile_url' in db_columns:
            cur.execute("SELECT profile_url FROM prospects WHERE profile_url IS NOT NULL AND profile_url <> ''")
            for (url,) in cur.fetchall():
                if url:
                    existing_keys.add(url.strip())
        # If profile_url column missing or we also want to fallback to full_name+company, we could also load those
        # but spec says deduplicate primarily by profile_url, fallback to full_name+company when profile_url missing.
        # We'll also load existing full_name+company pairs for fallback dedup when profile_url missing in DB row.
        if 'full_name' in db_columns and 'company' in db_columns:
            cur.execute("SELECT full_name, company FROM prospects WHERE full_name IS NOT NULL AND company IS NOT NULL")
            for fname, comp in cur.fetchall():
                if fname and comp:
                    existing_keys.add((fname.strip().lower(), comp.strip().lower()))
    finally:
        conn.close()

    # Process rows
    seen_keys = set()
    valid_rows = 0
    duplicate_rows = 0
    new_rows = []  # list of mapped dicts ready for insertion

    for row in rows:
        # Basic validation: require at least full_name not empty
        if not row.get('full_name', '').strip():
            continue  # skip invalid
        valid_rows += 1

        mapped = map_csv_to_db(row, db_columns)

        # Determine dedup key
        profile_url = mapped.get('profile_url', '').strip()
        full_name = mapped.get('full_name', '').strip().lower()
        company = mapped.get('company', '').strip().lower()
        key = None
        if profile_url:
            key = ('profile_url', profile_url.lower())
        else:
            # fallback to full_name+company
            key = ('name_company', f"{full_name}|{company}")

        # Check against existing DB and seen in this batch
        if key in existing_keys or key in seen_keys:
            duplicate_rows += 1
            continue
        seen_keys.add(key)
        new_rows.append(mapped)

    print(f"Valid rows (non-empty full_name): {valid_rows}")
    print(f"Duplicate rows (already in DB or seen): {duplicate_rows}")
    print(f"New rows to insert: {len(new_rows)}")

    if args.dry_run:
        print("\n=== DRY-RUN: First 10 new prospects ===")
        for i, rec in enumerate(new_rows[:10], start=1):
            print(f"{i}. full_name: {rec.get('full_name')}, headline: {rec.get('headline')}, profile_url: {rec.get('profile_url')}, email: {rec.get('email')}, score: {rec.get('score')}")
        if len(new_rows) > 10:
            print(f"... and {len(new_rows) - 10} more")
        print("\nDRY-RUN completed. No changes made to DB.")
    else:
        # Actual insertion (not requested, but we could implement if user confirms)
        print("Insertion not performed because --dry-run was not specified. Add --dry-run flag to see what would be imported.")
        # For safety, we do not perform insert without explicit flag; user asked to not import without confirmation.

if __name__ == '__main__':
    main()