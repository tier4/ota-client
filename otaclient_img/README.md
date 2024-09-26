# otaclient image build

Tools and resources for building otaclient squashfs image.

## Build squashfs

```shell
sudo mksquashfs ./otaclient_img otaclient.squashfs \
    -b 1M \
    -mkfs-time 1729810800 \
    -all-time 1729810800 \
    -no-xattrs \
    -all-root \
    -progress \
    -comp zstd \
    -Xcompression-level 22
```