# OTAClient

## Overview

OTAClient is software to perform over-the-air software updates for linux devices.
It provides a set of APIs for user to start the OTA and monitor the progress and status.

It is designed to work with web.auto FMS OTA component.

## Feature

- A/B partition update with support for generic x86_64 device, NVIDIA Jetson series based devices and Raspberry Pi device.
- Full Rootfs update, with delta update support.
- Local delta calculation, allowing update to any version of OTA image without the need of a pre-generated delta OTA package.
- Support persist files from active slot to newly updated slot.
- Verification over OTA image by digital signature and PKI.
- Support for protected OTA server with cookie.
- Optional OTA proxy support and OTA cache support.
- Multiple ECU OTA supports.

## License

OTAClient is licensed under the Apache License, Version 2.0.
