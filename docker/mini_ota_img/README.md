# Mini OTA image for deterministic OTAClient test

Repo: [ota_img_for_test](https://github.com/tier4/ota-client/pkgs/container/ota-client%2Fota_img_for_test)

Old OTA image: `ghcr.io/tier4/ota-client/ota_img_for_test:ubuntu_22.04`
New OTA image: `ghcr.io/tier4/ota-client/ota_img_for_test:ubuntu_22.04-ota_image_v1`

## Build OTA image

```
# at the root of this repo
sudo docker build \
    -f docker/mini_ota_img/new_ota_image.Dockerfile -t ghcr.io/tier4/ota-client/ota_img_for_test:ubuntu_22.04-ota_image_v1 .
```
