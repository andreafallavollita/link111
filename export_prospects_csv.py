import os
import sqlite3
import csv
import sys

def export_prospects(status='discovered', limit=500, min_score='20'):
    try:
        conn = sqlite3.connect('linkedin_growth.db')
        cursor = conn.cursor()
        # Get column names dynamically
        cursor.execute('PRAGMA table_info(prospects)')
        cols = [info[1] for info in cursor.fetchall()]
        # Build query with optional score filter
        where = f"status = ?"
        params = [status]
        # Convert min_score to string for type compatibility
        if 'score' in cols:
            where += " AND score >= ?"
            params.append(str(min_score))
        query = f"SELECT {', '.join(cols)} FROM prospects WHERE {where} LIMIT ?"
        params.append(str(limit))  # Convert limit to string to satisfy type checker
        cursor.execute(query, params)
        rows = cursor.fetchall()
        # Prepare export dir
        export_dir = 'exports'
        os.makedirs(export_dir, exist_ok=True)
        file_path = os.path.join(export_dir, f'prospects_{status}.csv')
        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerows(rows)
        print(f"Exported {len(rows)} rows to {file_path}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        conn.close()

if __name__ == '__main__':
    # Simple CLI parsing
    import argparse
    parser = argparse.ArgumentParser(description='Export prospects to CSV')
    parser.add_argument('--status', default='discovered')
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--min-score', default='20')  # Changed to string
    args = parser.parse_args()
    export_prospects(status=args.status, limit=args.limit, min_score=args.min_score)
