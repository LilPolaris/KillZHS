"""HTTP boundaries: origin/token/host, incremental reads and public assets only."""

import http.client
import json
import threading

import pytest

from killzhs.app.cli import assets_directory
from killzhs.app.server import LocalServer
from killzhs.models import InputError


class Controller:
    token = "local-test-token"

    def __init__(self):
        self.calls = []

    def state(self, since, revision):
        return {"since": since, "history_revision": revision}

    def action_request(self, name, params):
        if name == "unknown":
            raise InputError("未知操作。")
        self.calls.append((name, params))

    def task_log(self, identifier):
        if identifier != "a" * 32:
            raise InputError("任务记录不存在。")
        return {"text": "saved"}


@pytest.fixture
def local_http():
    controller = Controller()
    server = LocalServer(("127.0.0.1", 0), controller, assets_directory())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        status, content, output_headers = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return status, content, output_headers

    yield server, controller, request
    server.shutdown()
    server.server_close()
    thread.join(3)


def test_http_rejects_foreign_host_origin_and_missing_token(local_http):
    server, controller, request = local_http
    body = json.dumps({"settings": {"limit": 10}})
    headers = {"X-Local-Token": controller.token, "Content-Type": "application/json"}
    assert request("GET", "/api/state", headers={"Host": "foreign.invalid"})[0] == 403
    assert request("POST", "/api/actions/settings", body)[0] == 403
    assert request("POST", "/api/actions/settings", body, {**headers, "X-Local-Token": "é"})[0] == 403
    assert request("POST", "/api/actions/settings", body, {**headers, "Origin": "https://foreign.invalid"})[0] == 403
    assert request("POST", "/api/actions/settings", body, {**headers, "Origin": f"http://127.0.0.1:{server.server_port}"})[0] == 200
    assert controller.calls == [("settings", {"settings": {"limit": 10}})]
    assert request("POST", "/api/actions/unknown", "{}", headers)[0] == 400
    for value in ("{broken", "[]", '{"speed": NaN}', '"text"'):
        assert request("POST", "/api/actions/settings", value, headers)[0] == 400
    assert request("POST", "/api/actions/settings", "a" * 65537, headers)[0] == 400


def test_static_allowlist_and_security_headers(local_http):
    _, _, request = local_http
    status, _, headers = request("GET", "/styles.css")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    for path in ("/config.json", "/cookies.json", "/.local/data/config.json", "/../config.json", "/js/../config.json", "/js/%2e%2e/config.json"):
        assert request("GET", path)[0] == 404


def test_state_cursors_and_log_access(local_http):
    _, controller, request = local_http
    for path in ("/api/state?since=oops", "/api/state?since=-1"):
        assert request("GET", path)[0] == 400
    response = request("GET", "/api/state?since=10&history_revision=old")
    assert json.loads(response[1]) == {"since": 10, "history_revision": "old"}
    path = "/api/task-log/" + "a" * 32
    assert request("GET", path)[0] == 403
    headers = {"X-Local-Token": controller.token}
    assert request("GET", path, headers=headers)[0] == 200
    assert request("GET", "/api/task-log/unknown", headers=headers)[0] == 404


def test_cannot_bind_public_interfaces():
    with pytest.raises(ValueError):
        LocalServer(("0.0.0.0", 0), Controller(), assets_directory())
