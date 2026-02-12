# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Entrypoint of otaclient."""

from __future__ import annotations

import atexit
import logging
import multiprocessing as mp
import multiprocessing.context as mp_ctx
import multiprocessing.resource_tracker as mp_resource_tracker
import multiprocessing.shared_memory as mp_shm
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path
from typing import NoReturn

from otaclient import __version__
from otaclient._types import (
    AbortState,
    ClientUpdateControlFlags,
    MultipleECUStatusFlags,
    OTAAbortState,
)
from otaclient._utils import (
    SharedOTAClientMetricsReader,
    SharedOTAClientMetricsWriter,
    SharedOTAClientStatusReader,
    SharedOTAClientStatusWriter,
)
from otaclient.configs.cfg import cfg
from otaclient_common import replace_root
from otaclient_common._typing import StrOrPath
from otaclient_common.cmdhelper import (
    bind_mount_ro,
    bind_mount_rw,
    ensure_mount,
    ensure_umount,
    mount_tmpfs,
)

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 6  # seconds
# NOTE: the reason to let daemon_process exits after 16 seconds of ota_core dead
#   is to allow grpc API server to respond to the status API calls with up-to-date
#   failure information from ota_core.
SHUTDOWN_AFTER_CORE_EXIT = 16  # seconds
SHUTDOWN_AFTER_API_SERVER_EXIT = 3  # seconds
SHUTDOWN_AFTER_ABORT_REQUEST_RECEIVED = 3  # seconds
SHUTDOWN_ON_DYNAMIC_APP_FAILED = 6  # seconds

STATUS_SHM_SIZE = 4096  # bytes
METRICS_SHM_SIZE = 512  # bytes, the pickle size of OTAMetricsSharedMemoryData
MAX_TRACEBACK_SIZE = 2048  # bytes
SHM_HMAC_KEY_LEN = 64  # bytes

_ota_core_p: mp_ctx.SpawnProcess | None = None
_grpc_server_p: mp_ctx.SpawnProcess | None = None
_shm: mp_shm.SharedMemory | None = None
_shm_metrics: mp_shm.SharedMemory | None = None

_global_shutdown_lock = threading.Lock()


def _on_shutdown(sys_exit: bool | int = True):  # pragma: no cover
    """
    NOTE: this handler should only be actually executed once!
          i.e., the total shutdown should only happen once!
    """
    # NOTE: _global_shutdown_lock is intentionally never released.
    #       It acts as a one-time guard to ensure that the shutdown
    #       sequence is executed at most once, even if _on_shutdown
    #       is triggered multiple times (e.g., via signals).
    if _global_shutdown_lock.acquire(blocking=False):
        global _ota_core_p, _grpc_server_p, _shm, _shm_metrics
        if _ota_core_p:
            _ota_core_p.terminate()
            _ota_core_p.join()
            _ota_core_p = None

        if _grpc_server_p:
            _grpc_server_p.terminate()
            _grpc_server_p.join()
            _grpc_server_p = None

        if _shm:
            _shm.close()
            _shm.unlink()
            _shm = None

        if _shm_metrics:
            _shm_metrics.close()
            _shm_metrics.unlink()
            _shm_metrics = None

        ensure_umount(cfg.RUNTIME_OTA_SESSION, ignore_error=True, max_retry=2)
        ensure_umount(cfg.ACTIVE_SLOT_MNT, ignore_error=True, max_retry=2)
        ensure_umount(cfg.STANDBY_SLOT_MNT, ignore_error=True, max_retry=2)

        if sys_exit is not False:
            logger.warning("otaclient will exit now ...")
            sys.exit(sys_exit if isinstance(sys_exit, int) else 1)


