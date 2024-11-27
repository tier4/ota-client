#!/bin/bash
set -eu

TEST_ROOT=/test_root
OTACLIENT_SRC=/otaclient_src
OUTPUT_DIR="${OUTPUT_DIR:-/test_result}"

# copy the source code as source is read-only
cp -R "${OTACLIENT_SRC}" "${TEST_ROOT}"

# exec the input params
echo "execute test with coverage"
cd ${TEST_ROOT}
hatch env create dev
hatch run dev:coverage run -m pytest --junit-xml=${OUTPUT_DIR}/pytest.xml ${@:-}
hatch run dev:coverage combine
hatch run dev:coverage xml -o ${OUTPUT_DIR}/coverage.xml
