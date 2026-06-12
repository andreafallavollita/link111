"""LinkedIn growth dashboard.

Reads linkedin_growth.db and prints a concise terminal report.

Usage:
  report_growth.py           # full snapshot
  report_growth.py --week    # add weekly trend
  report_growth.py --csv     # also export growth_dashboard.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import growth_db as db

SCRIPT_DIR = Path(__file__).parent
CSV_OUT = SCRIPT_DIR / "growth_dashboard.csv"
GOAL = 5000


def fmt_int(n) -> str:
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return str(n)


def section(title: str) -> None:
    print()
    print("=" * 62)
    print(f"  {title}")
    print("=" * 62)


def kv(label: str, value, width: int = 28) -> None:
    print(f"  {label:<{width}} {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", action="store_true", help="Show weekly trend table")
    parser.add_argument("--csv", action="store_true", help="Export full CSV snapshot")
    parser.add_argument("--top", type=int, default=10, help="Top N prospects in queue")
    args = parser.parse_args()

    db.init_db()
    with db.db_session() as conn:
        stats = db.stats_overview(conn)

        existing_total = stats["existing_contacts_total"]
        prospects_total = stats["prospects_total"]
        by_status = stats["prospects_by_status"]
        by_icp = stats["prospects_by_icp"]
        by_source = stats["existing_by_source"]
        last_7 = stats["last_7_days_sent"]

        connected_now = conn.execute(
            "SELECT COUNT(*) AS c FROM existing_contacts WHERE status='connected'"
        ).fetchone()["c"]
        sent_total = conn.execute(
            "SELECT COUNT(*) AS c FROM prospects WHERE status IN ('sent', 'accepted')"
        ).fetchone()["c"]
        queue = by_status.get("discovered", 0) + by_status.get("queued", 0)
        accepted = by_status.get("accepted", 0)
        already_skip = by_status.get("already_connected", 0) + by_status.get("already_pending", 0)

        cap_active, resume = db.is_cap_active(conn)

        today_row = conn.execute(
            "SELECT * FROM daily_quota WHERE date = ?", (db.today_iso(),)
        ).fetchone()

        week_rows = conn.execute(
            """
            SELECT date, target_count, sent_count, skipped_count, error_count,
                   cap_reached, captcha_count
              FROM daily_quota
             WHERE date >= date('now', '-14 days')
             ORDER BY date DESC
            """
        ).fetchall()

        top_prospects = conn.execute(
            """
            SELECT id, full_name, score, icp_segment, language, country, headline, profile_url
              FROM prospects
             WHERE status IN ('discovered', 'queued')
             ORDER BY score DESC, discovered_at ASC
             LIMIT ?
            """,
            (args.top,),
        ).fetchall()

        last_runs = conn.execute(
            """
            SELECT component, started_at, ended_at, status, items_processed, error_message
              FROM run_health
             ORDER BY id DESC
             LIMIT 8
            """
        ).fetchall()

    # ============================================================
    # OVERVIEW
    # ============================================================
    section("LINKEDIN GROWTH AGENT - OVERVIEW")
    base_connected = max(connected_now, 0)
    sent_so_far = sent_total
    progress_total = base_connected + sent_so_far
    pct = 100.0 * progress_total / GOAL if GOAL else 0
    bar_w = 40
    filled = int(bar_w * min(progress_total, GOAL) / GOAL) if GOAL else 0
    bar = "#" * filled + "-" * (bar_w - filled)

    kv("Goal", f"{fmt_int(GOAL)} contacts")
    kv("Current connected", fmt_int(base_connected))
    kv("Requests sent (queue)", fmt_int(sent_so_far))
    kv("Progress estimate", f"{fmt_int(progress_total)} / {fmt_int(GOAL)} ({pct:.1f}%)")
    print(f"  [{bar}]")

    # ETA estimate based on last 7d
    if last_7 > 0:
        per_day = last_7 / 7.0
        accept_rate = 0.22
        per_day_accepted = per_day * accept_rate
        remaining = max(0, GOAL - progress_total)
        if per_day_accepted > 0:
            days = remaining / per_day_accepted
            eta = f"{days:.0f} days (~{days/30:.1f} months, ~{days/365:.1f} years) at current pace"
        else:
            eta = "n/a (no acceptances yet)"
    else:
        eta = "n/a (no sends in last 7 days)"
    kv("ETA to goal", eta)

    # ============================================================
    # TODAY
    # ============================================================
    section("TODAY")
    if cap_active:
        kv("Status", f"WEEKLY CAP ACTIVE - resumes {resume}")
    if today_row:
        kv("Target", today_row["target_count"])
        kv("Sent", today_row["sent_count"])
        kv("Skipped", today_row["skipped_count"])
        kv("Errors", today_row["error_count"])
        kv("CAPTCHAs", today_row["captcha_count"])
        kv("Cap reached today", "yes" if today_row["cap_reached"] else "no")
    else:
        kv("Status", "Not yet started today")

    # ============================================================
    # DEDUP POOL
    # ============================================================
    section("DEDUP POOL (existing_contacts)")
    kv("Total", fmt_int(existing_total))
    for src, c in sorted(by_source.items(), key=lambda x: -x[1]):
        kv(f"  {src}", fmt_int(c), width=30)

    # ============================================================
    # PROSPECTS
    # ============================================================
    section("PROSPECTS (discovery agent output)")
    kv("Total discovered", fmt_int(prospects_total))
    kv("In queue (to send)", fmt_int(queue))
    kv("Sent (this DB)", fmt_int(by_status.get("sent", 0)))
    kv("Accepted", fmt_int(accepted))
    kv("Already connected (skip)", fmt_int(by_status.get("already_connected", 0)))
    kv("Already pending (skip)", fmt_int(by_status.get("already_pending", 0)))
    kv("Skipped (other)", fmt_int(by_status.get("skipped", 0)))
    kv("Errors", fmt_int(by_status.get("error", 0)))

    print()
    print("  By ICP segment:")
    for icp, c in sorted(by_icp.items(), key=lambda x: -x[1]):
        kv(f"    {icp}", fmt_int(c), width=32)

    # ============================================================
    # TOP QUEUE
    # ============================================================
    if top_prospects:
        section(f"TOP {len(top_prospects)} PROSPECTS IN QUEUE")
        for p in top_prospects:
            print(
                f"  [{p['score']:3d}] {p['full_name']:<35s} "
                f"| {(p['icp_segment'] or '?'):<18s} "
                f"| {(p['country'] or '?'):<10s} "
                f"| {p['language'] or '?'}"
            )
            if p["headline"]:
                hl = p["headline"]
                if len(hl) > 90:
                    hl = hl[:87] + "..."
                print(f"        {hl}")

    # ============================================================
    # WEEK TREND
    # ============================================================
    if args.week and week_rows:
        section("LAST 14 DAYS")
        print(f"  {'date':<12s} {'target':>7s} {'sent':>6s} {'skip':>6s} {'err':>5s} {'capt':>5s} {'cap':>4s}")
        for r in week_rows:
            cap_mark = "YES" if r["cap_reached"] else "-"
            print(f"  {r['date']:<12s} {r['target_count']:>7d} {r['sent_count']:>6d} {r['skipped_count']:>6d} {r['error_count']:>5d} {r['captcha_count']:>5d} {cap_mark:>4s}")

    # ============================================================
    # LAST RUNS
    # ============================================================
    if last_runs:
        section("LAST RUN HEALTH")
        for r in last_runs:
            print(f"  {r['started_at']} | {r['component']:<18s} | {r['status']:<20s} | items={r['items_processed']}")
            if r["error_message"]:
                print(f"      ERROR: {r['error_message'][:120]}")

    # ============================================================
    # CSV EXPORT
    # ============================================================
    if args.csv:
        with CSV_OUT.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["metric", "value"])
            w.writerow(["goal", GOAL])
            w.writerow(["connected_now", base_connected])
            w.writerow(["sent_total_db", sent_so_far])
            w.writerow(["progress_estimate", progress_total])
            w.writerow(["progress_pct", f"{pct:.1f}"])
            w.writerow(["existing_pool_total", existing_total])
            w.writerow(["prospects_total", prospects_total])
            w.writerow(["queue_size", queue])
            w.writerow(["last_7d_sent", last_7])
            w.writerow(["cap_active", "1" if cap_active else "0"])
            w.writerow(["cap_resume_date", resume or ""])
            for icp, c in by_icp.items():
                w.writerow([f"icp_{icp or 'unknown'}", c])
            for src, c in by_source.items():
                w.writerow([f"existing_source_{src}", c])
        print(f"\n  CSV written to {CSV_OUT}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
