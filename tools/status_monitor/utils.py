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
from typing import Callable, List, NamedTuple, Sequence, Tuple

from .configs import config, key_mapping


class FormatValue:
    KB = 1000
    MB = 1000**2
    GB = 1000**3

    @classmethod
    def bytes_count(cls, bytes_count: int) -> str:
        if bytes_count > cls.GB:
            return f"{bytes_count / cls.GB:,.2f}GB"
        elif bytes_count > cls.MB:
            return f"{bytes_count / cls.MB:,.2f}MB"
        elif bytes_count > cls.KB:
            return f"{bytes_count / cls.KB:,.2f}KB"
        return f"{bytes_count:,}B"

    @classmethod
    def count(cls, count: int) -> str:
        return f"{count:,}"


def splitline_break_long_string(_str: str, length: int) -> List[str]:
    # first split the line
    _input = _str.splitlines()
    _output = []
    # search through all lines and break up long line
    for line in _input:
        _cuts = len(line) // length + 1
        for _cut in range(_cuts):
            _output.append(line[_cut * length : (_cut + 1) * length])
    return _output


ContentsGetterFunc = Callable[[], Tuple[Sequence[str], int]]


class ScreenHandler:
    def __init__(
        self,
        stdscr: curses.window,
        title: str,
        contents_getter: ContentsGetterFunc,
    ) -> None:
        self.stdscr = stdscr
        self.title = title
        self.manual = "<x>: go back, <p>: pause, <Arrow_key/PN_UP/PN_DOWN>: navigate, <Home>: reset pos."
        self.contents_getter = contents_getter

    def subwin_handler(self):
        """Popup a sub window and render it with <contents>."""
        stdscr_h, stdscr_w = self.stdscr.getmaxyx()
        _, begin_y, begin_x, stdscrn_h, stdscrn_w = reset_scr(
            self.stdscr, header=self.title, footer=self.manual
        )
        pad = init_pad(config.PAD_ILNIT_HLINES, stdscrn_w)
        last_updated = 0

        last_cursor_y, last_cursor_x = pad.getyx()
        contents_area_y = 0
        cursor_moved = False
        while True:
            contents, _last_updated = self.contents_getter()
            # redraw the pad if contents updated, resized or cursor moved
            if _last_updated > last_updated:
                contents_area_y, _ = draw_pad(pad, stdscrn_h, stdscrn_w, contents)
                last_updated = _last_updated
                cursor_moved = True  # trigger a refresh on pad rendering

            if cursor_moved:
                # re-render the pad onto the window
                cursor_moved = False
                pad.refresh(
                    last_cursor_y,
                    last_cursor_x,
                    begin_y,
                    begin_x,
                    stdscrn_h,
                    stdscrn_w,
                )

            key = pad.getch()
            if key == key_mapping.EXIT_ECU_STATUS_BOX_SUBWIN:
                return

            if key in PAGE_SCROLL_KEYS:
                new_cursor_y, new_cursor_x = page_scroll_key_handler(
                    pad,
                    key,
                    last_cursor_y,
                    last_cursor_x,
                    scroll_len=config.SCROLL_LINES,
                    contents_area_max_y=contents_area_y,
                )
                cursor_moved = (
                    last_cursor_y != new_cursor_y or last_cursor_x != new_cursor_x
                )
                last_cursor_y, last_cursor_x = new_cursor_y, new_cursor_x

            elif key == key_mapping.PAUSE:
                self.stdscr.addstr(stdscr_h - 1, 0, "Paused, press any key to resume.")
                self.stdscr.refresh()
                self.stdscr.getch()  # block until any key is pressed
                self.stdscr.addstr(stdscr_h - 1, 0, " " * (stdscr_w - 1))
                self.stdscr.refresh()

            elif key == curses.KEY_RESIZE:
                # if terminal window becomes too small, clear and break out to main window
                if self.stdscr.getmaxyx() < config.MIN_TERMINAL_SIZE:
                    self.stdscr.clear()
                    return

                # fully reset the whole window and re-render everything
                _, begin_y, begin_x, stdscrn_h, stdscrn_w = reset_scr(
                    self.stdscr, header=self.title, footer=self.manual
                )
                pad = init_pad(config.PAD_ILNIT_HLINES, stdscrn_w)
                stdscr_h, stdscr_w = self.stdscr.getmaxyx()
                last_updated = 0  # force update contents

            time.sleep(config.RENDER_INTERVAL)


