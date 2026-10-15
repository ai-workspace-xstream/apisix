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
"""PG config control plane and file publisher; never modifies APISIX core."""
import argparse
import copy
import fcntl
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import yaml

LOG = logging.getLogger("gateway-config")
KINDS = {"routes": "route", "upstreams": "upstream", "services": "service",
         "plugin_configs": "plugin_config"}
MAX_BODY = 4 * 1024 * 1024


def canonical(config):
    return json.dumps(config, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode()


def digest(config):
    return hashlib.sha256(canonical(config)).hexdigest()


def bootstrap(path):
    conf = yaml.safe_load(Path(path).read_text())
    dep = conf.get("deployment", {})
    if (dep.get("role") != "data_plane"
            or dep.get("role_data_plane", {}).get("config_provider") != "yaml"
            or "etcd" in dep or "etcd" in conf
            or "role_traditional" in dep or "role_control_plane" in dep
            or conf.get("apisix", {}).get("enable_admin") is not False):
        raise ValueError("requires file-driven YAML data_plane, Admin API off, no etcd")
    return set(conf.get("plugins", []))


def validate(config, plugins):
    """Structural/referential checks, followed separately by native Lua checks."""
    if not isinstance(config, dict) or not config or set(config) - KINDS.keys():
        raise ValueError("only routes, upstreams, services and plugin_configs are supported")
    canonical(config)  # reject NaN/Infinity
    ids = {}
    for kind in KINDS:
        entries = config.get(kind, [])
        if not isinstance(entries, list):
            raise ValueError("resource collections must be arrays")
        ids[kind] = set()
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("id"), (str, int)):
                raise ValueError("every resource requires an id")
            key = str(item["id"])
            if key in ids[kind]:
                raise ValueError("duplicate resource id")
            ids[kind].add(key)
            if not isinstance(item.get("plugins", {}), dict):
                raise ValueError("plugins must be an object")
            if set(item.get("plugins", {})) - plugins:
                raise ValueError("plugin is not enabled in bootstrap configuration")
    for kind in ("routes", "services"):
        for item in config.get(kind, []):
            for field, target in (("upstream_id", "upstreams"),
                                  ("service_id", "services"),
                                  ("plugin_config_id", "plugin_configs")):
                if field in item and str(item[field]) not in ids[target]:
                    raise ValueError("unresolved resource reference")


def render(config):
    # Materialize omitted collections: a full snapshot must remove old objects.
    full = {kind: copy.deepcopy(config.get(kind, [])) for kind in KINDS}
    return (yaml.safe_dump(full, sort_keys=True) + "#END\n").encode()


def run_hook(command, path, metadata=None):
    env = dict(os.environ)
    env["GATEWAY_CONFIG_METADATA"] = json.dumps(metadata or {})
    # Never run a shell, or log hook output (configuration may contain secrets).
    subprocess.run(shlex.split(command) + [str(path)], check=True,
                   timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


def native_validate(config, command):
    with tempfile.TemporaryDirectory(prefix="gateway-validate-") as directory:
        path = Path(directory) / "candidate.json"
        path.write_bytes(canonical(config))
        os.chmod(path, 0o600)
        run_hook(command, path)


class Store:
    def __init__(self, dsn):
        self.dsn = dsn

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5,
                               options="-c statement_timeout=10000")

    def draft(self, gateway, config, actor):
        with self.connect() as db:
            row = db.execute("""INSERT INTO gateway_config.revisions
                (gateway_id,config,checksum,created_by) VALUES (%s,%s,%s,%s)
                RETURNING revision,checksum""",
                (gateway, Jsonb(config), digest(config), actor)).fetchone()
        return row

    def revision(self, gateway, revision):
        with self.connect() as db:
            return db.execute("""SELECT revision,config,checksum FROM gateway_config.revisions
                WHERE gateway_id=%s AND revision=%s""", (gateway, revision)).fetchone()

    def publish(self, gateway, revision, expected_generation, actor):
        with self.connect() as db:
            db.execute("""INSERT INTO gateway_config.publications (gateway_id)
                VALUES (%s) ON CONFLICT DO NOTHING""", (gateway,))
            row = db.execute("""UPDATE gateway_config.publications SET revision=%s,
                generation=generation+1,published_by=%s,published_at=now()
                WHERE gateway_id=%s AND generation=%s RETURNING generation,revision""",
                (revision, actor, gateway, expected_generation)).fetchone()
            if row is None:
                raise ValueError("publication generation conflict")
            db.execute("""INSERT INTO gateway_config.audit (gateway_id,generation,revision,actor)
                VALUES (%s,%s,%s,%s)""", (gateway, row["generation"], revision, actor))
        return row

    def snapshot(self, gateway):
        with self.connect() as db:
            return db.execute("""SELECT p.generation,r.revision,r.config,r.checksum
                FROM gateway_config.publications p JOIN gateway_config.revisions r
                ON r.gateway_id=p.gateway_id AND r.revision=p.revision
                WHERE p.gateway_id=%s""", (gateway,)).fetchone()

    def status(self, gateway, node, status):
        with self.connect() as db:
            db.execute("""INSERT INTO gateway_config.node_status (gateway_id,node_id,status)
                VALUES (%s,%s,%s) ON CONFLICT (gateway_id,node_id)
                DO UPDATE SET status=excluded.status,updated_at=now()""",
                (gateway, node, Jsonb(status)))

    def statuses(self, gateway):
        with self.connect() as db:
            return db.execute("""SELECT node_id,status,extract(epoch FROM updated_at) AS updated_at
                FROM gateway_config.node_status WHERE gateway_id=%s ORDER BY node_id""",
                (gateway,)).fetchall()