def _dynamic_otaclient_init():  # pragma: no cover
    """Some special treatments for dynamic otaclient starting.

    This includes:
    1. setting up the /ota-cache folder from host mount.
    2. setting up the /run/otaclient/mnt/active_slot mount point
        from /host_root.
    3. setting up a tmpfs mount on /tmp.
    """
    _host_root = Path(cfg.DYNAMIC_CLIENT_MNT_HOST_ROOT)
    _host_root_ota_cache = Path(
        replace_root(
            cfg.OTAPROXY_CACHE_DIR,
            cfg.CANONICAL_ROOT,
            _host_root,
        )
    )
    _host_root_ota_cache.mkdir(exist_ok=True, parents=True)

    # NOTE: otaclient mount space is located in /run/otaclient/mnt,
    #       which is bind mounted(the whole /run) into the APP image from host.
    _active_slot_mp = Path(cfg.ACTIVE_SLOT_MNT)
    _active_slot_mp.mkdir(exist_ok=True, parents=True)

    ensure_mount(
        target=_host_root_ota_cache,
        mnt_point=cfg.OTAPROXY_CACHE_DIR,
        mount_func=bind_mount_rw,
        raise_exception=True,
    )
    ensure_mount(
        target=_host_root,
        mnt_point=_active_slot_mp,
        mount_func=bind_mount_ro,
        raise_exception=True,
    )

    # NOTE: for ubuntu 18.04 backward compat, we don't use systemd's TemporaryFileSystem
    #       directory to configure tmpfs within otaclient app.
    # NOTE: although the /tmp mostly will not be used, but for fallback, still
    #       prepare a tmpfs mount on the /tmp.
    _tmp_mp = "/tmp"
    ensure_mount(
        "tmpfs",
        _tmp_mp,
        mount_func=partial(
            mount_tmpfs,
            size_in_mb=cfg.OTACLIENT_APP_TMPFS_SIZE_IN_MB,
        ),
        raise_exception=True,
    )


def _dynamic_otaclient_launch(
    _dynamic_service_unit: str,
) -> NoReturn:  # pragma: no cover
    """execvpe to systemd-run to launch the dynamic otaclient."""
    from otaclient_common import _env

    Path(cfg.OTACLIENT_PID_FILE).unlink(missing_ok=True)
    # NOTE: the old otaclient's main process will just wait for the dynamic otaclient
    #       finishes up running as we don't want the old otaclient restart.
    # NOTE(20251010): we cannot use os.execve as if we are running as systemd managed
    #                 APP image, os.execve will be executed from within the APP image.
    logger.info(f"launch dynamic otaclient with {_dynamic_service_unit}")
    # fmt: off
    _dynamic_otaclient_cmd = [
            "systemd-run",
            f"--unit={_dynamic_service_unit}", "-G",
            # NOTE: let otaclient directly attaches to the tty of the
            #       dynamic launched otaclient to control its life-cycle.
            "--wait", "-t",
            "--setenv=RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT=yes",
            "--setenv=RUNNING_AS_APP_IMAGE=",
            "-p", "Restart=no",
            "-p", f"Description={_dynamic_service_unit}",
            "-p", "Type=simple",
            # NOTE: prevent the dynamic otaclient APP being stop manually, the stop should
            #       be done by stop the main otaclient.service instead. Restart is also prohibited.
            "-p", "RefuseManualStop=true",
            # NOTE: subprocess_call here will do a chroot back to host_root.
            "-p", f"RootImage={cfg.DYNAMIC_CLIENT_SQUASHFS_FILE}",
            "-p", "ExecStartPre=/bin/mkdir -p /run/otaclient/mnt/active_slot",
            "-p", "ExecStartPre=/bin/mkdir -p /host_root/ota-cache",
            "-p", "BindPaths=/boot:/boot:rbind",
            "-p", "BindPaths=/dev:/dev",
            "-p", "BindPaths=/dev/shm:/dev/shm",
            "-p", "BindPaths=/etc:/etc",
            "-p", "BindPaths=/opt:/opt",
            "-p", "BindPaths=/proc:/proc",
            "-p", "BindPaths=/root:/root",
            "-p", "BindPaths=/sys:/sys:rbind",
            "-p", "BindPaths=/run:/run",
            "-p", "BindReadOnlyPaths=-/usr/share/ca-certificates:/usr/share/ca-certificates",
            "-p", "BindReadOnlyPaths=-/usr/local/share/ca-certificates:/usr/local/share/ca-certificates",
            "-p", "BindReadOnlyPaths=-/usr/share/zoneinfo:/usr/share/zoneinfo",
            "-p", "BindPaths=/:/host_root:rbind",
            # NOTE: although new systemd compatible APP image runs from /otaclient/otaclient, for backward compatibility
            #       concern, we still start the otaclient from /otaclient/venv/bin/python3.
            #       for new systemd compatible APP image, the /otaclient/venv/bin/python3 is just a wrapper script to call
            #       /otaclient/otaclient.
            # NOTE: although new APP image can configure the ota-cache and active_slot mount points by it self, for backward compatibility
            #       with old otaclient APP image, we still setup the mount points here.
            "/bin/bash", "-c",
            (
                "mount -o bind /host_root/ota-cache /ota-cache && "
                "mount -o bind,ro /host_root /run/otaclient/mnt/active_slot && "
                f"mount -t tmpfs -o size={cfg.OTACLIENT_APP_TMPFS_SIZE_IN_MB}M tmpfs /tmp && "
                "/otaclient/venv/bin/python3 -m otaclient"
            ),
        ]
    # fmt: on

    if _chroot_dir := _env.get_dynamic_client_chroot_path():
        os.execvpe(
            "chroot",
            ["chroot", _chroot_dir, *_dynamic_otaclient_cmd],
            os.environ,
        )
    else:
        os.execvpe("systemd-run", _dynamic_otaclient_cmd, os.environ)


