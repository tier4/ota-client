# ota-proxy

## Introduction

For environment that internal ECUs are not allowed to directly connect to the Internet for security reason,
ota-proxy is introduced to allow internal ECUs to apply ota update. Ota-proxy can also be used as a cached
proxy server to serve multiple ECUs' update simultaneously.

## Architecture

Ota-proxy is an ASGI application served by **uvicorn** with **httptools** as the HTTP parser and **uvloop** as the event loop, providing high-throughput HTTP proxy performance.

### Multi-level cache retrieval

When a file is requested, ota-proxy tries the following sources in order:

1. **External device cache** — USB/attached storage with filesystem label `ota_cache_src` (if configured).
2. **Local cache** — the on-disk cache directory managed by ota-proxy.
3. **NFS cache** — network-mounted external cache (if configured).
4. **New caching** — download from remote and cache locally (if disk limits allow and `no_cache` is not set).
5. **Direct download** — fallback when cache is disabled or disk hard limit is reached.

## Features

1. **HTTP proxy for files downloading**: ota-proxy implements standard HTTP proxy mechanism to allow normal HTTP proxy requests from the ota-client. Only the GET method is supported.
2. **Cache mechanism for requested files**: ota-proxy can cache the requests into a `cache folder`, which is useful for multi-devices ota-update, and can also benefit recovering an interrupted ota update as the ota-client can start again with already cached files.
3. **On-going cache streaming**: allow streaming a file that is being requested by multiple clients at the same time, with only one request by ota-proxy to the remote server. This uses a provider/subscriber model so subscribers read from the in-progress cache write. This feature depends on cache mechanism being enabled.
4. **Threshold-based space management**: ota-proxy monitors available disk space where the cache folder is located. Below the **soft limit** (default 75%), cache entries are committed normally. Between the soft and **hard limit** (default 80%), cache teeing still works for the current request but entries are not persisted. Above the hard limit, caching is disabled entirely and ota-proxy serves as a pure proxy server.
5. **External cache support**: ota-proxy can use an external device cache (auto-detected by filesystem label `ota_cache_src`) or an NFS-mounted cache as additional cache sources, with zstd compression support for external cache storage.
6. **In-memory cache index with SQLite persistence**: the cache index is kept in memory (up to 600,000 entries, ~120MB) for fast lookups during the current session, while a background writer thread flushes changes to SQLite in batches so that cache metadata can be recovered on restart.
7. **Concurrent request limiting**: a semaphore limits the maximum number of concurrent requests (default 1024) to prevent resource exhaustion.
8. **OTA-File-Cache-Control header**: this header allows the ota-client to indicate how ota-proxy should handle the request. Available directives:
   - `no_cache` — do not use or populate cache for this request.
   - `retry_caching` — the existing cached entry is corrupted, re-cache it.
   - `file_sha256=<hash>` — the SHA-256 hash of the original OTA file.
   - `file_compression_alg=<alg>` — the compression algorithm used for the OTA file.

## Configuration

### Static configuration

The behavior of ota-proxy can be adjusted by modifying the `config.py`. The common use configurations are as follow:

1. **BASE_DIR**: the cache folder to store cache entries (default: `/ota-cache`).
2. **DISK_USE_LIMIT_SOFT_P**: when reached, ota-proxy will stop persisting new cache entries (default: 75%).
3. **DISK_USE_LIMIT_HARD_P**: when reached, ota-proxy will disable cache mechanism (default: 80%).
4. **CACHE_WRITE_WORKERS_NUM** / **CACHE_READ_WORKERS_NUM**: number of thread pool workers for cache I/O (defaults scale with CPU count: 8-10 write, 10-12 read).
5. **MAX_CONCURRENT_REQUESTS**: maximum number of concurrent proxy requests (default: 1024).
6. **MAX_INDEX_ENTRIES**: upper bound on in-memory cache index entries (default: 600,000).

### Runtime configuration

Runtime configuration can be configured by passing parameters to CLI.

```bash
usage: ota_proxy [-h] [--host HOST] [--port PORT] [--upper-proxy UPPER_PROXY]
                 [--enable-cache] [--enable-https] [--init-cache]
                 [--cache-dir CACHE_DIR] [--cache-db-file CACHE_DB_FILE]
                 [--external-cache-mnt-point EXTERNAL_CACHE_MNT_POINT]
                 [--external-nfs-cache-mnt-point EXTERNAL_NFS_CACHE_MNT_POINT]

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
                        the location of cache db sqlite file (default: /ota-cache/cache_db)
  --external-cache-mnt-point EXTERNAL_CACHE_MNT_POINT
                        if specified, otaproxy will try to detect external cache dev,
                        mount the dev on this mount point, and use the cache store in it
                        (default: None)
  --external-nfs-cache-mnt-point EXTERNAL_NFS_CACHE_MNT_POINT
                        if specified, otaproxy will try to use external NFS cache at
                        this mount point (default: None)
```

## Usage

### standalone

User can launch standalone ota_proxy server as follow:

```bash
# launch ota_proxy on 127.0.0.1:23456 with cache enabled.
uv run python3 -m otaclient.ota_proxy \
    --host "127.0.0.1" --port 23456 \
    --enable-cache

# launch with external cache device support.
uv run python3 -m otaclient.ota_proxy \
    --host "127.0.0.1" --port 23456 \
    --enable-cache \
    --external-cache-mnt-point /mnt/external-cache
```

### Integration in ota-client

The ota-proxy is launched by ota-client as a separate process when doing ota update. The behavior of ota-proxy is configured by the `/boot/ota/proxy_info.yml`.

For more details about how ota-proxy being used in the ota-client,
please refer to the ota-client's document.
