# ota-client

## setup

```bash
sudo apt install -y python3.8 python3-setuptools
sudo python3.8 -m pip install -U pip
sudo python3.8 -m pip install -r app/requirements.txt
```

## run tests

```bash
$ docker-compose up --abort-on-container-exit
```

## run tests individually

```bash
$ docker-compose -f docker-compose.yml -f docker-compose.dev.yml run --rm client
# python3 -m pytest tests --cov=app
```

## to update app/otaclient_v2_pb2\*py

```bash
$ python3 -m grpc_tools.protoc -I./proto --python_out=app --grpc_python_out=app ./proto/otaclient_v2.proto
```