PAGE_SCROLL_KEYS = (
    curses.KEY_DOWN,
    curses.KEY_UP,
    curses.KEY_LEFT,
    curses.KEY_RIGHT,
    curses.KEY_SR,
    curses.KEY_SF,
    curses.KEY_PPAGE,
    curses.KEY_NPAGE,
    curses.KEY_HOME,
)

ASCII_NUM_MAPPING = {ord(f"{i}"): i for i in range(9)}


def page_scroll_key_handler(
    pad: curses.window,
    key_pressed: int,
    last_cursor_y: int,
    last_cursor_x: int,
    *,
    scroll_len: int,
    contents_area_max_y: int,
) -> Tuple[int, int]:
    """Logic for handling cursor position on pad when screen is UP/DOWN
        direction scrolling, move cursor to new position.

    Params:
        hlines: the display area's count of lines
        contents_area_max_y: the y boundary of populated area of pad

    Returns:
        A tuple of new cursor's position in (y, x).
    """

    new_cursor_y, new_cursor_x = last_cursor_y, last_cursor_x
    if key_pressed == curses.KEY_DOWN or key_pressed == curses.KEY_SR:
        new_cursor_y = min(new_cursor_y + 1, contents_area_max_y)
    elif key_pressed == curses.KEY_UP or key_pressed == curses.KEY_SF:
        new_cursor_y = max(0, new_cursor_y - 1)
    elif key_pressed == curses.KEY_PPAGE:
        new_cursor_y = max(0, new_cursor_y - scroll_len)
    elif key_pressed == curses.KEY_NPAGE:
        new_cursor_y = min(new_cursor_y + scroll_len, contents_area_max_y)
    elif key_pressed == curses.KEY_HOME:
        new_cursor_y = 0

    pad.move(new_cursor_y, new_cursor_x)
    return new_cursor_y, new_cursor_x


class WindowTuple(NamedTuple):
    window: curses.window
    # drawable area
    begin_y: int
    begin_x: int
    hlines: int
    hcols: int


def reset_scr(scr: curses.window, header: str, footer: str):
    scr.erase()
    stdscrn_h, stdscrn_w = scr.getmaxyx()

    # setup header of the screen
    if len(header) > stdscrn_w:
        scr.addstr(0, 0, header[:stdscrn_w])
    else:
        scr.addstr(0, (stdscrn_w - len(header)) // 2, header)

    # write manual
    scr.addstr(stdscrn_h - 2, config.MAINWIN_LEFT_RIGHT_GAP, footer[:stdscrn_w])
    scr.refresh()

    # create new window as main container
    begin_y, begin_x, hlines, hcols = (
        config.MAINWIN_TITLE_HLINES,
        config.MAINWIN_LEFT_RIGHT_GAP,
        stdscrn_h - config.MAINWIN_TITLE_HLINES - config.MAINWIN_MANUAL_HLINES,
        stdscrn_w - config.MAINWIN_LEFT_RIGHT_GAP,
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

    # return a main window for rendering
    # NOTE: reserve space for boarder
    return WindowTuple(main_win, begin_y + 1, begin_x + 1, hlines - 1, hcols - 1)


def init_pad(hlines: int, hcols: int):
    pad = curses.newpad(hlines, hcols)
    pad.keypad(True)
    pad.scrollok(True)
    pad.idlok(True)
    pad.nodelay(True)

    pad.erase()
    return pad


def draw_pad(pad: curses.window, win_h: int, win_w: int, contents: Sequence[str]):
    """Draw contents onto a pad within a window with size of <win_h>:<win_w>.

    NOTE: after drawing, render_pad function should be called to reflect the newly updated pad.
    """
    pad.clear()

    # preprocess contents to break up too-long lines
    _processed = []
    for line in contents:
        _processed.extend(splitline_break_long_string(line, length=win_w - 1))

    # draw line onto the pad
    line_idx = 0
    for line_idx, line_content in enumerate(_processed):
        pad.addstr(line_idx, 0, line_content)

    pad.move(0, 0)  # move the cursor back to original pos
    return line_idx, win_w - 1
