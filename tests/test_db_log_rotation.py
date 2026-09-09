import time

from app.core.db import Database


def make_db(tmp_path):
    return Database(tmp_path / "test.db")


def test_rotate_logs_deletes_old_rows_by_age(tmp_path):
    db = make_db(tmp_path)
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute("INSERT INTO crawl_log (ts, level, message) VALUES (?, 'info', 'old')", (now - 100 * 86400,))
        cur.execute("INSERT INTO crawl_log (ts, level, message) VALUES (?, 'info', 'new')", (now,))

    deleted = db.rotate_logs(max_age_days=30, max_rows=5000)

    assert deleted == 1
    rows = db.recent_logs(10)
    assert len(rows) == 1
    assert rows[0]["message"] == "new"


def test_rotate_logs_trims_to_max_rows(tmp_path):
    db = make_db(tmp_path)
    now = int(time.time())
    with db.cursor() as cur:
        for i in range(10):
            cur.execute(
                "INSERT INTO crawl_log (ts, level, message) VALUES (?, 'info', ?)",
                (now, f"msg{i}"),
            )

    deleted = db.rotate_logs(max_age_days=30, max_rows=5)

    assert deleted == 5
    rows = db.recent_logs(100)
    assert len(rows) == 5
    # the 5 most recent (highest id) survive
    assert {r["message"] for r in rows} == {"msg5", "msg6", "msg7", "msg8", "msg9"}


def test_rotate_logs_noop_when_under_limits(tmp_path):
    db = make_db(tmp_path)
    db.log("info", "hello")
    deleted = db.rotate_logs(max_age_days=30, max_rows=5000)
    assert deleted == 0
    assert len(db.recent_logs(10)) == 1
