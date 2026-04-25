"""
OncoSearch Retention Worker
It is a standalone process running the data retention scheduler independently of the HIU.

Why is it seperated from HIU services?
- Multiple HIU replicas can scale freely without each spawning its own scheduler
- HIU becomes stateless w.r.t. cleanup — it only serves HTTP

Retention strategy (two-phase):
  Phase 1 (hourly)      — soft-delete expired health_data_cache rows + audit log
  Phase 2 (daily 2 AM)  — hard-delete soft-deleted rows older than 24h + mark consent_transactions EXPIRED
"""

import os
import time
import logging
import psycopg2
import psycopg2.extras
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RetentionWorker] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://oncosearch_user:oncosearch_pass@oncosearch-postgres:5432/oncosearch_db"
)


def get_db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def cleanup_expired_data():
    """
    Phase 1: Soft-delete expired health data (hourly).
    Marks rows as deleted and writes audit trail.
    """
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        cur.execute("""
            UPDATE health_data_cache
            SET is_deleted = TRUE, deleted_at = NOW()
            WHERE expires_at < NOW() AND is_deleted = FALSE
            RETURNING transaction_id, abha_address
        """)
        expired_rows = cur.fetchall()

        for row in expired_rows:
            cur.execute(
                "INSERT INTO data_access_log (abha_address, transaction_id, action) VALUES (%s, %s, %s)",
                (row['abha_address'], row['transaction_id'], 'DATA_EXPIRED')
            )

        conn.commit()
        conn.close()
        log.info("Phase 1 cleanup: soft-deleted %d expired health data records", len(expired_rows))
    except Exception as e:
        log.error("Phase 1 cleanup failed: %s", e)


def hard_delete_expired_data():
    """
    Phase 2: Hard-delete soft-deleted data (daily at 2:00 AM UTC).
    Also marks consent transactions as EXPIRED.
    """
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        cur.execute("""
            DELETE FROM health_data_cache
            WHERE is_deleted = TRUE AND deleted_at < NOW() - INTERVAL '24 hours'
        """)
        deleted_count = cur.rowcount

        cur.execute("""
            UPDATE consent_transactions
            SET status = 'EXPIRED'
            WHERE data_erase_at < NOW() AND status NOT IN ('EXPIRED', 'ERROR')
        """)
        expired_txn_count = cur.rowcount

        conn.commit()
        conn.close()
        log.info(
            "Phase 2 cleanup: hard-deleted %d health records, marked %d transactions EXPIRED",
            deleted_count, expired_txn_count
        )
    except Exception as e:
        log.error("Phase 2 cleanup failed: %s", e)


def wait_for_db(retries: int = 20, delay: int = 3) -> None:
    """Block until the database is reachable."""
    for attempt in range(1, retries + 1):
        try:
            conn = get_db_conn()
            conn.close()
            log.info("Database connection established")
            return
        except Exception as e:
            log.warning("DB not ready (attempt %d/%d): %s", attempt, retries, e)
            time.sleep(delay)
    raise RuntimeError("Could not connect to database after %d attempts" % retries)


if __name__ == "__main__":
    wait_for_db()

    scheduler = BlockingScheduler()
    scheduler.add_job(cleanup_expired_data, IntervalTrigger(hours=1), id="soft_delete")
    scheduler.add_job(hard_delete_expired_data, CronTrigger(hour=2, minute=0), id="hard_delete")

    log.info("Retention worker started — soft-delete every hour, hard-delete daily at 02:00 UTC")

    # Run once immediately on startup so first-boot doesn't skip a cycle
    cleanup_expired_data()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Retention worker stopped")
