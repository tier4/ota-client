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

### Performance tests

Performance tests are marked with `@pytest.mark.performance` and are excluded from regular test runs.
They are executed separately in the `performance_test.yaml` workflow.

To run performance tests locally:

```bash
# at project root directory
docker compose -f docker/test_base/docker-compose_tests_py313.yml run --rm tester-ubuntu-22.04 \
   tests/test_otaclient/test_performance/test_e2e_performance.py -v -s -m performance
```

The performance comparison report will be generated at `test_result/performance_comparison.md`.
