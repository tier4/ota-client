#!/bin/bash
set -eu

OTA_CLIENT_DIR="${OTACLIENT_DIR:-/ota-client}"
OUTPUT_DIR="${OUTPUT_DIR:-/test_result}"
CERTS_DIR="${CERTS_DIR:-/certs}"
VENV="${OTA_CLIENT_DIR}/.venv"

# copy the certs generated in the docker image to otaclient dir
echo "setup certificates for testing..."
cd ${OTA_CLIENT_DIR}
mkdir -p ./certs
cp -av ${CERTS_DIR}/root.pem ./certs/1.root.pem
cp -av ${CERTS_DIR}/interm.pem ./certs/1.interm.pem

source ${VENV}/bin/activate

# setup dependencies unconditionally
# NOTE: if we only do minor modified to the requirements.txt,
#       we don't have to rebuild the image, just call pip here
APP_DEPENDENCIES="${OTA_CLIENT_DIR}/otaclient/requirements.txt"
[ -f "$APP_DEPENDENCIES" ] && echo "setup app dependencies..." && \
    python3 -m pip install --no-cache-dir -q -r $APP_DEPENDENCIES
TESTS_DEPENDENCIES="${OTA_CLIENT_DIR}/tests/requirements.txt"
[ -f "$TESTS_DEPENDENCIES" ] && echo "setup tests dependencies..." && \
    python3 -m pip install --no-cache-dir -q -r $TESTS_DEPENDENCIES

# exec the input params
echo "execute test with coverage"
cd ${OTA_CLIENT_DIR}
coverage run -m pytest --junit-xml=${OUTPUT_DIR}/pytest.xml ${@:-}
coverage xml -o ${OUTPUT_DIR}/coverage.xml
