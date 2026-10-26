# OTAClient

## Overview

OTAClient is software to perform over-the-air software updates for linux devices.
It provides a set of APIs for user to start the OTA and monitor the progress and status.

It is designed to work with web.auto FMS OTA component.

## Features

- A/B partition update with support for generic x86_64 device, NVIDIA Jetson series based devices and Raspberry Pi
- A/B update of the partition contract layouts (`bootloader: grub-verity` on x86_64, `jetson-dpi` on NVIDIA Jetson), where the platform's DPI writes the slots as partition images and otaclient drives it
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

- Python 3.10 or higher
- Linux (Ubuntu 22.04, 24.04)

Tested on ubuntu 22.04~24.04, and should work on modern linux distros that use systemd.

## Installation

- **For development**: Install from source code or use a released Python wheel package. See [Development Guide](DEVELOPMENT.md#development-setup).
- **For deployment**: Using the OTAClient app image is recommended.

## Configuration

Sample configuration files are available in the [`samples/`](samples/) directory:

- `ecu_info.yaml` - ECU information configuration
- `proxy_info.yaml` - Proxy settings
- `otaclient.service` - systemd service file

`ecu_info.yaml` and `proxy_info.yaml` are installed at `/boot/ota/`. At each OTA, the copies in the OTA image's `/boot/ota/` replace them:

- A file listed in the OTA image's persist files, by itself or by a parent folder, keeps its current content.
- A file not shipped in the OTA image, or not valid, keeps its current content.
- When the OTA image's `ecu_info.yaml` has a different `ecu_id`, neither file is replaced, as the OTA image is not for this ECU.
- Changes made on the ECU are overwritten, so change the files in the OTA image instead.

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

## Testing

> **Note:** Tests live under the `tests/` folder, organized by category:
>
> - `tests/unit/` - Unit tests
> - `tests/integration/` - Integration tests
> - `tests/e2e/` - End-to-end tests
>
> Please follow this structure when adding or updating tests.

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
