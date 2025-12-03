ARG UBUNTU_BASE
ARG UV_VERSION=0.8.22

FROM ghcr.io/tier4/ota-client/ota_img_for_test:ubuntu_22.04 AS ota_image

FROM ghcr.io/tier4/ota-client/ota_img_for_test:ubuntu_22.04-ota_image_v1 AS ota_image_v1

FROM ${UBUNTU_BASE}

# NOTE: for fallback when no entry_point.sh is specified in compose file
COPY --chmod=755 ./entry_point.sh /entry_point.sh
COPY --from=ota_image /ota-image /ota-image
COPY --from=ota_image /certs /certs
COPY --from=ota_image_v1 /ota-image /ota-image_v1
COPY --from=ota_image_v1 /certs /certs_ota-image_v1

# bootstrapping the python environment
RUN set -eux; \
    apt-get update; \
    apt-get install -y -qq --no-install-recommends \
        git ca-certificates wget python3 python3-setuptools; \
    export UV_INSTALL_DIR=/usr/bin; \
    wget -qO- "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh; \
    apt-get clean; \
    rm -rf \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

ENTRYPOINT [ "/bin/bash", "/entry_point.sh" ]
