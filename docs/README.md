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

## OTA client setup

### Requirements

- supported boot loader
  - GRUB
  - [CBoot](https://docs.nvidia.com/jetson/archives/l4t-archived/l4t-3271/index.html#page/Tegra%20Linux%20Driver%20Package%20Development%20Guide/bootflow_jetson_xavier.html#wwpID0E0JB0HA)

- runtime
  - python3.8 (or higher)
  - pip
  - setuptools

### Dependency installation

```bash
sudo python3.8 -m pip install -r otaclient/requirements.txt
```

Note that OTA client is run with super user privileges so `sudo` is required for the above command.

### Partitioning

For the GRUB system, the disk is partitioned as follows:

```bash
$ sudo fdisk -l /dev/sda
Disk /dev/sda: 128 GiB, 137438953472 bytes, 268435456 sectors
Disk model: VBOX HARDDISK   
Units: sectors of 1 * 512 = 512 bytes
Sector size (logical/physical): 512 bytes / 512 bytes
I/O size (minimum/optimal): 512 bytes / 512 bytes
Disklabel type: dos
Disk identifier: 0x6cf681a1

Device     Boot     Start       End   Sectors  Size Id Type
/dev/sda1  *         2048   2000895   1998848  976M 83 Linux
/dev/sda2         2000896 135217151 133216256 63.5G 83 Linux
/dev/sda3       135217152 268433407 133216256 63.5G 83 Linux

$ lsblk /dev/sda
NAME   MAJ:MIN RM  SIZE RO TYPE MOUNTPOINT
sda      8:0    0  128G  0 disk 
├─sda1   8:1    0  976M  0 part /boot
├─sda2   8:2    0 63.5G  0 part /
└─sda3   8:3    0 63.5G  0 part 
```

In this example, A(=active) partition is sda2 and B(=standby) partition is sda3.
And `/boot` partition is shared by A/B partitions.
Note that the disk and the sector size depend on the system, but the size of A and B should be the same, basically.

## OTA client Configurations

OTA client can update a single ECU or multiple ECUs and is installed for each ECU.
There are two types of ECU, Main ECU - receives user request, Secondary ECUs - receive request from Main ECU. One or multiple Secondary ECUs can also have Secondary ECUs.

The figure below shows an example ECU structure, that shows Main ECU and 6 Secondary ECUs(A~F).
Secondary ECU A, B and C are connected to Main ECU, and D, E, F are connected to Secondary ECU A.

```text
  +----------------+    
  |   OTA server   |
  +----------------+    
           |
           |(internet)
           |                
+----------+-------------------------------------------------------+
|          |(internal ECU-to-ECU network)                          |
| +----------------+     +----------------+     +----------------+ |
| |    Main ECU    |--+--|Secondary ECU(A)|--+--|Secondary ECU(D)| |
| +----------------+  |  +----------------+  |  +----------------+ |
|                     |  +----------------+  |  +----------------+ |
|                     +--|Secondary ECU(B)|  +--|Secondary ECU(E)| |
|                     |  +----------------+  |  +----------------+ |
|                     |  +----------------+  |  +----------------+ |
|                     +--|Secondary ECU(C)|  +--|Secondary ECU(F)| |
|                        +----------------+     +----------------+ |
+------------------------------------------------------------------+
```

### ecu\_info.yaml

ecu_info.yaml is the setting file for ECU configuration.

#### File path

/boot/ota/ecu_info.yaml

#### Entries

- format_version (string, required)

  This field specifies the ecu_info.yaml format.  
  Currently this field is not used but `1` should be specified for future use.

- ecu_id (string, required)

  This field specifies ECU id and that should be unique in all EUCs.

- ip_addr (string, optional)

  This field specifies this OTA client's IP address.  
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

  This field specifies a list of all ECU ids to be updated.  
  Only the main ECU should have this information.  

  NOTE: For backward-compatibility, if this `available_ecu_ids` filed does not exist, the ecu_id of the main ecu is considered the entry for available_ecu_ids.

  NOTE: for web.auto user, web.auto agent will use `available_ecu_ids` to generate update request, only ECUs listed in `available_ecu_ids` will be included in the update request list.

  NOTE: The difference between secondaries and available_ecu_ids:

  `secondaries` lists the directly connected children ECUs, available_ecu_ids consists of all children ECU ids(including directly connected, indirectly connected and itself) to be updated.

#### The default setting

If ecu_info.yaml doesn't exist, the default setting is used as follows:

- format_version
  - 1

- ecu_id
  - "autoware"

### proxy\_info.yaml

proxy_info.yaml is the setting file for OTA proxy configuration.

OTA proxy is the software integrated into the OTA client that access the OTA server on behalf of the OTA client.
Whether OTA proxy access the OTA server directly or indirectly depends on the configuration.

See [OTA proxy](../ota_proxy/README.md) more details.

#### File path

/boot/ota/proxy_info.yaml

#### Entries

- enable_local_ota_proxy (boolean, default=**true**)

  This field specifies whether OTA client uses local OTA proxy or not.
  If this field is set to **true**, OTA client will not download remote OTA files using local OTA proxy, it means OTA client connects to remote OTA server directly. If the local OTA proxy is enabled, the OTA client requests the remote OTA files via the local OTA proxy.

- upper_ota_proxy (string, default=**""**)

  This field specifies the upper OTA proxy address to be use by the OTA client or local OTA proxy server. Only **HTTP** proxy is supported, like `http://192.168.20.11:8082`. If not set or set to `""`, not upper_ota_proxy will be used.

- gateway (boolean, default=**false**)

  When the `enable_local_ota_proxy` field is true, this field specifies whether the **local OTA proxy** requests the remote OTA server directly with HTTPS or HTTP. If it is true or this config entry is not presented, HTTPS is used otherwise HTTP is used.  
  Note that if the ECU can't access to the remote OTA server directly, the value **MUST** be set to false, `enable_local_ota_proxy` **MUST** be set to true to enable local OTA proxy server, and a valid `upper_ota_proxy` **MUST** be set for local OTA proxy server to connect to parent ECU's OTA proxy server.

- enable_local_ota_proxy_cache (boolean, default=**true**)

  When the `enable_local_ota_proxy` field is true, this field specifies whether the local OTA proxy caches the requests and uses the local caches to satisify the requests or not. If it is true or this config entry is not set, the local cache is used otherwise not used.

- local_ota_proxy_listen_addr (string, default=**"0.0.0.0"**)

  When the `enable_local_ota_proxy` field is true, this field specifies the listen address for local OTA proxy. If not specified, local OTA proxy will listen on **"0.0.0.0"**.

- local_ota_proxy_listen_port (integer, default=**8082**)

  When the `enable_local_ota_proxy` field is true, this field specifies the listen port for local OTA proxy. If not specified, port **8082** will be used.

#### NOTE about OTA client behavior under different combination of `enable_local_ota_proxy` and `upper_ota_proxy` setting

The behavior of OTA client under different `enable_local_ota_proxy` and `upper_ota_proxy` setting is as follow:

| `enable_local_ota_proxy` | `upper_ota_proxy`   | `behavior`                          |
| :---:                     | :---:                | ---                                 |
| (unset, default=false)   | (unset, default="") | OTA client directly connects to remote without any proxy |
| true                     | set                 | OTA client connects to remote via OTA proxy, and local OTA proxy itself also uses `upper_ota_proxy` to connect to remote |
| true                     | (unset, default="") | OTA client connects to remote via OTA proxy, and local OTA proxy connects to remote directly |
| false                    | set                 | OTA client connects to remote via `upper_ota_proxy`, local OTA proxy is not enabled and not used |
| false                    | not set             | OTA client directly connects to remote without any proxy |

#### Example `proxy_info.yaml` for main ECU

The main ECU defined here is responsible to provide OTA proxy for all child and sub child ECUs to connect to the remote OTA server.

```yaml
# local OTA proxy for main ECU also provides proxy service for all child/sub child ECUs
enable_local_ota_proxy: true
# for main ECU that directly connects to the Internet, HTTPS should be used, so set gateway=true
gateway: true
```

#### Example `proxy_info.yaml` for sub ECU

The sub ECU defines here is which has at least one parent ECU and cannot connect the Internet directly. The sub ECU requires at least one upper OTA client that provides OTA proxy, and download OTA files via this upper proxy.

If this sub ECU also serves sub ECUs, the OTA proxy for this sub ECU MUST be enabled to provide OTA proxy for its sub ECUs(and subsub ECUs if any).

```yaml
# local OTA proxy Must be enabled if the sub ECU also serves child ECUs, 
# should be enabled if local OTA files cache is needed.
enable_local_ota_proxy: true
# a valid upper_ota_proxy MUST be set for local OTA proxy to connect to remote as sub ECU cannot
# directly connect the Internet
upper_ota_proxy: "http://192.168.20.11:8082"
# optional: if the sub ECU's disk is not so fast(like USB device), local OTA proxy cache can be disabled
# enable_local_ota_proxy_cache: false
```

### Note about the behavior when `proxy_info.yaml` is missing

If `proxy_info.yaml` file is missing/not found, OTA client is expected to running on main ECU(or equivalent that directly connects to the Internet), a pre-defined `proxy_info.yaml` will be used as follow:

```yaml
enable_local_ota_proxy: true
gateway: true
```

## OTA image generation

It is not the OTA client's responsibility to prepare an OTA image, but this section describes how to create an OTA image with docker.

### Preparation

Create an empty working directory and clone the following two repositories.

```bash
cd $(mktemp -d)
git clone https://github.com/tier4/ota-client
git clone https://github.com/tier4/ota-metadata
```

### OTA image sign and verification key generation

OTA image is signed by the OTA image server side and verified by OTA client to make sure the image is legitimate.
This section describes how to generate sign and verification key with sample generation script.

```bash
bash ota-client/tests/keys/gen_certs.sh
```

Note that the above script is a sample, so some setting might need to be changed for each product.

The keys to be created are as follows:

<!-- markdownlint-disable no-inline-html -->
| file name | install location   |description |
| ---:      | ---:               | --- |
| root.pem  | OTA client local   | Root certificate.<br> This file should be installed to the ota-client/certs directory. |
| interm.pem| OTA client local   | Intermediate certificate.<br> This file should be installed to the ota-client/certs directory. |
| sign.pem  | OTA image server   | Certificate file to verify OTA image.<br> This file is downloaded from OTA server and verified with root and intermediate certificate. |
| sign.key  | OTA image generator | Key to sign OTA image.<br> This is only required by the OTA server when signing an OTA image. |
<!-- markdownlint-enable no-inline-html -->

### Dockerfile

The Dockerfile need to be prepared as follows:

```Dockerfile
FROM ubuntu:20.04
SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
ARG KERNEL_VERSION="5.8.0-53-generic"

RUN \
    apt-get update && apt-get install -y --no-install-recommends \
    sudo ubuntu-minimal openssh-server \
    ubuntu-desktop-minimal \
    fonts-ubuntu \
    systemd-coredump vim git \
    grub-efi-amd64 \
    linux-image-${KERNEL_VERSION} linux-headers-${KERNEL_VERSION} linux-modules-extra-${KERNEL_VERSION} \
    apt-utils python3-pip usbutils \
    gcc libc6-dev \
    dirmngr rsyslog gpg-agent initramfs-tools

RUN git clone https://github.com/tier4/ota-client
WORKDIR /ota-client
RUN python3 -m pip install -r app/requirements.txt
# install certificates to verify sign.pem
RUN mkdir certs
COPY root.pem certs/0.root.pem
COPY interm.pem certs/0.interm.pem

# add ota-client user
RUN useradd -m ota-client -s /bin/bash && \
    echo ota-client:ota-client | chpasswd && \
    gpasswd -a ota-client sudo
```

### Metadata generation

Build the docker image with Dockerfile created above and export rootfs image from the docker instance.

Metadata generation now support optional zstd compression, files that can be compressed(file size or compression ratio reach threshold) will be compressed and saved to another folder. Note that the original files will still be kept under main rootfs folder.

```bash
docker build -t ota-image .
docker create -it --rm --name ota-image ota-image
docker export ota-image > ota-image.tar
mkdir rootfs
sudo tar xf ota-image.tar -C rootfs
```

Note: `sudo` is required to extract `ota-image.tar` since some privileged files need to be created.

Generate metadata(with or without zstd compression) under extraced rootfs folder as follow:

```bash
# optional zstd compression disabled
sudo python3 ota-metadata/metadata/ota_metadata/metadata_gen.py \
    --target-dir <rootfs_folder> \
    --ignore-file ota-metadata/metadata/ignore.txt
# or
# optional zstd compression enabled
sudo python3 ota-metadata/metadata/ota_metadata/metadata_gen.py \
    --target-dir <rootfs_folder> \
    --compressed-dir <rootfs_compressed> \
    --ignore-file ota-metadata/metadata/ignore.txt
```

Sign the generated metadata as follow:

```bash
# if compression is not enabled for OTA image
sudo python3 ota-metadata/metadata/ota_metadata/metadata_sign.py \
    --sign-key sign.key \
    --cert-file sign.pem \
    --persistent-file persistents.txt \
    --rootfs-directory <rootfs_folder>
# or
# zstd compression enabled for OTA image
sudo python3 ota-metadata/metadata/ota_metadata/metadata_sign.py \
    --sign-key sign.key \
    --cert-file sign.pem \
    --persistent-file ota-metadata/metadata/persistents.txt \
    --rootfs-directory <rootfs_folder> \
    --compressed-rootfs-directory  <rootfs_compressed> 

sudo chown -R $(whoami) rootfs
```

Created metadata are as follows:

- `metadata.jwt`
- `dirs.txt`
- `symlinks.txt`
- `regulars.txt`
- `total_regular_size.txt`
- `persistents.txt`

The OTA image consists of metadata above, `sign.pen` and `rootfs` directory and can be served by the OTA server.

## Services

About OTA client services, see [Services](SERVICES.md).
