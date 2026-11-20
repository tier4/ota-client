# OTAClient configuration files samples

This folder contains the sample otaclient configuration files **ecu_info.yaml**, **proxy_info.yaml** and systemd service unit file **otaclient.service**(for running otaclient with python venv) or **otaclient.service_app**(for running otaclient as systemd managed app image) for a single ECU OTA setup.

**otaclient-parameter-set.service_app** is the boot-time unit for parameter sets, run from the same app image: before `local-fs.target`, it mounts the applied parameter set over its slots (`otaclient mount-parameter-set`), see [docs/parameter_set.md](../docs/parameter_set.md).
Install it next to **otaclient.service_app** as `otaclient-parameter-set.service` and enable it (`systemctl enable otaclient-parameter-set.service`).
