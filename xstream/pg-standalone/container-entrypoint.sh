#!/bin/sh
# Licensed under the Apache License, Version 2.0.
set -eu
root=/opt/apisix-pg-standalone
/opt/gateway-venv/bin/python -c 'import sys; sys.path.insert(0,"/opt/apisix-pg-standalone"); from gateway_config import bootstrap; bootstrap("/gateway-config/config.yaml")'
cp /gateway-config/config.yaml /usr/local/apisix/conf/config.yaml
mode=$1
shift
case "$mode" in
    gateway)
        ln -sf /gateway-config/apisix.yaml /usr/local/apisix/conf/apisix.yaml
        export APISIX_STAND_ALONE=true
        exec /docker-entrypoint.sh docker-start
        ;;
    serve|sync)
        exec /opt/gateway-venv/bin/python "$root/gateway_config.py" "$mode" \
            --bootstrap /gateway-config/config.yaml --validator "$root/validate-native.sh" "$@"
        ;;
    *) echo 'Expected gateway, serve or sync' >&2; exit 2 ;;
esac
