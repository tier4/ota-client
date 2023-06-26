import curses
from typing import List, NamedTuple, Tuple

__all__ = (
    "DrawableArea",
    "splitline_break_long_string",
    "truncate_long_string",
    "init_pad",
    "draw_pad",
    "render_pad",
    "PAGE_SCROLL_KEYS",
    "pad_scroll_handler",
)

PAGE_SCROLL_KEYS = (
    curses.KEY_DOWN,
    curses.KEY_UP,
    curses.KEY_LEFT,
    curses.KEY_SLEFT,
    curses.KEY_RIGHT,
    curses.KEY_SRIGHT,
    curses.KEY_SR,
    curses.KEY_SF,
    curses.KEY_PPAGE,
    curses.KEY_NPAGE,
    # reset the cursor location
    curses.KEY_HOME,
)


class DrawableArea(NamedTuple):
    begin_y: int
    begin_x: int
    hlines: int
    hcols: int


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


def truncate_long_string(_str: str, length: int, *, _truncated_indicator="...") -> str:
    _indicator_len = len(_truncated_indicator)
    if length <= _indicator_len:
        return "." * length
    if len(_str) < _indicator_len:
        return _str
    if len(_str) > length - _indicator_len:
        return _str[: length - _indicator_len] + _truncated_indicator
    return _str


def init_pad(hlines: int, hcols: int) -> curses.window:
    pad = curses.newpad(hlines, hcols)
    pad.keypad(True)
    pad.scrollok(True)
    pad.idlok(True)
    pad.nodelay(True)

    pad.erase()
    return pad


def draw_pad(pad: curses.window, contents: List[str], *, cleanup=True):
    """Redraw the whole pad and render it onto the stdscr."""
    _, pad_hcols = pad.getmaxyx()

    if cleanup:
        pad.clear()
    for y, line in enumerate(contents):
        pad.addstr(y, 0, truncate_long_string(line, pad_hcols))


def render_pad(
    pad: curses.window,
    pad_render_begin_y: int,
    pad_render_begin_x: int,
    drawable_area: DrawableArea,
):
    pad.move(pad_render_begin_y, pad_render_begin_x)
    pad.refresh(
        pad_render_begin_y,
        pad_render_begin_x,
        drawable_area.begin_y,
        drawable_area.begin_x,
        drawable_area.hlines,
        drawable_area.hcols,
    )


def pad_scroll_handler(
    pad: curses.window,
    key_pressed: int,
    drawable_area: DrawableArea,
    *,
    scroll_updown_line=1,
    scroll_page_line=8,
    scroll_leftright_col=5,
) -> Tuple[int, int]:
    """Return new pad render start point indicated by cursor."""
    pad_hlines, pad_hcols = pad.getmaxyx()

    new_cursor_y, new_cursor_x = pad.getyx()
    # scroll up_down
    if key_pressed == curses.KEY_DOWN or key_pressed == curses.KEY_SR:
        new_cursor_y = min(
            new_cursor_y + scroll_updown_line, pad_hlines - drawable_area.hlines
        )
    elif key_pressed == curses.KEY_UP or key_pressed == curses.KEY_SF:
        new_cursor_y = max(0, new_cursor_y - scroll_updown_line)
    if key_pressed == curses.KEY_PPAGE:
        new_cursor_y = max(0, new_cursor_y - scroll_page_line)
    elif key_pressed == curses.KEY_NPAGE:
        new_cursor_y = min(
            new_cursor_y + scroll_page_line, pad_hlines - drawable_area.hlines
        )
    # scroll left_right
    elif key_pressed == curses.KEY_LEFT or key_pressed == curses.KEY_SLEFT:
        new_cursor_x = min(
            new_cursor_x + scroll_leftright_col, pad_hcols - drawable_area.hcols
        )
    elif key_pressed == curses.KEY_RIGHT or key_pressed == curses.KEY_SRIGHT:
        new_cursor_x = max(0, new_cursor_x - scroll_leftright_col)
    elif key_pressed == curses.KEY_HOME:
        new_cursor_y, new_cursor_x = 0, 0

    return (new_cursor_y, new_cursor_x)
