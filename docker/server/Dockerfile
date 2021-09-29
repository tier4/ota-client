FROM ubuntu:20.04
SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ARG KERNEL_VERSION="5.8.0-53-generic"

RUN \
    apt-get update && \
    apt-get install -y linux-image-${KERNEL_VERSION} && \
    apt-get install -y wget git python3 python3-pip

WORKDIR /ota-server

# prepare environment
RUN \
    wget http://cdimage.ubuntu.com/ubuntu-base/releases/20.04/release/ubuntu-base-20.04.1-base-amd64.tar.gz && \
    mkdir -p data && \
    tar zxf ubuntu-base-20.04.1-base-amd64.tar.gz -C data && \
    cp /boot/vmlinuz-${KERNEL_VERSION} /boot/initrd.img-${KERNEL_VERSION} /boot/config-${KERNEL_VERSION} /boot/System.map-${KERNEL_VERSION} data/boot

# create ota-baseimage
RUN \
    git clone https://github.com/tier4/ota-metadata && \
    python3 -m pip install -r ota-metadata/metadata/ota_metadata/requirements.txt && \
    python3 ota-metadata/metadata/ota_metadata/metadata_gen.py --target-dir data --ignore-file ota-metadata/metadata/ignore.txt && \
    ./ota-metadata/metadata/key-gen.sh && \
    python3 ota-metadata/metadata/ota_metadata/metadata_sign.py \
        --sign-key privatekey.pem \
        --cert-file certificate.pem \
        --persistent-file ota-metadata/metadata/persistents.txt \
        --rootfs-directory data && \
    cp ota-metadata/metadata/persistents.txt .


