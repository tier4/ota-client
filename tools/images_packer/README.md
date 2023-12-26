# OTA images packer

`tools.images_packer` is a helper package for bundling multiple images into one single images bundle.
The input OTA images are expected to be built by ota-metadata(or images compatible with OTA image specification).

This package provide the following two packing mode, each for different purposes:

1. `build-cache-src`: with this mode, the built image can be recognized and utilized by otaproxy to reduce network downloading during OTA update.

    The following features are provided in this mode:

    1. build external cache source image,

    2. export the built image as tar archive,

    3. prepare and export built image onto specific block device as external cache source device recognized by otaproxy.

2. `build-offline-ota-imgs-bundle`: with this mode, the built image can be used by the `fully offline OTA helper` script to trigger fullly offline OTA locally without any network.

    The following feature are provided in this mode:

    1. build offline OTA image,

    2. export the built image as tar archive.

    **NOTE**: full offline OTA helper util is not yet implemented.

<!--- Please check documentation at [OTA cache mechanism design for otaproxy](https://tier4.atlassian.net/l/cp/yzmnPx9T) for more details. -->

## Expected image layout for input OTA images

This package currently only recognizes the OTA images generated by [ota-metadata@a711013](https://github.com/tier4/ota-metadata/commit/a711013), or the image layout compatible with the ota-metadata and OTA image layout specification as follow.

```text
Example OTA image layout:
.
├── data
│   ├── <...files with full paths...>
│   └── ...
├── data.zst
│   ├── <... compressed OTA file ...>
│   └── <... naming: <file_sha256>.zst ...>
├── certificate.pem
├── dirs.txt
├── metadata.jwt
├── persistents.txt
├── regulars.txt
└── symlinks.txt
```

Please also refere to [ota-metadata](https://github.com/tier4/ota-metadata) repo for the implementation of OTA image layout specification.

## OTA image bundle layout

The rootfs of the built image has the following layout:

```text
.
├── manifest.json
├── data
│   ├── <OTA_file_sha256hash> # uncompressed file
│   ├── <OTA_file_sha256hash>.zst # zst compressed file
│   └── ...
└── meta
    ├── <idx> # corresponding to the order in manifest.image_meta
    │   └── <list of OTA metafiles...>
    └── ...
        └── <list of OTA metafiles...>

```

Both external cache source image and offline OTA image bundle use this image bundle layout.
It means that offline OTA image can also be used as external cache source recognized by otaproxy(but user still needs to prepare the device either by themselves and extract the offline OTA image rootfs onto the prepared device, or prepare the device with this **images_packer** package).

<!--- Please check [External cache source](https://tier4.atlassian.net/wiki/spaces/WEB/pages/2813984854/OTA+cache+mechanism+design+for+otaproxy#External-cache-source) section of the doc for more details. --->

## OTA image bundle Manifest schema

Offline OTA image's rootfs contains a `manifest.json` file with a single JSON object in it. This file includes the information related to this offline OTA image, including the bundled OTA images' metadata.

```json
{
  "schema_version": 1,
  "image_layout_version": 1,
  "build_timestamp": 1693291679,
  "data_size": 1122334455,
  "data_files_num": 12345,
  "meta_size": 112233,
  "image_meta": [
    {
      "ecu_id": "<ecu_id>",
      "image_version": "<image_version>",
      "ota_metadata_version": 1,
    },
    ...
  ],
}
```

<!--- Please check [Build external cache source](https://tier4.atlassian.net/wiki/spaces/WEB/pages/2813984854/OTA+cache+mechanism+design+for+otaproxy#Build-external-cache-source) section for more details. --->

## How to use `images_packer`

### Prepare the dependencies

This image builder requires latest ota-client to be installed/accessable. The recommended way to install dependencies is to init a virtual environment and install the ota-client into this venv, and then execute the image_builder with this venv.

1. git clone the latest ota-client repository:

    ```bash
    $ git clone https://github.com/tier4/ota-client.git
    # image builder is located at ota-client/tools/offline_ota_image_builder
    ```

2. prepare the virtual environment and activate it:

    ```bash
    $ python3 -m venv venv
    $ . venv/bin/active
    (venv) $
    ```

3. install the ota-client into the virtual environment:

    ```bash
    (venv) $ pip install ota-client
    ```

### Examples of building and exporting image

#### Usage 1: Build external cache source image and export it as tar archive

```bash
# current folder layout: venv ota-client
(venv) $ cd ota-client
(venv) $ sudo -E env PATH=$PATH python3 -m tools.images_packer build-cache-src --image-dir=./images_dir --output=t2.tar
```

This will build the external cache source image with images listed in `./images_dir`, and export the built image as `t2.tar` tar archive.

#### Usage 2: Build offline OTA images bundle and export it as tar archive

```bash
# current folder layout: venv ota-client
(venv) $ cd ota-client
(venv) $ sudo -E env PATH=$PATH python3 -m tools.images_packer build-offline-ota-imgs-bundle --image=p1:p1_image.tgz:ver_20230808 --image=p2:p2_image.tgz:ver_20230808 --output=t2.tar
```

This will build the offline OTA image with `p1_image.tgz` and `p2_image.tgz`, which are for ECU `p1` and `p2`, and export the built image as `t2.tar` tar archive.

#### Usage 3: Build the external cache source image and create external cache source device

```bash
# current folder layout: venv ota-client
(venv) $ cd ota-client
(venv) $ sudo -E env PATH=$PATH python3 -m tools.images_packer build-cache-src  --image-dir=./images_dir --write-to=/dev/<target_dev>
```

This will build the external cache source image OTA images listed in the `./images_dir`, and prepare the `/dev/<target_dev>` as external cache source device(ext4 filesystem labelled with `ota_cache_src`) with image rootfs exported to the filesystem on `/dev/<target_device>`.

## CLI Usage reference

### `build-cache-src` subcommand

```text
usage: images_packer build-cache-src [-h] [--image <IMAGE_FILE_PATH>] [--image-dir <IMAGE_FILES_DIR>] [-o <OUTPUT_FILE_PATH>] [-w <DEVICE>] [--force-write-to]

build external OTA cache source recognized and used otaproxy.

optional arguments:
  -h, --help            show this help message and exit
  --image <IMAGE_FILE_PATH>
                        (Optional) OTA image to be included in the external cache source, this argument can be specified multiple times to include multiple images.
  --image-dir <IMAGE_FILES_DIR>
                        (Optional) Specify a dir of OTA images to be included in the cache source.
  -o <OUTPUT_FILE_PATH>, --output <OUTPUT_FILE_PATH>
                        save the generated image bundle tar archive to <OUTPUT_FILE_PATH>.
  -w <DEVICE>, --write-to <DEVICE>
                        (Optional) write the external cache source image to <DEVICE> and prepare the device, and then prepare the device to be used as external cache source storage.
  --force-write-to      (Optional) prepare <DEVICE> as external cache source device without inter-active confirmation, only valid when used with -w option.
```

### `build-offline-ota-imgs-bundle` subcommand

```text
usage: images_packer build-offline-ota-imgs-bundle [-h] --image <ECU_NAME>:<IMAGE_PATH>[:<IMAGE_VERSION>] -o <OUTPUT_FILE_PATH>

build OTA image bundle for offline OTA use.

optional arguments:
  -h, --help            show this help message and exit
  --image <ECU_NAME>:<IMAGE_PATH>[:<IMAGE_VERSION>]
                        OTA image for <ECU_ID> as tar archive(compressed or uncompressed), this option can be used multiple times to include multiple images.
                        NOTE: if multiple OTA target image is specified for the same ECU, the later one will override the previous set one.
  -o <OUTPUT_FILE_PATH>, --output <OUTPUT_FILE_PATH>
                        save the generated image bundle tar archive to <OUTPUT_FILE_PATH>.
```