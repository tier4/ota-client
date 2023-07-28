import datetime
import time
import curses
import threading
from itertools import zip_longest
from typing import Sequence
from otaclient.app.proto import wrapper as proto_wrapper

from .utils import FormatValue, splitline_break_long_string
from .configs import config, key_mapping


class ECUStatusDisplayBox:
    DISPLAY_BOX_HLINES = config.ECU_DISPLAY_BOX_HLINES
    DISPLAY_BOX_HCOLS = config.ECU_DISPLAY_BOX_HCOLS
    SCROLL_LINES = config.SCROLL_LINES
    UPDATE_INTERVAL = config.RENDER_INTERVAL

    UNKNOWN_OTACLIENT_VERSION = "UNKNOWN"
    UNKNOWN_FIRMWARE_VERSION = "unknown_version"

    def __init__(self, ecu_id: str, index: int) -> None:
        self.ecu_id = ecu_id
        self.index = index

        # contents for the main windows
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
        self._lock = threading.Lock()
        self._last_updated = 0

    def _subwin_handler(self, stdscr: curses.window, contents: Sequence[str]):
        """Popup a sub window and render it with <contents>."""
        stdscr.clear()
        stdscrn_h, stdscrn_w = stdscr.getmaxyx()
        stdscr.addstr(0, 1, f"Failure info for {self.ecu_id}(#{self.index}) ECU")
        stdscr.hline(1, 1, curses.ACS_HLINE, stdscrn_w // 2)

        pad = curses.newpad(600, stdscrn_w)
        pad.keypad(True)
        pad.scrollok(True)
        pad.idlok(True)

        # add simple manual at the bottom
        stdscr.addstr(
            stdscrn_h - 2, 1, "press x key to go back, press arrow_up/down to scroll."
        )
        stdscr.refresh()

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

        last_cursor_y, _ = pad.getyx()
        while True:
            key = pad.getch()

            new_cursor_y = last_cursor_y
            if key == curses.KEY_DOWN:
                new_cursor_y = min(line_idx, new_cursor_y + self.SCROLL_LINES)
            elif key == curses.KEY_UP:
                new_cursor_y = max(0, new_cursor_y - self.SCROLL_LINES)
            elif key == curses.KEY_HOME:
                new_cursor_y = 0
            elif key == key_mapping.EXIT_ECU_STATUS_BOX_SUBWIN or curses.KEY_RESIZE:
                return
            else:
                time.sleep(self.UPDATE_INTERVAL)
                continue

            # re-render the pad
            if new_cursor_y != last_cursor_y:
                last_cursor_y = new_cursor_y
                pad.move(new_cursor_y, 0)
                pad.refresh(new_cursor_y, 0, 2, 1, stdscrn_h - 3, stdscrn_w - 1)

    def update_ecu_status(self, ecu_status: proto_wrapper.StatusResponseEcuV2):
        """Update internal contents storage with input <ecu_status>."""
        if ecu_status == self._last_status:
            return

        with self._lock:
            self.contents = [
                f"({self.index})ECU_ID: {self.ecu_id} ota_status:{ecu_status.ota_status.name}",
                f"version: {ecu_status.firmware_version} oaclient_ver: {ecu_status.otaclient_version}",
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
                self.failure_contents.clear()

            elif ecu_status.ota_status is proto_wrapper.StatusOta.FAILURE:
                self.contents.extend(
                    [
                        f"failure_type: {ecu_status.failure_type.name}",
                        f"failure_reason: {ecu_status.failure_reason}",
                        "-" * (self.DISPLAY_BOX_HCOLS - 2),
                        f"Press {self.index} key for detailed failure info.",
                    ]
                )
                self.failure_contents.extend(
                    [
                        f"failure_type: {ecu_status.failure_type.name}",
                        f"failure_reason: {ecu_status.failure_reason}",
                        "failure_traceback: ",
                        ecu_status.failure_traceback,
                    ]
                )
            else:
                self.failure_contents.clear()

            self.raw_contents = str(ecu_status).splitlines()

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
        self._subwin_handler(stdscr, self.failure_contents)

    def raw_ecu_status_subwin_handler(self, stdscr: curses.window):
        """The handler for raw_ecu_status subwin."""
        self._subwin_handler(stdscr, self.raw_contents)
