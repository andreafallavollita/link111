import sqlite3
c = sqlite3.connect('linkedin_growth.db')
print("=== SENT ===")
for r in c.execute("SELECT id, full_name, status, sent_at FROM prospects WHERE sent_at IS NOT NULL OR status = 'sent'"):
    print(r)
print("\n=== ALL BY STATUS ===")
for r in c.execute("SELECT status, COUNT(*) FROM prospects GROUP BY status"):
    print(r)
