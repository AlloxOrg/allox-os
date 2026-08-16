# Runtime image used inside the outer Kata VM. Kata is the isolation boundary;
# this image supplies Agent tools and services inside that VM.
#
# Build:
#   cd images/aio-runtime && ./build.sh
# Or:
#   docker build -t allox/aio-runtime:v2 -f images/aio-runtime/Dockerfile images/aio-runtime/

ARG BASE_TAG=1.0.0.150
FROM ghcr.io/agent-infra/sandbox:${BASE_TAG}

LABEL org.opencontainers.image.title="allox-aio-runtime"
LABEL org.opencontainers.image.description="Allox 2.0 Agent runtime for a Kata VM"
LABEL org.opencontainers.image.source="https://github.com/AlloxOrg/allox-os"

USER root

# ----------------------
# System packages (apt)
# ----------------------
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        jq \
        tree \
        curl; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# ----------------------
# Python packages (pip)
# ----------------------
RUN pip install --no-cache-dir httpx==0.28.1

# ----------------------
# Custom health service (supervisord + nginx)
# Convention: /opt/gem/supervisord/*.conf, /opt/gem/nginx/*.conf
# ----------------------
RUN mkdir -p /opt/allox
COPY allox_health_server.py /opt/allox/health_server.py
RUN chmod +x /opt/allox/health_server.py

COPY supervisord.allox_health.conf /opt/gem/supervisord/supervisord.allox_health.conf
COPY nginx.allox_health.conf /opt/gem/nginx/nginx.allox_health.conf

# Marker file for exec verification
RUN echo "allox-aio-runtime:v2" > /opt/allox/image-version.txt

# Optional: remove AIO components you don't need (uncomment to slim image)
# RUN rm -f /opt/gem/supervisord/supervisord.code_server.conf \
#           /opt/gem/nginx/code_server.conf

USER gem
