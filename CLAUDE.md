# CLAUDE.md

## Project Overview

OTAClient is an over-the-air software update system for Linux devices with a gRPC API. It supports A/B partition updates on x86_64, NVIDIA Jetson, and Raspberry Pi.

## Development Setup

```bash
# Install uv, then:
uv sync --locked

# Build wheel
uv build --wheel
```

## Running Tests

Tests require Docker (Python 3.13, Ubuntu 22.04).

```bash
# Run all tests
docker compose -f docker/test_base/docker-compose_tests_py313.yml run --rm tester-ubuntu-22.04

# Run specific tests
docker compose -f docker/test_base/docker-compose_tests_py313.yml run --rm tester-ubuntu-22.04 \
    tests/<specific_test_file>

# Interactive shell
docker compose -f docker/test_base/docker-compose_tests_py313.yml run --entrypoint=/bin/bash -it --rm tester-ubuntu-22.04
```

Performance tests (`@pytest.mark.performance`) are excluded from regular runs and run separately.

## Code Style

- Formatter: **ruff format**
- Linter: **ruff** (with auto-fix)
- Pre-commit hooks enforce formatting: `pre-commit run --all-files`
- Protobuf generated files (`*_pb2.py`, `*_pb2_grpc.py`) are excluded from linting

## Key Architecture

```text
src/
├── otaclient/          # Main OTA client (boot_control, ota_core, grpc, configs, create_standby)
├── otaclient_api/      # gRPC API definitions (v2)
├── otaclient_common/   # Shared utilities (downloader, cmdhelper, logging)
├── otaclient_manifest/ # Manifest schema
├── ota_metadata/       # OTA metadata parsing (v1, legacy)
└── ota_proxy/          # OTA proxy and caching
```

- gRPC service: `OtaClientService` with `Update`, `Abort`, `Rollback`, `Status`, `ClientUpdate` methods
- Async throughout: pytest-asyncio with `asyncio_mode = "auto"`
- Python 3.8+ minimum, tested up to 3.13

## Dependency Management

- Use **uv** (not pip directly)
- Lock file: `uv.lock`
- `requirements.txt` is frozen deps for reference
