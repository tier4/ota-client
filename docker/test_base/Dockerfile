FROM ubuntu:20.04
SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ARG KERNEL_VERSION="5.8.0-53-generic"
ARG BASE_IMG_URL="http://cdimage.ubuntu.com/ubuntu-base/releases/20.04/release/ubuntu-base-20.04.1-base-amd64.tar.gz"
ARG OTA_METADATA_REPO="https://github.com/tier4/ota-metadata"
ENV OTA_CLIENT_DIR="/ota-client"
ENV OTA_IMAGE_SERVER_ROOT="/ota-image"
ENV OTA_IMAGE_DIR="${OTA_IMAGE_SERVER_ROOT}/data"
ENV VENV="${OTA_CLIENT_DIR}/.venv"
ENV SPECIAL_FILE="path;adf.ae?qu.er\y=str#fragファイルement"


# install required packages
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        python3-minimal \
        python3-pip \
        python3-venv \
        python3-dev \
	    libcurl4-openssl-dev \
	    libssl-dev \
	    gcc \
        wget \
        git && \
    apt-get install -y -qq \
        linux-image-${KERNEL_VERSION}

# prepare ubuntu base
WORKDIR ${OTA_IMAGE_SERVER_ROOT}
RUN wget -q -O /tmp/base_image.tar.gz ${BASE_IMG_URL} && \
    mkdir -p ${OTA_IMAGE_DIR} && \
    tar zxf /tmp/base_image.tar.gz -C ${OTA_IMAGE_DIR} && \
    cp -a \
        /boot/vmlinuz-${KERNEL_VERSION} \
        /boot/initrd.img-${KERNEL_VERSION} \
        /boot/config-${KERNEL_VERSION} \
        /boot/System.map-${KERNEL_VERSION} ${OTA_IMAGE_DIR}/boot

# special treatment to the ota-image: create file that needs url escaping
# NOTE: include special identifiers #?; into the pathname
RUN echo -n "${SPECIAL_FILE}" > "${OTA_IMAGE_DIR}/${SPECIAL_FILE}"

# install otaclient dependencies
RUN python3 -m venv ${VENV}
COPY ./otaclient/requirements.txt /tmp/requirements.txt
COPY ./tests/requirements.txt /tmp/requirements-dev.txt
RUN source ${VENV}/bin/activate && \
    python3 -m pip install --no-cache-dir -q -U pip && \
    python3 -m pip install \
        --no-cache-dir -q \
        -r /tmp/requirements.txt \
        -r /tmp/requirements-dev.txt

# prepare build environment
RUN git clone ${OTA_METADATA_REPO}
COPY ./tests/keys/sign.key sign.key
COPY ./tests/keys/sign.pem sign.pem
# build base image
RUN python3 -m venv ota-metadata/.venv
RUN source ota-metadata/.venv/bin/activate && \
    python3 -m pip install --no-cache-dir -q \
        -r ota-metadata/metadata/ota_metadata/requirements.txt && \
    python3 ota-metadata/metadata/ota_metadata/metadata_gen.py \
        --target-dir data \
        --compressed-dir data.zst \
        --ignore-file ota-metadata/metadata/ignore.txt && \
    python3 ota-metadata/metadata/ota_metadata/metadata_sign.py \
        --sign-key sign.key \
        --cert-file sign.pem \
        --persistent-file ota-metadata/metadata/persistents.txt \
        --rootfs-directory data \
        --compressed-rootfs-directory data.zst && \
    cp ota-metadata/metadata/persistents.txt .

# cleanup
RUN apt-get clean && \
    rm -rf \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/* \
        ota-metadata

# copy and setup the entry_point.sh
COPY ./docker/test_base/entry_point.sh /entry_point.sh
RUN chmod +x /entry_point.sh

WORKDIR ${OTA_CLIENT_DIR}
ENTRYPOINT [ "/entry_point.sh" ]
