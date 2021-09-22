FROM ubuntu:20.04
SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ARG KERNEL_VERSION="5.8.0-53-generic"

RUN apt-get update
RUN apt-get install -y linux-image-${KERNEL_VERSION}
RUN apt-get install -y wget git python3 python3-pip

RUN mkdir /ota-server
WORKDIR /ota-server


RUN wget http://cdimage.ubuntu.com/ubuntu-base/releases/20.04/release/ubuntu-base-20.04.1-base-amd64.tar.gz
RUN mkdir -p data
RUN tar zxf ubuntu-base-20.04.1-base-amd64.tar.gz -C data
RUN cp /boot/vmlinuz-${KERNEL_VERSION} /boot/initrd.img-${KERNEL_VERSION} /boot/config-${KERNEL_VERSION} /boot/System.map-${KERNEL_VERSION} data/boot
RUN git clone https://github.com/tier4/ota-metadata
RUN python3 -m pip install -r ota-metadata/metadata/ota_metadata/requirements.txt
RUN python3 ota-metadata/metadata/ota_metadata/metadata_gen.py --target-dir data --ignore-file ota-metadata/metadata/ignore.txt 
RUN ./ota-metadata/metadata/key-gen.sh
RUN python3 ota-metadata/metadata/ota_metadata/metadata_sign.py --sign-key privatekey.pem --cert-file certificate.pem --persistent-file ota-metadata/metadata/persistents.txt --rootfs-directory data
RUN cp ota-metadata/metadata/persistents.txt .


