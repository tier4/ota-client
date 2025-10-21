#!/bin/bash
set -eux

PY_VER=python3.13
TEST_ROOT=/test_root
OTACLIENT_SRC=/otaclient_src
OUTPUT_DIR="${OUTPUT_DIR:-/test_result}"

mkdir -p "${TEST_ROOT}"
# source code needed to be rw, so copy it to the test_root
cp -R "${OTACLIENT_SRC}/src" "${TEST_ROOT}"
cp "${OTACLIENT_SRC}/uv.lock" "${TEST_ROOT}"
# symlink all the other needed folders/files into test root
ln -sf "${OTACLIENT_SRC}/tests" "${TEST_ROOT}"
ln -sf "${OTACLIENT_SRC}/.git" "${TEST_ROOT}"
ln -sf "${OTACLIENT_SRC}/pyproject.toml" "${TEST_ROOT}"
ln -sf "${OTACLIENT_SRC}/README.md" "${TEST_ROOT}"
ln -sf "${OTACLIENT_SRC}/LICENSE.md" "${TEST_ROOT}"
ln -sf "${OTACLIENT_SRC}/scripts" "${TEST_ROOT}"

# exec the input params
echo "execute test with coverage"
cd "${TEST_ROOT}"
# NOTE: use managed python3.13 provided by uv
uv run --python "${PY_VER}" coverage run -m pytest --junit-xml="${OUTPUT_DIR}/pytest.xml" "$@"
uv run --python "${PY_VER}" coverage combine
uv run --python "${PY_VER}" coverage xml -o "${OUTPUT_DIR}/coverage.xml"
