## otaclient application squashfs image creation howto

### Build otaclient APP container image

```bash
# at the project root
sudo docker build \
    -f docker/app_img/Dockerfile \
    --no-cache \
    --build-arg=UBUNTU_BASE=ubuntu:22.04 \
    --build-arg=OTACLIENT_VERSION=${OTACLIENT_VERSION} \
    -t otaclient_app:${OTACLIENT_VERSION} .
```

### Build otaclient APP squashfs image from APP container image

```bash
# at the project root
sudo docker create --name otaclient_app_export otaclient_app:${OTACLIENT_VERSION}
sudo docker export otaclient_app_export | mksquashfs - dist/otaclient_${OTACLIENT_VERSION}.squashfs \
    -tar -b 1M \
    -mkfs-time 1729810800 \
    -all-time 1729810800 \
    -no-xattrs \
    -all-root \
    -progress \
    -comp gzip
    # if squashfs zstd kernel support is available, use the following instead of gzip
    # -comp zstd \
    # -Xcompression-level 22
```

## Step to create otaclient app image update patch

```bash
zstd --patch-from=otaclient_${BASE_VERSION}.squashfs otaclient_${TARGET_VERSION}.squashfs -o ${BASE_VERSION}-${TARGET_VERSION}_patch
```

## Step to apply app image update patch

```bash
zstd -d --patch-from=otaclient_${BASE_VERSION}.squashfs ${BASE_VERSION}-${TARGET_VERSION}_patch -o otaclient_${TARGET_VERSION}.squashfs
```
