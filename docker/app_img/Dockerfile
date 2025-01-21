ARG UBUNTU_BASE="ubuntu:24.04"

FROM ${UBUNTU_BASE} AS image_build

ARG OTACLIENT_WHL
COPY ./${OTACLIENT_WHL} /tmp

ENV OTACLIENT_INSTALLATION="/opt/ota/client"
ENV OTACLIENT_VENV="/opt/ota/client/venv"

RUN set -eux; \
    apt-get update -qq; \
    apt-get install --no-install-recommends -y \
        python3-venv \
        python3-pip; \
    # setup virtual env
    mkdir -p ${OTACLIENT_VENV}; \
    cd ${OTACLIENT_INSTALLATION}; \
    python3 -m venv ${OTACLIENT_VENV}; \
    . ${OTACLIENT_VENV}/bin/activate; \
    # install otaclient
    python3 -m pip install -U pip; \
    python3 -m pip install /tmp/${OTACLIENT_WHL}; \
    # cleanup
    apt-get clean; \
    \
    # NOTE: copy from docker official python3 dockerfile
	find ${OTACLIENT_VENV} -depth \
		\( \
			\( -type d -a \( -name test -o -name tests -o -name idle_test \) \) \
			-o \( -type f -a \( -name '*.pyc' -o -name '*.pyo' -o -name 'libpython*.a' \) \) \
		\) -exec rm -rf '{}' + \
	; \
	\
    rm -rf \
        /root/.cache \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

FROM ${UBUNTU_BASE}

ARG OTACLIENT_VERSION

COPY --from=image_build /opt/ota/client /opt/ota/client

ENV OTACLIENT_VERSION=${OTACLIENT_VERSION}
ENV OTACLIENT_INSTALLATION="/opt/ota/client"
ENV OTACLIENT_VENV="/opt/ota/client/venv"

RUN set -eux; \
    apt-get update -qq; \
    apt-get install --no-install-recommends -y \
        python3; \
    apt-get clean; \
    rm -rf \
        /root/.cache \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

CMD ["/opt/ota/client/venv/bin/python3", "-m", "otaclient"]