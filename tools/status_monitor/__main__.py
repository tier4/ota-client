import argparse
import curses
import sys

from .ecu_status_tracker import TrackerThread
from .window import MainScreen


def main(title: str, host: str, port: int):
    tracker = TrackerThread()
    tracker.start(host, port)

    main_scrn = MainScreen(title)
    main_scrn.add_ecu_display(*tracker.ecu_status_display.values())
    try:
        curses.wrapper(main_scrn.main)
    except KeyboardInterrupt:
        tracker.stop()
        sys.exit(0)


if __name__ == "__main__":
    main("OTA status monitor", "10.0.1.10", 50051)
