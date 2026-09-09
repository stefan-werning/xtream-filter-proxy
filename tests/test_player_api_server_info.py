from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import xtream as xtream_module
from app.api.xtream import router as xtream_router
from app.core.config import ConfigManager
from app.core.db import Database


def make_app(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("upstream:\n  base_url: http://vip5546.space\n  username: u\n  password: p\n")
    config_mgr = ConfigManager(config_path)

    app = FastAPI()
    app.include_router(xtream_router)
    app.state.config_mgr = config_mgr
    app.state.db = db

    async def fake_player_api(self, params):
        return {
            "user_info": {"username": "u", "status": "Active"},
            "server_info": {
                "url": "vip5546.space",
                "port": "80",
                "https_port": "443",
                "server_protocol": "http",
            },
        }

    monkeypatch.setattr(xtream_module.UpstreamClient, "player_api", fake_player_api)
    return app, db


def test_bare_login_rewrites_server_info_to_proxy_host(tmp_path, monkeypatch):
    """A bare player_api.php login call must not leak the real upstream's
    server_info back to the client -- clients like Smarters read url/port
    from it and switch to talking to the real provider directly for every
    subsequent request, completely bypassing our filtering.
    """
    app, _db = make_app(tmp_path, monkeypatch)
    client = TestClient(app, base_url="http://192.168.188.102:8099")

    resp = client.get("/player_api.php", params={"username": "x", "password": "y"})
    assert resp.status_code == 200
    data = resp.json()

    assert data["server_info"]["url"] == "192.168.188.102"
    assert data["server_info"]["port"] == "8099"
    assert data["server_info"]["server_protocol"] == "http"
    # never leak the real upstream host
    assert "vip5546.space" not in str(data["server_info"])


def test_other_actions_are_not_touched(tmp_path, monkeypatch):
    """Only the bare login response carries server_info that needs
    rewriting -- other passthrough actions (e.g. get_vod_info) must be
    returned untouched.
    """
    app, _db = make_app(tmp_path, monkeypatch)

    async def fake_player_api(self, params):
        return {"info": {"name": "Some Movie"}}

    monkeypatch.setattr(xtream_module.UpstreamClient, "player_api", fake_player_api)
    client = TestClient(app, base_url="http://192.168.188.102:8099")

    resp = client.get(
        "/player_api.php", params={"username": "x", "password": "y", "action": "get_vod_info", "vod_id": "1"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"info": {"name": "Some Movie"}}
