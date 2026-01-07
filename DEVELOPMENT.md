# Development Guide

This document describes how to set up the development environment, run tests, and maintain the OTAClient repository.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (for dependency management)
- Docker and Docker Compose (for running tests)

For basic requirements (Python version, supported OS), see [README.md](README.md#requirements).

## Development Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)

2. Setup a venv environment:

```bash
uv sync --locked
```

3. Build the package:

```bash
uv build --wheel
```

The built package will be placed under `./dist` folder.

## Testing

### Running Tests with Docker Compose

Test containers are defined in `docker/test_base/docker-compose_tests.yml`.
Containers for Ubuntu 20.04, 22.04, and 24.04 are available.

The `./docker/test_base/entry_point.sh` script is mounted as the entrypoint and can be customized as needed.

#### Run all tests

```bash
# At project root directory
docker compose -f docker/test_base/docker-compose_tests.yml run --rm tester-ubuntu-20.04
```

#### Run specific tests

```bash
# At project root directory
docker compose -f docker/test_base/docker-compose_tests.yml run --rm tester-ubuntu-20.04 \
   tests/<specific_test_file> [<test_file_2> [...]]
```

#### Interactive shell

```bash
# At project root directory
docker compose -f docker/test_base/docker-compose_tests.yml run --entrypoint=/bin/bash -it --rm tester-ubuntu-20.04
```

## Protobuf Maintenance

OTAClient uses protobuf for its gRPC interface. After updating protobuf files under `proto/*.proto`, follow these steps:

### Update Python protobuf files

Update the protobuf definitions under `otaclient/app/proto`:

```bash
python3 -m grpc_tools.protoc -I./proto --python_out=app --grpc_python_out=app ./proto/otaclient_v2.proto
```

### Build protobuf wheel package

1. Edit and update version in `proto/VERSION`

2. Build the wheel:

```bash
cd proto
make
```

3. The wheel file will be generated in `proto/whl` directory

### Update API documentation

Generate `docs/SERVICES.md` using protoc-gen-doc:

```bash
docker run --rm --user $(id -u):$(id -g) -v $(pwd)/docs:/out -v $(pwd)/proto:/protos pseudomuto/protoc-gen-doc --doc_opt=markdown,SERVICES.md
```
