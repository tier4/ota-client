# ------ common build args ------ #
# python base configs
ARG PYTHON_IMAGE_VER=3.11.7-slim
ARG DEBIAN_BASE_CODENAME=bookworm

# build configs
ARG BUILD_CONTEXT=./
ARG OTACLIENT_REQUIREMENTS./otaclient/app/requirements.txt
ARG OTACLIENT_VERSION
ARG OTACLIENT_COMMIT
ARG BUILD_DATE
ARG PLATFORM

# fixed build configs
ARG PYTHON_VENV=/otaclient_venv

#
# ------ stage 1: prepare virtual env ------ #
#
# this stage installs and builds the otaclient requirements
FROM python:${PYTHON_IMAGE_VER}-${DEBIAN_BASE_CODENAME} as deps_installer
RUN set -eux; \
	apt-get update && \
	apt-get install -y --no-install-recommends \
		libcurl4-openssl-dev \
		libssl-dev \
		python3-dev \
		gcc \
		git 

# install otaclient deps
ARG PYTHON_VENV
ARG OTACLIENT_REQUIREMENTS
COPY "${OTACLIENT_REQUIREMENTS}" /tmp/requirements.txt

RUN set -eux; \
	python3 -m venv ${PYTHON_VENV} && \
	. ${PYTHON_VENV}/bin/activate && \
	export PYTHONDONTWRITEBYTECODE=1 && \
	python3 -m pip install --no-cache-dir -U pip setuptools wheel && \
	python3 -m pip install --no-cache-dir -r /tmp/requirements.txt

# 
# ------ stage 2: build otaclient wheel ------ #
#
FROM deps_installer as otaclient_builder
ARG PYTHON_VENV
ARG BUILD_CONTEXT
COPY "${BUILD_CONTEXT}/*" /tmp/otaclient

RUN set -eux; \
	. ${PYTHON_VENV}/bin/activate && \
	export PYTHONDONTWRITEBYTECODE=1 && \
	python3 -m pip install --no-cache-dir -U build setuptools setuptools_scm && \
	python3 -m build --wheel --no-isolation --outdir /tmp/otaclient_dist /tmp/otaclient

#
# ------ stage 3: final image build ------ #
#
FROM scratch

# store the labels at the final image
LABEL build_version="otaclient ${OTACLIENT_VERSION}-${OTACLIENT_COMMIT} (on ${PLATFORM} ${PYTHON_IMAGE_VER}-${DEBIAN_BASE_CODENAME}, ${BUILD_DATE})"
LABEL otaclient_version="otaclient ${OTACLIENT_VERSION}-${OTACLIENT_COMMIT}"
LABEL image_base="${PYTHON_IMAGE_VER}-${DEBIAN_BASE_CODENAME}"
LABEL maintainer="bodong.yang@tier4.jp"

# squash the python base
ARG PYTHON_IMAGE_VER
ARG DEBIAN_BASE_CODENAME
COPY --from=python:${PYTHON_IMAGE_VER}-${DEBIAN_BASE_CODENAME} / /

ARG BUILD_CONTEXT
ARG PYTHON_VENV

COPY --from=deps_installer ${PYTHON_VENV} ${PYTHON_VENV}
COPY --from=otaclient_builder /tmp/otaclient_dist /tmp/otaclient_dist

RUN set -eux; \
	. ${PYTHON_VENV}/bin/activate && \
	export PYTHONDONTWRITEBYTECODE=1 && \
	python3 -m pip install --no-deps /tmp/otaclient_dist/*.whl && \
	# post install cleanup
	rm -rf \
		/tmp/* \
		/root/.cache

WORKDIR /
CMD ["/usr/bin/bash"]