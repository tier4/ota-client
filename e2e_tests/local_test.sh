#!/bin/bash

WORKING_DIR=$1
REPO_LOCATION=$2

_print_usage() {
    echo "Usage: local_test.sh <workding_dir> <repo_location>"
}

_echo() {
    printf '[%s] %s' "$(date +'%Y/%m/%d %H:%M:%S')" $1
}

_clean_up() {
    _echo "Test fnished/terminated ..."
    read -r -p "Cleanup the working_dir $WORKING_DIR? [Y/N]:"
    echo
    if [ $REPLAY ~= "^[Yy]$" ]
    then
        _echo "Start to cleanup the working dir..."
        rm -rf "$WORKING_DIR"
        _echo "Finished!"
    fi
    exit 0
}

_exit_with_msg() {
    _echo $1
    exit $2
}

trap _clean_up SIGINI SIGKILL SIGTERM

if [ -z "$1" ] && [ -z "$2" ]
then
    _print_usage
    exit -1
fi

# check dependencies

# cp the repo to the working dir
_echo "Setting up working_dir $WORKING_DIR..."
cp -av "$REPO_LOCATION" "$WORKING_DIR"
cd "$WORKING_DIR"

# prepare dependencies

_echo "Building OTA baseimage..."
docker build \
    -t base-image \
    --build-arg KERNEL_VERSION=5.8.0-53-generic \
    ./"$REPO_LOCATION"/e2e_tests/Dockerfile_OTA-baseimage
docker create --name base-image base-image
docker export base-image > "$WORKING_DIR"/base-image.tgz
_echo "Finished building OTA baseimage."

_echo "Extracting OTA baseimage..."
mkdir -p "$WORKING_DIR"/data
sudo tar xf "$WORKING_DIR"/base-image.tgz -C "$WORKING_DIR"/data

_echo "Setup ota-metadata signtools..."
git clone https://github.com/tier4/ota-metadata


_echo "Signing the OTA-metadata..."
sudo python3 ota-metadata/metadata/ota_metadata/metadata_gen.py \
    --target-dir data --ignore-file ota-metadata/metadata/ignore.txt
sudo "$WORKING_DIR"/ota-metadata/metadata/key-gen.sh
sudo python3 "$WORKING_DIR"/ota-metadata/metadata/ota_metadata/metadata_sign.py \
    --sign-key privatekey.pem \
    --cert-file certificate.pem \
    --directory-file dirs.txt \
    --symlink-file symlinks.txt \
    --regular-file regulars.txt \
    --rootfs-directory data \
    --persistent-file ota-client/e2e_tests/persistents-x1.txt
sudo cp "$WORKING_DIR"/ota-client/e2e_tests/persistents-x1.txt .
_echo "Finished preparing the OTA baseimage!"
