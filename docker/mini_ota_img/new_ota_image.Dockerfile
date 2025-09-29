# syntax=docker/dockerfile:1-labs
ARG SYS_IMG=ghcr.io/tier4/ota-client/sys_img_for_test:ubuntu_22.04
ARG UBUNTU_BASE=ubuntu:22.04

#
# ------ stage 1: prepare base image ------ #
#

FROM ${SYS_IMG} AS sys_img


#
# ------ stage 2: build new OTA image ------ #
#

FROM ${UBUNTU_BASE} AS ota_img_builder

ARG OTA_IMAGE_BUILDER_RELEASE="https://github.com/tier4/ota-image-builder/releases/download/v0.3.2/ota-image-builder-x86_64"

SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive

ENV OTA_IMAGE_BUILDER_RELEASE=${OTA_IMAGE_BUILDER_RELEASE}
ENV OTA_IMAGE_SERVER_ROOT="/ota-image"
ENV ROOTFS="/rootfs"
ENV CERTS_DIR="/certs"
ENV BUILD_ROOT="/tmp/build_root"

COPY --chmod=755 ./tests/keys/gen_certs.sh ${CERTS_DIR}/gen_certs.sh
COPY ./tests/data/ota_image_builder ${BUILD_ROOT}
# COPY --chmod=755 ./ota-image-builder ${BUILD_ROOT}/ota-image-builder

WORKDIR ${BUILD_ROOT}

RUN --mount=type=bind,source=/,target=/rootfs,from=sys_img,rw \
    set -eux; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends \
        ca-certificates wget; \
    apt-get clean; \
    wget ${OTA_IMAGE_BUILDER_RELEASE} -O ${BUILD_ROOT}/ota-image-builder; \
    chmod +x ota-image-builder; \
    # --- generate certs --- #
    pushd ${CERTS_DIR}; \
    bash ${CERTS_DIR}/gen_certs.sh; \
    popd; \
    # --- start to build the OTA image --- #
    mkdir -p ${OTA_IMAGE_SERVER_ROOT}; \
    ./ota-image-builder -d init \
        --annotations-file full_annotations.yaml \
        ${OTA_IMAGE_SERVER_ROOT}; \
    ./ota-image-builder -d add-image \
        --annotations-file full_annotations.yaml \
        --release-key dev \
        --sys-config "autoware:sys_config.yaml" \
        --rootfs ${ROOTFS} \
        ${OTA_IMAGE_SERVER_ROOT}; \
    ./ota-image-builder -d add-image \
        --annotations-file full_annotations.yaml \
        --release-key prd \
        --sys-config "autoware:sys_config.yaml" \
        --rootfs ${ROOTFS}/var \
        ${OTA_IMAGE_SERVER_ROOT}; \
    ./ota-image-builder -d finalize ${OTA_IMAGE_SERVER_ROOT}; \
    ./ota-image-builder -d sign \
        --sign-cert ${CERTS_DIR}/sign.pem \
        --sign-key ${CERTS_DIR}/sign.key \
        --ca-cert ${CERTS_DIR}/test.interm.pem \
        ${OTA_IMAGE_SERVER_ROOT}; \
    # --- clean up --- #
    rm ${CERTS_DIR}/*.sh

#
# ------ stage 4: output OTA image ------ #
#

# NOTE: use busybox so that the overall image size will not increase much,
#       while enable us to examine the built OTA image.

FROM ${UBUNTU_BASE}

ARG SYS_IMG

LABEL org.opencontainers.image.description=\
"A mini OTA image v1 OTA image for OTAClient test, based on system image ${SYS_IMG}"

COPY --from=ota_img_builder /ota-image /ota-image
COPY --from=ota_img_builder /certs /certs
