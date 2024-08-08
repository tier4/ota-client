# OTA Service API proto

This folder includes the OTA service API proto file, and a set of tools to generate the python lib from the proto files.

## How to build

1. setup a venv and install hatch: 

```shell
python3 -m venv .venv
# enable the venv
. .venv/bin/activate
python3 -m pip -U pip
python3 -m pip install hatch
```

2. build the wheel package with hatch

```shell
# enable the venv
. .venv/bin/activate
hatch build -t wheel
```

3. the built package will be placed under `./dist` folder