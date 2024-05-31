#!/bin/bash
set -eu

TEST_ROOT=/test_root
OTACLIENT_SRC=/otaclient_src
OUTPUT_DIR="${OUTPUT_DIR:-/test_result}"
CERTS_DIR="${CERTS_DIR:-/certs}"

# copy the source code as source is read-only
cp -R "${OTACLIENT_SRC}" "${TEST_ROOT}"

# copy the certs generated in the docker image to otaclient dir
echo "setup certificates for testing..."
cd ${TEST_ROOT}
mkdir -p ./certs
cp -av ${CERTS_DIR}/root.pem ./certs/1.root.pem
cp -av ${CERTS_DIR}/interm.pem ./certs/1.interm.pem

# exec the input params
echo "execute test with coverage"
cd ${TEST_ROOT}
hatch env create dev
hatch run dev:coverage run -m pytest --junit-xml=${OUTPUT_DIR}/pytest.xml ${@:-}
hatch run dev:coverage xml -o ${OUTPUT_DIR}/coverage.xml
