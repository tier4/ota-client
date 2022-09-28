from .log_proxy_server import launch_server

if __name__ == "__main__":
    import argparse
    from .greengrass_config import GreengrassConfig

    parser = argparse.ArgumentParser(
        prog="aws_iot_log_server", formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--host", help="host name", default="localhost")
    parser.add_argument("--port", help="port number", default="8080")
    parser.add_argument("--aws_credential_provider_endpoint", required=True)
    parser.add_argument("--aws_role_alias", required=True)
    parser.add_argument("--aws_cloudwatch_log_group", required=True)
    parser.add_argument(
        "--greengrass_v2_config",
        help="greengrass v2 config.yaml.\n"
        "If this option is specified, it is evaluated prior to the following.",
    )
    parser.add_argument(
        "--greengrass_config",
        help="greengrass config.json.\n"
        "If this option is not specified, the following arguments are required:",
    )
    parser.add_argument("--ca_cert_file")
    parser.add_argument("--private_key_file")
    parser.add_argument("--cert_file")
    parser.add_argument("--region")
    parser.add_argument("--thing_name")
    args = parser.parse_args()
    kwargs = dict(
        host=args.host,
        port=int(args.port),
        aws_credential_provider_endpoint=args.aws_credential_provider_endpoint,
        aws_role_alias=args.aws_role_alias,
        aws_cloudwatch_log_group=args.aws_cloudwatch_log_group,
    )
    if args.greengrass_config or args.greengrass_v2_config:
        ggcfg = GreengrassConfig.parse_config(args.greengrass_config, args.greengrass_v2_config)
        kwargs.update(
            dict(
                ca_cert_file=ggcfg["ca_cert"],
                private_key_file=ggcfg["private_key"],
                cert_file=ggcfg["cert"],
                region=ggcfg["region"],
                thing_name=ggcfg["thing_name"],
            )
        )
    else:
        kwargs.update(
            dict(
                ca_cert_file=args.ca_cert_file,
                private_key_file=args.private_key_file,
                cert_file=args.cert_file,
                region=args.region,
                thing_name=args.thing_name,
            )
        )

    launch_server(**kwargs)
