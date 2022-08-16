FROM ubuntu:20.04
SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ARG KERNEL_VERSION="5.8.0-53-generic"
ARG BASE_IMG_URL="http://cdimage.ubuntu.com/ubuntu-base/releases/20.04/release/ubuntu-base-20.04.1-base-amd64.tar.gz"
ARG OTA_METADATA_REPO="https://github.com/tier4/ota-metadata"

# install deps
COPY ./app/requirements.txt /tmp/requirements.txt
COPY ./tests/requirements.txt /tmp/requirements-dev.txt
RUN apt-get update && \
    apt-get install -y \
        python3 \
        python3-pip \ 
        wget \
        git \
        linux-image-${KERNEL_VERSION} && \
    python3 -m pip install --no-cache-dir -U pip && \
    python3 -m pip install \
        --no-cache-dir \
        -r /tmp/requirements.txt \
        -r /tmp/requirements-dev.txt

# prepare ubuntu base
WORKDIR /ota-image
RUN wget -O /tmp/base_image.tar.gz ${BASE_IMG_URL} && \
    mkdir -p data && \
    tar zxf /tmp/base_image.tar.gz -C data && \
    cp -a \
        /boot/vmlinuz-${KERNEL_VERSION} \
        /boot/initrd.img-${KERNEL_VERSION} \
        /boot/config-${KERNEL_VERSION} \
        /boot/System.map-${KERNEL_VERSION} data/boot

# create file that needs url escaping
RUN echo "url escaping is required" > data/a:b:c

# prepare build environment
RUN git clone ${OTA_METADATA_REPO} && \
    python3 -m pip install -r ota-metadata/metadata/ota_metadata/requirements.txt

# create ota-baseimage
RUN python3 ota-metadata/metadata/ota_metadata/metadata_gen.py \
        --target-dir data --ignore-file ota-metadata/metadata/ignore.txt
COPY tests/keys/sign.key sign.key
COPY tests/keys/sign.pem sign.pem
RUN python3 ota-metadata/metadata/ota_metadata/metadata_sign.py \
        --sign-key sign.key \
        --cert-file sign.pem \
        --persistent-file ota-metadata/metadata/persistents.txt \
        --rootfs-directory data && \
    cp ota-metadata/metadata/persistents.txt .

# cleanup
RUN apt-get clean && \
        rm -rf \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

# copy and setup the entry_point.sh
COPY docker/test_base/entry_point.sh /entry_point.sh
RUN chmod +x /entry_point.sh

WORKDIR /ota-client
ENTRYPOINT [ "/entry_point.sh" ]
