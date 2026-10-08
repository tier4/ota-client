# OTA error codes

When an OTA operation fails, otaclient reports the failure via the status API with
`failure_type`, `failure_reason` and `failure_traceback`.
The error codes are defined in `src/otaclient/errors.py`.

## `failure_reason` format

```text
E<code>: <description>[; detail: <detail message>]
```

- `E<code>` is the 3-digit error code listed below.
- `<description>` is the fixed description of the error code.
- `<detail message>` is the context-specific message of this failure, truncated to at most 256 chars.
  It is omitted when no detail is available.

Example:

```text
E504: downloading made no progress for too long, please check the network connection to the OTA image server or upper otaproxy; detail: OTA resources downloading made no progress for 300s, abort OTA
```

## Persistence across restarts

When an OTA update fails, otaclient sets the current slot's `status` to `FAILURE` and saves `failure_type`
and `failure_reason` to `failure_info` in the same OTA status folder.
After otaclient restarts or the ECU reboots, the saved failure is reported again together with the `FAILURE` status.

- `failure_info` is cleared when the next OTA reaches pre-update(current slot) and post-update(standby slot).
- `failure_info` records the inode and mtime of the `status` file written together with it.
  If `status` has been re-written afterwards, for example by an older otaclient which doesn't know `failure_info`,
  the saved failure is regarded as stale and ignored.
- `status` keeps its original format, so older otaclient can still read it and ignores `failure_info`.

## Rules

- The error codes follow the HTTP status code convention:
  - `4xx`: USER side failure.
    Caused by the OTA request or the OTA image, the requester side can resolve it by fixing the request or the OTA image.
  - `5xx`: SYSTEM side failure.
    Caused by the ECU, otaclient itself or the environment(network, boot control, storage, etc.), requires investigation on the ECU side.
- The well-known HTTP status code is used when the meaning matches, for example `401`, `404`, `409`, `499`, `500`, `501`, `502`, `503`, `504` and `507`.
- The code range does NOT imply whether the failure is recoverable, check `failure_type` for it.
- Once released, a code number MUST NOT be re-assigned to another meaning.
  When a code is moved or deprecated, it is kept as `RESERVED_<code>`.
