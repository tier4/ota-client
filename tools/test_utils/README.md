# Test utils for debugging otaclient

This test utils set provides lib to directly query otaclient `update/status/rollback` API, and a tool to simulate dummy multi-ecu setups.

## Usage guide for setting up test environemnt

This test_utils can be used to setup a test environment consists of a real otaclient(either on VM or on actually ECU) as main ECU,
and setup many dummy subECUs that can receive update request and return the expected status report.

### 1. Install the otaclient's dependencies

`test_utils` depends on otaclient, so you need to install at least the dependencies of otaclient.
Please refer to [docs/INSTALLATION.md](docs/INSTALLATION.md).

### 2. Update the `ecu_info.yaml` and `update_request.yaml` accordingly

Update the `ecu_info.yaml` under the `test_utils` folder as your test environment design,
the example `ecu_info.yaml` consists of one mainECU `autoware`(expecting to be a real otaclient),
and 2 dummy subECUs which will be prepared at step 2.

Update the `update_request.yaml` under the `test_utils` folder as your test environment setup.
This file contains the update request to be sent.

### 3. Launch `setup_ecu.py` to setup dummy subECUs, and launch the real otaclient

Setup subECUs:

```python
# with venv, under the tools/ folder
python3 -m test_utils.setup_ecu subecus
```

And then launch the real otaclient, be sure that the otaclient is reachable to the machine
that running the test_utils.

### 4. Send an update request to main ECU

For example, we have `autoware` ECU as main ECU, then

```python
# with venv, under the tools/ folder
python3 -m test_utils.api_caller update -t autoware
```

### 5. Check the update progresss

Use `api_caller` status command to query the update progress as follow:

```python
# with venv, under the tools/ folder
python3 -m test_utils.api_caller status -t autoware
```
