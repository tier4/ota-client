ARG SYS_IMG=ghcr.io/tier4/ota-client/sys_img_for_test:ubuntu_22.04
ARG UBUNTU_BASE=ubuntu:22.04
ARG BUSYBOX_VER=busybox:1.37.0

#
# ------ stage 1: prepare base image ------ #
#

FROM ${SYS_IMG} AS sys_img

#
# ------ stage 2: prepare OTA image build environment ------ #
#
FROM ${UBUNTU_BASE} AS ota_img_builder

SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive

ENV OTA_METADATA_REPO="https://github.com/tier4/ota-metadata"
ENV OTA_IMAGE_SERVER_ROOT="/ota-image"
ENV OTA_IMAGE_DIR="${OTA_IMAGE_SERVER_ROOT}/data"
ENV CERTS_DIR="/certs"
ENV SPECIAL_FILE="path;adf.ae?qu.er\y=str#fragファイルement"

COPY --from=sys_img / /${OTA_IMAGE_DIR}
COPY --chmod=755 ./tests/keys/gen_certs.sh /tmp/certs/

WORKDIR ${OTA_IMAGE_SERVER_ROOT}

RUN set -eux; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends \
	    gcc \
        git \
        libcurl4-openssl-dev \
        libssl-dev \
        python3-dev \
        python3-minimal \
        python3-pip \
        python3-venv \
        wget; \
    apt-get install -y -qq linux-image-generic; \
    apt-get clean; \
    # install uv
    python3 -m pip install --no-cache-dir -q -U pip; \
    python3 -m pip install --no-cache-dir uv; \
    # generate keys and certs for signing
    mkdir -p "${CERTS_DIR}"; \
    pushd /tmp/certs; \
    ./gen_certs.sh; \
    mv ./* "${CERTS_DIR}"; \
    popd; \
    cp "${CERTS_DIR}"/sign.pem sign.pem; \
    # git clone the ota-metadata repository
    git clone ${OTA_METADATA_REPO}; \
    pushd ota-metadata; git checkout 6c8ad47; popd; \
    # prepare build environment
    python3 -m venv ota-metadata/.venv; \
    source ota-metadata/.venv/bin/activate; \
    python3 -m pip install --no-cache-dir -U pip; \
    python3 -m pip install --no-cache-dir -q \
        -r ota-metadata/metadata/ota_metadata/requirements.txt; \
    # patch the ignore files
    echo "" > ota-metadata/metadata/ignore.txt; \
    # build the OTA image
    python3 ota-metadata/metadata/ota_metadata/metadata_gen.py \
        --target-dir data \
        --compressed-dir data.zst \
        --ignore-file ota-metadata/metadata/ignore.txt; \
    python3 ota-metadata/metadata/ota_metadata/metadata_sign.py \
        --sign-key "${CERTS_DIR}"/sign.key \
        --cert-file sign.pem \
        --persistent-file ota-metadata/metadata/persistents.txt \
        --rootfs-directory data \
        --compressed-rootfs-directory data.zst; \
    cp ota-metadata/metadata/persistents.txt .; \
    # cleanup
    apt-get clean; \
    rm -rf \
        /certs/*.key \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/* \
        ota-metadata

#
# ------ stage 3: output OTA image ------ #
#

# NOTE: use busybox so that the overall image size will not increase much,
#       while enable us to examine the built OTA image.

FROM ${BUSYBOX_VER}

ARG SYS_IMG

LABEL org.opencontainers.image.description=\
"A mini OTA image for OTAClient test, based on system image ${SYS_IMG}"

COPY --from=ota_img_builder /ota-image /ota-image
COPY --from=ota_img_builder /certs /certs
