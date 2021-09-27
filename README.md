# ota-client

# run e2e test
$ docker-compose up --abort-on-container-exit

## to run tests individually
(on a terminal)

```
$ docker-compose up server
```

(on another terminal)

```
$ docker run -it -v $(pwd):/ota-client --net ota-client_default --rm ota-client bash
# python3 -m pytest tests2 --cov=app2 --log-cli-level=INFO
```
