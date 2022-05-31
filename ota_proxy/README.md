# ota-proxy

## Introduction

For environment that internal ECUs are not allowed to directly connect to the Internet for security reason,
ota-proxy is introduced to allow internal ECUs to apply ota update. Ota-proxy can also be used as a cached
proxy server to serve multiple ECUs' update simultaneously.

## Features

1. **HTTP proxy for files downloading**: ota-proxy implements standard HTTP proxy mechanism to allow normal HTTP proxy requests from the ota-client.
2. **cache mechanism for requested files**: ota-proxy can cache the requests into `cache folder`, which is useful for multi-devices ota-update, can also benefit recovering an interuppted ota update as the ota-client can start again with already cached files.
3. **on-going cache streaming**: allow streaming on the file that being requested by multi-clients at the same time, with only one request by the ota-proxy to the remote server. This feature depends on cache mechanism enabled.
4. **space awared LRU cache rotate**: ota-proxy will always check the available space of the disk where cache folder located. If reached to **soft space limit**, LRU cache rotation will activate; if reached to **hard space limit**, ota-proxy will disable cache mechanism but serve as pure proxy server.
5. **OTA-File-Cache-Control**: this header allow the ota-client to indicate how ota-proxy should handle the request. Available options: use_cache(default), no_cache, retry_caching(indicates the already cached entry is corrupted and requires re-caching).

## Configuration

### Static configuration

The behavior of ota-proxy can be adjusted by modifying the config.py. The common use configurations are as follow:

1. **BASE_DIR**: the cache folder to store cache entries.
2. **DISK_USE_LIMIT_SOFT_P**: when reached, ota-proxy will enable LRU cache rotate to reserver space for new cached files.
3. **DISK_USE_LIMIT_HARD_P**: when reached, ota-proxy will disable cache mechanism.
4. **LOG_LEVEL**: default to `INFO`.

### Runtime configuration

Runtime configuration is configured by setting up `ota-cache` instance when launching ota-proxy.

1. **upper_proxy** str: the upper proxy that ota_cache uses to send out request, default is None
2. **cache_enabled** bool: when set to False, ota_cache will only relay requested data, default is False.
3. **enable_https** bool: whether the ota_cache should send out the requests with HTTPS, default is False. NOTE: scheme change is applied unconditionally.
4. **init_cache** bool: whether to clear the existed cache, default is True.
5. **scrub_cache_event**: optional, an multiprocessing.Event that sync status with the ota-client.

## Usage

### standalone

User should write a launcher to setup the `ota-cache`, and then launch the `ota-proxy` with it.

```python
# example launcher
import uvicorn
from ota_proxy import OTACache, App

# if the launcher needs to ensure the ota-proxy is ready,
# user can create an instance of multiprocessing.Event, 
# pass it to the ota_cache instance. Then ota-proxy will
# set the Event after it finished setting up and ready for serving requests.
ota_cache = OTACache(
    cache_enabled=True, 
    init_cache=False, 
    enable_https=False
    )

uvicorn.run(
    App(ota_cache),
    host="0.0.0.0",
    port=8082,
    log_level="error",
    lifespan="on",
    workers=1,
    loop="asyncio",
    http="h11",
)
```

### Integration in ota-client

The ota-proxy is launched by ota-client as a separate process when doing ota update. The behavior of ota-proxy is configured by the `/boot/ota/proxy_info.yml` as follow.

```yaml
# whether to use HTTPS to send request to remote.
gateway: <true | false>

# whether to enable the local ota-proxy, 
# if set to false, the ota-client will directly request to remote.
enable_ota_proxy: <true | false>

# ip:port the local ota-proxy to bind.
local_server: ["0.0.0.0", 8082]

# the upper HTTP proxy that the local ota-proxy used
# to send requests to. If not set, the ota-proxy will
# send the request directly to the remoted indicated by
# the client request.
upper_ota_proxy: "http://10.0.0.2:8082"
```

For more details about how ota-proxy being used in the ota-client,
please refer to the ota-client's document.
