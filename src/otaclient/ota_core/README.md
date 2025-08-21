# OTAClient core

The implementation of OTA request and OTA execution handling.

The structure of the `ota_core` package is as follows:

1. **`_updater_base`**: Implements the common shared base `OTAUpdateOperator` for `OTAUpdater` and `OTAClientUpdater` implementations.

2. **`_updater`**: Implements the OTA Update logic as `OTAUpdate`.

3. **`_client_updater`**: Implements the dynamic OTAClient update as `OTAClientUpdate`.

4. **`_common`**: Common utilities and helper functions for `ota_core`, currently include:

    - `download_exception_handler`: A common exception handler for all downloadings during OTA operations.

5. **`_download_resources`**: Implements the downloading functionality for OTA operations as `DownloadHelper`.

6. **`_main`**: Implements the ota_process entrypoint and the RPC adapter between the otaclient gRPC server process and ota_core process.