# Local OTA status monitor util

A helper util for querying specific otaclient's status API and showing the OTA status in real time in the terminal window. If multiple ECUs' status are included, all of these ECUs' status(up to 10) will be displayed.

## Usage

This helper util requires otaclient installed. Prepare a virtual environment with otaclient installed, and then execute the package as follow:

```bash
# get the source code
$ git clone https://github.com/tier4/ota-client
...

# prepare virtual environment
$ python3 -m venv venv
$ . venv/bin/activate
(venv) $

# install otaclient
(venv) $ pip install -e ota-client
...

# under ota-client/ folder
(venv) $ cd ota-client
(venv) $ python3 -m tools.status_monitor --host <ecu_ip>
```

## Manual

```text
usage: ota_status_monitor [-h] [--host HOST] [--port PORT] [--title TITLE]

CLI program for monitoring target ecu status

options:
  -h, --help     show this help message and exit
  --host HOST    server listen ip (default: 192.168.10.11)
  --port PORT    server listen port (default: 50051)
  --title TITLE  terminal title (default: OTA status monitor)
```

## UI presentation

In each window, navigation is possible by using arrow_key, mouse scroll or page_up/page_down key. Pause is possible by pressing `p` in any window.

### Main window

The main window of the CLI, it will display all ECUs listed in the ECU status API response from the queried otaclient.

``` text
                                        OTA status monitor
 ┌───────────────────────────────────────────────────────────────────────────────────────────────┐
 │┌──────────────────────────────────────────────────────────┐                                   │
 ││(0)ECU_ID: autoware ota_status:UPDATING                   │                                   │
 ││current_firmware_version: unknown                         │                                   │
 ││oaclient_ver: 3.5.1.post27                                │                                   │
 ││----------------------------------------------------------│                                   │
 ││update_starts_at: 2023-08-10 12:45:00                     │                                   │
 ││update_phase: DOWNLOADING_OTA_FILES (elapsed_time: 1725s) │                                   │
 ││update_version: 789.x                                     │                                   │
 ││file_progress: 57,458(1.54GB) / 294,680(23.64GB)          │                                   │
 ││download_progress: 12,364(877.63MB) / 184,948(20.70GB)    │                                   │
 ││downloaded_bytes: 384.93MB                                │                                   │
 │└──────────────────────────────────────────────────────────┘                                   │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 │                                                                                               │
 └───────────────────────────────────────────────────────────────────────────────────────────────┘
 <NUM>: ECU#NUM's raw status, <ALT+NUM>: detailed failure info, <p>: pause
Paused, press any key to resume.

```

### SubWindow 1: raw ECU status

By pressing `<Num>` key on main window, the corresponding ECU's raw status report will be shown in a sub window.

```text
                             Raw ECU status info for autoware(#0) ECU
 ┌───────────────────────────────────────────────────────────────────────────────────────────────┐
 │ ecu_id :autoware,                                                                             │
 │ failure_reason :,                                                                             │
 │ failure_traceback :,                                                                          │
 │ failure_type :FailureType.NO_FAILURE,                                                         │
 │ firmware_version :unknown,                                                                    │
 │ ota_status :StatusOta.UPDATING,                                                               │
 │ otaclient_version :3.5.1.post27,                                                              │
 │ update_status :{                                                                              │
 │          delta_generating_elapsed_time :{                                                     │
 │                  seconds :19,                                                                 │
 │                  nanos :483885949,                                                            │
 │                 },                                                                            │
 │          downloaded_bytes :396352792,                                                         │
 │          downloaded_files_num :12623,                                                         │
 │          downloaded_files_size :911960343,                                                    │
 │          downloading_elapsed_time :{                                                          │
 │                  seconds :1509,                                                               │
 │                  nanos :0,                                                                    │
 │                 },                                                                            │
 │          downloading_errors :0,                                                               │
 │          phase :UpdatePhase.DOWNLOADING_OTA_FILES,                                            │
 │          processed_files_num :57717,                                                          │
 │          processed_files_size :1570743051,                                                    │
 │          removed_files_num :0,                                                                │
 │          total_download_files_num :184948,                                                    │
 │          total_download_files_size :20703405289,                                              │
 │          total_elapsed_time :{                                                                │
 │                  seconds :1773,                                                               │
 │                  nanos :350749695,                                                            │
 │                 },                                                                            │
 │          total_files_num :294680,                                                             │
 │          total_files_size_uncompressed :23637537004,                                          │
 │          total_remove_files_num :25734,                                                       │
 │          update_applying_elapsed_time :{                                                      │
 │                  seconds :0,                                                                  │
 │                  nanos :0,                                                                    │
 │                 },                                                                            │
 │          update_firmware_version :789.x,                                                      │
 │          update_start_timestamp :1691639100,                                                  │
 │         },                                                                                    │
 │                                                                                               │
 │                                                                                               │
 └───────────────────────────────────────────────────────────────────────────────────────────────┘
 <x>: go back, <p>: pause, <Arrow_key/PN_UP/PN_DOWN>: navigate, <Home>: reset pos.
Paused, press any key to resume.
```

