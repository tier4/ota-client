FROM ubuntu:20.04
SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ARG KERNEL_VERSION="5.8.0-53-generic"
# ARG KERNEL_VERSION="5.4.0-74-generic"

RUN apt-get update && \
        apt-get install -y \
        init systemd linux-image-${KERNEL_VERSION} \
        sudo git python3-pip vim \
        openssh-server netplan.io iputils-ping netbase isc-dhcp-client

RUN useradd -m autoware -s /bin/bash
RUN echo autoware:autoware | chpasswd
RUN gpasswd -a autoware sudo
RUN echo 'autoware ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers.d/autoware

COPY app /opt/ota/client/app
COPY app/requirements.txt /opt/ota/client/app/requirements.txt

RUN python3 -m pip install -r /opt/ota/client/app/requirements.txt

WORKDIR /home/autoware
USER autoware