def atomic_write(path, data):
    path = Path(path)
    previous = int(path.stat().st_mtime) if path.exists() else 0
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".gateway-")
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o640)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        # Upstream compares mtime in whole seconds. Also advance on rollback.
        stamp = max(int(time.time()), previous + 1)
        os.utime(name, (stamp, stamp))
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def apply_snapshot(snapshot, target, state_path, validator, probe, plugins):
    validate(snapshot["config"], plugins)
    if digest(snapshot["config"]) != snapshot["checksum"]:
        raise ValueError("snapshot checksum mismatch")
    target, state_path = Path(target), Path(state_path)
    content = render(snapshot["config"])
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    generation = snapshot["generation"]
    if generation < state.get("generation", 0):
        raise ValueError("stale generation; rollback must create a new publication")
    if generation == state.get("generation") and state.get("checksum") != snapshot["checksum"]:
        raise ValueError("generation reused with different content")
    if (state.get("generation") == generation and target.exists()
            and target.read_bytes() == content):
        return state
    native_validate(snapshot["config"], validator)
    if not target.exists():
        raise ValueError("bootstrap deny-all file is required before synchronizing")
    previous = target.read_bytes()
    status = {k: snapshot[k] for k in ("generation", "revision", "checksum")}
    # Durable journal first: recover an interrupted deployment on next start.
    backup = Path(str(state_path) + ".previous")
    pending = Path(str(state_path) + ".pending")
    atomic_write(backup, previous)
    atomic_write(pending, canonical(status))
    try:
        atomic_write(target, content)
        time.sleep(2)  # let upstream file watcher and entity watchers run
        run_hook(probe, target, status)
        status["verified_at"] = int(time.time())
        atomic_write(state_path, canonical(status))
        pending.unlink()
    except Exception:
        atomic_write(target, previous)
        pending.unlink(missing_ok=True)
        raise
    return status


def recover(target, state_path):
    pending = Path(str(state_path) + ".pending")
    if pending.exists():
        intended = json.loads(pending.read_text())
        committed = json.loads(Path(state_path).read_text()) if Path(state_path).exists() else {}
        if any(committed.get(k) != intended.get(k) for k in ("generation", "checksum")):
            atomic_write(target, Path(str(state_path) + ".previous").read_bytes())
        pending.unlink()


