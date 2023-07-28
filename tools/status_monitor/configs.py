class BasicConfig:
    # ------ Main window configs ------ #
    PAD_ILNIT_HLINES = 600
    MAINWIN_TITLE_HLINES = 1
    MAINWIN_MANUAL_HLINES = 2
    MAINWIN_LEFT_RIGHT_GAP = 1
    MIN_TERMINAL_SIZE = (10, 30)
    MAINWIN_BOXES_PER_ROW = 2

    # how many lines to go up/down when scroll
    SCROLL_LINES = 6
    # the interval to handle input and refresh the display,
    # should not be larger than 1, otherwise the window will be
    # very lag and broken on re-size.
    RENDER_INTERVAL = 0.1

    # ------ ECU status box configs ------ #
    ECU_DISPLAY_BOX_HLINES = 12
    ECU_DISPLAY_BOX_HCOLS = 70


class KeyMapping:
    EXIT_ECU_STATUS_BOX_SUBWIN = ord("x")
    PAUSE = ord("p")
    ALT_OR_ESC = 27


config = BasicConfig()
key_mapping = KeyMapping()
