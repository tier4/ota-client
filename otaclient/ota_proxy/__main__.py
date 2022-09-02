import argparse
import asyncio

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="ota_proxy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="ota_proxy server with local cache feature",
    )
    parser.add_argument("--host", help="server listen ip", default="0.0.0.0")
    parser.add_argument("--port", help="server listen port", default=8080)
    parser.add_argument(
        "--upper-proxy",
        help="upper proxy that used for requesting remote",
        default="",
    )
    parser.add_argument(
        "--enable-cache",
        help="enable local ota cache",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--enable-https",
        help="enable HTTPS when retrieving data from remote",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--init-cache",
        help="cleanup remaining cache if any",
        action="store_true",
        default=True,
    )
    args = parser.parse_args()

    async def _launch_server():
        import uvicorn
        from . import App, OTACache

        _ota_cache = OTACache(
            cache_enabled=args.enable_cache,
            upper_proxy=args.upper_proxy,
            enable_https=args.enable_https,
            init_cache=args.init_cache,
        )
        _config = uvicorn.Config(
            App(_ota_cache),
            host=args.host,
            port=args.port,
            log_level="info",
            lifespan="on",
            loop="asyncio",
            http="h11",
        )
        _server = uvicorn.Server(_config)
        await _server.serve()

    asyncio.run(_launch_server())
