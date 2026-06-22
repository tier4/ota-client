## System image for testing DinD

Image: `ghcr.io/tier4/ota-client/sys_img_for_test:ubuntu22.04_systemd_dind`

### Build and push multi-arch image

The [`Dockerfile`](./Dockerfile) already consumes the `TARGETARCH` build arg, which `buildx`
sets automatically per platform, so a single `buildx build` produces a correct multi-arch manifest.
A multi-arch manifest cannot be loaded into the local Docker daemon, so it must be pushed.

```shell
# onetime: create a builder for multiarch build
docker buildx create --name multiarch-builder --driver docker-container --use
```

```shell
IMAGE=ghcr.io/tier4/ota-client/sys_img_for_test:ubuntu22.04_systemd_dind
# actually build and push the image
docker buildx build --builder multiarch-builder \
    --platform linux/amd64,linux/arm64 \
    -t ${IMAGE} \
    --output type=image,name=${IMAGE},compression=zstd,compression-level=19,oci-mediatypes=true,force-compression=true,push=true \
    .
```