# OTA Service API proto

This folder includes the OTA service API proto file, and is also a configured python package.
You can use `hatch` to simply build the OTA service API python package for building grpc server or client.

## How to build

1. setup a venv and install hatch:

```shell
python3 -m venv .venv
# enable the venv
. .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install hatch
```

2. build the wheel package with hatch

```shell
# enable the venv
. .venv/bin/activate
hatch build -t wheel
```

3. the built package will be placed under `./dist` folder
