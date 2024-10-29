ARG UBUNTU_BASE

#
# ------ stage 1: prepare base image ------ #
#

FROM ${UBUNTU_BASE} AS image_build

SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ENV SPECIAL_FILE="path;adf.ae?qu.er\y=str#fragファイルement"

# special treatment to the ota-image: create file that needs url escaping
# NOTE: include special identifiers #?; into the pathname
RUN set -eux; \
    echo -n "${SPECIAL_FILE}" > "/${SPECIAL_FILE}"; \
    # install required packages
    apt-get update -qq; \
    apt-get install -y linux-image-generic; \
    apt-get clean; \
    rm -rf \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/* \

#
# ------ stage 2: prepare test environment ------ #
#
ARG UBUNTU_BASE

FROM ${UBUNTU_BASE}

SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive

ENV OTA_METADATA_REPO="https://github.com/tier4/ota-metadata"
ENV OTA_IMAGE_SERVER_ROOT="/ota-image"
ENV OTA_IMAGE_DIR="${OTA_IMAGE_SERVER_ROOT}/data"
ENV CERTS_DIR="/certs"
ENV SPECIAL_FILE="path;adf.ae?qu.er\y=str#fragファイルement"

# install required packages
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
    # install hatch
    python3 -m pip install --no-cache-dir -q -U pip; \
    python3 -m pip install --no-cache-dir -U hatch

WORKDIR ${OTA_IMAGE_SERVER_ROOT}

COPY --from=image_build / /${OTA_IMAGE_DIR}

# generate test certs and sign key
COPY --chmod=755 ./tests/keys/gen_certs.sh /tmp/certs/
RUN set -eu; \
    mkdir -p "${CERTS_DIR}"; \
    pushd /tmp/certs; \
    ./gen_certs.sh; \
    cp ./* "${CERTS_DIR}"; \
    popd; \
    # build the test OTA image
    cp "${CERTS_DIR}"/sign.key sign.key; \
    cp "${CERTS_DIR}"/sign.pem sign.pem; \
    git clone ${OTA_METADATA_REPO}; \
    python3 -m venv ota-metadata/.venv; \
    source ota-metadata/.venv/bin/activate; \
    python3 -m pip install --no-cache-dir -U pip; \
    python3 -m pip install --no-cache-dir -q \
        -r ota-metadata/metadata/ota_metadata/requirements.txt; \
    # patch the ignore files
    echo "" > ota-metadata/metadata/ignore.txt; \
    python3 ota-metadata/metadata/ota_metadata/metadata_gen.py \
        --target-dir data \
        --compressed-dir data.zst \
        --ignore-file ota-metadata/metadata/ignore.txt; \
    python3 ota-metadata/metadata/ota_metadata/metadata_sign.py \
        --sign-key sign.key \
        --cert-file sign.pem \
        --persistent-file ota-metadata/metadata/persistents.txt \
        --rootfs-directory data \
        --compressed-rootfs-directory data.zst; \
    cp ota-metadata/metadata/persistents.txt .; \
    # cleanup
    apt-get clean; \
    rm -rf \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/* \
        ota-metadata

# copy and setup the entry_point.sh
COPY ./docker/test_base/entry_point.sh /entry_point.sh
RUN chmod +x /entry_point.sh

ENTRYPOINT [ "/entry_point.sh" ]
