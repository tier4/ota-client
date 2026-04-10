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
"""Shared test data constants for boot_control tests."""

from __future__ import annotations

from pathlib import Path

TEST_DATA_DIR = Path(__file__).parents[3] / "data"
GRUB_CFG = (TEST_DATA_DIR / "grub.cfg").read_text()
OTA_MANAGED_GRUB_CFG = (TEST_DATA_DIR / "ota_managed_grub.cfg").read_text()
OLD_GRUB_BOOT_CFG = (TEST_DATA_DIR / "old_grub_boot_grub.cfg").read_text()
GRUB_DEFAULT = (TEST_DATA_DIR / "grub_default").read_text()
GRUB_DEFAULT_WITH_BLACKLISTED = (
    TEST_DATA_DIR / "grub_default_with_blacklisted"
).read_text()
GRUB_DEFAULT_WITH_DUPLICATES = (
    TEST_DATA_DIR / "grub_default_with_duplicates"
).read_text()
EXPECTED_OTA_MANAGED_DEFAULT = (
    TEST_DATA_DIR / "expected_ota_managed_default"
).read_text()

BOOT_MENU_ENTRY = """\
menuentry 'Ubuntu' --class ubuntu --class gnu-linux --class gnu --class os $menuentry_id_option 'gnulinux-simple-186d206e-73e7-401c-9d8a-fad4390922f2' {
\trecordfail
\tload_video
\tgfxmode $linux_gfx_mode
\tinsmod gzio
\tif [ x$grub_platform = xxen ]; then insmod xzio; insmod lzopio; fi
\tinsmod part_gpt
\tinsmod ext2
\tsearch --no-floppy --fs-uuid --set=root 30cca32a-cbf5-4b05-8934-c6887a134162
\tlinux\t/vmlinuz-6.11.0-29-generic root=UUID=186d206e-73e7-401c-9d8a-fad4390922f2 ro  quiet splash $vt_handoff
\tinitrd\t/initrd.img-6.11.0-29-generic
}
"""

# NOTE: normally we will not see menuentry with nested braces,
#       but we need to support it.
BOOT_MENU_ENTRY_WITH_NESTED_BRACES = """\
menuentry 'Ubuntu' --class ubuntu --class gnu-linux --class gnu --class os $menuentry_id_option 'gnulinux-simple-186d206e-73e7-401c-9d8a-fad4390922f2' {
\trecordfail
\tload_video
\tgfxmode $linux_gfx_mode
\tinsmod gzio
\tif [ x$grub_platform = xxen ]; then insmod xzio; insmod lzopio; fi
\tsome command that uses braces {
\t\tsub command 1
\t\tsub command 2
\t}
\tinsmod part_gpt
\tinsmod ext2
\tsearch --no-floppy --fs-uuid --set=root 30cca32a-cbf5-4b05-8934-c6887a134162
\tlinux\t/vmlinuz-6.11.0-29-generic root=UUID=186d206e-73e7-401c-9d8a-fad4390922f2 ro  quiet splash $vt_handoff
\tinitrd\t/initrd.img-6.11.0-29-generic
}
"""
