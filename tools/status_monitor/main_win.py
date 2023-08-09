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
from typing import Callable, List, NamedTuple, Tuple

from .configs import config, key_mapping
from .ecu_status_box import ECUStatusDisplayBox


class WindowTuple(NamedTuple):
    window: curses.window
    # drawable area
    begin_y: int
    begin_x: int
    hlines: int
    hcols: int


class MainScreen:
    PAD_INIT_HLINES = config.PAD_ILNIT_HLINES
    TITLE_HLINES = config.MAINWIN_TITLE_HLINES
    MANUAL_HLINES = config.MAINWIN_MANUAL_HLINES
    LEFT_RIGHT_GAP = config.MAINWIN_LEFT_RIGHT_GAP

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
        self._display_getter = display_boxes_getter

    def _init_mainwin(self, stdscr: curses.window) -> WindowTuple:
        stdscr.erase()
        stdscrn_h, stdscrn_w = stdscr.getmaxyx()

        # setup title
        if len(self.title) > stdscrn_w:
            stdscr.addstr(0, 0, self.title[:stdscrn_w])
        else:
            stdscr.addstr(0, (stdscrn_w - len(self.title)) // 2, self.title)

        # write manual
        manual = "press <num> for ECU status, <ALT_num> for failure info, 'p' for pause"
        stdscr.addstr(stdscrn_h - 2, self.LEFT_RIGHT_GAP, manual[:stdscrn_w])
        stdscr.refresh()

        # create new window as main container
        begin_y, begin_x, hlines, hcols = (
            self.TITLE_HLINES,
            self.LEFT_RIGHT_GAP,
            stdscrn_h - self.TITLE_HLINES - self.MANUAL_HLINES,
            stdscrn_w - self.LEFT_RIGHT_GAP,
        )
        main_win = curses.newwin(
            hlines,
            hcols,
            begin_y,
            begin_x,
        )
        main_win.erase()
        main_win.border()
        main_win.refresh()

        # NOTE: reserve space for boarder
        return WindowTuple(main_win, begin_y + 1, begin_x + 1, hlines - 1, hcols - 1)

    def _init_pad(self, hlines: int, hcols: int) -> curses.window:
        pad = curses.newpad(hlines, hcols)
        pad.keypad(True)
        pad.scrollok(True)
        pad.idlok(True)
        pad.nodelay(True)

        pad.erase()
        return pad

    def _draw_ecu_status_to_pad(self, pad: curses.window) -> bool:
        """Draw contents onto the pad."""
        updated = False
        old_cursor_y, old_cursor_x = pad.getyx()
        for count, ecu_status_display in enumerate(self._display_getter()):
            row, col = divmod(count, 2)
            updated |= ecu_status_display.update_status_box_pad(
                pad,
                begin_y=row * ECUStatusDisplayBox.DISPLAY_BOX_HLINES,
                begin_x=col * ECUStatusDisplayBox.DISPLAY_BOX_HCOLS,
            )
        # move the cursor back to original location after draw
        pad.move(old_cursor_y, old_cursor_x)
        return updated

    def _render_pad(
        self,
        pad: curses.window,
        cursor_y: int,
        cursor_x: int,
        begin_y: int,
        begin_x: int,
        hlines: int,
        hcols: int,
    ) -> bool:
        """Re-render the displayed area of pad according to cursor's position.

        This method is called when window scroll detected or pad is updated.
        """
        pad.move(cursor_y, 0)
        pad.refresh(cursor_y, cursor_x, begin_y, begin_x, hlines, hcols)
        return True

    _PAGE_SCROLL_KEYS = (
        curses.KEY_DOWN,
        curses.KEY_UP,
        curses.KEY_LEFT,
        curses.KEY_RIGHT,
        curses.KEY_SR,
        curses.KEY_SF,
        curses.KEY_PPAGE,
        curses.KEY_NPAGE,
    )

    # NOTE: currently we only supports up to 10 ECUs in total!
    _ASCII_NUM_MAPPING = {ord(f"{i}"): i for i in range(9)}

    def _page_scroll_key_handler(
        self, pad: curses.window, key_pressed: int, *, hlines: int
    ) -> Tuple[int, int]:
        """

        Params:
            hlines: the display area's count of lines
        """
        pad_hlines, pad_hcols = pad.getmaxyx()

        new_cursor_y, new_cursor_x = pad.getyx()
        if key_pressed == curses.KEY_DOWN or key_pressed == curses.KEY_SR:
            new_cursor_y = min(new_cursor_y + 1, pad_hlines - hlines)
        elif key_pressed == curses.KEY_UP or key_pressed == curses.KEY_SF:
            new_cursor_y = max(0, new_cursor_y - 1)
        if key_pressed == curses.KEY_PPAGE:
            new_cursor_y = max(0, new_cursor_y - 8)
        elif key_pressed == curses.KEY_NPAGE:
            new_cursor_y = min(new_cursor_y + 8, pad_hlines - hlines)

        return new_cursor_y, new_cursor_x

    def _window_session(self, stdscr: curses.window):
        _stdscrn_h, _stdscrn_w = stdscr.getmaxyx()
        _, begin_y, begin_x, hlines, hcols = self._init_mainwin(stdscr)
        pad = self._init_pad(
            ECUStatusDisplayBox.DISPLAY_BOX_HLINES * self.DISPLAY_BOX_ROWS_MAX,
            ECUStatusDisplayBox.DISPLAY_BOX_HCOLS * self.DISPLAY_BOX_PER_ROW,
        )

        last_cursor_y, last_cursor_x = pad.getyx()

        alt_key_pressed = False
        while True:
            # try to update the contents at current location first
            if self._draw_ecu_status_to_pad(pad):
                current_cursor_y, current_cursor_x = pad.getyx()
                self._render_pad(
                    pad,
                    current_cursor_y,
                    current_cursor_x,
                    begin_y,
                    begin_x,
                    hlines,
                    hcols,
                )

            # wait for key_press event unblockingly
            key = pad.getch()
            if alt_key_pressed:
                alt_key_pressed = False
                if key in self._ASCII_NUM_MAPPING:
                    _ecu_status_display_boxes = self._display_getter()
                    _ecu_idx = self._ASCII_NUM_MAPPING[key]
                    if _ecu_idx >= len(_ecu_status_display_boxes):
                        continue

                    # open a new subwindow and hand over the control to this windows' handler
                    _display = _ecu_status_display_boxes[_ecu_idx]
                    _display.failure_info_subwin_handler(stdscr)
                    return

            elif key in self._PAGE_SCROLL_KEYS:
                new_cursor_y, new_cursor_x = self._page_scroll_key_handler(
                    pad, key, hlines=hlines
                )
                if last_cursor_y != new_cursor_y or last_cursor_x != new_cursor_x:
                    last_cursor_y, last_cursor_x = new_cursor_y, new_cursor_x
                    self._render_pad(
                        pad,
                        new_cursor_y,
                        new_cursor_x,
                        begin_y,
                        begin_x,
                        hlines,
                        hcols,
                    )
            elif key in self._ASCII_NUM_MAPPING:
                _ecu_status_display_boxes = self._display_getter()
                _ecu_idx = self._ASCII_NUM_MAPPING[key]
                if _ecu_idx >= len(_ecu_status_display_boxes):
                    continue

                # open a new subwindow and hand over the control to this windows' handler
                _display = _ecu_status_display_boxes[_ecu_idx]
                _display.raw_ecu_status_subwin_handler(stdscr)
                return

            elif key == curses.KEY_RESIZE:
                stdscr.clear()
                return

            elif key == key_mapping.PAUSE:
                stdscr.addstr(_stdscrn_h - 1, 0, "Paused, press any key to resume.")
                stdscr.refresh()
                stdscr.getch()
                stdscr.addstr(_stdscrn_h - 1, 0, " " * (_stdscrn_w - 1))
                stdscr.refresh()

            elif key == key_mapping.ALT_OR_ESC:
                alt_key_pressed = True
                continue

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

            self._window_session(stdscr)
