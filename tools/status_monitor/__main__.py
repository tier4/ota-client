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
        print("stop OTA status monitor")
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
