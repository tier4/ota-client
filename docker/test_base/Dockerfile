ARG UBUNTU_BASE
ARG UV_VERSION=0.8.22

FROM ghcr.io/tier4/ota-client/ota_img_for_test:ubuntu_22.04 AS ota_image

FROM ghcr.io/tier4/ota-client/ota_img_for_test:ubuntu_22.04-ota_image_v1 AS ota_image_v1

FROM ${UBUNTU_BASE}

# bootstrapping the python environment
RUN --mount=type=bind,source=/ota-image,target=/ota-image,from=ota_image,ro \
    --mount=type=bind,source=/certs,target=/certs,from=ota_image,ro \
    --mount=type=bind,source=/ota-image,target=/ota-image_v1,from=ota_image_v1,ro \
    --mount=type=bind,source=/certs,target=/certs_ota-image_v1,from=ota_image_v1,ro \
    set -eux; \
    apt-get update; \
    apt-get install -y -qq --no-install-recommends \
        git ca-certificates wget python3; \
    wget -qO- https://astral.sh/uv/install.sh | sh

ENV PATH="/root/.local/bin:${PATH}"
ENTRYPOINT [ "/bin/bash", "-c", "/entry_point.sh" ]