### SubWindow 2: detailed failure info

If any of the ECU failed, by pressing `<Alt+num>` key combination on main window, pretty formatted detailed failure information(including failure_reason, failure_traceback) for the corresponding ECU will be shown in a sub window.

```text
                                Failure info for autoware(#0) ECU
 ┌───────────────────────────────────────────────────────────────────────────────────────────────┐
 │ota_status: FAILURE                                                                            │
 │failure_type: RECOVERABLE                                                                      │
 │failure_reason: E1-0103-100: error related to network connection detected, please check the Int│
 │ernet connection and try again.                                                                │
 │failure_traceback:                                                                             │
 │Traceback (most recent call last):                                                             │
 │  File "/home/autoware/venv/lib/python3.8/site-packages/requests/adapters.py", line 486, in sen│
 │d                                                                                              │
 │    resp = conn.urlopen(                                                                       │
 │  File "/home/autoware/venv/lib/python3.8/site-packages/urllib3/connectionpool.py", line 878, i│
 │n urlopen                                                                                      │
 │    return self.urlopen(                                                                       │
 │  File "/home/autoware/venv/lib/python3.8/site-packages/urllib3/connectionpool.py", line 878, i│
 │n urlopen                                                                                      │
 │    return self.urlopen(                                                                       │
 │  File "/home/autoware/venv/lib/python3.8/site-packages/urllib3/connectionpool.py", line 878, i│
 │n urlopen                                                                                      │
 │    return self.urlopen(                                                                       │
 │  File "/home/autoware/venv/lib/python3.8/site-packages/urllib3/connectionpool.py", line 868, i│
 │n urlopen                                                                                      │
 │    retries = retries.increment(method, url, response=response, _pool=self)                    │
 │  File "/home/autoware/venv/lib/python3.8/site-packages/urllib3/util/retry.py", line 592, in in│
 │crement                                                                                        │
 │    raise MaxRetryError(_pool, url, error or ResponseError(cause))                             │
 │urllib3.exceptions.MaxRetryError: HTTPConnectionPool(host='0.0.0.0', port=8082): Max retries ex│
 │ceeded with url: http://10.0.0.1:8443/data/usr/share/vim/vim81/syntax/rc.vim (Caused by Respons│
 │eError('too many 502 error responses'))                                                        │
 │                                                                                               │
 │During handling of the above exception, another exception occurred:                            │
 │                                                                                               │
 │Traceback (most recent call last):                                                             │
 │  File "/home/autoware/ota-client/otaclient/app/downloader.py", line 346, in _downloading_excep│
 │tion_mapping                                                                                   │
 │    yield                                                                                      │
 │  File "/home/autoware/ota-client/otaclient/app/downloader.py", line 498, in _download_task    │
 │    with self._downloading_exception_mapping(url, dst), self._session.get(                     │
 │  File "/home/autoware/venv/lib/python3.8/site-packages/requests/sessions.py", line 602, in get│
 │                                                                                               │
 │    return self.request("GET", url, **kwargs)                                                  │
 │  File "/home/autoware/venv/lib/python3.8/site-packages/requests/sessions.py", line 589, in req│
 │uest                                                                                           │
 │    resp = self.send(prep, **send_kwargs)                                                      │
 └───────────────────────────────────────────────────────────────────────────────────────────────┘
 <x>: go back, <p>: pause, <Arrow_key/PN_UP/PN_DOWN>: navigate, <Home>: reset pos.
Paused, press any key to resume.
```
