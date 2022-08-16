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

## How to run OTA client on the target ECU

Appropriate partitioning and configurations are required. See [docs/README.md](docs/README.md).

### Setup

```bash
sudo apt install -y python3-setuptools
sudo python3 -m pip install -U pip
git clone https://github.com/tier4/ota-client
cd ota-client
sudo python3 -m pip install -r app/requirements.txt
```

### Run

```bash
sudo python3 app/main.py
```

## How to test OTA client on the development PC

### Build the image for testing

Build the `ota-test_base` image for running tests under a container as follow:

```bash
docker-compose -f ./docker-compose.yml build
```

This `ota-test_base` image contains a copy of pre-build minimum `ota-image` under `/ota-image` folder, and pre-installed dependencies needed for running and testing OTA client.

### Run all tests at once

```bash
docker-compose -f ./docker-compose.yml up --no-log-prefix
```

### Run specific tests

Adjust the `docker-compose.yml` file to specify the test files you want to run as follow:

```yaml
   ...
    command: >
      bash -c 
        "mkdir -p /ota-client/certs && 
         cp -av /ota-client/tests/keys/root.pem /ota-client/certs/1.root.pem && 
         cp -av /ota-client/tests/keys/interm.pem /ota-client/certs/1.interm.pem && 
         python3 -m pytest ./tests/<test_files>"
    container_name: ota-test
   ...
```

and then launch the container to run the tests:

```bash
docker-compose -f ./docker-compose.yml up --no-log-prefix
```

## How to update protobuf

OTA client service is using protobuf interface.
After updating the protobuf files under `protobuf/*.proto`, some operations are required.

## Updating app/otaclient_v2_pb2*py

The protobuf definition for python implementation under `app` directory should be updated.

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

### Updating docs/SERVICES.md

The protobuf document docs/SERVICES.md should be updated by protoc-gen-doc tool.

```bash
docker run --rm -v $(pwd)/docs:/out -v $(pwd)/proto:/protos pseudomuto/protoc-gen-doc --doc_opt=markdown,SERVICES.md
```
