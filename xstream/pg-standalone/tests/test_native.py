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
"""Opt-in real APISIX file watcher test. No etcd container or exposed port."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gateway_config as g


def test_native_file_hot_reload(tmp_path):
    image = os.getenv("GATEWAY_NATIVE_IMAGE")
    if not image:
        pytest.skip("set GATEWAY_NATIVE_IMAGE to a fixed APISIX image")
    root = Path(__file__).resolve().parents[1]
    name = "apisix-pg-native-" + uuid.uuid4().hex[:10]
    conf = tmp_path / "conf"
    conf.mkdir()
    boot = yaml.safe_load((root / "config.yaml").read_text())
    boot["apisix"]["node_listen"][0]["ip"] = "0.0.0.0"
    boot["nginx_config"] = {"worker_processes": 1}
    boot["plugins"].append("redirect")
    (conf / "config.yaml").write_text(yaml.safe_dump(boot))
    target = conf / "apisix.yaml"
    target.write_bytes(g.render({"routes": []}))
    def docker(*args, check=True):
        return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)
    docker("run", "-d", "--user", "0:0", "--name", name,
           "-p", "127.0.0.1::9080",
           "-v", f"{conf / 'config.yaml'}:/usr/local/apisix/conf/config.yaml:ro",
           "-v", f"{conf}:/config-sync",
           "-v", f"{root}:/opt/pg-sync:ro", image,
           "sh", "-c", "cp /config-sync/apisix.yaml conf/apisix.yaml && "
           "rm conf/apisix.yaml && ln -s /config-sync/apisix.yaml conf/apisix.yaml && "
           "apisix start && tail -f /dev/null")
    try:
        port = docker("port", name, "9080").stdout.strip().split(":")[-1]
        url = f"http://127.0.0.1:{port}/e2e"
        for _ in range(30):
            ready = subprocess.run(["curl", "-s", "-o", "/dev/null", url], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            logs = docker("logs", name)
            pytest.fail(logs.stdout + logs.stderr)
        pid = docker("exec", name, "cat", "/usr/local/apisix/logs/nginx.pid").stdout
        validator = tmp_path / "validator.sh"
        validator.write_text(f'''#!/bin/sh
set -eu
exec >"{tmp_path / 'native.log'}" 2>&1
docker cp "$1" {name}:/tmp/gateway-candidate.json >/dev/null
docker exec {name} /opt/pg-sync/validate-native.sh /tmp/gateway-candidate.json
''')
        validator.chmod(0o700)
        probe = tmp_path / "probe.py"
        probe.write_text(f'''import subprocess, sys, yaml
config = yaml.safe_load(open(sys.argv[1]))
expected = config['routes'][0]['plugins']['redirect']['uri']
for _ in range(10):
    result = subprocess.check_output(['curl', '-s', '-o', '/dev/null', '-w', '%{{redirect_url}}', '{url}'], text=True)
    if result.endswith(expected):
        sys.exit(0)
    import time; time.sleep(0.3)
sys.exit(1)
''')
        def snapshot(gen, uri):
            cfg = {"routes": [{"id": "probe", "uri": "/e2e",
                               "plugins": {"redirect": {"uri": uri, "ret_code": 302}}}]}
            return dict(generation=gen, revision=gen, config=cfg, checksum=g.digest(cfg))
        plugins = g.bootstrap(conf / "config.yaml")
        state = tmp_path / "state.json"
        command = str(validator)
        good = snapshot(1, "/version-one")
        try:
            g.apply_snapshot(good, target, state, command, f"{sys.executable} {probe}", plugins)
        except subprocess.SubprocessError:
            pytest.fail((tmp_path / "native.log").read_text())
        assert json.loads(state.read_text())["revision"] == 1
        bad = snapshot(2, "/version-two")
        with pytest.raises(subprocess.CalledProcessError):
            g.apply_snapshot(bad, target, state, command, "/usr/bin/false", plugins)
        time.sleep(2)
        g.run_hook(f"{sys.executable} {probe}", target)
        g.apply_snapshot(snapshot(3, "/version-three"), target, state, command,
                         f"{sys.executable} {probe}", plugins)
        assert docker("exec", name, "cat", "/usr/local/apisix/logs/nginx.pid").stdout == pid
        invalid = snapshot(4, "/invalid")
        invalid["config"]["routes"][0]["plugins"]["redirect"]["ret_code"] = "invalid"
        with pytest.raises(subprocess.CalledProcessError):
            g.native_validate(invalid["config"], command)
    finally:
        docker("rm", "-f", name, check=False)
