# OTAClient

## Overview

OTAClient is software to perform over-the-air software updates for linux devices.
It provides a set of APIs for user to start the OTA and monitor the progress and status.

It is designed to work with web.auto FMS OTA component.

## Features

- A/B partition update with support for generic x86_64 device, NVIDIA Jetson series based devices and Raspberry Pi
  device.
- Full Rootfs update, with delta update support.
- Local delta calculation, allowing update to any version of OTA image without the need of a pre-generated delta OTA
  package.
- Support persist files from active slot to newly updated slot.
- Verification over OTA image by digital signature and PKI.
- Support for protected OTA server with cookie.
- Optional OTA proxy support and OTA cache support.
- Multiple ECU OTA supports.

## Requirements

- Python 3.8 or higher
- Linux (Ubuntu 20.04, 22.04, 24.04)

## Installation

```bash
pip install otaclient
```

For building from source, see [Development Guide](DEVELOPMENT.md#development-setup).

## Configuration

Sample configuration files are available in the [`samples/`](samples/) directory:

- `ecu_info.yaml` - ECU information configuration
- `proxy_info.yaml` - Proxy settings
- `otaclient.service` - systemd service file

## Protobuf Package

OTAClient uses protobuf for its API interface. To use OTAClient API in your Python project:

### Installation

```bash
pip install https://raw.githubusercontent.com/tier4/ota-client/main/proto/whl/otaclient_pb2-<version>-py3-none-any.whl
```

Or add to your `requirements.txt`:

```text
https://raw.githubusercontent.com/tier4/ota-client/main/proto/whl/otaclient_pb2-<version>-py3-none-any.whl
```

### Usage

```python
from otaclient_pb2.v2 import otaclient_pb2
from otaclient_pb2.v2 import otaclient_pb2_grpc
```

## Documentation

- [Development Guide](DEVELOPMENT.md) - For contributors and developers

## Contributing

Please refer to [DEVELOPMENT.md](DEVELOPMENT.md) for development setup, testing, and contribution guidelines.

## License

OTAClient is licensed under the Apache License 2.0.

This project uses open-source software, each under its own license.
For details, see the table below:

| Software | License                                           | Source                                              |
|----------|---------------------------------------------------|-----------------------------------------------------|
| certifi  | [MPL-2.0](https://opensource.org/license/MPL-2.0) | [GitHub](https://github.com/certifi/python-certifi) |
