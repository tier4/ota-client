import curses
import time
from typing import NamedTuple

from .ecu_status_box import ECUStatusDisplayBox


class WindowTuple(NamedTuple):
    window: curses.window
    # drawable area
    begin_y: int
    begin_x: int
    hlines: int
    hcols: int


class MainScreen:
    PAD_INIT_HLINES = 600
    TITLE_HLINES = 1
    MANUAL_HLINES = 2
    LEFT_RIGHT_GAP = 1

    MIN_WIN_SIZE = (10, 30)

    DISPLAY_BOX_PER_ROW = 2

    def __init__(self, title: str) -> None:
        self.title = title
        self._ecu_display: list[ECUStatusDisplayBox] = []

    def _init_mainwin(self, stdscr: curses.window) -> WindowTuple:
        stdscr.erase()
        stdscrn_h, stdscrn_w = stdscr.getmaxyx()

        # setup title
        if len(self.title) > stdscrn_w:
            stdscr.addstr(0, 0, self.title[:stdscrn_w])
        else:
            stdscr.addstr(0, (stdscrn_w - len(self.title)) // 2, self.title)
        stdscr.refresh()
        # TODO: setup manual

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

        # reserve space for boarder
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
        for count, ecu_status_display in enumerate(self._ecu_display):
            row, col = divmod(count, 2)
            updated |= ecu_status_display.update_display(
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
        begin_y: int,
        begin_x: int,
        hlines: int,
        hcols: int,
    ) -> bool:
        """Re-render the displayed area of pad according to cursor's position.

        This method is called when window scroll detected or pad is updated.
        """
        if cursor_y < 0:
            cursor_y = 0
        # TODO: lower bound

        pad.move(cursor_y, 0)
        pad.refresh(
            cursor_y,
            0,
            begin_y,
            begin_x,
            hlines,
            hcols,
        )
        return True

    _PAGE_SCROLL_KEYS = (
        curses.KEY_DOWN,
        curses.KEY_UP,
        curses.KEY_SR,
        curses.KEY_SF,
        curses.KEY_PPAGE,
        curses.KEY_NPAGE,
    )

    _ASCII_NUM_MAPPING = {ord(f"{i}"): i for i in range(9)}

    def _page_scroll_key_handler(
        self, pad: curses.window, key_pressed: int, *, hlines: int
    ) -> int:
        """

        Params:
            hlines: the display area's count of lines
        """
        cursor_y, _ = pad.getyx()
        pad_hlines, _ = pad.getmaxyx()

        new_cursor_y = cursor_y
        if key_pressed == curses.KEY_DOWN or key_pressed == curses.KEY_SR:
            new_cursor_y += 1
        elif key_pressed == curses.KEY_UP or key_pressed == curses.KEY_SF:
            new_cursor_y -= 1
        if key_pressed == curses.KEY_PPAGE:
            new_cursor_y -= hlines - 3
        elif key_pressed == curses.KEY_NPAGE:
            new_cursor_y += hlines - 3

        if new_cursor_y < 0:
            new_cursor_y = 0
        elif new_cursor_y > pad_hlines - hlines:
            new_cursor_y = pad_hlines - hlines

        return new_cursor_y

    def _window_session(self, stdscr: curses.window):
        _stdscrn_h, _stdscrn_w = stdscr.getmaxyx()
        _, begin_y, begin_x, hlines, hcols = self._init_mainwin(stdscr)
        pad = self._init_pad(
            ECUStatusDisplayBox.DISPLAY_BOX_HLINES * 30,
            ECUStatusDisplayBox.DISPLAY_BOX_HCOLS * 2,
        )

        last_cursor_y, _ = pad.getyx()
        while True:
            # try to update the contents at current location first
            if self._draw_ecu_status_to_pad(pad):
                current_cursor_y, _ = pad.getyx()
                self._render_pad(
                    pad,
                    current_cursor_y,
                    begin_y,
                    begin_x,
                    hlines,
                    hcols,
                )

            # wait for key_press event unblockingly
            key = pad.getch()
            if key in self._PAGE_SCROLL_KEYS:
                new_cursor_y = self._page_scroll_key_handler(pad, key, hlines=hlines)
                if last_cursor_y != new_cursor_y:
                    last_cursor_y = new_cursor_y
                    self._render_pad(
                        pad,
                        new_cursor_y,
                        begin_y,
                        begin_x,
                        hlines,
                        hcols,
                    )
            elif key in self._ASCII_NUM_MAPPING:
                # pop up a window to show the traceback
                _ecu_idx = self._ASCII_NUM_MAPPING[key]
                if _ecu_idx >= len(self._ecu_display):
                    continue

                _ecu_status_display = self._ecu_display[_ecu_idx]
                # this function will open a new screen and take over the control of
                # the whole terminal
                _ecu_status_display.draw_popup_for_failure_info(stdscr)
                return
            elif key == curses.KEY_RESIZE:
                stdscr.clear()
                return
            elif key == ord("p"):
                stdscr.addstr(_stdscrn_h - 1, 0, "Paused, press any key to resume.")
                stdscr.refresh()
                stdscr.getch()
                stdscr.addstr(_stdscrn_h - 1, 0, " " * (_stdscrn_w - 1))
                stdscr.refresh()
            else:
                time.sleep(0.1)

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

    def add_ecu_display(self, *ecu_display: ECUStatusDisplayBox):
        self._ecu_display.extend(ecu_display)
