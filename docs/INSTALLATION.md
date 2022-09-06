# Installation

## Install otaclient with pip to custom install location

NOTE: upgrade `setuptools`, `setuptools-scm` and `pip` before installation

Here we take installing otaclient to `/opt/ota/otaclient` as example.

### 1. git clone the specific branch/tag to a temporary folder

```bash
git clone https://github.com/tier4/ota-client -b <tag|branch> /tmp
```

### 2. prepare virtual env and activate it

```bash
python3 -m venv /opt/ota/.venv
source /opt/ota/.venv/bin/activate
```

### 3. under the venv, install dependencies for otaclient with pip

```bash
# NOTE: we will not use the otaclient install here, 
# check the following instructions
pip install /tmp/ota-client
```

### 4. under the venv, install the otaclient package to /opt/ota

```bash
# NOTE: --no-deps MUST be set, otherwise all dependencies 
#              will also be install under /opt/ota
pip install -t /opt/ota --no-deps /tmp/ota-client
# cleanup
rm -rf /opt/ota/otaclient*dist-info
rm -rf /tmp/ota-client
```

## Launch otaclient from custom install location

If we install the otaclient to custom directory instead of the default location, we must indicate python the path to the install location.

### method 1: indicate path by **PYTHONPATH**

```bash
# we have to append the /opt/ota to the PYTHONPATH, to tell the 
# python interpreter to search otaclient package under /opt/ota, instead of 
# using the one install under <virtualenv>/lib/python3.8/site-packages

# with venv activated: 
PYTHONPATH=/opt/ota python3 -m otaclient
# or
PYTHONPATH=/opt/ota python3 -m otaclient.app
```

### method 2: change working dir to the install location

```bash
# change work dir to /opt/ota
cd /opt/ota

# with venv activated:
# NOTE:python will insert current working dir at index 0 in `sys.path`
# under /opt/ota folder, so that python will first use otaclient under /opt/ota
python3 -m otaclient 
# or
python3 -m otaclient.app
```

## Launch aws_iot_log_server from custom install location

The same as above, we have to indicate the otaclient installation path before launching the `aws_iot_log_server`.

```bash
source /opt/ota/.venv/bin/activate
cd /opt/ota

# with venv enabled, under the /opt/ota folder
python3 -m aws_iot_log_server \
    --host 0.0.0.0 \
    --port {{ ota_client_log_server_port }} \
    --aws_credential_provider_endpoint ${AWS_CREDENTIAL_PROVIDER_ENDPOINT} \
    --aws_role_alias ${AWS_ROLE_ALIAS} \
    --aws_cloudwatch_log_group ${AWS_CLOUDWATCH_LOG_GROUP} \
    --greengrass_config ${AWS_GREENGRASS_CONFIG}
```