- Codes below 400 are the legacy codes used by previous otaclient versions, see [Legacy error codes](#legacy-error-codes).
  They don't overlap with the current codes, so the upper layer can tell which scheme a failure_reason uses by the code range.
- A more specific code is implemented as a sub-class of the more generic one, for example
  `E552`/`E553` are sub-classes of `E551`, and `E555`/`E556`/`E557` are sub-classes of `E554`.

## 4xx: USER side errors

| Code | Name | Failure type | When |
| ---- | ---- | ---- | ---- |
| E400 | `E_INVALID_OTAUPDATE_REQUEST` | unrecoverable | The OTA update request is invalid(for example, invalid cookies). |
| E401 | `E_UPDATE_REQUEST_COOKIE_INVALID` | recoverable | HTTP 401/403 when downloading from the OTA image server. |
| E404 | `E_OTA_IMAGE_NOT_FOUND` | recoverable | HTTP 404 when downloading the OTA image files. |
| E409 | `E_OTA_BUSY` | recoverable | Another OTA operation is on-going. |
| E412 | `E_INVALID_STATUS_FOR_OTAROLLBACK` | recoverable | Previous OTA didn't succeed, rollback rejected. |
| E422 | `E_OTA_IMAGE_INVALID` | recoverable | OTA image metadata is broken. |
| E460 | `E_METADATAJWT_CERT_VERIFICATION_FAILED` | unrecoverable | Certificate verification for the OTA image metadata failed. |
| E461 | `E_METADATAJWT_INVALID` | unrecoverable | metadata.jwt is verified but its content is invalid. |
| E462 | `E_BOOTCONTROL_BSP_VERSION_COMPATIBILITY_FAILED` | recoverable | OTA image's BSP version doesn't match the ECU's firmware BSP version. |
| E470 | `E_CLIENT_UPDATE_SAME_VERSIONS` | recoverable | Client update skipped as the versions are the same. |
| E499 | `E_OTA_ABORTED` | recoverable | OTA aborted by the abort request. |

## 5xx: SYSTEM side errors

| Code | Name | Failure type | When |
| ---- | ---- | ---- | ---- |
| E500 | `E_OTA_ERR_UNRECOVERABLE` | unrecoverable | Generic unrecoverable error, also used for unexpected errors during OTA update. |
| E501 | `E_BOOTCONTROL_PLATFORM_UNSUPPORTED` | unrecoverable | Failed to determine the boot controller for this ECU at otaclient startup. |
| E502 | `E_NETWORK` | recoverable | Generic network error. |
| E503 | `E_OTA_ERR_RECOVERABLE` | recoverable | Generic recoverable error, retry after rebooting the ECU or restarting otaclient. |
| E504 | `E_DOWNLOAD_STALLED` | recoverable | Metadata or resources downloading made no progress for `DOWNLOAD_INACTIVE_TIMEOUT`. |
| E507 | `E_STANDBY_SLOT_INSUFFICIENT_SPACE` | unrecoverable | No space left on the standby slot during downloading or applying update. |
| E520 | `E_OTAMETA_DOWNLOAD_FAILED` | recoverable | Failed to download or prepare OTA image metadata. |
| E521 | `E_OTA_RESOURCE_DOWNLOAD_FAILED` | recoverable | OTA image resources downloading was interrupted by an error other than stall. |
| E522 | `E_OTACLIENT_PACKAGE_DOWNLOAD_FAILED` | recoverable | Failed to download otaclient package for client update. |
| E523 | `E_UPPER_OTAPROXY_UNREACHABLE` | recoverable | The upper otaproxy didn't become reachable before the OTA started. |
| E530 | `E_OTACLIENT_STARTUP_FAILED` | unrecoverable | Failed to start otaclient instance. |
| E531 | `E_OTAPROXY_FAILED_TO_START` | unrecoverable | otaproxy failed to start. |
| E532 | `E_CA_CERT_NOT_INSTALLED` | unrecoverable | No CA cert is installed on this ECU, OTA image cannot be verified. |
| E533 | `E_CLIENT_UPDATE_FAILED` | recoverable | Client update failed. |
| E540 | `E_PREUPDATE_FAILED` | unrecoverable | otaclient side pre-update failed(standby slot setup, OTA image metadata saving), boot control is not included. |
| E541 | `E_UPDATEDELTA_GENERATION_FAILED` | unrecoverable | Failed to calculate the update delta. |
| E542 | `E_APPLY_OTAUPDATE_FAILED` | unrecoverable | Failed to apply update to the standby slot, also the fallback for other unexpected failures inside the update flow. |
| E543 | `E_POSTUPDATE_FAILED` | unrecoverable | otaclient side post-update failed(persist files, OTA image metadata preserving), boot control is not included. |
| E550 | `E_BOOTCONTROL_STARTUP_ERR` | unrecoverable | Boot controller failed to start up. |
| E551 | `E_BOOTCONTROL_PREUPDATE_FAILED` | unrecoverable | Boot controller pre-update failed(not covered by `E552`/`E553`). |
| E552 | `E_BOOTCONTROL_STANDBY_SLOT_PREPARE_FAILED` | unrecoverable | Boot controller failed to prepare(format) the standby slot device. |
| E553 | `E_BOOTCONTROL_SLOT_MOUNT_FAILED` | unrecoverable | Boot controller failed to mount the active or standby slot. |
| E554 | `E_BOOTCONTROL_POSTUPDATE_FAILED` | unrecoverable | Boot controller post-update failed(not covered by `E555`/`E556`/`E557`). |
| E555 | `E_BOOTCONTROL_BOOT_CONFIG_UPDATE_FAILED` | unrecoverable | Boot controller failed to update boot files or boot config(fstab, grub config, extlinux.conf, /boot on internal emmc). |
| E556 | `E_BOOTCONTROL_FIRMWARE_UPDATE_FAILED` | unrecoverable | Boot controller failed to update firmware. |
| E557 | `E_BOOTCONTROL_SWITCH_BOOT_FAILED` | unrecoverable | Boot controller failed to switch boot to the standby slot. |
| E558 | `E_BOOTCONTROL_PREROLLBACK_FAILED` | unrecoverable | Boot controller pre-rollback failed. |
| E559 | `E_BOOTCONTROL_POSTROLLBACK_FAILED` | unrecoverable | Boot controller post-rollback failed. |

## Legacy error codes

The mapping from the legacy error codes(used by previous otaclient versions) to the current error codes.

| Legacy code | Current code | Note |
| ---- | ---- | ---- |
| E100 | E502 / E521 / E504 | Resources downloading failures are now reported as E521(interrupted), E504(stalled), or E401/E404/E507 by their root causes. |
| E101 | E520 | Stalled downloading is now reported as E504. |
| E102 | E522 | |
| E200 | E503 | |
| E201 | E409 | |
| E202 | E412 | |
| E203 | E422 / E404 | HTTP 404 is now reported as E404. |
| E204 | E401 | |
| E205 | E470 | |
| E206 | E533 | |
| E207 | E499 | |
| E208 | E462 | |
| E300 | E500 | |
| E301 | E501 | |
| E302 | E550 | |
| E303 | E551 / E552 / E553 | |
| E304 | E554 / E555 / E556 / E557 | |
| E305 | E558 | |
| E306 | E559 | |
| E307 | E507 | |
| E308 | E400 | |
| E309 | E460 / E532 | No CA installed on the ECU is now reported as E532. |
| E310 | E461 | |
| E311 | E531 | |
| E312 | E541 | |
| E313 | E542 / E540 / E543 / E507 | Failures are now split by update phase(pre-update E540, post-update E543), and no space left during applying update is reported as E507. |
| E314 | E530 | |
| E315 | - | Reserved, never used. |
