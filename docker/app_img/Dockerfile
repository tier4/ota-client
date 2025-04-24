ARG UBUNTU_BASE=ubuntu:22.04

FROM ${UBUNTU_BASE} AS image_build

ARG OTACLIENT_WHL
COPY ./${OTACLIENT_WHL} /tmp

ARG OTACLIENT_INSTALLATION="/otaclient"
ENV OTACLIENT_VENV=${OTACLIENT_INSTALLATION}/venv

# Install Python 3.8 with proper GPG handling
RUN set -eux && \
    apt-get update -qq && \
    apt-get install --no-install-recommends -y \
        software-properties-common \
        gnupg \
        dirmngr \
        apt-transport-https \
        ca-certificates && \
    apt-key adv --keyserver keyserver.ubuntu.com --recv-keys F23C5A6CF475977595C89F51BA6932366A755776 && \
    echo "deb http://ppa.launchpad.net/deadsnakes/ppa/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) main" > /etc/apt/sources.list.d/deadsnakes-ppa.list && \
    apt-get update -qq && \
    apt-get install --no-install-recommends -y \
        python3.8 \
        python3.8-venv \
        python3.8-distutils \
        python3-pip && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.8 1 && \
    update-alternatives --set python3 /usr/bin/python3.8 && \
    python3 --version

# Setup venv and install otaclient
RUN set -eux && \
    mkdir -p ${OTACLIENT_VENV} && \
    cd ${OTACLIENT_INSTALLATION} && \
    python3 -m venv ${OTACLIENT_VENV} && \
    . ${OTACLIENT_VENV}/bin/activate && \
    python3 -m pip install -U pip && \
    python3 -m pip install /tmp/${OTACLIENT_WHL}

# Cleanup
RUN set -eux && \
    apt-get clean && \
    find ${OTACLIENT_VENV} -depth \
        \( \
            \( -type d -a \( -name test -o -name tests -o -name idle_test -o -name __pycache__ \) \) \
            -o \( -type f -a \( -name '*.pyc' -o -name '*.pyo' -o -name 'libpython*.a' \) \) \
        \) -exec rm -rf '{}' + && \
    rm -rf \
        /root/.cache \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

FROM ${UBUNTU_BASE}

ARG OTACLIENT_VERSION
ARG OTACLIENT_INSTALLATION="/otaclient"

COPY --from=image_build ${OTACLIENT_INSTALLATION} ${OTACLIENT_INSTALLATION}

ENV OTACLIENT_VERSION=${OTACLIENT_VERSION}
ENV OTACLIENT_INSTALLATION=${OTACLIENT_INSTALLATION}
ENV OTACLIENT_VENV=${OTACLIENT_INSTALLATION}/venv

RUN set -eux; \
    apt-get update -qq; \
    apt-get install --no-install-recommends -y \
        software-properties-common; \
    add-apt-repository ppa:deadsnakes/ppa; \
    apt-get update -qq; \
    apt-get install --no-install-recommends -y \
        python3.8; \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.8 1; \
    update-alternatives --set python3 /usr/bin/python3.8; \
    python3 --version; \
    apt-get clean; \
    rm -rf \
        /root/.cache \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

CMD ["${OTACLIENT_VENV}/bin/python3", "-m", "otaclient"]
