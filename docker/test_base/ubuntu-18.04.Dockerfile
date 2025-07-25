ARG UBUNTU_BASE

FROM ghcr.io/tier4/ota-client/ota_img_for_test:ubuntu_22.04 AS ota_image

FROM ${UBUNTU_BASE}

COPY --from=ota_image /ota-image /ota-image
COPY --from=ota_image /certs /certs

# copy and setup the entry_point.sh
COPY --chmod=755 ./entry_point.sh /entry_point.sh

# bootstrapping the python environment
RUN set -eux; \
    apt-get update; \
    apt-get install -y -qq --no-install-recommends \
        python3.8 \
        python3-pip \
        git; \
    python3.8 -m pip install -U pip; \
    python3.8 -m pip install uv

ENTRYPOINT [ "/entry_point.sh" ]
