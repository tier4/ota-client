FROM ubuntu:22.04

ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8

RUN rm -f /etc/apt/apt.conf.d/docker-clean; echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        initramfs-tools \
        ubuntu-minimal \
        grub-efi-$TARGETARCH \
        systemd \
        systemd-sysv \
        libsystemd0 \
        ca-certificates \
        dbus \
        iptables \
        iproute2 \
        kmod \
        locales \
        sudo \
        udev \
    && \
    # Prevents journald from reading kernel messages from /dev/kmsg
    echo "ReadKMsg=no" >> /etc/systemd/journald.conf

# Install Docker
RUN --mount=type=cache,id=apt-cache$TARGETARCH,sharing=locked,target=/var/cache/apt --mount=type=cache,id=apt-lib-$TARGETARCH,sharing=locked,target=/var/lib/apt \
    apt-get update \
    && apt-get install -y curl \
    && curl -fsSL https://get.docker.com -o get-docker.sh  \
    && sh get-docker.sh --version 28.5.1

RUN set -eux; \
    . /etc/os-release; \ 
     if [ -z "${VERSION_ID:-}" ] || [ -z "${VERSION_CODENAME:-}" ]; then \ 
         echo "Unable to detect Ubuntu version metadata"; exit 1; \ 
     fi; \ 
     apt-get update; \ 
     apt-get install -y --allow-downgrades "containerd.io=1.7.28-1~ubuntu.${VERSION_ID}~${VERSION_CODENAME}" && \ 
     apt-mark hold containerd.io 

# Disable systemd services/units that are unnecessary within a container.
RUN systemctl mask systemd-udevd.service \
                   systemd-udevd-kernel.socket \
                   systemd-udevd-control.socket \
                   systemd-modules-load.service \
                   sys-kernel-debug.mount \
                   sys-kernel-tracing.mount

# Make use of stopsignal (instead of sigterm) to stop systemd containers.
STOPSIGNAL SIGRTMIN+3

# Set systemd as entrypoint.
ENTRYPOINT [ "/sbin/init", "--log-level=err" ]
