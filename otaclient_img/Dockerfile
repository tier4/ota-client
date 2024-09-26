######################################################
# Dockerfile for creating otaclient image.
######################################################

ARG OVERRIDE_PLATFORM=""

#
# ------ stage 1: prepare otaclient installation venv ------ #
#

FROM --platform=${OVERRIDE_PLATFORM} python:3.12-slim-bookworm AS builder

ARG OTACLIENT_PACKAGE
ENV OTACLIENT_PACKAGE=${OTACLIENT_PACKAGE}
ENV OTACLIENT_VENV=/otaclient_venv

COPY ./${OTACLIENT_PACKAGE} /tmp/${OTACLIENT_PACKAGE}

RUN set -eux; \
    # install otaclient
    python3 -m venv ${OTACLIENT_VENV}; \
    . ${OTACLIENT_VENV}/bin/activate; \
    pip install -U --no-cache-dir pip; \
    pip install --no-cache-dir /tmp/${OTACLIENT_PACKAGE}; \
    # cleanup
	find ${OTACLIENT_VENV} -depth \
		\( \
			\( -type d -a \( -name '__pycache__' \) \) \
			-o \( -type f -a \( -name '*.pyc' -o -name '*.pyo' \) \) \
		\) -exec rm -rf '{}' + \
	; \
    \
    echo "Done"

#
# ------ stage 1: prepare otaclient export otaclient image ------ #
#

FROM --platform=${OVERRIDE_PLATFORM} python:3.12-slim-bookworm

ARG OTACLIENT_PACKAGE
ARG OVERRIDE_PLATFORM=""

ENV OTACLIENT_PLATFORM=${OVERRIDE_PLATFORM}
ENV OTACLIENT_PACKAGE=${OTACLIENT_PACKAGE}
ENV OTACLIENT_VENV=/otaclient_venv
ENV OTACLIENT_HOST_ROOT_MP=/host_root
COPY --from=builder ${OTACLIENT_VENV} ${OTACLIENT_VENV}
RUN mkdir -p /host_root

CMD ["/otaclient_venv/bin/python3", "-m", "otaclient"]