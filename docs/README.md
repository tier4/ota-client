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
- Multiple ECU support
- By the internal proxy cache mechanism, the cache can be used for the download requests to the same file from multiple ECU.

## License

OTA client is licensed under the Apache License, Version 2.0.

## OTA client setup

### Requirements

- supported boot loader
  - GRUB
  - [CBoot](https://docs.nvidia.com/jetson/archives/l4t-archived/l4t-3271/index.html#page/Tegra%20Linux%20Driver%20Package%20Development%20Guide/bootflow_jetson_xavier.html#wwpID0E0JB0HA)

- runtime
  - python3.8 (or higher)

### Partitioning

TODO

### Configurations

OTA client can update a single ECU or multiple ECUs and is installed for each ECU.
There are two types of ECU, Main ECU - receives user request, Secondary ECUs - receive request from Main ECU. One or multiple Secondary ECUs can also have Secondary ECUs.

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

  This field specifies list of **directly connected** secondary ECUs information.  
  If this field is not specified, it is treated as if secondary ecu doesn't exist.

  - ecu_id (string, required)
    ecu id of secondary ECU

  - ip_addr (string, required)
    IP address of secondary ECU.

- available_ecu_ids (optional)

  This field specifies a list of all ECU ids, including directly connected, indirectly connected and itself.
  Only the main ECU should have this information.  
  If this field is not specified, value of `ecu_id` is used as this value.  

  NOTE: The difference between secondaries and available_ecu_ids:

  `secondaries` lists the directly connected children ECUs, available_ecu_ids consists of all children ECU ids(including directly connected, indirectly connected and itself).

##### The default setting

If ecu_info.yml doesn't exist, the default setting is used as follows:

- format_version
  - 1

- ecu_id
  - "autoware"

#### proxy\_info.yml

proxy_info.yml is the setting file for OTA proxy configuration.

OTA proxy is the software integrated into the ota client that access the ota server on behalf of the ota client.
Whether ota proxy access the ota server directly or indirectly depends on the configuration.

See [OTA proxy](../ota_proxy/README.md) more details.

##### File path

/boot/ota/proxy_info.yml

##### Entries

- enable_local_ota_proxy (boolean, optional)

  This field specifies whether ota client uses local ota proxy or not.
  If this field is not specified, ota client doesn't use local ota proxy, it means ota client connects to ota server directly. If the local ota proxy is enabled, the ota client requests the local ota proxy.

- upper_ota_proxy (string, optional)

  This field specifies the upper ota proxy address to be accessed by the ota client or local ota proxy.

  | `enable_local_ota_proxy` | `upper_ota_proxy` | who accesses    | where?              |
  | :---:                    | :---:             | :---:           | :---:               |
  | true                     | set               | local ota proxy | `upper_ota_proxy`   |
  | true                     | not set           | local ota proxy | ota server directly |
  | false                    | set               | ota client      | `upper_ota_proxy`   |
  | false                    | not set           | ota client      | ota server directly |

  To specify the upper ota proxy address, `http://192.168.20.11:8082` notation is used.

The configuration for local ota proxy are as follows.

- gateway (boolean, optional if `enable_local_ota_proxy` is true otherwise not required)

  When the `enable_local_ota_proxy` field is true, this field specifies whether the **local ota proxy** requests the ota server directly with HTTPS or HTTP. If it is true, HTTPS is used otherwise HTTP is used.  
  If this field is not specified, HTTP is used.  
  Note that if the ECU can't access to the ota server directly, the value should be set to false.

- enable_local_ota_proxy_cache (boolean, optional if `enable_local_ota_proxy` is true otherwise not required)

  When the `enable_local_ota_proxy` field is true, this field specifies whether the local ota proxy uses the local cache or not. If it is true, the local cache is used otherwise not used.  
  If this field is not specified, the local cache is used.

- local_ota_proxy_listen_addr (string, optional if `enable_local_ota_proxy` is true otherwise not required)

  When the `enable_local_ota_proxy` field is true, this field specifies the listen address for local ota proxy.  
  If this field is not specified, "0.0.0.0" is used.

- local_ota_proxy_listen_port (integer, optional if `enable_local_ota_proxy` is true otherwise not required)

  When the `enable_local_ota_proxy` field is true, this field specifies the listen port for local ota proxy.  
  If this field is not specified, 8082 is used.

##### The default setting

If proxy_info.yml doesn't exist, the default setting is used as follows:

- enable_local_ota_proxy
  - true

- gateway
  - true

### apt and requirements.txt

TODO

## OTA server setup

TODO

### OTA image generation

TODO

## Services

About ota client services, see [Services](SERVICES.md).
