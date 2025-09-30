# OTA client development

THIS DOCUMENT IS FOR INTERNAL DEVELOPER ONLY.

This document describes how to run and test OTA client and how to maintain the repository.
In this document, Ubuntu 20.04 is used for both running and development environment.

## Python version

Python3.8 or higher is required. Please make sure the `python3` is appropriate version.

```bash
$ python3 --version
Python 3.8.10
```

## How to build

You can build the python wheel package as follow:

1. install `uv`: <https://docs.astral.sh/uv/getting-started/installation/>

2. setup a venv environment:

```bash
uv sync --locked
```

3. build the package

```bash
uv build --wheel
```

4. the built package will be placed under `./dist` folder

## How to test OTA client on the development PC

### Use docker compose to run tests in a container

The test containers are defined in `docker/test_base/docker-compose_tests.yml`,
Test containers for ubuntu 18.04, 20.04 and 22.04 are defined.

The `./docker/test_base/entry_point.sh` will be mounted into the test container as entrypoint,
you can adjust the script as needed.

### Run all tests at once

The following example will run all tests in the container for ubuntu 20.04:

```bash
# at project root directory
docker compose -f docker/test_base/docker-compose_tests.yml run --rm tester-ubuntu-20.04
```

### Run specific tests manually by override the command

Specify to only run specific tests is also possible as follow:

```bash
# at project root directory
docker compose -f docker/test_base/docker-compose_tests.yml run --rm tester-ubuntu-20.04 \
   tests/<specific_test_file>  [<test_file_2> [...]]
```

### Control the test container interactively

Directly drop to bash shell in the test base container as follow:

```bash
# at the project root directory
docker compose -f docker/test_base/docker-compose_tests.yml run --entrypoint=/bin/bash -it --rm tester-ubuntu-20.04
```

## How to update protobuf

OTA client service is using protobuf interface.
After updating the protobuf files under `proto/*.proto`, some operations are required.

## Updating otaclient/app/proto/otaclient_v2_pb2*py

The protobuf definition for python implementation under `otaclient/app/proto` directory should be updated.

```bash
python3 -m grpc_tools.protoc -I./proto --python_out=app --grpc_python_out=app ./proto/otaclient_v2.proto
```

### Updating protobuf whl

The whl package for the OTA client user implemented in python should be updated.

#### How to build protobuf whl

1. Edit and update version in proto/VERSION.
2. Build whl as follows:

   ```bash
   cd proto
   make
   ```

3. After build, whl file is generated in proto/whl directory.

#### How to install protobuf whl

You can install protobuf whl with pip command.

```bash
python3 -m pip install https://raw.githubusercontent.com/tier4/ota-client/main/proto/whl/otaclient_pb2-xxxxx-py3-none-any.whl
```

If you're using requirement.txt, add the following line into the requirements.txt.

```bash
https://raw.githubusercontent.com/tier4/ota-client/main/proto/whl/otaclient_pb2-xxxxx-py3-none-any.whl
```

Note that `xxxxx` above should be replaced by the actual file name.

#### How to import protobuf package

```bash
$ python3
Python 3.8.10 (default, Nov 26 2021, 20:14:08)
[GCC 9.3.0] on linux
Type "help", "copyright", "credits" or "license" for more information.
>>> from otaclient_pb2.v2 import otaclient_pb2
>>> from otaclient_pb2.v2 import otaclient_pb2_grpc
```

### Creating docs/SERVICES.md

The protobuf document docs/SERVICES.md should be updated by protoc-gen-doc tool.

```bash
docker run --rm -v $(pwd)/docs:/out -v $(pwd)/proto:/protos pseudomuto/protoc-gen-doc --doc_opt=markdown,SERVICES.md
```
