#!/usr/bin/env bash

set -eu

usage() {
  echo "\
Usage:
  sudo $0 (images_dir|image_path) device
images_dir:  Directory that contains rootfs tgz images.
images_path: rootfs tgz image file.
device:      External cache storage device to be created. e.g. /dev/sdc."
}

if [ $(id -u) -ne 0 ]; then
  echo "Error: \"sudo\" must be specified."
  usage
  exit 1
fi

if [ $# -ne 2 ]; then
  echo "Error: invalid argument"
  usage
  exit 1
fi

IMAGE=$(realpath $1)
DEVICE=$2

UUID=$(lsblk ${DEVICE} -n -o UUID || true)
if [ "${UUID}" = "" ]; then
  echo "Error: device(=${DEVICE})"
  usage
  exit 1
fi

if grep -q ${UUID} /etc/fstab; then
  echo "Error: device(=${DEVICE}) is a internal device"
  exit 1
fi

if grep -q ${DEVICE} /etc/fstab; then
  echo "Error: device(=${DEVICE}) is a internal device"
  exit 1
fi

SCRIPT_DIR=$(dirname $(realpath $0))
ROOT_DIR=${SCRIPT_DIR}/../..

cd ${ROOT_DIR}
python3 -m venv venv
source venv/bin/activate

pip install . -q

if [ -d ${IMAGE} ]; then
  python -m tools.images_packer build-cache-src  --image-dir=${IMAGE} --write-to=${DEVICE} --force-write-to
elif [ -f ${IMAGE} ]; then
  python -m tools.images_packer build-cache-src  --image=${IMAGE} --write-to=${DEVICE} --force-write-to
else
  usage
fi
