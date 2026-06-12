"""Reset errored prospects back to discovered state for re-testing."""

import growth_db as db

db.init_db()
conn = db.get_connection()
n = conn.execute(
    "UPDATE prospects SET status='discovered', error_message=NULL WHERE status='error'"
).rowcount
print(f"Reset {n} prospects to 'discovered'.")
q = conn.execute(
    "SELECT COUNT(*) AS c FROM prospects WHERE status='discovered'"
).fetchone()["c"]
print(f"Current queue size: {q}")
