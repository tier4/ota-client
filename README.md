# OTA client

## Overview

This OTA client is a client software to perform over-the-air software updates for linux devices.
To enable updating of software at any layer (kernel, kernel module, user library, user application), the OTA client targets the entire rootfs for updating.
When the OTA client receives an update request, it downloads a list from the OTA server that contains the file paths and the hash values of the files, etc., to be updated, and compares them with the files in its own storage and if there is a match, that file is used to update the rootfs. By this delta mechanism, it is possible to reduce the download size even if the entire rootfs is targeted and this mechanism does not require any specific server implementation, nor does it require the server to keep a delta for each version of the rootfs.

## Feature

- Rootfs updating
- Delta updating
- Redundant configuration with A/B partition update
- Arbitrary files can be copied from A to B partition. This can be used to take over individual files.
- No specific server implementation is required. The server that supports HTTP GET is only required.
  - TLS connection is also required.
- Delta management is not required for server side.
- To restrict access to the server, cookie can be used.
- All files to be updated are verified by the hash included in the metadata, and the metadata is also verified by X.509 certificate locally installed.
- Transfer data is encrypted by TLS
- Multiple ECU(Electronic Control Unit) support
- By the internal proxy cache mechanism, the cache can be used for the download requests to the same file from multiple ECU.

## License

OTA client is licensed under the Apache License, Version 2.0.
