# local OTA

Tools for triggering OTA locally.

Currently supports API version 2.

## Usage

1. define the `update_request.yaml` as follow:

```yaml
# adjust the settings as your needs
- ecu_id: "autoware"
  version: "789.x"
  url: "https://10.0.1.1:8443/"
  cookies: '{"test": "my-cookie"}'
```

2. make the OTA update request API call as follow:

```shell
 python3 tools/local_ota/api_v2 -i 10.0.1.10 update_request.yaml
```
