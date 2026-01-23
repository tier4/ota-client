# NOTE: due to newer version of mkfs.ext4 will enable some flags
#       by default that are not supported by older version of ubuntu,
#       we have to pin the ubuntu base to 22.04.
# NOTE: due to glibc compat issue, we need to pair ubuntu:22.04 with bullseye.
ARG UBUNTU_BASE=ubuntu:22.04
ARG BUILD_BASE=python:3.13-bullseye

ARG UV_VERSION=0.9.4

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM ${BUILD_BASE} AS app_build

ARG PYINSTALLER_VER=6.16.0

COPY --from=uv /uv /uvx /bin/

WORKDIR /build
RUN --mount=type=bind,source=.,target=/build,rw \
    set -eux; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends \
        git python3 python3-setuptools; \
    uv run --with=pyinstaller==${PYINSTALLER_VER} --locked --no-managed-python --python=3.13 \
        pyinstaller -D -s --name otaclient src/otaclient/__main__.py; \
    cp -R /build/dist /dist

FROM ${UBUNTU_BASE}

ARG OTACLIENT_INSTALLATION="/otaclient"
ARG COMPAT_VENV_PATH="/otaclient/venv/bin"

COPY --from=app_build /dist/otaclient ${OTACLIENT_INSTALLATION}

RUN set -eux; \
    chmod +x "${OTACLIENT_INSTALLATION}/otaclient"; \
    # add necessary runtime directories for OTA
    mkdir -p /mnt /ota-cache /host_root /usr/share/ca-certificates /usr/share/zoneinfo; \
    # add compatible layer for otaclient launching the dynamic otaclient app,
    #   see otaclient.main module for more details.
    mkdir -p ${COMPAT_VENV_PATH} ;\
    echo '#!/bin/bash\n'${OTACLIENT_INSTALLATION}'/otaclient\n' > ${COMPAT_VENV_PATH}/python3; \
    chmod +x ${COMPAT_VENV_PATH}/python3; \
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

ENTRYPOINT [ "/otaclient/otaclient" ]
