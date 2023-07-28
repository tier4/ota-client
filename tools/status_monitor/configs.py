class BasicConfig:
    # how many lines to go up/down when scroll
    SCROLL_LINES = 6
    # the interval to handle input and refresh the display,
    # should not be larger than 1, otherwise the window will be
    # very lag and broken on re-size.
    RENDER_INTERVAL = 0.1

    # ------ ECU status box config ------ #
    ECU_DISPLAY_BOX_HLINES = 12
    ECU_DISPLAY_BOX_HCOLS = 70


class KeyMapping:
    EXIT_ECU_STATUS_BOX_SUBWIN = ord("x")


config = BasicConfig()
key_mapping = KeyMapping()
