"""_has_free_slot: the provider counts the very player_api call this makes
in active_cons, so an idle single-connection account reports
active_cons == 1. The check must subtract that so ffprobe can still run.
"""
import pytest

from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    return CrawlerWorker(db, ConfigManager(cfg_path))


class FakeClient:
    def __init__(self, active, max_conns):
        self._active = active
        self._max = max_conns

    async def player_api(self, params):
        return {"user_info": {"active_cons": str(self._active), "max_connections": str(self._max)}}


@pytest.mark.asyncio
async def test_idle_single_connection_account_has_a_slot(tmp_path):
    """active_cons=1 (just this check), max_connections=1, reserve=0 ->
    other_connections=0 < 1 -> free."""
    worker = make_worker(tmp_path)
    cfg = {"crawler": {"reserve_slots": 0}}
    assert await worker._has_free_slot(cfg, FakeClient(active=1, max_conns=1)) is True


@pytest.mark.asyncio
async def test_single_connection_account_in_use_has_no_slot(tmp_path):
    """A live stream running: active_cons=2 (stream + this check),
    other_connections=1, not < 1 -> blocked."""
    worker = make_worker(tmp_path)
    cfg = {"crawler": {"reserve_slots": 0}}
    assert await worker._has_free_slot(cfg, FakeClient(active=2, max_conns=1)) is False


@pytest.mark.asyncio
async def test_reserve_slots_still_respected(tmp_path):
    worker = make_worker(tmp_path)
    # max 3, reserve 1 -> ffprobe may use up to 2. active_cons=2 means
    # other_connections=1 < 2 -> free; active_cons=3 -> other=2, not < 2.
    assert await worker._has_free_slot({"crawler": {"reserve_slots": 1}}, FakeClient(2, 3)) is True
    assert await worker._has_free_slot({"crawler": {"reserve_slots": 1}}, FakeClient(3, 3)) is False


@pytest.mark.asyncio
async def test_unparseable_or_missing_info_does_not_block(tmp_path):
    worker = make_worker(tmp_path)

    class Bad:
        async def player_api(self, params):
            return {"user_info": {"active_cons": "?", "max_connections": None}}

    assert await worker._has_free_slot({"crawler": {}}, Bad()) is True
