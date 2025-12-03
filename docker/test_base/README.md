# Base container image for OTAClient test

Built images and corresponding Ubuntu version:

1. `ubuntu:18.04`: `ghcr.io/tier4/ota-client/test_base:ubuntu_18.04`
1. `ubuntu:20.04`: `ghcr.io/tier4/ota-client/test_base:ubuntu_20.04`
1. `ubuntu:22.04`: `ghcr.io/tier4/ota-client/test_base:ubuntu_22.04`

## Build cmds

### GitHub Actions build workflow

Images are automatically built and pushed to ghcr.io when changes are made to `docker/test_base/` directory. See [`.github/workflows/test_image.yaml`](../../.github/workflows/test_image.yaml) for the workflow configuration.

To manually trigger a build, use the workflow dispatch feature on GitHub Actions.

### Manual build command example

#### Build for Ubuntu 18.04

> [!NOTE]
> A new buildx instance with `Driver: docker-container` is needed. The default buildx instance doesn't support zstd compression!
> Create a new instance with `docker buildx create --name zstd-builder --use`.

```shell
BASE_URI=ghcr.io/tier4/ota-client/test_base
UBUNTU_VER=18.04
docker login ghcr.io -u YOUR_GITHUB_USERNAME -p YOUR_GITHUB_TOKEN
docker buildx create --name zstd-builder --driver docker-container --use
docker buildx build --builder zstd-builder \
    -f ubuntu-18.04.Dockerfile \
    --build-arg=UBUNTU_BASE=ubuntu:${UBUNTU_VER} \
    --output type=image,name=${BASE_URI}:ubuntu_${UBUNTU_VER},compression=zstd,compression-level=19,oci-mediatypes=true,force-compression=true,push=true \
    .
```

#### Build for Ubuntu 20.04 and newer

```shell
BASE_URI=ghcr.io/tier4/ota-client/test_base
UBUNTU_VER=20.04
docker login ghcr.io -u YOUR_GITHUB_USERNAME -p YOUR_GITHUB_TOKEN
docker buildx create --name zstd-builder --driver docker-container --use
docker buildx build --builder zstd-builder \
    -t ghcr.io/tier4/ota-client/test_base:ubuntu_${UBUNTU_VER} \
    --build-arg=UBUNTU_BASE=ubuntu:${UBUNTU_VER} \
    --output type=image,name=${BASE_URI}:ubuntu_${UBUNTU_VER},compression=zstd,compression-level=19,oci-mediatypes=true,force-compression=true,push=true \
    .
```
