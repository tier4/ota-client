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

import curses
import datetime
import threading
import time
from typing import Sequence, Tuple

from otaclient_api.v2 import types as api_types

from .configs import config
from .utils import FormatValue, ScreenHandler


class ECUStatusDisplayBox:
    DISPLAY_BOX_HLINES = config.ECU_DISPLAY_BOX_HLINES
    DISPLAY_BOX_HCOLS = config.ECU_DISPLAY_BOX_HCOLS

    UNKNOWN_OTACLIENT_VERSION = "UNKNOWN"
    UNKNOWN_FIRMWARE_VERSION = "unknown_version"

    def __init__(self, ecu_id: str, index: int) -> None:
        self.ecu_id = ecu_id
        self.index = index

        # initial contents for the main windows
        self.contents = [
            f"({self.index})ECU_ID: {self.ecu_id} ota_status:{self.UNKNOWN_OTACLIENT_VERSION}",
            f"version: {self.UNKNOWN_FIRMWARE_VERSION}",
            "-" * (self.DISPLAY_BOX_HCOLS - 2),
        ]
        # contents for failure info sub window
        self.failure_contents = []
        # contents for raw ecu status info sub window
        self.raw_ecu_status_contents = []

        self._last_status = api_types.StatusResponseEcuV2()
        # prevent conflicts between status update and pad update
        self._lock = threading.Lock()
        self.last_updated = 0

    def get_failure_contents(self) -> Tuple[Sequence[str], int]:
        """Getter for failure_contents."""
        return self.failure_contents, self.last_updated

    def get_raw_info_contents(self) -> Tuple[Sequence[str], int]:
        """Getter for raw_ecu_status_contents."""
        return self.raw_ecu_status_contents, self.last_updated

    def update_ecu_status(self, ecu_status: api_types.StatusResponseEcuV2, index: int):
        """Update internal contents storage with input <ecu_status>.

        This method is called by tracker module to update the contents within
            the status display box.
        """
        self.index = index  # the order of ECU might be changed in tracker
        if ecu_status == self._last_status:
            return

        with self._lock:
            self.contents = [
                f"({self.index})ECU_ID: {self.ecu_id} ota_status:{ecu_status.ota_status.name}",
                f"current_firmware_version: {ecu_status.firmware_version}",
                f"oaclient_ver: {ecu_status.otaclient_version}",
                "-" * (self.DISPLAY_BOX_HCOLS - 2),
            ]

            if ecu_status.ota_status is api_types.StatusOta.UPDATING:
                update_status = ecu_status.update_status
                # TODO: render a progress bar according to ECU status V2's specification
                self.contents.extend(
                    [
                        (
                            f"update_starts_at: {datetime.datetime.fromtimestamp(update_status.update_start_timestamp)}"
                        ),
                        f"update_firmware_version: {update_status.update_firmware_version}",
                        (
                            f"update_phase: {update_status.phase.name} "
                            f"(elapsed_time: {update_status.total_elapsed_time.seconds}s)"
                        ),
                        f"update_version: {update_status.update_firmware_version}",
                        (
                            f"file_progress: {FormatValue.count(update_status.processed_files_num)}"
                            f"({FormatValue.bytes_count(update_status.processed_files_size)}) / "
                            f"{FormatValue.count(update_status.total_files_num)}"
                            f"({FormatValue.bytes_count(update_status.total_files_size_uncompressed)})"
                        ),
                        (
                            f"download_progress: {FormatValue.count(update_status.downloaded_files_num)}"
                            f"({FormatValue.bytes_count(update_status.downloaded_files_size)}) / "
                            f"{FormatValue.count(update_status.total_download_files_num)}"
                            f"({FormatValue.bytes_count(update_status.total_download_files_size)})"
                        ),
                        f"downloaded_bytes: {FormatValue.bytes_count(update_status.downloaded_bytes)}",
                    ]
                )
                self.failure_contents = [
                    f"ota_status: {ecu_status.ota_status.name}",
                    "No detailed failure information.",
                ]

            elif ecu_status.ota_status is api_types.StatusOta.FAILURE:
                self.contents.extend(
                    [
                        f"ota_status: {ecu_status.ota_status.name}",
                        f"failure_type: {ecu_status.failure_type.name}",
                        f"failure_reason: {ecu_status.failure_reason}",
                        "-" * (self.DISPLAY_BOX_HCOLS - 2),
                        f"Press ALT+{self.index} key for detailed failure info.",
                    ]
                )
                self.failure_contents = [
                    f"ota_status: {ecu_status.ota_status.name}",
                    f"failure_type: {ecu_status.failure_type.name}",
                    f"failure_reason: {ecu_status.failure_reason}",
                    "failure_traceback: ",
                    ecu_status.failure_traceback,
                ]
            else:
                self.failure_contents = [
                    f"ota_status: {ecu_status.ota_status.name}",
                    "No detailed failure information.",
                ]

            self.raw_ecu_status_contents = str(ecu_status).splitlines()[1:-1]

            self._last_status = ecu_status
            self.last_updated = int(time.time())

    def render_status_box_pad(
        self, pad: curses.window, begin_y: int, begin_x: int
    ) -> bool:
        """Render contents onto the status_box pad."""
        if int(time.time()) <= self.last_updated:
            return False

        with self._lock:
            # ------ clear previous contents from the pad ------ #
            for i in range(self.DISPLAY_BOX_HLINES):
                pad.addstr(begin_y + i, begin_x, " " * self.DISPLAY_BOX_HCOLS)

            # ------ draw box frame ------ #
            # draw top line
            pad.hline(
                begin_y, begin_x + 1, curses.ACS_HLINE, self.DISPLAY_BOX_HCOLS - 1
            )
            pad.addch(begin_y, begin_x, curses.ACS_ULCORNER)
            pad.addch(
                begin_y, begin_x + self.DISPLAY_BOX_HCOLS - 1, curses.ACS_URCORNER
            )
            # draw bottom line
            pad.hline(
                begin_y + self.DISPLAY_BOX_HLINES - 1,
                begin_x + 1,
                curses.ACS_HLINE,
                self.DISPLAY_BOX_HCOLS - 1,
            )
            pad.addch(
                begin_y + self.DISPLAY_BOX_HLINES - 1, begin_x, curses.ACS_LLCORNER
            )
            pad.addch(
                begin_y + self.DISPLAY_BOX_HLINES - 1,
                begin_x + self.DISPLAY_BOX_HCOLS - 1,
                curses.ACS_LRCORNER,
            )

            # ------ draw contents onto the pad ------ #
            for line_idx in range(1, self.DISPLAY_BOX_HLINES - 1):
                line_content = ""
                if line_idx <= len(self.contents):
                    line_content = self.contents[line_idx - 1]

                # print the left and right frame border
                pad.addch(begin_y + line_idx, begin_x, curses.ACS_VLINE)
                pad.addch(
                    begin_y + line_idx,
                    begin_x + self.DISPLAY_BOX_HCOLS - 1,
                    curses.ACS_VLINE,
                )
                pad.addstr(
                    begin_y + line_idx,
                    begin_x + 1,
                    line_content[: self.DISPLAY_BOX_HCOLS - 1],
                )
            return True

    def failure_info_subwin_handler(self, stdscr: curses.window):
        """The handler for failure info subwin.

        The current window will be replaced by this subwin, and the controller
            will be replaced by this window handler.
        """
        ScreenHandler(
            stdscr,
            title=f"Failure info for {self.ecu_id}(#{self.index}) ECU",
            contents_getter=self.get_failure_contents,
        ).subwin_handler()

    def raw_ecu_status_subwin_handler(self, stdscr: curses.window):
        """The handler for raw_ecu_status subwin.

        The current window will be replaced by this subwin, and the controller
            will be replaced by this window handler.
        """
        ScreenHandler(
            stdscr,
            title=f"Raw ECU status info for {self.ecu_id}(#{self.index}) ECU",
            contents_getter=self.get_raw_info_contents,
        ).subwin_handler()
