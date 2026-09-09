import pytest

from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import STATUS_SYNCING, CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "upstream:\n  base_url: http://x\n  username: u\n  password: p\n"
        "crawler:\n  sync_interval_minutes: 0\n"
    )
    mgr = ConfigManager(config_path)
    return CrawlerWorker(db, mgr), db


@pytest.mark.asyncio
async def test_status_shows_syncing_while_full_sync_runs(tmp_path, monkeypatch):
    """A full sync can take a while (several upstream round-trips plus a
    big DB pass). The status must switch to 'syncing' as soon as it starts,
    not keep showing whatever the *previous* loop iteration's status was
    (e.g. a stale 'outside_window' from right after startup) until the sync
    finishes -- that misleads anyone watching the dashboard into thinking
    the crawler is off schedule when it's actually actively working.
    """
    worker, _db = make_worker(tmp_path)
    seen_status_during_sync = []

    async def fake_run_full_sync(*args, **kwargs):
        seen_status_during_sync.append(worker.status()["status"])

    import app.crawler.worker as worker_module

    monkeypatch.setattr(worker_module, "run_full_sync", fake_run_full_sync)
    monkeypatch.setattr(worker_module, "is_within_schedule", lambda cfg: True)

    async def fake_sleep(self, seconds):
        worker._stop_event.set()

    monkeypatch.setattr(CrawlerWorker, "_sleep_checking_stop", fake_sleep)

    await worker._loop_iteration()

    assert seen_status_during_sync == [STATUS_SYNCING]
