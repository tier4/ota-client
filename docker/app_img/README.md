## otaclient application squashfs image creation howto

### Build otaclient APP container image

```bash
# at the project root
sudo docker build \
    -f docker/app_image/Dockerfile \
    --no-cache \
    --build-arg=UBUNTU_BASE=ubuntu:22.04 \
    --build-arg=OTACLIENT_VERSION=${OTACLIENT_VERSION} \
    -t otaclient_app:${OTACLIENT_VERSION} .
```

### Build otaclient APP squashfs image from APP container image

```bash
sudo docker create --name otaclient_app_export otaclient:${OTACLIENT_VERSION}
sudo docker export otaclient_app_export | mksquashfs - otaclient_${OTACLIENT_VERSION}.squashfs \
    -tar -b 1M \
    -mkfs-time 1729810800 \
    -all-time 1729810800 \
    -no-xattrs \
    -all-root \
    -progress \
    -comp zstd \
    -Xcompression-level 22

# if squashfs zstd kernel support is not available, use `-comp gzip` instead.
```

## Step to create otaclient app image update patch

```bash
zstd --patch-from=otaclient_${BASE_VERSION}.squashfs otaclient_${TARGET_VERSION}.squashfs -o ${BASE_VERSIOn}-${TARGET_VERSION}_patch
```

## Step to apply app image update patch

```bash
zstd -d --patch-from=otaclient_${BASE_VERSION}.squashfs ${BASE_VERSIOn}-${TARGET_VERSION}_patch -o otaclient_${TARGET_VERSION}.squashfs
```
