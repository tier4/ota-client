#!/bin/bash
set -e # exit if any command failed

# configs
DEPENDENCIES=(python3 docker)
TIMESTAMP_FORMAT='%Y-%m-%d %H:%M:%S'
# flags
SETUP_ENVIRONMENT=0
DO_TEST=0

_print_usage() {
    echo "Usage: local_test.sh -w <workding_dir> -r <repo_location> [-s, -e]"
    echo "options: "
    echo "  -w <working_dir>       E2e test will executed under this folder."
    echo "  -r <repo_location>     The location of the to be tested repository."
    echo "  -s                     Setup the test environment."
    echo "  -e                     Do the e2e test."
    echo ""
    echo "If neither -s or -e are set, the whole test will be carried out."
}

_echo() {
    printf "[%(${TIMESTAMP_FORMAT})T] "
    printf "$1\n"
}

_clean_up() {
    [ $? == 0 ] && \
        _echo "Test finished!" || \
        _echo "Test failed!"
    read -rp "$(_echo "Cleanup the working_dir $WORKING_DIR? [Y/N]:")" reply
    echo
    if [ "$reply" == "Y" ]
    then
        rm -rf "$WORKING_DIR"
        _echo "Finished cleaning up working dir!"
    fi
}

setup_test_environment() {
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
}

do_e2e_test() {
    # switch to the working dir
    cd "$WORKING_DIR"
    _echo "Switch to working dir. Current working_dir is `$pwd`."

    # install test dependencies
    _echo "Install test dependencies..."
    python3 -m pip install --upgrade pip
    python3 -m pip install -r ./"$REPO"/app/requirements.txt
    python3 -m pip install -r ./"$REPO"/tests/requirements.txt

    _echo "Start OTA E2E test..."
    export WORKING_DIR="$WORKING_DIR"
    sudo -E python3 -m pytest --cov-report term-missing --cov=app ./"$REPO"/e2e_tests
    _echo "Finished OTA E2E test!"
}

# parse options
while getopts ":hr:w:se" option
do
    case $option in
    h)
        _print_usage
        exit;;
    r)
        REPO_LOCATION=$OPTARG
        REPO=`basename $OPTARG`;;
    w)
        WORKING_DIR=$OPTARG;;
    s)
        SETUP_ENVIRONMENT=1;;
    e)
        DO_TEST=1;;
    *)
        _print_usage
        exit;;
    esac
done
# check arguments
if [ -z "$WORKING_DIR" ] || [ -z "$REPO_LOCATION" ]
then
    _print_usage
    exit -1
else
    _echo "Get absolute paths for WORKING_DIR and REPO_LOCATION..."
    WORKING_DIR=`readlink -f $WORKING_DIR`
    REPO_LOCATION=`readlink -f $REPO_LOCATION`
fi

############# start the script ################
if [ $SETUP_ENVIRONMENT == 1 ] && [ $DO_TEST == 0 ];then
    _echo "Setup test environment only..."
    setup_test_environment
elif [ $SETUP_ENVIRONMENT == 0 ] && [ $DO_TEST == 1 ];then
    trap '_clean_up' SIGINT SIGKILL SIGTERM EXIT
    _echo "Do the e2e test only..."
    do_e2e_test
else
    trap '_clean_up' SIGINT SIGKILL SIGTERM EXIT
    setup_test_environment
    do_e2e_test
fi

exit 0