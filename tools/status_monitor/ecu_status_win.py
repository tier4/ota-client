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


import curses
import time
from enum import Enum
from typing import Dict, List
from typing_extensions import Self
from otaclient.app.proto import wrapper
from tools.utils import (
    DrawableArea,
    PAGE_SCROLL_KEYS,
    splitline_break_long_string,
    truncate_long_string,
    init_pad,
    draw_pad,
    render_pad,
    pad_scroll_handler,
)


class _ECU_DISPLAY(str, Enum):
    FAILURE_STATUS = "FAILURE_STATUS"
    RAW_ECU_STATUS = "RAW_ECU_STATUS"

    @classmethod
    def switch_content(cls, _cur: Self):
        if _cur is cls.FAILURE_STATUS:
            return cls.RAW_ECU_STATUS
        return cls.FAILURE_STATUS


class ECUStatusSubWindow:
    PAD_HLINES, PAD_HCOLS = 300, 300
    MIN_HLINES = 5

    def __init__(self, ecu_id: str, index: int) -> None:
        self.ecu_id = ecu_id
        self.index = index

        self.last_updated = 0
        self.contents: Dict[str, List[str]] = {
            _ECU_DISPLAY.FAILURE_STATUS: [],
            _ECU_DISPLAY.RAW_ECU_STATUS: [],
        }

    def update_ecu_v1(self, ecu_status_v1: wrapper.StatusResponseEcu):
        self.last_updated = int(time.time())
        self.contents[_ECU_DISPLAY.RAW_ECU_STATUS] = splitline_break_long_string(
            str(ecu_status_v1),
            length=self.PAD_HCOLS,
        )
        self.contents[_ECU_DISPLAY.FAILURE_STATUS] = [
            *truncate_long_string(
                f"failure_type: {ecu_status_v1.status.failure.name}", self.PAD_HCOLS
            ),
            *truncate_long_string(
                f"failure_reason: {ecu_status_v1.status.failure_reason}",
                self.PAD_HCOLS,
            ),
        ]

    def update_ecu_v2(self, ecu_status_v2: wrapper.StatusResponseEcuV2):
        self.last_updated = int(time.time())
        self.contents[_ECU_DISPLAY.RAW_ECU_STATUS] = splitline_break_long_string(
            str(ecu_status_v2),
            length=self.PAD_HCOLS,
        )

        # pre-processing traceback
        _traceback_lines = []
        for line in ecu_status_v2.failure_traceback:
            _traceback_lines.extend(truncate_long_string(line, self.PAD_HCOLS))

        self.contents[_ECU_DISPLAY.FAILURE_STATUS] = [
            *truncate_long_string(
                f"failure_type: {ecu_status_v2.failure_type.name}", self.PAD_HCOLS
            ),
            *truncate_long_string(
                f"failure_reason: {ecu_status_v2.failure_reason}",
                self.PAD_HCOLS,
            ),
            *truncate_long_string("failure_traceback:", self.PAD_HCOLS),
            *_traceback_lines,
        ]

    def _init_win(self, stdscr: curses.window) -> DrawableArea:
        stdscr.clear()
        stdscrn_h, stdscrn_w = stdscr.getmaxyx()
        # draw screen header(2 lines)
        stdscr.addstr(
            0,
            1,
            truncate_long_string(
                f"Advanced status info for ({self.index}){self.ecu_id} ECU", stdscrn_w
            ),
        )
        stdscr.hline(1, 1, curses.ACS_HLINE, stdscrn_w // 2)
        # draw screen footer(2 lines)
        stdscr.hline(stdscrn_h - 3, 1, curses.ACS_HLINE, stdscrn_w // 2)
        stdscr.addstr(
            stdscrn_h - 2,
            1,
            truncate_long_string(
                (
                    "[x]: go back, [<arrow_up/down>]: scroll,"
                    "[s]: switch between <raw_status_rep> and <failure_status>"
                ),
                stdscrn_w,
            ),
        )
        stdscr.refresh()

        return DrawableArea(
            begin_y=2,
            begin_x=0,
            hlines=stdscrn_h - 4,
            hcols=stdscrn_w,
        )

    def _should_screen_update(self) -> bool:
        return int(time.time()) > self.last_updated

    def screen_control(self, stdscr: curses.window, *, refresh_internval=0.1):
        drawable_area = self._init_win(stdscr)
        pad = init_pad(self.PAD_HLINES, self.PAD_HCOLS)
        render_pad(pad, 0, 0, drawable_area)

        # NOTE: the cursor position below are all related to the pad upper-left corner
        current_screen = _ECU_DISPLAY.FAILURE_STATUS
        while True:
            # update screen if pad contents updated
            if self._should_screen_update():
                draw_pad(pad, self.contents[current_screen])

            key = pad.getch()

            old_cursor_y, old_cursor_x = pad.getyx()
            new_cursor_y, new_cursor_x = old_cursor_y, old_cursor_x
            if key in PAGE_SCROLL_KEYS:
                new_cursor_y, new_cursor_x = pad_scroll_handler(pad, key, drawable_area)
                # re-render the pad on screen if cursor position changed
                if (new_cursor_y, new_cursor_x) != (old_cursor_y, old_cursor_x):
                    render_pad(pad, new_cursor_y, new_cursor_x, drawable_area)
            elif key == curses.KEY_RESIZE:
                # re-render the whole screen on terminal size change
                stdscr.clear()
                drawable_area = self._init_win(stdscr)
                pad = init_pad(self.PAD_HLINES, self.PAD_HCOLS)
                render_pad(pad, 0, 0, drawable_area)
            elif key == ord("s") or key == ord("S"):
                current_screen = _ECU_DISPLAY.switch_content(current_screen)
                self.last_updated = 0  # to force redraw pad
            elif key == ord("x") or key == ord("X"):
                return  # return to upper caller
            time.sleep(refresh_internval)
