# ota-client

## run tests

```
$ docker-compose up --abort-on-container-exit
```

## run tests individually

```
$ docker-compose -f docker-compose.yml -f docker-compose.dev.yml run --rm client
# python3 -m pytest tests2 --cov=app2
```
