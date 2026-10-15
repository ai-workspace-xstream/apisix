# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import json
import os
from pathlib import Path
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psycopg
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gateway_config as g

CONFIG = {"routes": [{"id": "api", "uri": "/api/*", "upstream_id": "accounts"}],
          "upstreams": [{"id": "accounts", "type": "roundrobin",
                         "nodes": {"127.0.0.1:8080": 1}}]}


def snapshot(generation=1, revision=1, config=None):
    config = copy.deepcopy(config or CONFIG)
    return dict(generation=generation, revision=revision, config=config, checksum=g.digest(config))


def test_reference_and_plugin_validation():
    g.validate(CONFIG, set())
    bad = copy.deepcopy(CONFIG)
    bad["upstreams"] = []
    with pytest.raises(ValueError):
        g.validate(bad, set())
    bad = copy.deepcopy(CONFIG)
    bad["routes"][0]["plugins"] = {"serverless-pre-function": {}}
    with pytest.raises(ValueError):
        g.validate(bad, {"forward-auth"})
    bad = copy.deepcopy(CONFIG)
    bad["routes"].append(bad["routes"][0])
    with pytest.raises(ValueError):
        g.validate(bad, set())


def test_full_snapshot_and_bootstrap(tmp_path):
    text = g.render({"routes": []}).decode()
    assert text.endswith("#END\n")
    assert yaml.safe_load(text) == {k: [] for k in g.KINDS}
    path = tmp_path / "config.yaml"
    path.write_text("deployment: {role: traditional}\n")
    with pytest.raises(ValueError):
        g.bootstrap(path)
    assert "forward-auth" in g.bootstrap(Path(__file__).resolve().parents[1] / "config.yaml")


def test_mtime_strictly_advances(tmp_path):
    path = tmp_path / "apisix.yaml"
    g.atomic_write(path, b"first")
    old = path.stat().st_mtime
    g.atomic_write(path, b"second")
    assert path.stat().st_mtime > old
    assert path.stat().st_mode & 0o777 == 0o640


def test_deploy_idempotence_rollback_and_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(g.time, "sleep", lambda _: None)
    calls = []
    monkeypatch.setattr(g, "run_hook", lambda *args: calls.append(args))
    target, state = tmp_path / "apisix.yaml", tmp_path / "state.json"
    target.write_bytes(g.render({"routes": []}))
    first = g.apply_snapshot(snapshot(), target, state, "validator", "probe", set())
    assert first["generation"] == 1
    count = len(calls)
    g.apply_snapshot(snapshot(), target, state, "validator", "probe", set())
    assert len(calls) == count
    old = target.read_bytes()
    def failing_hook(command, *_):
        if command == "probe":
            raise RuntimeError("unhealthy candidate")
    monkeypatch.setattr(g, "run_hook", failing_hook)
    with pytest.raises(RuntimeError):
        g.apply_snapshot(snapshot(2, 2), target, state, "validator", "probe", set())
    assert target.read_bytes() == old
    assert json.loads(state.read_text())["generation"] == 1
    monkeypatch.setattr(g, "run_hook", lambda *_: None)
    g.apply_snapshot(snapshot(3, 1), target, state, "validator", "probe", set())
    with pytest.raises(ValueError):
        g.apply_snapshot(snapshot(2), target, state, "validator", "probe", set())


def test_checksum_failure_does_not_touch_live_file(tmp_path):
    target = tmp_path / "apisix.yaml"
    target.write_bytes(b"original")
    bad = snapshot()
    bad["checksum"] = "bad"
    with pytest.raises(ValueError):
        g.apply_snapshot(bad, target, tmp_path / "state", "unused", "unused", set())
    assert target.read_bytes() == b"original"


def test_recover_crash_before_and_after_commit(tmp_path):
    state, target = tmp_path / "state.json", tmp_path / "apisix.yaml"
    Path(str(state) + ".previous").write_bytes(b"old")
    Path(str(state) + ".pending").write_text(json.dumps({"generation": 1, "checksum": "new"}))
    target.write_bytes(b"candidate")
    g.recover(target, state)
    assert target.read_bytes() == b"old"
    state.write_text(json.dumps({"generation": 1, "checksum": "new"}))
    Path(str(state) + ".pending").write_text(state.read_text())
    target.write_bytes(b"committed")
    g.recover(target, state)
    assert target.read_bytes() == b"committed"


@pytest.fixture
def store():
    dsn = os.getenv("GATEWAY_TEST_PG_DSN")
    if not dsn:
        pytest.skip("requires isolated integration PostgreSQL")
    st = g.Store(dsn)
    with st.connect() as db:
        db.execute((Path(__file__).resolve().parents[1] / "schema.sql").read_text())
    return st


def test_postgres_immutable_publish_conflict_and_rollback(store):
    import uuid
    gateway = "test-" + uuid.uuid4().hex
    first = store.draft(gateway, CONFIG, "test")
    second = store.draft(gateway, {"routes": []}, "test")
    with store.connect() as db, pytest.raises(psycopg.Error):
        db.execute("UPDATE gateway_config.revisions SET config='{}' WHERE revision=%s",
                   (first["revision"],))
    assert store.publish(gateway, first["revision"], 0, "test")["generation"] == 1
    with pytest.raises(ValueError):
        store.publish(gateway, second["revision"], 0, "other")
    store.publish(gateway, second["revision"], 1, "test")
    store.publish(gateway, first["revision"], 2, "rollback")
    published = store.snapshot(gateway)
    assert published["generation"] == 3
    assert published["config"] == CONFIG
    with pytest.raises(psycopg.Error):
        store.publish("other-gateway", first["revision"], 0, "test")


def test_management_api_permissions(store, monkeypatch):
    import uuid
    monkeypatch.setattr(g, "native_validate", lambda *_: None)
    admin, node = "admin-token", "node-token"
    handler = g.api_handler(store, "api-" + uuid.uuid4().hex, set(), "validator", admin, {"node-a": node})
    server = g.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, token, body=None):
        req = Request("http://127.0.0.1:" + str(server.server_port) + path,
                      data=None if body is None else g.canonical(body),
                      headers={"Authorization": "Bearer " + token})
        with urlopen(req, timeout=5) as response:
            return json.loads(response.read())
    try:
        with pytest.raises(HTTPError) as err:
            request("/drafts", node, {"config": CONFIG})
        assert err.value.code == 403
        revision = request("/drafts", admin, {"config": CONFIG})["revision"]
        request("/publish", admin, {"revision": revision, "expected_generation": 0})
        assert request("/snapshot", node)["revision"] == revision
        request("/status", node, {"generation": 1, "revision": revision})
        assert request("/status", admin)["nodes"][0]["node_id"] == "node-a"
        with pytest.raises(HTTPError):
            request("/status", node, {"node_id": "other"})
        with pytest.raises(HTTPError) as err:
            request("/snapshot", "wrong")
        assert err.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