def _bind_external_nfs_cache(external_nfs_cache_mnt_point: StrOrPath | None):
    """
    Bind mount the external NFS cache from host root.

    Args:
        external_nfs_cache_mnt_point (StrOrPath | None): The mount point of the external NFS cache to bind mount, or None to skip binding.

    Returns:
        None
    """
    if not external_nfs_cache_mnt_point:
        return

    _host_root = Path(cfg.DYNAMIC_CLIENT_MNT_HOST_ROOT)
    _host_root_external_nfs_cache = Path(
        replace_root(
            external_nfs_cache_mnt_point,
            cfg.CANONICAL_ROOT,
            _host_root,
        )
    )
    if not _host_root_external_nfs_cache.is_dir():
        logger.warning(
            f"external NFS cache mount point {_host_root_external_nfs_cache} does not exist on host root"
        )
        return

    ensure_mount(
        target=_host_root_external_nfs_cache,
        mnt_point=external_nfs_cache_mnt_point,
        mount_func=bind_mount_ro,
        raise_exception=False,
    )


def _signal_handler(signal_value, _) -> None:  # pragma: no cover
    print(f"otaclient receives {signal_value=}, shutting down ...")
    # NOTE: the daemon_process needs to exit also.
    _on_shutdown()


def main() -> None:  # pragma: no cover
    from otaclient._logging import configure_logging
    from otaclient._otaproxy_ctx import (
        otaproxy_control_thread,
        otaproxy_on_global_shutdown,
    )
    from otaclient._utils import check_other_otaclient
    from otaclient.configs.cfg import cfg, ecu_info, proxy_info
    from otaclient.grpc.api_v2.main import grpc_server_process
    from otaclient.ota_core import ota_core_process
    from otaclient_common import _env

    # configure logging before any code being executed
    configure_logging()

    logger.info("started")
    logger.info(f"otaclient started with {sys.executable=}, {sys.argv=}")
    logger.info(f"pid: {os.getpid()}")
    logger.info(f"otaclient version: {__version__}")
    logger.info(f"ecu_info.yaml: \n{ecu_info}")
    logger.info(f"proxy_info.yaml: \n{proxy_info}")
    logger.info(f"running as app image: {_env.is_dynamic_client_running()}")
    logger.info(
        f"env.running_downloaded_dynamic_ota_client: {_env.is_running_as_downloaded_dynamic_app()}"
    )

    # NOTE: if we are running as dynamic client by OTACLIENTUPDATE, we don't need the init,
    #       as the launcher otaclient has setup the environment for us.
    if _env.is_running_as_app_image():
        logger.info("initializing for running as dynamic otaclient ...")
        _dynamic_otaclient_init()

    # NOTE: external NFS cache mount is not setup by dynamic client launcher,
    #       so we need to setup the bind mount here for both app image and dynamic client.
    if _env.is_dynamic_client_running():
        _bind_external_nfs_cache(proxy_info.external_nfs_cache_mnt_point)

    # Log system uptime (time since OS boot)
    try:
        uptime_seconds = time.clock_gettime(time.CLOCK_BOOTTIME)
        uptime_hours = uptime_seconds // 3600
        uptime_minutes = (uptime_seconds % 3600) // 60
        uptime_secs = uptime_seconds % 60
        logger.info(
            f"system uptime: {uptime_hours:.0f}h {uptime_minutes:.0f}m {uptime_secs:.1f}s ({uptime_seconds:.1f}s total)"
        )
    except Exception as e:
        logger.warning(f"failed to read system uptime: {e}")

    # for dynamic loaded otaclient, skip other otaclient check
    if not _env.is_running_as_downloaded_dynamic_app():
        check_other_otaclient(cfg.OTACLIENT_PID_FILE)

    #
    # ------ start each processes ------ #
    #
    global _ota_core_p, _grpc_server_p, _shm, _shm_metrics

    # --- setup signal handlers and aexit hook --- #
    # NOTE: if the atexit hook is triggered by signal received,
    #   first the signal handler will be executed, and then atexit hook.
    #   At the time atexit hook is executed, the global shutdown lock is
    #   already acquired, no action will be taken by atexit register.
    # NOTE: sys.exit at atexit hook will just be ignored by python.
    atexit.register(partial(_on_shutdown, sys_exit=False))
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    # NOTE(20260121): also handle SIGHUP, this is for dynamic launched otaclient
    #                 to gracefully shutdown.
    signal.signal(signal.SIGHUP, _signal_handler)

    mp_ctx = mp.get_context("spawn")
    _shm = mp_shm.SharedMemory(size=STATUS_SHM_SIZE, create=True)
    _key = secrets.token_bytes(SHM_HMAC_KEY_LEN)

    _shm_metrics = mp_shm.SharedMemory(size=METRICS_SHM_SIZE, create=True)
    _key_metrics = secrets.token_bytes(SHM_HMAC_KEY_LEN)

    # shared queues and flags
    local_otaclient_op_queue = mp_ctx.Queue()
    local_otaclient_resp_queue = mp_ctx.Queue()
    ecu_status_flags = MultipleECUStatusFlags(
        any_child_ecu_in_update=mp_ctx.Event(),
        any_requires_network=mp_ctx.Event(),
        all_success=mp_ctx.Event(),
    )
    client_update_control_flags = ClientUpdateControlFlags(
        notify_data_ready_event=mp_ctx.Event(),
        request_shutdown_event=mp_ctx.Event(),
    )
    abort_ota_state = OTAAbortState(mp_ctx.Value("i", AbortState.NONE))

    _ota_core_p = mp_ctx.Process(
        target=partial(
            ota_core_process,
            shm_writer_factory=partial(
                SharedOTAClientStatusWriter, name=_shm.name, key=_key
            ),
            shm_metrics_reader_factory=partial(
                SharedOTAClientMetricsReader, name=_shm_metrics.name, key=_key_metrics
            ),
            ecu_status_flags=ecu_status_flags,
            op_queue=local_otaclient_op_queue,
            resp_queue=local_otaclient_resp_queue,
            max_traceback_size=MAX_TRACEBACK_SIZE,
            client_update_control_flags=client_update_control_flags,
            abort_ota_state=abort_ota_state,
        ),
        name="otaclient_ota_core",
    )
    _ota_core_p.start()

    _grpc_server_p = mp_ctx.Process(
        target=partial(
            grpc_server_process,
            shm_reader_factory=partial(
                SharedOTAClientStatusReader, name=_shm.name, key=_key
            ),
            op_queue=local_otaclient_op_queue,
            resp_queue=local_otaclient_resp_queue,
            ecu_status_flags=ecu_status_flags,
            abort_ota_state=abort_ota_state,
        ),
        name="otaclient_api_server",
    )
    _grpc_server_p.start()

    del _key

    # ------ setup main process ------ #

    _otaproxy_control_t = None
    if proxy_info.enable_local_ota_proxy:
        _otaproxy_control_t = threading.Thread(
            target=partial(
                otaproxy_control_thread,
                ecu_status_flags,
                shm_metrics_writer_factory=partial(
                    SharedOTAClientMetricsWriter,
                    name=_shm_metrics.name,
                    key=_key_metrics,
                ),
            ),
            daemon=True,
            name="otaclient_otaproxy_control_t",
        )
        _otaproxy_control_t.start()

    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)

        if abort_ota_state.state == AbortState.ABORTED:
            logger.info(
                "OTA abort completed by updater. "
                f"Shutting down after {SHUTDOWN_AFTER_ABORT_REQUEST_RECEIVED} seconds..."
            )
            time.sleep(SHUTDOWN_AFTER_ABORT_REQUEST_RECEIVED)
            return _on_shutdown()

        if not _ota_core_p.is_alive():
            logger.error(
                "ota_core process is dead! "
                f"otaclient will exit in {SHUTDOWN_AFTER_CORE_EXIT} seconds ..."
            )
            time.sleep(SHUTDOWN_AFTER_CORE_EXIT)
            return _on_shutdown()

        if not _grpc_server_p.is_alive():
            logger.error(
                f"ota API server is dead, whole otaclient will exit in {SHUTDOWN_AFTER_API_SERVER_EXIT} seconds ..."
            )
            time.sleep(SHUTDOWN_AFTER_API_SERVER_EXIT)
            return _on_shutdown()

        # launch the dynamic client preparation process
        if client_update_control_flags.notify_data_ready_event.is_set():
            _dynamic_service_unit = f"otaclient_dynamic_app_{os.urandom(6).hex()}"
            logger.info(
                f"will launch dynamic otaclient app with systemd(service_unit: {_dynamic_service_unit}) ..."
            )

            try:
                # exit ota proxy thread if it is running
                if _otaproxy_control_t and _otaproxy_control_t.is_alive():
                    logger.info("exit otaproxy control thread ...")
                    otaproxy_on_global_shutdown()
                    _otaproxy_control_t.join()
                # kill other resources except main process
                logger.info("on main shutdown...")
                _on_shutdown(sys_exit=False)

                logger.info("cleaning up resources ...")
                if _shm:
                    del _shm
                if _shm_metrics:
                    del _shm_metrics
                if ecu_status_flags:
                    del ecu_status_flags.any_child_ecu_in_update
                    del ecu_status_flags.any_requires_network
                    del ecu_status_flags.all_success
                if client_update_control_flags:
                    del client_update_control_flags.notify_data_ready_event
                    del client_update_control_flags.request_shutdown_event
                if local_otaclient_op_queue:
                    del local_otaclient_op_queue
                if local_otaclient_resp_queue:
                    del local_otaclient_resp_queue

                # this is a python bug(https://github.com/python/cpython/issues/88887),
                # and it is fixed since python3.12 (https://github.com/python/cpython/pull/131530).
                if sys.version_info < (3, 12):
                    logger.info("stopping resource tracker ...")
                    _resource_tracker = getattr(
                        mp_resource_tracker, "_resource_tracker", None
                    )
                    if _resource_tracker and hasattr(_resource_tracker, "_stop"):
                        try:
                            _resource_tracker._stop()
                        except Exception as e:
                            logger.error(f"failed to stop the resource tracker: {e!r}")

                _dynamic_otaclient_launch(_dynamic_service_unit)
            except Exception as e:
                logger.exception(f"failed to launch dynamic client with systemd: {e}")
                if isinstance(e, subprocess.CalledProcessError):
                    logger.error(f"systemd-run failed: \n{e.stderr=}\n{e.stdout=}")

                logger.warning(
                    f"otaclient will exit in {SHUTDOWN_ON_DYNAMIC_APP_FAILED}!"
                )
                time.sleep(SHUTDOWN_ON_DYNAMIC_APP_FAILED)
                _on_shutdown()
            finally:
                sys.exit(1)  # just for typing

        # shutdown request
        if client_update_control_flags.request_shutdown_event.is_set():
            return _on_shutdown()
