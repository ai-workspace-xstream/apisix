#!/bin/sh
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
set -eu
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
APISIX_HOME=${APISIX_HOME:-/usr/local/apisix}
export APISIX_HOME
LUA_CPATH="$APISIX_HOME/deps/lib/lua/5.1/?.so;${LUA_CPATH:-;}"
export LUA_CPATH
cd "$APISIX_HOME"
exec "${RESTY_BIN:-/usr/local/openresty/bin/resty}" \
  --main-conf "user $(id -un);" \
  -I "$APISIX_HOME" -I "$APISIX_HOME/deps/share/lua/5.1" \
  "$script_dir/validate.lua" "$1"
