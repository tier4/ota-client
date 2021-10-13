#!/bin/bash

set -eux

docker build -t ota-image -f Dockerfile.build_image .
id=$(docker create -it ota-image)
ota_image_dir="ota-image.$(date +%Y%m%d%H%M%S)"
mkdir ${ota_image_dir}
(
cd ${ota_image_dir}
docker export ${id} > ota-image.tar
mkdir data
sudo tar xf ota-image.tar -C data
git clone https://github.com/tier4/ota-metadata

cp ../tests/keys/sign.pem .
cp ota-metadata/metadata/persistents.txt .

sudo python3 ota-metadata/metadata/ota_metadata/metadata_gen.py --target-dir data --ignore-file ota-metadata/metadata/ignore.txt
sudo python3 ota-metadata/metadata/ota_metadata/metadata_sign.py --sign-key ../tests/keys/sign.key --cert-file sign.pem --persistent-file persistents.txt --rootfs-directory data

sudo chown -R $(whoami) data
)
