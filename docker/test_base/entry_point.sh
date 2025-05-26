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
uv run --python python3 --no-managed-python pytest --junit-xml=${OUTPUT_DIR}/pytest.xml ${@:-}
uv run --python python3 --no-managed-python coverage combine
uv run --python python3 --no-managed-python coverage xml -o ${OUTPUT_DIR}/coverage.xml
