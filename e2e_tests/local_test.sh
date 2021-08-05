#!/bin/bash
set -e # exit if any command failed

# configs
DEPENDENCIES=(python3 docker)
TIMESTAMP_FORMAT='%Y-%m-%d %H:%M:%S'
CONTAINER_NAME=base-image-`date +%s`
# flags
SETUP_ENVIRONMENT=0
DO_TEST=0

_print_usage() {
    echo "Usage: local_test.sh -w <workding_dir> -r <repo_location> [-s -e -c]"
    echo "options: "
    echo "  -w <working_dir>       E2e test will executed under this folder."
    echo "  -r <repo_location>     The location of the to be tested repository."
    echo "  -s                     Setup the test environment."
    echo "  -e                     Do the e2e test."
    echo "  -c                     Cleanup the working dir after the test."
    echo ""
    echo "If neither -s or -e are set, the whole test will be carried out."
}

_echo() {
    printf "[%(${TIMESTAMP_FORMAT})T] "
    case $1 in
    "Error")
        printf "\033[0;31m$2\n\033[0m";;
    "Warn")
        printf "\033[1;33m$2\n\033[0m";;
    *)
        printf "\033[0;32m$1\n\033[0m";;
    esac
    
}

_clean_up() {
    [ $? == 0 ] && \
        _echo "Test finished!" || \
        _echo "Error" "Test failed!"

    ([ $SETUP_ENVIRONMENT == 1 ] && [ $DO_TEST == 0 ] ) || [ $CLEANUP == 1 ] && {
        read -rp "$(_echo "Warn" "Cleanup the working_dir $WORKING_DIR? [Y/N]:")" reply
        echo
        if [ "$reply" == "Y" ]
        then
            rm -rf "$WORKING_DIR"
            _echo "Finished cleaning up working dir!"
        fi
    }
}

setup_test_environment() {
    # check dependencies
    _echo "Warn" "Checking dependencies..."
    for cmd in ${DEPENDENCIES[@]}
    do
        _echo "Check for $cmd presents..."
        which $cmd > /dev/null
    done

    # cp the repo to the working dir
    _echo "Copying the $REPO source codes to $WORKING_DIR..."
    cp -a "$REPO_LOCATION" "$WORKING_DIR"
    cd "$WORKING_DIR" && _echo "Warn" "Switch to working dir. Current working_dir is `$WORKING_DIR`."

    # build & prepare ota baseimage
    _echo "Warn" "Building OTA baseimage..."
    docker build \
        -t base-image \
        --build-arg KERNEL_VERSION=5.8.0-53-generic - < ./"$REPO"/e2e_tests/Dockerfile_OTA-baseimage
    docker create --name $CONTAINER_NAME base-image
    docker export $CONTAINER_NAME > ./base-image.tgz
    docker rm -f $CONTAINER_NAME

    _echo "Finished building OTA baseimage."

    _echo "Warn" "Extracting OTA baseimage..."
    mkdir -p ./data
    tar xf ./base-image.tgz -C ./data

    # sign the ota baseimage
    _echo "Warn" "Setup ota-metadata signtools..."
    git clone https://github.com/tier4/ota-metadata
    python3 -m pip install -r ./ota-metadata/metadata/ota_metadata/requirements.txt
    _echo "Warn" "Signing the OTA-metadata..."
    python3 ./ota-metadata/metadata/ota_metadata/metadata_gen.py \
        --target-dir ./data --ignore-file ./ota-metadata/metadata/ignore.txt
    ./ota-metadata/metadata/key-gen.sh
    python3 ./ota-metadata/metadata/ota_metadata/metadata_sign.py \
        --sign-key privatekey.pem \
        --cert-file certificate.pem \
        --directory-file dirs.txt \
        --symlink-file symlinks.txt \
        --regular-file regulars.txt \
        --rootfs-directory data \
        --persistent-file ./"$REPO"/e2e_tests/persistents-x1.txt
    cp ./"$REPO"/e2e_tests/persistents-x1.txt .
    _echo "Finished preparing the OTA baseimage!"
}

do_e2e_test() {
    # switch to the working dir
    cd "$WORKING_DIR" && \
    _echo "Warn" "Switch to working dir. Current working_dir is $PWD."

    # install test dependencies
    _echo "Warn" "Install test dependencies..."
    python3 -m pip install --upgrade pip
    python3 -m pip install -r ./"$REPO"/app/requirements.txt
    python3 -m pip install -r ./"$REPO"/e2e_tests/requirements.txt

    _echo "Warn" "Start OTA E2E test..."
    export WORKING_DIR="$WORKING_DIR"
    python3 -m pytest --cov-report term-missing --cov=app ./"$REPO"/e2e_tests
    _echo "Finished OTA E2E test!"
}

# parse options
while getopts ":hr:w:sec" option
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
    c)
        CLEANUP=1
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
    _echo "Warn" "Get absolute paths for WORKING_DIR and REPO_LOCATION..."
    WORKING_DIR=`readlink -f $WORKING_DIR`
    REPO_LOCATION=`readlink -f $REPO_LOCATION`
fi
# check priviledge
[ `whoami` != "root" ] && _echo "Error" "Please run the script under root priviledge!" && exit -1

############# start the script ################
trap '_clean_up' SIGINT SIGKILL SIGTERM EXIT

if [ $SETUP_ENVIRONMENT == 1 ] && [ $DO_TEST == 0 ];then
    _echo "Warn" "Setup test environment only..."
    setup_test_environment
elif [ $SETUP_ENVIRONMENT == 0 ] && [ $DO_TEST == 1 ];then
    _echo "Warn" "Do the e2e test only..."
    do_e2e_test
else
    _echo "Warn" "Do the whole e2e test ..."
    setup_test_environment
    do_e2e_test
fi

exit 0