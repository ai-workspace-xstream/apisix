FROM apache/apisix@sha256:c228717165ecf4c0055818c9e2d9843f7b303f17a6df9e5fbaf8eafeb5007ae4
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/apisix-pg-standalone
COPY requirements.txt ./
RUN python3 -m venv /opt/gateway-venv && /opt/gateway-venv/bin/pip install --no-cache-dir -r requirements.txt
COPY gateway_config.py validate.lua validate-native.sh schema.sql container-entrypoint.sh probe-local.py ./
RUN chmod 0755 validate-native.sh container-entrypoint.sh
ARG SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/ai-workspace-xstream/apisix" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"
ENV APISIX_HOME=/usr/local/apisix APISIX_STAND_ALONE=true
ENTRYPOINT ["/opt/apisix-pg-standalone/container-entrypoint.sh"]
CMD ["gateway"]
