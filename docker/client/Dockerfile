FROM ubuntu:20.04
SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ARG KERNEL_VERSION="5.8.0-53-generic"

COPY ./app/requirements.txt requirements.txt
COPY ./tests/requirements.txt requirements-dev.txt

RUN \
    apt-get update && \
    apt-get install -y python3 python3-pip && \
    python3 -m pip install -r requirements.txt && \
    python3 -m pip install -r requirements-dev.txt

WORKDIR /ota-client
