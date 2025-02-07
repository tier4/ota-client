## otaclient application squashfs image creation howto

Please note that upto otaclient v3.8.x, otaclient still cannot run inside container environment.
Future PRs will implement the above feature.

Also the container environment needed to be carefully setup to let otaclient run as expected. See related document for more details.

### Steps to create otaclient application image

1. download the otaclient whl package

2. build docker image

```bash
sudo docker build \
    --no-cache \
    --build-arg=UBUNTU_BASE=ubuntu:22.04 \
    --build-arg=OTACLIENT_VERSION=3.8.4 \
    --build-arg=OTACLIENT_WHL=otaclient-3.8.4-py3-none-any.whl \
    -t otaclient:3.8.4 .
```

3. export docker image and create zstd compressed squashfs

```bash
# enter root shell
sudo -s

docker create --name otaclient_v3.8.4 otaclient:3.8.4
docker export otaclient_v3.8.4 | mksquashfs - otaclient_v3.8.4.squashfs \
    -tar -b 1M \
    -mkfs-time 1729810800 \
    -all-time 1729810800 \
    -no-xattrs \
    -all-root \
    -progress \
    -comp zstd \
    -Xcompression-level 22
```

## Step to create otaclient app image update patch

```bash
zstd --patch-from=otaclient_v3.7.1.squashfs otaclient_v3.8.2.squashfs -o v3.7.1-v3.8.2_patch
```

## Step to apply app image update patch

```bash
zstd -d --patch-from=otaclient_v3.7.1.squashfs v3.7.1-v3.8.2_patch -o v3.8.2_from_patch.squashfs
```
