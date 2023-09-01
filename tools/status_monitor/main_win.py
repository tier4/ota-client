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
from typing import Callable, List

from .configs import config, key_mapping
from .ecu_status_box import ECUStatusDisplayBox
from .utils import (
    PAGE_SCROLL_KEYS,
    ASCII_NUM_MAPPING,
    page_scroll_key_handler,
    init_pad,
    reset_scr,
)


class MainScreen:
    MIN_WIN_SIZE = config.MIN_TERMINAL_SIZE
    DISPLAY_BOX_PER_ROW = config.MAINWIN_BOXES_PER_ROW
    DISPLAY_BOX_ROWS_MAX = config.MAINWIN_BOXES_ROW_MAX

    RENDER_INTERVAL = config.RENDER_INTERVAL

    def __init__(
        self,
        title: str,
        *,
        display_boxes_getter: Callable[..., List[ECUStatusDisplayBox]],
    ) -> None:
        self.title = title
        self.manual = (
            "<Num>: ECU#Num's raw status, <Alt+Num>: detailed failure info, <p>: pause"
        )
        self._display_getter = display_boxes_getter

    def _draw_ecu_status_to_pad(self, pad: curses.window) -> bool:
        """Draw contents onto the pad."""
        updated = False
        for count, ecu_status_display in enumerate(self._display_getter()):
            row, col = divmod(count, 2)
            updated |= ecu_status_display.render_status_box_pad(
                pad,
                begin_y=row * ECUStatusDisplayBox.DISPLAY_BOX_HLINES,
                begin_x=col * ECUStatusDisplayBox.DISPLAY_BOX_HCOLS,
            )
        if updated:
            pad.move(0, 0)  # move the cursor back to top left on update
        return updated

    def _window_session(self, stdscr: curses.window):
        _stdscrn_h, _stdscrn_w = stdscr.getmaxyx()
        _, begin_y, begin_x, hlines, hcols = reset_scr(
            stdscr, header=self.title, footer=self.manual
        )
        pad_hlines, pad_hcols = (
            config.PAD_ILNIT_HLINES,
            ECUStatusDisplayBox.DISPLAY_BOX_HCOLS * self.DISPLAY_BOX_PER_ROW,
        )
        pad = init_pad(pad_hlines, pad_hcols)

        last_cursor_y, last_cursor_x = pad.getyx()
        alt_key_pressed, cursor_moved = False, False
        while True:
            # try to update the contents at current location first
            if self._draw_ecu_status_to_pad(pad):
                pad_hlines = ECUStatusDisplayBox.DISPLAY_BOX_HLINES * len(
                    self._display_getter()
                )
                cursor_moved = True

            if cursor_moved:
                cursor_moved = False
                pad.refresh(
                    last_cursor_y,
                    last_cursor_x,
                    begin_y,
                    begin_x,
                    hlines,
                    hcols,
                )

            # wait for key_press event unblockingly
            key = pad.getch()
            if key in PAGE_SCROLL_KEYS:
                new_cursor_y, new_cursor_x = page_scroll_key_handler(
                    pad,
                    key,
                    last_cursor_y,
                    last_cursor_x,
                    scroll_len=config.SCROLL_LINES,
                    contents_area_max_y=pad_hlines,
                )
                cursor_moved = (
                    last_cursor_y != new_cursor_y or last_cursor_x != new_cursor_x
                )
                last_cursor_y, last_cursor_x = new_cursor_y, new_cursor_x

            elif key in ASCII_NUM_MAPPING:
                _ecu_status_display_boxes = self._display_getter()
                _ecu_idx = ASCII_NUM_MAPPING[key]
                if _ecu_idx >= len(_ecu_status_display_boxes):
                    continue

                if alt_key_pressed:
                    alt_key_pressed = False

                    # jump to failure_info subwin
                    _display = _ecu_status_display_boxes[_ecu_idx]
                    _display.failure_info_subwin_handler(stdscr)
                    return

                else:
                    # jump to raw_ecu_status subwin
                    _display = _ecu_status_display_boxes[_ecu_idx]
                    _display.raw_ecu_status_subwin_handler(stdscr)
                    return

            elif key == key_mapping.PAUSE:
                stdscr.addstr(_stdscrn_h - 1, 0, "Paused, press any key to resume.")
                stdscr.refresh()
                stdscr.getch()  # block until any key is pressed
                stdscr.addstr(_stdscrn_h - 1, 0, " " * (_stdscrn_w - 1))
                stdscr.refresh()

            elif key == key_mapping.ALT_OR_ESC:
                alt_key_pressed = True

            elif key == curses.KEY_RESIZE:
                stdscr.clear()
                return

            time.sleep(self.RENDER_INTERVAL)

    def main(self, stdscr: curses.window):
        """Main entry for the screen."""
        # NOTE: all values are for drawable area(exclude boarder, etc)
        curses.curs_set(0)
        stdscr.keypad(True)
        while True:
            # refuse to render the screen if terminal window is too small
            hlines, hcols = stdscr.getmaxyx()
            if (hlines, hcols) < self.MIN_WIN_SIZE:
                stdscr.addstr(0, 0, "Please enlarge the terminal window.")
                stdscr.refresh()
                stdscr.getkey()
                continue

            try:
                self._window_session(stdscr)
            except curses.error:
                continue  # do not crash on window's crash
