import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


DB_PATH = "linkedin_growth.db"


def get_discovered_count(db_path: str = DB_PATH) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM prospects WHERE status = 'discovered'")
        row = cur.fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def get_status_counts(db_path: str = DB_PATH) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, COUNT(*) FROM prospects GROUP BY status")
        return {str(status): int(count) for status, count in cur.fetchall()}
    finally:
        conn.close()


def next_progressive_target(current: int, final_target: int, step: int) -> int:
    if current >= final_target:
        return final_target

    next_target = ((current // step) + 1) * step
    if next_target <= current:
        next_target = current + step

    return min(next_target, final_target)


def build_discovery_command(
    queue_target: int,
    seed: int,
    min_score: int,
    max_queries: int,
    max_pages: int,
) -> list[str]:
    return [
        sys.executable,
        "discovery_agent.py",
        "--mode",
        "search",
        "--queue-target",
        str(queue_target),
        "--min-score",
        str(min_score),
        "--max-queries",
        str(max_queries),
        "--max-pages",
        str(max_pages),
        "--seed",
        str(seed),
    ]


def command_to_text(cmd: list[str]) -> str:
    return " ".join(cmd)


def run_discovery(
    queue_target: int,
    seed: int,
    min_score: int,
    max_queries: int,
    max_pages: int,
    dry_run: bool,
) -> int:
    cmd = build_discovery_command(
        queue_target=queue_target,
        seed=seed,
        min_score=min_score,
        max_queries=max_queries,
        max_pages=max_pages,
    )

    if dry_run:
        print(f"[DRY-RUN] {command_to_text(cmd)}")
        return 0

    print(f"[RUN] {command_to_text(cmd)}")
    result = subprocess.run(cmd, check=False)

    if result.returncode != 0:
        print(f"ERROR: discovery_agent.py exited with code {result.returncode}")
        return result.returncode

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a LinkedIn prospect list using discovery_agent.py search mode only."
    )

    parser.add_argument("--target", type=int, default=500)
    parser.add_argument("--batch-target-step", type=int, default=50)
    parser.add_argument("--min-score", type=int, default=20)
    parser.add_argument("--max-queries", type=int, default=40)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--start-seed", type=int, default=700)
    parser.add_argument("--sleep-between-batches", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-zero-batches", type=int, default=3)

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not Path(DB_PATH).exists():
        print(f"ERROR: database not found: {DB_PATH}")
        return 1

    if args.batch_target_step <= 0:
        print("ERROR: --batch-target-step must be > 0")
        return 1

    if args.target <= 0:
        print("ERROR: --target must be > 0")
        return 1

    discovered_before = get_discovered_count()
    discovered_after = discovered_before

    print("=== BUILD PROSPECT LIST ===")
    print(f"Current discovered: {discovered_before}")
    print(f"Final target:        {args.target}")
    print(f"Batch step:          {args.batch_target_step}")
    print(f"Dry run:             {args.dry_run}")
    print("")

    if discovered_before >= args.target:
        print("Target already reached.")
        print(f"Final discovered count: {discovered_before}")
        return 0

    batch_number = 0
    consecutive_zeros = 0
    current_seed = args.start_seed

    while discovered_before < args.target and consecutive_zeros < args.max_zero_batches:
        batch_number += 1

        queue_target = next_progressive_target(
            current=discovered_before,
            final_target=args.target,
            step=args.batch_target_step,
        )

        rc = run_discovery(
            queue_target=queue_target,
            seed=current_seed,
            min_score=args.min_score,
            max_queries=args.max_queries,
            max_pages=args.max_pages,
            dry_run=args.dry_run,
        )

        if rc != 0:
            print("STOP: discovery command failed.")
            return rc

        if args.dry_run:
            discovered_after = queue_target
        else:
            if args.sleep_between_batches > 0:
                time.sleep(args.sleep_between_batches)

            discovered_after = get_discovered_count()

        new_added = discovered_after - discovered_before

        print(
            f"Batch {batch_number}: "
            f"Seed={current_seed} | "
            f"Before={discovered_before} | "
            f"After={discovered_after} | "
            f"New={new_added} | "
            f"QueueTarget={queue_target}"
        )

        if not args.dry_run:
            print(f"Status counts: {get_status_counts()}")

        if new_added <= 0:
            consecutive_zeros += 1
        else:
            consecutive_zeros = 0

        discovered_before = discovered_after
        current_seed += 1

        print("")

    if consecutive_zeros >= args.max_zero_batches:
        print("STOP: discovery exhausted or mostly duplicates.")
        print(f"Consecutive zero batches: {consecutive_zeros}")

    print(f"Final discovered count: {discovered_after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())