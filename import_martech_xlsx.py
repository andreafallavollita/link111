"""Convert and import martech xlsx to existing_contacts."""
import sys
from pathlib import Path
import pandas as pd
import growth_db as db

SCRIPT_DIR = Path(__file__).parent
XLSX_FILE = SCRIPT_DIR / "lead_b2b_eu_martech_growth_2026-06-11 (1).xlsx"

def main():
    if not XLSX_FILE.exists():
        print(f"File not found: {XLSX_FILE}")
        return 1
    df = pd.read_excel(XLSX_FILE)
    # Normalize column names
    df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]
    
    # Map common columns
    name_candidates = ['full_name', 'name', 'first_name', 'firstname', 'lastname', 'nome_completo', 'nome completo']
    name_col = next((c for c in name_candidates if c in df.columns), None)
    if not name_col:
        print("No name column found")
        return 1
    
    # Determine profile_url column
    url_candidates = ['profile_url', 'linkedin_url', 'url', 'linkedin', 'url_linkedin', 'url linkedin']
    profile_col = next((c for c in url_candidates if c in df.columns), None)
    
    # Determine company column
    company_candidates = ['company', 'company_name', 'organization', 'employer', 'azienda']
    company_col = next((c for c in company_candidates if c in df.columns), None)
    
    new = 0
    skip = 0
    with db.db_session() as conn:
        for _, row in df.iterrows():
            name = str(row[name_col]).strip() if pd.notna(row[name_col]) else ''
            if not name:
                continue
            before = conn.execute("SELECT COUNT(*) AS c FROM existing_contacts WHERE full_name = ?", (name,)).fetchone()['c']
            if before:
                skip += 1
                continue
            db.upsert_existing_contact(
                conn,
                full_name=name,
                source='eu_martech_growth',
                profile_url=str(row[profile_col]) if profile_col and pd.notna(row[profile_col]) else None,
                company=str(row[company_col]) if company_col and pd.notna(row[company_col]) else None,
                status='discovered',
            )
            new += 1
    print(f"Imported {new} new, skipped {skip} existing")
    return 0

if __name__ == "__main__":
    sys.exit(main())