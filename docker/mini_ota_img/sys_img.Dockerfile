ARG UBUNTU_BASE=ubuntu:22.04

FROM ${UBUNTU_BASE}

ARG UBUNTU_BASE
ARG OTACLIENT_VER=3.13.1

ENV OTACLIENT_MANIFEST=https://github.com/tier4/ota-client/releases/download/v${OTACLIENT_VER}/manifest.json
ENV OTACLIENT_ARM64_APP=https://github.com/tier4/ota-client/releases/download/v${OTACLIENT_VER}/otaclient-arm64-v${OTACLIENT_VER}.squashfs
ENV OTACLIENT_AMD64_APP=https://github.com/tier4/ota-client/releases/download/v${OTACLIENT_VER}/otaclient-x86_64-v${OTACLIENT_VER}.squashfs
ENV OTACLIENT_RELEASE_DIR=/opt/ota/otaclient_release

SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ENV SPECIAL_FILE="path;adf.ae?qu.er\y=str#fragファイルement"
# NOTE(20250225): for PR#492, add a folder that contains 1024 empty files
ENV EMPTY_FILE_FOLDER="/usr/empty_files"
ENV EMPTY_FILES_COUNT=1024

# special treatment to the ota-image: create file that needs url escaping
# NOTE: include special identifiers #?; into the pathname
RUN set -eux; \
    echo -n "${SPECIAL_FILE}" > "/${SPECIAL_FILE}"; \
    # create special folder containing a lot of empty files
    mkdir -p ${EMPTY_FILE_FOLDER}; for i in $(seq 1 ${EMPTY_FILES_COUNT}); do touch "${EMPTY_FILE_FOLDER}/$i"; done; \
    # install required packages
    apt-get update -qq; \
    apt-get install -y linux-image-generic wget ca-certificates; \
    mkdir -p $OTACLIENT_RELEASE_DIR; \
    cd $OTACLIENT_RELEASE_DIR; \
    wget $OTACLIENT_MANIFEST; \
    wget $OTACLIENT_ARM64_APP; \
    wget $OTACLIENT_AMD64_APP; \
    apt-get clean; \
    rm -rf \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

LABEL org.opencontainers.image.description="A mini system image for OTAClient test, based on ${UBUNTU_BASE}"

CMD ["/bin/bash"]
