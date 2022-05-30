# ota-client

## setup

```bash
sudo apt install -y python3.8 python3-setuptools
sudo python3.8 -m pip install -U pip
sudo python3.8 -m pip install -r app/requirements.txt
```

## run tests

```bash
docker-compose up --abort-on-container-exit
```

## run tests individually

```bash
docker-compose -f docker-compose.yml -f docker-compose.dev.yml run --rm client
# python3 -m pytest tests --cov=app
```

## to update app/otaclient_v2_pb2\*py

```bash
python3 -m grpc_tools.protoc -I./proto --python_out=app --grpc_python_out=app ./proto/otaclient_v2.proto
```

## to use protobuf whl

### how to build protobuf whl

1. Edit and update version in proto/VERSION.
2. Build whl as follows:

   ```bash
   cd proto
   make
   ```

3. After build, whl file is generated in proto/whl directory.

### how to install protobuf whl

You can install protobuf whl with pip command.

```bash
pip3 install https://raw.githubusercontent.com/tier4/ota-client/main/proto/whl/otaclient_pb2-xxxxxx-py3-none-any.whl
```

If you use requirement.txt, you can add protobuf whl as follows.

```bash
(snip)
https://raw.githubusercontent.com/tier4/ota-client/main/proto/whl/otaclient_pb2-xxxx-py3-none-any.whl
(snip)
```

### how to import protobuf package

```bash
$ python3
Python 3.8.10 (default, Nov 26 2021, 20:14:08)
[GCC 9.3.0] on linux
Type "help", "copyright", "credits" or "license" for more information.
>>> from otaclient_pb2.v2 import otaclient_pb2
>>> from otaclient_pb2.v2 import otaclient_pb2_grpc
```

### How to generate docs/SERVICES.md

```bash
docker run --rm -v $(pwd)/docs:/out -v $(pwd)/proto:/protos pseudomuto/protoc-gen-doc --doc_opt=markdown,SERVICES.md
```
