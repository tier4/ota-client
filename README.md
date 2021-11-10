# ota-client

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
