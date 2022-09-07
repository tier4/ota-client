#!/bin/bash
set -eu

OTA_CLIENT_DIR="/ota-client"
VENV="${OTA_CLIENT_DIR}/.venv"

# setup certs
echo "setup certificates for testing..."
mkdir -p ${OTA_CLIENT_DIR}/certs && 
    cp -av ${OTA_CLIENT_DIR}/tests/keys/root.pem ${OTA_CLIENT_DIR}/certs/1.root.pem && 
    cp -av ${OTA_CLIENT_DIR}/tests/keys/interm.pem ${OTA_CLIENT_DIR}/certs/1.interm.pem

# activate virtual env
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
echo "execute command..."
exec "$@"