def api_handler(store, gateway, plugins, validator, admin_token, nodes):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            self.connection.settimeout(10)
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            admin = hmac.compare_digest(token, admin_token)
            node = next((name for name, value in nodes.items()
                         if hmac.compare_digest(token, value)), None)
            code, payload = 200, {}
            try:
                if not admin and node is None:
                    code, payload = 401, {"error": "unauthorized"}
                elif self.command == "GET" and self.path == "/snapshot":
                    payload = store.snapshot(gateway)
                    if payload is None:
                        code, payload = 404, {"error": "not_published"}
                elif self.command == "GET" and self.path == "/status" and admin:
                    payload = {"nodes": [{**r, "updated_at": float(r["updated_at"])}
                                         for r in store.statuses(gateway)]}
                elif self.command == "POST":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        raise ValueError("body length out of range")
                    body = json.loads(self.rfile.read(length))
                    if self.path == "/drafts" and admin:
                        validate(body["config"], plugins)
                        native_validate(body["config"], validator)
                        payload = store.draft(gateway, body["config"], "management-api")
                    elif self.path == "/publish" and admin:
                        revision = store.revision(gateway, int(body["revision"]))
                        if revision is None:
                            raise ValueError("unknown revision")
                        validate(revision["config"], plugins)
                        native_validate(revision["config"], validator)
                        payload = store.publish(gateway, revision["revision"],
                                                int(body["expected_generation"]), "management-api")
                    elif self.path == "/status" and node is not None:
                        allowed = {"generation", "revision", "checksum", "verified_at", "error"}
                        if not isinstance(body, dict) or set(body) - allowed:
                            raise ValueError("invalid status")
                        store.status(gateway, node, body)
                    else:
                        code, payload = 403, {"error": "forbidden"}
                else:
                    code, payload = 404, {"error": "not_found"}
            except (ValueError, KeyError, TypeError, subprocess.SubprocessError):
                code, payload = 400, {"error": "invalid_configuration_or_publication"}
            except Exception:
                LOG.error("control operation failed")
                code, payload = 503, {"error": "temporarily_unavailable"}
            encoded = canonical(payload)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
    return Handler


def remote(base, token, status=None):
    path = "/snapshot" if status is None else "/status"
    request = Request(base.rstrip("/") + path,
                      data=None if status is None else canonical(status),
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        data = response.read(MAX_BODY + 1)
        if len(data) > MAX_BODY:
            raise ValueError("snapshot exceeds size limit")
        return json.loads(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["serve", "sync"])
    parser.add_argument("--bootstrap", required=True)
    parser.add_argument("--validator", required=True,
                        help="native schema validator argv; candidate JSON path appended")
    parser.add_argument("--gateway", default="selfhost-uat")
    parser.add_argument("--port", type=int, default=9089)
    parser.add_argument("--api", default="http://127.0.0.1:9089")
    parser.add_argument("--output", default="/usr/local/apisix/conf/apisix.yaml")
    parser.add_argument("--state", default="/var/lib/apisix-pg-sync/state.json")
    parser.add_argument("--probe", help="verification argv; YAML path appended")
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    plugins = bootstrap(args.bootstrap)
    if args.mode == "serve":
        admin = os.environ["GATEWAY_ADMIN_TOKEN"]
        nodes = json.loads(Path(os.environ["GATEWAY_NODE_TOKENS_FILE"]).read_text())
        if (len(admin) < 32 or not isinstance(nodes, dict) or not nodes
                or any(not isinstance(v, str) or len(v) < 32 for v in nodes.values())
                or admin in nodes.values() or len(set(nodes.values())) != len(nodes)):
            raise ValueError("distinct runtime tokens of at least 32 characters required")
        server = ThreadingHTTPServer(("127.0.0.1", args.port),
            api_handler(Store(os.environ["GATEWAY_PG_DSN"]), args.gateway,
                        plugins, args.validator, admin, nodes))
        server.serve_forever()
    else:
        if not args.probe or args.interval < 1:
            parser.error("sync requires --probe and interval >= 1")
        if not (args.api.startswith("https://") or args.api.startswith("http://127.0.0.1:")):
            parser.error("remote sync requires HTTPS")
        Path(args.state).parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        with open(args.state + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            recover(args.output, args.state)
            while True:
                try:
                    snapshot = remote(args.api, os.environ["GATEWAY_NODE_TOKEN"])
                    status = apply_snapshot(snapshot, args.output, args.state,
                                            args.validator, args.probe, plugins)
                    remote(args.api, os.environ["GATEWAY_NODE_TOKEN"], status)
                except Exception as exc:
                    LOG.error("sync failed (%s); retaining last known configuration", type(exc).__name__)
                    try:
                        remote(args.api, os.environ["GATEWAY_NODE_TOKEN"],
                               {"error": "sync_or_verification_failed"})
                    except Exception:
                        pass
                time.sleep(args.interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
