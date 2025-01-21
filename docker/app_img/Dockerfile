ARG UBUNTU_BASE="ubuntu:24.04"

FROM ${UBUNTU_BASE} AS image_build

ARG OTACLIENT_WHL
COPY ./${OTACLIENT_WHL} /tmp

ENV OTACLIENT_INSTALLATION="/opt/ota/client"
ENV OTACLIENT_VENV="/opt/ota/client/venv"
ENV OTA_CERTS_DIR="/certs"

RUN set -eux; \
    apt-get update -qq; \
    apt-get install --no-install-recommends -y \
        python3-venv \
        python3-pip; \
    # setup virtual env
    mkdir -p ${OTACLIENT_VENV}; \
    cd ${OTACLIENT_INSTALLATION}; \
    python3 -m venv ${OTACLIENT_VENV}; \
    source ${OTACLIENT_VENV}/bin/activate; \
    # install otaclient
    python3 -m pip install -U pip; \
    python3 -m pip install /tmp/${OTACLIENT_WHL}; \
    # cleanup
    apt-get clean; \
    rm -rf \
        /root/.cache \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

FROM scratch

ARG OTACLIENT_VERSION

COPY --from image_build / /

ENV OTACLIENT_VERSION=${OTACLIENT_VERSION}
ENV OTACLIENT_INSTALLATION="/opt/ota/client"
ENV OTACLIENT_VENV="/opt/ota/client/venv"

CMD ["/opt/ota/client/venv/bin/python3", "-m", "otaclient"]