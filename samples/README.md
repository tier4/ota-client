# OTAClient configuration files samples

This folder contains the sample otaclient configuration files **ecu_info.yaml**, **proxy_info.yaml** and systemd service unit file **otaclient.service**(for running otaclient with python venv) or **otaclient.service_app**(for running otaclient as systemd managed app image) for a single ECU OTA setup.

**otaclient-parameter-set.service_app** is the boot-time unit for parameter sets, run from the same app image: before `local-fs.target`, it mounts the applied parameter set over its slots (`otaclient mount-parameter-set`), see [docs/parameter_set.md](../docs/parameter_set.md).
Install it next to **otaclient.service_app** as `otaclient-parameter-set.service` and enable it (`systemctl enable otaclient-parameter-set.service`): otaclient refuses a parameter set on an ECU where it is not enabled.

- It needs an otaclient app image that has the `mount-parameter-set` subcommand (released after v4.0.0rc6); with an older one, the unit would start otaclient itself before `local-fs.target`.
- A platform that prepares `/opt/data` at boot (who owns it included) is ordered before it, by a drop-in with `After=`.
- `/opt/data` and `/opt/data/ota` must stay root's and not writable by others (the eSync agent's first-boot setup keeps `/opt/data/ota` root's, 0755): otherwise no parameter set is stored or laid there.
