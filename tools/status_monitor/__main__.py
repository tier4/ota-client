import argparse
import curses
import sys

from .ecu_status_tracker import Tracker
from .main_win import MainScreen


def main(title: str, host: str, port: int):
    tracker = Tracker()
    tracker.start(host, port)

    main_scrn = MainScreen(title, display_boxes_getter=tracker.get_display_boxes)
    try:
        curses.wrapper(main_scrn.main)
    except KeyboardInterrupt:
        tracker.stop()
        sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="ota_status_monitor",
        description="CLI program for monitoring target ecu status",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", help="server listen ip", default="192.168.10.11")
    parser.add_argument("--port", help="server listen port", default=50051, type=int)
    parser.add_argument("--title", help="terminal title", default="OTA status monitor")

    args = parser.parse_args()
    main(args.title, args.host, args.port)
