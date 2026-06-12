import sqlite3
import sys

def check_counts():
    try:
        conn = sqlite3.connect('linkedin_growth.db')
        cursor = conn.cursor()

        # Totale prospect
        cursor.execute("SELECT COUNT(*) FROM prospects")
        total = cursor.fetchone()[0]
        print(f"Total prospects: {total}")

        # Conteggio per status
        cursor.execute("SELECT status, COUNT(*) FROM prospects GROUP BY status")
        statuses = cursor.fetchall()
        print("\nStatus counts:")
        for status, count in statuses:
            print(f"  {status}: {count}")

        # Discovered e Sent
        cursor.execute("SELECT COUNT(*) FROM prospects WHERE status = 'discovered'")
        discovered_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM prospects WHERE status = 'sent'")
        sent_count = cursor.fetchone()[0]
        print(f"\nDiscovered: {discovered_count}")
        print(f"Sent: {sent_count}")

        # Ultimi 20 discovered
        print("\nLast 20 discovered:")
        cursor.execute("SELECT * FROM prospects WHERE status = 'discovered' ORDER BY id DESC LIMIT 20")
        rows = cursor.fetchall()
        for row in rows:
            print(row)

        # Top 20 discovered per score
        # Gestione colonna score: se manca, ordina per ID
        try:
            cursor.execute("SELECT * FROM prospects WHERE status = 'discovered' ORDER BY score DESC LIMIT 20")
        except sqlite3.OperationalError:
            print("\nColumn 'score' not found, using default order")
            cursor.execute("SELECT * FROM prospects WHERE status = 'discovered' LIMIT 20")
        
        print("\nTop 20 discovered by score:")
        rows = cursor.fetchall()
        for row in rows:
            print(row)

        conn.close()
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    check_counts()
