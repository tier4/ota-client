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


class BasicConfig:
    # ------ Main window configs ------ #
    PAD_ILNIT_HLINES = 600
    MAINWIN_TITLE_HLINES = 1
    MAINWIN_MANUAL_HLINES = 2
    MAINWIN_LEFT_RIGHT_GAP = 1
    MIN_TERMINAL_SIZE = (10, 30)
    # NOTE: due to number keys' limitation, only 10 ECUs are allowed in total
    MAX_ECU_ALLOWED = 10
    MAINWIN_BOXES_PER_ROW = 2
    MAINWIN_BOXES_ROW_MAX = MAX_ECU_ALLOWED // MAINWIN_BOXES_PER_ROW

    # how many lines to go up/down when scroll
    SCROLL_LINES = 6
    # the interval to handle input and refresh the display,
    # should not be larger than 1, otherwise the window will be
    # very lag and broken on re-size.
    RENDER_INTERVAL = 0.01

    # ------ ECU status box configs ------ #
    ECU_DISPLAY_BOX_HLINES = 14
    ECU_DISPLAY_BOX_HCOLS = 60


class KeyMapping:
    EXIT_ECU_STATUS_BOX_SUBWIN = ord("x")
    PAUSE = ord("p")
    ALT_OR_ESC = 27


config = BasicConfig()
key_mapping = KeyMapping()
