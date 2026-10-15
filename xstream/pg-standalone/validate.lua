-- Licensed to the Apache Software Foundation (ASF) under one or more
-- contributor license agreements.  See the NOTICE file distributed with
-- this work for additional information regarding copyright ownership.
-- The ASF licenses this file to You under the Apache License, Version 2.0
-- (the "License"); you may not use this file except in compliance with
-- the License.  You may obtain a copy of the License at
--
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.
-- Run with the same APISIX installation, bootstrap and plugins as the gateway.
local profile = require("apisix.core.profile")
profile.apisix_home = (os.getenv("APISIX_HOME") or "/usr/local/apisix") .. "/"
local cjson = require("cjson.safe")
local schema = require("apisix.core.schema")
local config_local = require("apisix.core.config_local")
local conf = assert(config_local.local_conf())
assert(conf.deployment.config_provider == "yaml", "YAML Standalone required")
local allowed = {}
for _, name in ipairs(conf.plugins or {}) do
    allowed[name] = true
end
local file = assert(io.open(arg[1], "r"))
local candidate = assert(cjson.decode(file:read("*a")))
file:close()
local kinds = {routes = "route", upstreams = "upstream",
               services = "service", plugin_configs = "plugin_config"}
for kind, items in pairs(candidate) do
    assert(kinds[kind], "unsupported resource collection")
    for _, item in ipairs(items) do
        local ok = schema.check(schema[kinds[kind]], item)
        assert(ok, "invalid " .. kind .. " schema")
        for name, plugin_conf in pairs(item.plugins or {}) do
            assert(allowed[name], "plugin not enabled")
            local plugin = require("apisix.plugins." .. name)
            local valid = plugin.check_schema(plugin_conf)
            assert(valid, "invalid plugin configuration")
        end
    end
end
