"""Real ASGI endpoints with temporary files; no network, model or desktop."""

import fcntl
import json

import pytest
from fastapi.testclient import TestClient

from src.core.assistant.web import create_app


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"whitelist": [], "LLM_API_KEY": "private-secret-never-expose"})
    )
    with TestClient(
        create_app(path, tmp_path / "state.db"), base_url="http://127.0.0.1"
    ) as app:
        yield app


def state(client):
    data = client.get("/api/state").json()
    return data, {"X-CSRF-Token": data["csrf_token"]}


def contact(client):
    data, headers = state(client)
    added = client.post(
        "/api/contacts",
        headers=headers,
        json={
            "revision": data["revision"],
            "name": "小李",
            "conversation": "local-chat",
            "sender": "local-peer",
        },
    )
    assert added.status_code == 200
    data = added.json()
    cid = data["contacts"][0]["id"]
    assert (
        client.post(
            "/api/selection",
            headers=headers,
            json={"revision": data["revision"], "ids": [cid]},
        ).status_code
        == 200
    )
    return cid, headers


def test_page_and_assets_no_external_dependencies(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "勾选允许回复的联系人。" in response.text
    assert "frame-ancestors" in response.headers["content-security-policy"]
    for path in ["/assets/app.js", "/assets/style.css"]:
        assert client.get(path).status_code == 200
    assert client.get("/api/state").headers["cache-control"] == "no-store"
    assert client.get("/assets/../../.env").status_code == 404


def test_state_does_not_return_secret_config_or_fake_contacts(client):
    data, _ = state(client)
    assert data["contacts"] == []
    assert "private-secret" not in json.dumps(data)
    assert data["wechat_connected"] is False


def test_add_select_refresh_and_clear(client):
    cid, headers = contact(client)
    data = client.get("/api/state").json()
    assert data["contacts"][0]["selected"]
    assert (
        client.post(
            "/api/selection",
            headers=headers,
            json={"ids": [], "revision": data["revision"]},
        ).status_code
        == 200
    )
    refreshed = client.get("/api/state").json()
    assert len(refreshed["contacts"]) == 1
    assert not refreshed["contacts"][0]["selected"]
    assert client.get(f"/api/contacts/{cid}/details").status_code == 403


def test_missing_token_and_cross_origin_writes_rejected(client):
    data, headers = state(client)
    payload = {"revision": data["revision"], "ids": []}
    assert client.post("/api/selection", json=payload).status_code == 403
    assert (
        client.post(
            "/api/selection",
            headers={**headers, "Origin": "https://untrusted.example"},
            json=payload,
        ).status_code
        == 403
    )
    assert (
        client.get("/api/state", headers={"Host": "untrusted.example"}).status_code
        == 400
    )


def test_stale_revision_returns_conflict(client):
    old, headers = state(client)
    contact(client)
    assert (
        client.post(
            "/api/selection",
            headers=headers,
            json={"revision": old["revision"], "ids": []},
        ).status_code
        == 409
    )
    assert len(client.get("/api/state").json()["contacts"]) == 1


def test_memory_and_attachment_management_scoped(client, tmp_path):
    cid, headers = contact(client)
    route = f"/api/contacts/{cid}"
    assert client.get(route + "/details").json() == {
        "memory": [],
        "documents": [],
        "short_term": {"recent": [], "older_excerpts": []},
    }
    response = client.post(
        route + "/memory",
        headers=headers,
        json={"key": "风格", "value": "简短", "ttl": 86400},
    )
    assert response.status_code == 200
    assert response.json()["memory"][0]["value"] == "简短"
    assert client.delete(route + "/memory/风格", headers=headers).json()["removed"] == 1
    assert (
        client.delete(route + "/documents/nonexistent", headers=headers).json()[
            "removed"
        ]
        == 0
    )
    assert client.get("/api/contacts/missing/details").status_code == 404
    assert (
        client.post(
            route + "/memory",
            headers=headers,
            json={"key": "x", "value": "v", "ttl": True},
        ).status_code
        == 422
    )


def test_running_watch_database_cannot_be_modified(client, tmp_path):
    cid, headers = contact(client)
    with (tmp_path / "state.db.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert client.get(f"/api/contacts/{cid}/details").status_code == 409
        # Contact selection remains independent of the database process lock.
        data = client.get("/api/state").json()
        assert (
            client.post(
                "/api/selection",
                headers=headers,
                json={"revision": data["revision"], "ids": []},
            ).status_code
            == 200
        )


def test_no_live_send_or_desktop_api(client):
    _, headers = state(client)
    for path in ["/api/send", "/api/start", "/api/stop"]:
        assert client.post(path, headers=headers).status_code == 404


def test_oversized_and_extra_fields_rejected(client):
    data, headers = state(client)
    assert (
        client.post(
            "/api/selection",
            headers=headers,
            json={"revision": data["revision"], "ids": [], "live": True},
        ).status_code
        == 422
    )
    assert (
        client.post("/api/selection", headers=headers, content="x" * 100001).status_code
        == 413
    )


def test_shutdown_requires_csrf_and_supported_runtime(tmp_path):
    from unittest.mock import Mock

    path = tmp_path / "config.json"
    path.write_text('{"whitelist": []}')
    callback = Mock()
    with TestClient(
        create_app(path, tmp_path / "state.db", shutdown=callback),
        base_url="http://127.0.0.1",
    ) as app:
        data, headers = state(app)
        assert data["shutdown_available"] is True
        assert app.get("/api/shutdown").status_code == 405
        assert app.post("/api/shutdown").status_code == 403
        assert (
            app.post(
                "/api/shutdown",
                headers={**headers, "Origin": "https://untrusted.example"},
            ).status_code
            == 403
        )
        callback.assert_not_called()
        response = app.post("/api/shutdown", headers=headers)
        assert response.status_code == 202
        assert response.json() == {"status": "stopping"}
        callback.assert_called_once_with()
        assert app.post("/api/shutdown", headers=headers).status_code == 202
        callback.assert_called_once()
        assert (
            app.post(
                "/api/selection",
                headers=headers,
                json={"revision": data["revision"], "ids": []},
            ).status_code
            == 503
        )


def test_unmanaged_asgi_app_does_not_kill_host_process(client):
    data, headers = state(client)
    assert data["shutdown_available"] is False
    response = client.post("/api/shutdown", headers=headers)
    assert response.status_code == 409
    assert "Control+C" in response.json()["detail"]
    assert client.get("/api/state").status_code == 200


def test_page_has_exit_confirmation_and_startup_help(client):
    html = client.get("/").text
    for text in [
        "退出程序",
        "确认退出",
        "当前选择尚未保存",
        "启动与退出",
        "关闭浏览器标签页不会退出服务",
    ]:
        assert text in html


def test_real_owned_server_exits_and_preserves_saved_config(tmp_path):
    import socket
    import subprocess
    import sys
    import time

    import httpx

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    config = tmp_path / "local.json"
    original = '{"whitelist": []}'
    config.write_text(original)
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        port = socket_.getsockname()[1]
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    with (tmp_path / "server.log").open("w") as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "src.core.assistant.web",
                "--config",
                str(config),
                "--db",
                str(tmp_path / "state.db"),
                "--port",
                str(port),
            ],
            cwd=root,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False
            ) as http:
                data = None
                for _ in range(100):
                    try:
                        response = http.get("/api/state")
                        if response.status_code == 200:
                            data = response.json()
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                assert data is not None, (tmp_path / "server.log").read_text()
                assert data["shutdown_available"]
                response = http.post(
                    "/api/shutdown", headers={"X-CSRF-Token": data["csrf_token"]}
                )
                assert response.status_code == 202
            assert server.wait(timeout=8) == 0
            assert unrelated.poll() is None
            assert config.read_text() == original
        finally:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=5)
            unrelated.terminate()
            unrelated.wait(timeout=5)


def test_session_reset_requires_csrf_and_selection(client, tmp_path):
    import time

    from src.core.assistant.store import Store, encoded

    cid, headers = contact(client)
    db = Store(tmp_path / "state.db")
    scope = encoded(["local-chat", "local-peer"])
    db.execute(
        "INSERT INTO history(scope,role,text,source,created) VALUES(?,?,?,?,?)",
        (scope, "user", "hello", "source", time.time()),
    )
    db.close()
    assert (
        client.get(f"/api/contacts/{cid}/details").json()["short_term"]["recent"][0][
            "text"
        ]
        == "hello"
    )
    route = f"/api/contacts/{cid}/session/reset"
    assert client.post(route).status_code == 403
    result = client.post(route, headers=headers)
    assert result.status_code == 200
    assert result.json()["short_term"]["recent"] == []
    data = client.get("/api/state").json()
    client.post(
        "/api/selection",
        headers=headers,
        json={"revision": data["revision"], "ids": []},
    )
    assert client.post(route, headers=headers).status_code == 403
