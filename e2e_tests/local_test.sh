#!/bin/bash
set -e # exit if any command failed

WORKING_DIR=$1
REPO_LOCATION=$2
REPO=`basename $2`
DEPENDENCIES=(python3 docker)
TIMESTAMP_FORMAT='%Y-%m-%d %H:%M:%S'

_print_usage() {
    echo "Usage: local_test.sh <workding_dir> <repo_location>\n"
}

_echo() {
    printf "[%(${TIMESTAMP_FORMAT})T] "
    printf "$1\n"
}

_clean_up() {
    [ $? == 0 ] && \
        _echo "Test finished!" || \
        _echo "Test failed!"
    read -r -p "Cleanup the working_dir $WORKING_DIR? [Y/N]:"
    echo
    if [ $REPLAY == "Y" ]
    then
        rm -rf "$WORKING_DIR"
        _echo "Finished cleaning up working dir!"
    fi
}

trap '_clean_up' SIGINT SIGKILL SIGTERM EXIT

# working_dir and repo_location must be set
if [ -z "$WORKING_DIR" ] || [ -z "$REPO_LOCATION" ]
then
    _print_usage
    exit -1
fi

# check dependencies
_echo "Checking dependencies..."
for cmd in ${DEPENDENCIES[@]}
do
    _echo "Check for $cmd presents..."
    which cmd
done

# cp the repo to the working dir
_echo "Copying the $REPO source codes to $WORKING_DIR..."
cp -a "$REPO_LOCATION" "$WORKING_DIR"
cd "$WORKING_DIR"
_echo "Switch to working dir. Current working_dir is `$pwd`."

# install test dependencies
_echo "Install test dependencies..."
python3 -m pip install --upgrade pip
python3 -m pip install -r ./"$REPO"/app/requirements.txt
python3 -m pip install -r ./"$REPO"/tests/requirements.txt

# build & prepare ota baseimage
_echo "Building OTA baseimage..."
docker build \
    -t base-image \
    --build-arg KERNEL_VERSION=5.8.0-53-generic \
    ./"$REPO"/e2e_tests/Dockerfile_OTA-baseimage
docker create --name base-image base-image
docker export base-image > ./base-image.tgz
_echo "Finished building OTA baseimage."

_echo "Extracting OTA baseimage..."
mkdir -p ./data
sudo tar xf ./base-image.tgz -C ./data

# sign the ota baseimage
_echo "Setup ota-metadata signtools..."
git clone https://github.com/tier4/ota-metadata
python3 -m pip install -r ./ota-metadata/metadata/ota_metadata/requirements.txt
_echo "Signing the OTA-metadata..."
sudo -E python3 ./ota-metadata/metadata/ota_metadata/metadata_gen.py \
    --target-dir ./data --ignore-file ./ota-metadata/metadata/ignore.txt
sudo ./ota-metadata/metadata/key-gen.sh
sudo -E python3 ./ota-metadata/metadata/ota_metadata/metadata_sign.py \
    --sign-key privatekey.pem \
    --cert-file certificate.pem \
    --directory-file dirs.txt \
    --symlink-file symlinks.txt \
    --regular-file regulars.txt \
    --rootfs-directory data \
    --persistent-file ./"$REPO"/e2e_tests/persistents-x1.txt
sudo cp ./"$REPO"/e2e_tests/persistents-x1.txt .
_echo "Finished preparing the OTA baseimage!"

_echo "Start OTA E2E test..."
sudo python3 -m pytest --cov-report term-missing --cov=app ./"$REPO"/e2e_tests > pytest-coverage.txt
cat pytest-coverage.txt
_echo "Finished OTA E2E test!"

exit 0