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

The behavior of ota-proxy can be adjusted by modifying the `config.py`. The common use configurations are as follow:

1. **BASE_DIR**: the cache folder to store cache entries.
2. **DISK_USE_LIMIT_SOFT_P**: when reached, ota-proxy will enable LRU cache rotate to reserver space for new cached files.
3. **DISK_USE_LIMIT_HARD_P**: when reached, ota-proxy will disable cache mechanism.
4. **LOG_LEVEL**: default to `INFO`.

### Runtime configuration

Runtime configuration can be configured by passing parameters to CLI.

```python
usage: ota_proxy [-h] [--host HOST] [--port PORT] [--upper-proxy UPPER_PROXY]
                 [--enable-cache] [--enable-https] [--init-cache]
                 [--cache-dir CACHE_DIR] [--cache-db-file CACHE_DB_FILE]

ota_proxy server with local cache feature

optional arguments:
  -h, --help            show this help message and exit
  --host HOST           server listen ip (default: 0.0.0.0)
  --port PORT           server listen port (default: 8080)
  --upper-proxy UPPER_PROXY
                        upper proxy that used for requesting remote (default: )
  --enable-cache        enable local ota cache (default: False)
  --enable-https        enable HTTPS when retrieving data from remote (default: False)
  --init-cache          cleanup remaining cache if any (default: False)
  --cache-dir CACHE_DIR
                        where to store the cache entries (default: /ota-cache)
  --cache-db-file CACHE_DB_FILE
                        the location of cache db sqlite file (default: /ota-
                        cache/cache_db)
```

## Usage

### standalone

User can launch standalone ota_proxy server as follow:

```bash
# launch ota_proxy on 127.0.0.1:23456 with cache enabled.
python3 -m otaclient.ota_proxy \
    --host "127.0.0.1" --port 23456 \
    --enable-cache
```

Check `python3 -m otaclient.ota_proxy --help` for all available arguments.

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
