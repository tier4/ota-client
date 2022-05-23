# OTA client

## Overview

This OTA client is a client software to perform over-the-air updates for linux devices.
To enable updating of software at any layer (kernel, kernel module, user library, user application), the OTA client targets the entire rootfs for updating.
The OTA client compares the files in its own storage with the files to be updated and downloads only those files that have delta, so the download size can be reduced even if the entire rootfs is to be updated.
The OTA client downloads a list from the OTA server that contains the file paths and the hash values of the files, etc., to be updated, and compares them with the files in its own storage and if there is a match, that file is used to update the rootfs.
Therefore, this delta download mechanism does not require any specific server implementation, nor does it require the server to keep a delta for each version of the rootfs.

## Feature

- Rootfs updating
- Delta updating
- Redundant configuration with A/B partition update
- No specific server implementation is required. The server that supports HTTP GET is only required.
  - TLS connection is also required.
- Delta management is not required for server side.
- All files are verified and used.
- Transfer data is encrypted by TLS
- Multiple ECU support
- By the internal proxy cache mechanism, the cache can be used for the download requests to the same file from multiple ECU.

## License

OTA client is licensed under the Apache License, Version 2.0.

## OTA client setup

### Requirements

- boot loader
  - GRUB
  - [CBoot](https://docs.nvidia.com/jetson/archives/l4t-archived/l4t-3271/index.html#page/Tegra%20Linux%20Driver%20Package%20Development%20Guide/bootflow_jetson_xavier.html#wwpID0E0JB0HA)

- runtime
  - python3.8

### Partitioning

TODO

### Configurations

OTA client can update a single ECU or multiple ECUs and is installed for each ECU.
There two types of ECU, Main ECU - receives user request, Secondary ECUs - receives request from Main ECU. One or multiple Secondary ECUs can also have Secondary ECUs.
Note that if only one ECU is updated, there is not Secondary ECUs.

#### ecu\_info.yml

ecu_info.yml is the setting file for ECU configuration.

##### File path

/boot/ota/ecu_info.yml

##### Entries

- format_version (string, required)

  This field specifies the ecu_info.yml format.  
  Currently this field is not used but `1` should be specified for future use.

- ecu_id (string, required)

  This field specifies ECU id and that should be unique in all EUCs.

- ip_addr (string, optional)

  This field specifies this ota client's IP address.  
  If this field is not specified, "localhost" is used.  
  NOTE: this IP address is used for the local server bind address.

- secondaries (array, optional)

  This field specifies list of secondary ECUs information.  
  If this field is not specified, it is treated as if secondary ecu doesn't exist.

  - ecu_id (string, required)
    ecu id of secondary ECU

  - ip_addr (string, required)
    IP address of secondary ECU.

- available_ecu_ids (optional)
  This field specifies a list of ECU id which indicates the target ECU of the ota update.  
  Only the main ECU should have this information.  
  If this field doesn't exist, ecu_id value is used as this value.  

  NOTE: The difference between secondaries and available_ecu_ids:  
  `secondaries` field is the information for listing children's ECUs, not for
  listing grandchildren. `available_ecu_ids` filed is the information for listing
  all ECUs. If the child ecu has its own children, and if that child doesn't
  respond to the parent's request, the grandchildren's information can't be
  retrieved by the request. That is the reason why available_ecu_ids field is
  needed.

##### The default setting

If ecu_info.yml doesn't exist, the default setting is used as follows:

- format_version
  - 1

- ecu_id
  - "autoware"

#### proxy\_info.yml

proxy_info.yml is the setting file for OTA internal proxy configuration.

##### File path

/boot/ota/proxy_info.yml

##### Entries

- enable_ota_proxy (string, optional)

  This field specifies whether ota proxy is used or not.  
  If this field is not specified, ota proxy is not used, it means ota client connects to ota server directly.

- gateway (string, optional if enable_ota_proxy is true otherwise not required)

  When the `enable_ota_proxy` field is true, this field specifies whether the ota proxy requests the ota server with HTTPS or HTTP. If true, HTTPS is used otherwise HTTP is used.  
  If this field is not specified, HTTP is used.

- upper_ota_proxy (string, optional if enable_ota_proxy is true otherwise not required)

  This field specifies the ota proxy address.
  To specify the ota proxy address, `http://192.168.20.11:8082` notation is used.
  If this field is not specified, "localhost" is used for accessing ota proxy.

##### The default setting

If proxy_info.yml doesn’t exist, the default setting is used as follows:

- enable_ota_proxy
  - true

- gateway
  - true

### apt and requirements.txt

TODO

## OTA server setup

TODO

### OTA image generation

TODO

## requests

### status

### update
