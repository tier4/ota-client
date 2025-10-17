# NOTE: due to newer version of mkfs.ext4 will enable some flags
#       by default that are not supported by older version of ubuntu,
#       we have to pin the ubuntu base to 22.04.
# NOTE: due to glibc compat issue, we need to pair ubuntu:22.04 with bullseye.
ARG UBUNTU_BASE=ubuntu:22.04
ARG BUILD_BASE=python:3.13-bullseye
ARG OTACLIENT_INSTALLATION="/otaclient"
ARG UV_VERSION=0.8.22

FROM ${BUILD_BASE} AS app_build

ARG UV_VERSION=0.8.22
ARG PYINSTALLER_VER=6.16.0

RUN mkdir /build
# minimum required files for building app
COPY ./src /build/src
COPY ./scripts /build/scripts
COPY ./pyproject.toml ./uv.lock ./README.md /build/

WORKDIR /build
RUN set -eux; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends \
        git ca-certificates wget python3 python3-setuptools; \
    export UV_INSTALL_DIR=/usr/bin; \
    wget -qO- "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh; \
    uv run --with=pyinstaller==${PYINSTALLER_VER} --locked --no-managed-python --python=3.13 \
        pyinstaller -D -s --name otaclient src/otaclient/__main__.py

FROM ${UBUNTU_BASE}

ARG OTACLIENT_INSTALLATION="/otaclient"
ARG OTACLIENT_VERSION

ENV OTACLIENT_VERSION=${OTACLIENT_VERSION}

COPY --from=app_build /build/dist/otaclient ${OTACLIENT_INSTALLATION}

RUN set -eux; \
    # add necessary runtime directories for OTA
    mkdir -p /mnt /ota-cache /host_root /usr/share/ca-certificates /usr/share/zoneinfo; \
    # add compatible layer for otaclient launching the dynamic otaclient app,
    #   see otaclient.main module for more details.
    mkdir -p /otaclient/venv/bin ;\
    echo '#!/bin/bash\n/otaclient/otaclient\n' > /otaclient/venv/bin/python3; \
    chmod +x /otaclient/venv/bin/python3; \
    apt-get update -qq; \
    apt-get install --no-install-recommends -y \
        # for e2label, mkfs.ext4
        e2fsprogs \
        # for TLS certs verification
        ca-certificates; \
    apt-get clean; \
    rm -rf \
        /root/.cache \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*
