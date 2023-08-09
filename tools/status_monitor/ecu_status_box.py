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


import datetime
import time
import curses
import threading
from itertools import zip_longest
from typing import Callable, Sequence, Tuple
from otaclient.app.proto import wrapper as proto_wrapper

from .utils import FormatValue, splitline_break_long_string
from .configs import config, key_mapping


ContentsGetterFunc = Callable[[], Tuple[Sequence[str], int]]


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
        self.raw_contents = []

        self._last_status = proto_wrapper.StatusResponseEcuV2()
        # prevent conflicts between status update and pad update
        self._lock = threading.Lock()
        self._last_updated = 0

    def get_failure_contents(self) -> Tuple[Sequence[str], int]:
        return self.failure_contents, self.last_updated

    def get_raw_info_contents(self) -> Tuple[Sequence[str], int]:
        return self.raw_contents, self.last_updated

    def update_ecu_status(
        self, ecu_status: proto_wrapper.StatusResponseEcuV2, index: int
    ):
        """Update internal contents storage with input <ecu_status>."""
        self.index = index  # the order of ECU might be changed
        if ecu_status == self._last_status:
            return

        with self._lock:
            self.contents = [
                f"({self.index})ECU_ID: {self.ecu_id} ota_status:{ecu_status.ota_status.name}",
                f"current_firmware_version: {ecu_status.firmware_version}",
                f"oaclient_ver: {ecu_status.otaclient_version}",
                "-" * (self.DISPLAY_BOX_HCOLS - 2),
            ]

            if ecu_status.ota_status is proto_wrapper.StatusOta.UPDATING:
                update_status = ecu_status.update_status
                self.contents.extend(
                    [
                        (
                            f"update_starts_at: {datetime.datetime.fromtimestamp(update_status.update_start_timestamp)}"
                        ),
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
                self.failure_contents = [f"ota_status: {ecu_status.ota_status.name}"]

            elif ecu_status.ota_status is proto_wrapper.StatusOta.FAILURE:
                self.contents.extend(
                    [
                        f"ota_status: {ecu_status.ota_status.name}",
                        f"failure_type: {ecu_status.failure_type.name}",
                        f"failure_reason: {ecu_status.failure_reason}",
                        "-" * (self.DISPLAY_BOX_HCOLS - 2),
                        f"Press {self.index} key for detailed failure info.",
                    ]
                )
                self.failure_contents.extend(
                    [
                        f"ota_status: {ecu_status.ota_status.name}",
                        f"failure_type: {ecu_status.failure_type.name}",
                        f"failure_reason: {ecu_status.failure_reason}",
                        "failure_traceback: ",
                        ecu_status.failure_traceback,
                    ]
                )
            else:
                self.failure_contents = [f"ota_status: {ecu_status.ota_status.name}"]

            self.raw_contents = str(ecu_status).splitlines()[1:-1]

            self._last_status = ecu_status
            self.last_updated = int(time.time())

    def update_status_box_pad(self, pad: curses.window, begin_y, begin_x) -> bool:
        """Render contents onto the status_box pad."""
        if int(time.time()) <= self._last_updated:
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
            for line_idx, line_content in zip_longest(
                range(1, self.DISPLAY_BOX_HLINES - 1),
                self.contents,
                fillvalue="",
            ):
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
        """The handler for failure info subwin."""
        _SubWinHelper(
            stdscr,
            title=f"Failure info for {self.ecu_id}(#{self.index}) ECU",
            contents_getter=self.get_failure_contents,
        ).subwin_handler()

    def raw_ecu_status_subwin_handler(self, stdscr: curses.window):
        """The handler for raw_ecu_status subwin."""
        _SubWinHelper(
            stdscr,
            title=f"Raw ECU status info for {self.ecu_id}(#{self.index}) ECU",
            contents_getter=self.get_raw_info_contents,
        ).subwin_handler()


class _SubWinHelper:
    MIN_WIN_SIZE = config.MIN_TERMINAL_SIZE
    SCROLL_LINES = config.SCROLL_LINES
    UPDATE_INTERVAL = config.RENDER_INTERVAL

    def __init__(
        self,
        stdscr: curses.window,
        title: str,
        contents_getter: ContentsGetterFunc,
    ) -> None:
        self.stdscr = stdscr
        self.title = title
        self.contents_getter = contents_getter

    def subwin_reset(self):
        """Reset the subwindow and re-render the ECU status to a new pad."""
        self.stdscr.clear()
        stdscrn_h, stdscrn_w = self.stdscr.getmaxyx()
        self.stdscr.addstr(0, 1, self.title)
        self.stdscr.hline(1, 1, curses.ACS_HLINE, stdscrn_w // 2)

        pad = curses.newpad(600, stdscrn_w)
        pad.keypad(True)
        pad.scrollok(True)
        pad.idlok(True)
        pad.nodelay(True)

        # add simple manual at the bottom
        self.stdscr.addstr(
            stdscrn_h - 2, 1, "press x key to go back, press arrow_up/down to scroll."
        )
        self.stdscr.refresh()
        return pad, stdscrn_h, stdscrn_w

    def subwin_draw_pad(
        self,
        pad: curses.window,
        stdscrn_h: int,
        stdscrn_w: int,
        contents: Sequence[str],
    ) -> int:
        # pre_process the failure info, breakup long line into multiple lines if any
        _processed = []
        for line in contents:
            _processed.extend(splitline_break_long_string(line, length=stdscrn_w - 1))

        # draw line onto the pad
        line_idx = 0
        for line_idx, line_content in enumerate(_processed):
            pad.addstr(line_idx, 0, line_content)

        pad.move(0, 0)  # move cursor back to pad's init pos
        pad.refresh(0, 0, 2, 1, stdscrn_h - 3, stdscrn_w - 1)
        return line_idx

    def subwin_handler(self):
        """Popup a sub window and render it with <contents>."""
        # init subwindow
        pad, stdscrn_h, stdscrn_w = self.subwin_reset()
        last_updated, line_idx = 0, 0

        last_cursor_y, _ = pad.getyx()
        while True:
            # redraw the pad if contents updated or if resized
            contents, _last_updated = self.contents_getter()
            if _last_updated > last_updated:
                line_idx = self.subwin_draw_pad(pad, stdscrn_h, stdscrn_w, contents)
                last_updated = _last_updated

            key = pad.getch()
            if key == key_mapping.EXIT_ECU_STATUS_BOX_SUBWIN:
                return
            if key == curses.KEY_RESIZE:
                # if terminal window becomes too small, clear and break out to main window
                if self.stdscr.getmaxyx() < self.MIN_WIN_SIZE:
                    self.stdscr.clear()
                    return

                # fully reset the whole window and re-render everything
                pad, stdscrn_h, stdscrn_w = self.subwin_reset()
                last_updated = 0  # force update contents
                continue

            new_cursor_y = last_cursor_y
            if key == curses.KEY_DOWN:
                new_cursor_y = min(line_idx, new_cursor_y + self.SCROLL_LINES)
            elif key == curses.KEY_UP:
                new_cursor_y = max(0, new_cursor_y - self.SCROLL_LINES)
            elif key == curses.KEY_HOME:
                new_cursor_y = 0
            else:
                time.sleep(self.UPDATE_INTERVAL)
                continue

            # re-render the pad
            if key == curses.KEY_RESIZE or new_cursor_y != last_cursor_y:
                last_cursor_y = new_cursor_y
                pad.move(new_cursor_y, 0)
                pad.refresh(new_cursor_y, 0, 2, 1, stdscrn_h - 3, stdscrn_w - 1)
