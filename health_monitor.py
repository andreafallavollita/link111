"""Health monitor for the LinkedIn growth agent.

Two complementary mechanisms (both optional):

   1. Healthchecks.io heartbeat (cloud fallback "you haven't run in 36h"):
        Set HEALTHCHECK_URL in .env (e.g. https://hc-ping.com/<uuid>).
        Call ping_heartbeat(status) at end of every run.
        If healthchecks.io doesn't receive a ping in the configured period
        (set on their dashboard), it sends you an email / Telegram / Discord /
        Slack alert. Free for up to 20 checks.

   2. Telegram alerts (immediate inline events):
        Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
        Call send_telegram(message) on important events (cap reached, errors,
        daily summary, etc.).

   3. Telegram command listener (polling mode):
        Every time this script runs (via Task Scheduler every 15 min), it
        checks Telegram for pending commands (/status, /queue, /report, etc.)
        and replies with current stats.

Both are no-ops if env vars are missing, so the agent works without them.

CLI:
  python health_monitor.py status                   # summary of monitor config
  python health_monitor.py ping --status ok         # ping healthchecks.io
  python health_monitor.py telegram --message "hello"
  python health_monitor.py check-stale --hours 36   # alert if last_run is stale
  python health_monitor.py listen                   # check Telegram for commands
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent
LAST_RUN_FILE = SCRIPT_DIR / "last_run.json"

load_dotenv()


def healthcheck_url() -> Optional[str]:
    return os.environ.get("HEALTHCHECK_URL")


def telegram_creds() -> tuple[Optional[str], Optional[str]]:
    return (
        os.environ.get("TELEGRAM_BOT_TOKEN"),
        os.environ.get("TELEGRAM_CHAT_ID"),
    )


def ping_heartbeat(status: str = "success", payload: str = "") -> bool:
    """Pings healthchecks.io.

    status: 'success' (default), 'fail', 'start', or an HTTP status code like '/123'.
    """
    base = healthcheck_url()
    if not base:
        return False
    url = base.rstrip("/")
    if status == "fail":
        url = f"{url}/fail"
    elif status == "start":
        url = f"{url}/start"
    elif status not in ("success", "ok"):
        url = f"{url}/{status}"
    try:
        data = payload.encode("utf-8") if payload else None
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"  [health] ping failed: {exc}", file=sys.stderr)
        return False


def send_telegram(message: str) -> bool:
    token, chat_id = telegram_creds()
    if not token or not chat_id:
        return False
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            api,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"  [telegram] send failed: {exc}", file=sys.stderr)
        return False


def get_updates(offset: int = 0) -> list:
    """Fetch pending Telegram messages. Returns list of updates."""
    token, _ = telegram_creds()
    if not token:
        return []
    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=5&offset={offset}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            return data.get("result", [])
    except Exception:
        return []
    return []


def reply_telegram(text: str, reply_to: int = 0) -> bool:
    """Send a reply message. If reply_to is set, thread-reply to that message."""
    token, chat_id = telegram_creds()
    if not token or not chat_id:
        return False
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    params = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_to:
        params["reply_to_message_id"] = reply_to
    body = urllib.parse.urlencode(params).encode("utf-8")
    try:
        req = urllib.request.Request(
            api, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as exc:
        print(f"  [telegram] reply failed: {exc}", file=sys.stderr)
        return False


def read_last_run() -> Optional[dict]:
    if not LAST_RUN_FILE.exists():
        return None
    try:
        with LAST_RUN_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def last_run_age_hours() -> Optional[float]:
    data = read_last_run()
    if not data or "timestamp" not in data:
        return None
    try:
        ts = data["timestamp"]
        if ts.endswith("Z"):
            ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 3600
    except Exception:
        return None


def status_summary() -> dict:
    hc = healthcheck_url()
    tg_token, tg_chat = telegram_creds()
    age = last_run_age_hours()
    return {
        "healthcheck_configured": bool(hc),
        "healthcheck_url_hint": (hc[:40] + "..." if hc and len(hc) > 40 else hc),
        "telegram_configured": bool(tg_token and tg_chat),
        "last_run_file": str(LAST_RUN_FILE),
        "last_run_file_exists": LAST_RUN_FILE.exists(),
        "last_run_age_hours": age,
    }


def cmd_status(_args) -> int:
    info = status_summary()
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


def cmd_ping(args) -> int:
    if not healthcheck_url():
        print("HEALTHCHECK_URL not configured in .env. Add it to enable.")
        return 1
    ok = ping_heartbeat(status=args.status)
    print(f"Ping status={args.status}: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


def cmd_telegram(args) -> int:
    token, chat = telegram_creds()
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not configured in .env.")
        print()
        print("To set up:")
        print("  1. Open Telegram, talk to @BotFather, /newbot, get token.")
        print("  2. Send any message to the bot.")
        print("  3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates and copy chat.id.")
        print("  4. Add to .env:")
        print("       TELEGRAM_BOT_TOKEN=...")
        print("       TELEGRAM_CHAT_ID=...")
        return 1
    ok = send_telegram(args.message)
    print(f"Telegram send: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


def _queue_stats_text() -> str:
    """Query the DB and return a concise status text for Telegram."""
    try:
        import growth_db as gdb
        with gdb.db_session() as conn:
            stats = gdb.stats_overview(conn)
            by_status = stats["prospects_by_status"]
            sent = by_status.get("sent", 0) + by_status.get("accepted", 0)
            queue = by_status.get("discovered", 0) + by_status.get("queued", 0)
            connected = by_status.get("already_connected", 0)
            pending = by_status.get("already_pending", 0)
            skipped = by_status.get("skipped", 0)
            total = stats["prospects_total"]
            existing = stats["existing_contacts_total"]
            week = stats["last_7_days_sent"]
            cap_active, resume = gdb.is_cap_active(conn)
            today = conn.execute(
                "SELECT sent_count, error_count FROM daily_quota WHERE date = ?",
                (gdb.today_iso(),)
            ).fetchone()
            today_sent = today["sent_count"] if today else 0
            today_err = today["error_count"] if today else 0
    except Exception as exc:
        return f"<b>Errore DB</b>: {exc}"

    lines = [
        f"<b>LinkedIn Growth Agent</b>",
        f"Inviati oggi: {today_sent}  (errori: {today_err})",
        f"Ultimi 7gg: {week}",
        f"Totale inviati: {sent} / 5000",
        f"In coda (queue): {queue}",
        f"Già connessi: {connected}  |  In sospeso: {pending}  |  Saltati: {skipped}",
        f"Prospect totali: {total}  |  Contatti in pool: {existing}",
    ]
    if cap_active:
        resume_str = resume or "?"
        lines.append(f"<b>CAP attivo</b>: riprendi il {resume_str}")
    age = last_run_age_hours()
    if age is not None:
        lines.append(f"Ultima run: {age:.1f}h fa")
    return "\n".join(lines)


def _weekly_report_text() -> str:
    """Return last 7 days of sends as text."""
    try:
        import growth_db as gdb
        with gdb.db_session() as conn:
            rows = conn.execute(
                """
                SELECT date, sent_count, error_count, skipped_count
                FROM daily_quota
                WHERE date >= date('now', '-14 days')
                ORDER BY date DESC
                """
            ).fetchall()
    except Exception as exc:
        return f"<b>Errore DB</b>: {exc}"
    if not rows:
        return "Nessun dato disponibile."
    lines = ["<b>Giorni recenti</b>", "Data        | Inviati | Errori | Saltati"]
    for r in rows[:10]:
        lines.append(f"{r['date']} | {r['sent_count']:>7} | {r['error_count']:>6} | {r['skipped_count']:>7}")
    return "\n".join(lines)


def _top_prospects_text(n: int = 5) -> str:
    """Return top prospects in queue."""
    try:
        import growth_db as gdb
        with gdb.db_session() as conn:
            rows = conn.execute(
                """
                SELECT full_name, score, icp_segment, headline
                FROM prospects
                WHERE status = 'discovered'
                ORDER BY score DESC
                LIMIT ?
                """,
                (n,),
            ).fetchall()
    except Exception as exc:
        return f"<b>Errore DB</b>: {exc}"
    if not rows:
        return "Nessun prospect in coda."
    lines = [f"<b>Top {n} prospect in coda</b>"]
    for r in rows:
        score = r["score"]
        name = (r["full_name"] or "?")[:25]
        icp = r["icp_segment"] or "?"
        headline = (r["headline"] or "")[:40]
        lines.append(f"  [{score}] {name} ({icp})")
    return "\n".join(lines)


HELP_TEXT = """<b>Comandi disponibili:</b>
/status — statistiche generali
/queue — top prospect in coda
/report — inviati ultimi 14 giorni
/help — questo messaggio"""

LAST_UPDATE_OFFSET_FILE = Path(__file__).parent / ".telegram_offset"


def _read_offset() -> int:
    try:
        return int(LAST_UPDATE_OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    try:
        LAST_UPDATE_OFFSET_FILE.write_text(str(offset))
    except Exception:
        pass


def cmd_listen(_args) -> int:
    """Check Telegram for pending commands and reply."""
    token, chat_id = telegram_creds()
    if not token or not chat_id:
        print("Telegram not configured.")
        return 1
    offset = _read_offset()
    updates = get_updates(offset=offset)
    if not updates:
        print("No new messages.")
        _save_offset(offset)
        return 0
    for upd in updates:
        update_id = upd.get("update_id", 0)
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip().lower()
        msg_id = msg.get("message_id", 0)
        if not text:
            _save_offset(update_id + 1)
            continue
        print(f"  [{update_id}] msg={msg_id} text='{text}'")
        if text in ("/status", "/start"):
            reply = _queue_stats_text()
        elif text == "/queue":
            reply = _top_prospects_text(10)
        elif text == "/report":
            reply = _weekly_report_text()
        elif text == "/help":
            reply = HELP_TEXT
        else:
            reply = f"Comando sconosciuto. {HELP_TEXT}"
        if reply_telegram(reply, reply_to=msg_id):
            print(f"  replied OK")
        _save_offset(update_id + 1)
    return 0


def cmd_check_stale(args) -> int:
    age = last_run_age_hours()
    if age is None:
        msg = f"<b>LinkedIn growth: no last_run.json found</b>\nThe agent has never reported a successful run on this machine, or the file is missing.\nHost: {os.environ.get('COMPUTERNAME', 'unknown')}"
        print(msg)
        if send_telegram(msg):
            print("Telegram alert sent.")
        ping_heartbeat(status="fail", payload="no last_run.json")
        return 2
    if age > args.hours:
        msg = (
            f"<b>LinkedIn growth: STALE RUN</b>\n"
            f"Last run was {age:.1f} hours ago (threshold: {args.hours}h).\n"
            f"Check if the PC is powered on and the scheduled task fired.\n"
            f"Host: {os.environ.get('COMPUTERNAME', 'unknown')}"
        )
        print(msg)
        if send_telegram(msg):
            print("Telegram alert sent.")
        ping_heartbeat(status="fail", payload=f"stale {age:.1f}h")
        return 2
    print(f"OK: last run {age:.1f} hours ago (under {args.hours}h threshold).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Print monitor configuration").set_defaults(func=cmd_status)

    p_ping = sub.add_parser("ping", help="Send heartbeat to healthchecks.io")
    p_ping.add_argument("--status", default="success", help="success | fail | start")
    p_ping.set_defaults(func=cmd_ping)

    p_tg = sub.add_parser("telegram", help="Send a message via Telegram")
    p_tg.add_argument("--message", required=True)
    p_tg.set_defaults(func=cmd_telegram)

    p_chk = sub.add_parser("check-stale", help="Alert if last_run is older than --hours")
    p_chk.add_argument("--hours", type=float, default=36.0)
    p_chk.set_defaults(func=cmd_check_stale)

    p_listen = sub.add_parser("listen", help="Check Telegram for pending commands and reply")
    p_listen.set_defaults(func=cmd_listen)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
