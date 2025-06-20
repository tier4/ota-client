ARG UBUNTU_BASE=ubuntu:22.04

FROM ${UBUNTU_BASE}

ARG UBUNTU_BASE

SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ENV SPECIAL_FILE="path;adf.ae?qu.er\y=str#fragファイルement"
# NOTE(20250225): for PR#492, add a folder that contains 5000 empty files
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
    apt-get install -y linux-image-generic; \
    apt-get clean; \
    rm -rf \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

LABEL org.opencontainers.image.description="A mini system image for OTAClient test, based on ${UBUNTU_BASE}"

CMD ["/bin/bash"]
