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

from __future__ import annotations

import errno
import hashlib
import json
import logging
from concurrent.futures import Future
from http import HTTPStatus
from typing import Any

import requests.exceptions as requests_exc
from requests import Response

from otaclient import errors as ota_errors
from otaclient_common.common import ensure_otaproxy_start
from otaclient_common.downloader import DownloaderPool

logger = logging.getLogger(__name__)

WAIT_FOR_OTAPROXY_ONLINE = 3 * 60  # 3mins


class OTAClientError(Exception): ...


def download_exception_handler(_fut: Future[Any]) -> bool:
    """Parse the exception raised by a downloading task.

    This handler will raise OTA Error on exceptions that cannot(should not) be
        handled by us. For handled exceptions, just let upper caller do the
        retry for us.

    Raises:
        UpdateRequestCookieInvalid on HTTP error 401 or 403,
        OTAImageInvalid on HTTP error 404,
        StandbySlotInsufficientSpace on disk space not enough.

    Returns:
        True on succeeded downloading, False on handled exceptions.
    """
    if not (exc := _fut.exception()):
        return True

    try:
        # exceptions that cannot be handled by us
        if isinstance(exc, requests_exc.HTTPError):
            _response = exc.response
            # NOTE(20241129): if somehow HTTPError doesn't contain response,
            #       don't do anything but let upper retry.
            # NOTE: bool(Response) is False when status_code != 200.
            if not isinstance(_response, Response):
                return False

            http_errcode = _response.status_code
            if http_errcode in [HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED]:
                raise ota_errors.UpdateRequestCookieInvalid(
                    f"download failed with critical HTTP error: {exc.errno}, {exc!r}",
                    module=__name__,
                )
            if http_errcode == HTTPStatus.NOT_FOUND:
                raise ota_errors.OTAImageInvalid(
                    f"download failed with 404 on some file(s): {exc!r}",
                    module=__name__,
                )

        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise ota_errors.StandbySlotInsufficientSpace(
                f"download failed due to space insufficient: {exc!r}",
                module=__name__,
            )

        # handled exceptions, let the upper caller do the retry
        return False
    finally:
        del exc, _fut  # drop ref to exc instance


def handle_upper_proxy(_upper_proxy: str) -> None:
    """Ensure the upper proxy is online before starting the local OTA update."""
    logger.info(
        f"use {_upper_proxy} for local OTA update, "
        f"wait for otaproxy@{_upper_proxy} online..."
    )

    # NOTE: will raise a built-in ConnnectionError at timeout
    ensure_otaproxy_start(
        _upper_proxy,
        probing_timeout=WAIT_FOR_OTAPROXY_ONLINE,
    )


def prepare_cookies(cookies_json: str) -> dict[str, str]:
    try:
        cookies = json.loads(cookies_json)
        if not isinstance(cookies, dict):
            raise ValueError(f"invalid cookies, expecting json object: {cookies_json}")
        return cookies
    except (json.JSONDecodeError, ValueError) as e:
        _err_msg = f"cookie is invalid: {cookies_json=}"
        logger.error(_err_msg)
        raise ota_errors.InvalidUpdateRequest(_err_msg, module=__name__) from e


def create_downloader_pool(
    raw_cookies_json: str,
    proxy_url: str | None = None,
    *,
    download_threads: int,
    chunk_size: int,
    hash_func=hashlib.sha256,
) -> DownloaderPool:
    cookies = prepare_cookies(raw_cookies_json)
    return DownloaderPool(
        instance_num=download_threads,
        hash_func=hash_func,
        chunk_size=chunk_size,
        cookies=cookies,
        # NOTE(20221013): check requests document for how to set proxy,
        #                 we only support using http proxy here.
        proxies={"http": proxy_url} if proxy_url else None,
    )
