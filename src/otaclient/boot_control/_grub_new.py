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

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import ClassVar, Generator

from typing_extensions import Self

from _otaclient_version import version
from otaclient_common._typing import StrEnum

from .configs import grub_new_cfg

VMLINUZ_SUFFIX = "vmlinuz-"
INITRD_SUFFIX = "initrd.img-"

GRUB_DEFAULT_OPTIONS = {
    "GRUB_TIMEOUT_STYLE": "menu",
    "GRUB_TIMEOUT": "0",
    "GRUB_DISABLE_SUBMENU": "y",
    "GRUB_DISABLE_OS_PROBER": "true",
    "GRUB_DISABLE_RECOVERY": "true",
    "GRUB_DEFAULT": "saved",
    "-GRUB_SAVEDEFAULT": "",
}
"""The required grub options for OTA grub boot control.

For entry name starts with `-`, it means the entry must be REMOVED.
"""


class GrubBootControllerError(Exception):
    """Grub boot controller internal used exception."""


@dataclass
class OTAManagedCfg:
    """Represents an OTA managed configuration file.

    OTA managed boot cfg files (e.g. grub.cfg) are generated and maintained by
    otaclient. Each file includes a header identifying it as OTA managed, and a
    footer containing metadata (otaclient version, grub version, and a checksum).

    Use `validate_managed_config` to check whether an existing config file is a
    valid OTA managed config. If validation passes, the config does not need to
    be regenerated. Use `export` to render the instance back into a file string.

    The file layout is:

    ```
    # OTAClient managed configuration file, DO NOT EDIT!
    # Manual edits to this file will NOT be preserved across OTA!
    <raw_contents>
    # ------ OTA managed metadata ------ #
    # otaclient_version: vx.xx.x
    # grub_version: x.xx
    # checksum: sha256:xxxxxxx
    # ------ End of OTA managed metadata ------ #
    ```
    """

    HEADER: ClassVar[str] = (
        "# OTAClient managed configuration file, DO NOT EDIT!\n"
        "# Manual edits to this file will NOT be preserved across OTA!\n"
    )
    FOOTER_HEAD: ClassVar[str] = "# ------ OTA managed metadata ------ #\n"
    FOOTER_TAIL: ClassVar[str] = "# ------ End of OTA managed metadata ------ #\n"

    raw_contents: str
    grub_version: str
    checksum: str
    otaclient_version: str = version

    def __post_init__(self) -> None:
        self.raw_contents = self.raw_contents.strip()

    @staticmethod
    def _parse_footer(_footer: str) -> dict[str, str]:
        _res: dict[str, str] = {}
        for _line in _footer.splitlines():
            if _line.startswith("# ------"):
                continue
            _line = _line.strip()
            if not _line.startswith("#"):
                raise ValueError

            _line = _line.lstrip("#").strip()
            _k, _, _v = _line.partition(":")
            _res[_k.strip()] = _v.strip()
        return _res

    @classmethod
    def validate_managed_config(cls, _in: str) -> Self | None:
        if not _in.startswith(cls.HEADER):
            return
        _in = _in[len(cls.HEADER) :]

        _footer_start, _footer_end = (
            _in.find(cls.FOOTER_HEAD),
            _in.find(cls.FOOTER_TAIL),
        )
        if _footer_start < 0 or _footer_end < 0:
            return

        try:
            metadata = cls._parse_footer(
                _in[_footer_start : _footer_end + len(cls.FOOTER_TAIL)]
            )
        except ValueError:
            return

        checksum = metadata.get("checksum")
        grub_version = metadata.get("grub_version")
        otaclient_version = metadata.get("otaclient_version")
        if not (checksum and grub_version and otaclient_version):
            return

        content = _in[:_footer_start].strip()
        _algorithm, _expected_digest = checksum.split(":", 1)
        try:
            _hasher = hashlib.new(_algorithm, content.encode())
        except ValueError:
            return

        if _hasher.hexdigest() != _expected_digest:
            return

        return cls(
            raw_contents=content,
            grub_version=grub_version,
            checksum=checksum,
            otaclient_version=otaclient_version,
        )

    def export(self, *, hash_algorithm: str = "sha256") -> str:
        _content = self.raw_contents
        _digest = hashlib.new(hash_algorithm, _content.encode()).hexdigest()
        _footer = (
            f"{self.FOOTER_HEAD}"
            f"# otaclient_version: {self.otaclient_version}\n"
            f"# grub_version: {self.grub_version}\n"
            f"# checksum: {hash_algorithm}:{_digest}\n"
            f"{self.FOOTER_TAIL}"
        )
        return f"{self.HEADER}{_content}{_footer}"


class OTASlotSuffix(StrEnum):
    slot_a = "_a"
    slot_b = "_b"


class OTASlotBootID(StrEnum):
    slot_a = f"{grub_new_cfg.OTA_BOOT_SLOT}_a"
    slot_b = f"{grub_new_cfg.OTA_BOOT_SLOT}_b"

    def get_suffix(self) -> str:
        return f"_{self.rsplit('_', 1)[-1]}"


MENUENTRY_HEAD_PA = re.compile(r"^\s*menuentry\s", re.MULTILINE)
LINUX_PA_MULTILINE = re.compile(r"^\s*linux\s*(?P<linux_fpath>[^\s]+)", re.MULTILINE)
LINUX_VERSION_PA = re.compile(r"vmlinuz-(?P<ver>[\.\w-]+)$")
INITRD_PA_MULTILINE = re.compile(r"^\s*initrd\s*(?P<initrd_fpath>[^\s]+)", re.MULTILINE)
MENUENTRY_TITLE_PA = re.compile(
    r"""^\s*menuentry\s+(?P<entry_title>(?:"[^"]*"|'[^']*'|[^\s]+))"""
)
MENUENTRY_ID_PA = re.compile(
    r"""^\s*menuentry.*?\$menuentry_id_option\s+(?P<entry_id>(?:"[^"]*"|'[^']*'|[^\s]+))"""
)


def _iter_menuentries(_in: str) -> Generator[str]:
    """Extract all menuentry blocks from grub-mkconfig output.

    Uses brace-depth counting to correctly handle nested {} blocks
    (e.g. if/fi statements with braces) within menuentry bodies.

    Raises:
        ValueError: If a menuentry has no opening brace or unclosed braces.
    """
    for ma in MENUENTRY_HEAD_PA.finditer(_in):
        # find the opening brace after "menuentry ..."
        _brace_start = _in.find("{", ma.start())
        if _brace_start < 0:
            raise ValueError(f"menuentry at offset {ma.start()} has no opening brace")

        _depth = 0
        for i in range(_brace_start, len(_in)):
            if _in[i] == "{":
                _depth += 1
            elif _in[i] == "}":
                _depth -= 1
                if _depth == 0:
                    yield _in[ma.start() : i + 1]
                    break
        else:
            raise ValueError(f"menuentry at offset {ma.start()} has unclosed braces")


@dataclass
class _BootMenuEntry:
    raw_entry: str
    slot_boot_id: OTASlotBootID
    kernel_ver: str

    @classmethod
    def _find_menuentry(cls, _in: str, *, kernel_ver: str) -> str:
        """Find a raw menuentry block from the input with matching kernel version.

        Iterates all menuentry blocks in the grub-mkconfig output, skipping
        recovery entries and entries without a valid linux directive, and
        returns the first entry whose kernel version matches.

        Args:
            _in (str): The full grub-mkconfig output string.
            kernel_ver (str): The kernel version to match (e.g. "5.19.0-50-generic").

        Returns:
            str: The raw menuentry block string.

        Raises:
            ValueError: If no matching non-recovery menuentry is found.
        """
        for _found in _iter_menuentries(_in):
            _linux_dir_ma = LINUX_PA_MULTILINE.search(_found)
            if not _linux_dir_ma:
                continue  # not a linux boot menuentry

            _linux_dir = _linux_dir_ma.group()
            if _linux_dir.find("recovery") >= 0:
                continue  # skip recovery entry

            _linux_ver_ma = LINUX_VERSION_PA.search(_linux_dir)
            if not _linux_ver_ma:
                continue  # invalid linux entry

            if _linux_ver_ma.group("ver") != kernel_ver:
                continue  # not the entry we are looking for
            return _found

        raise ValueError(f"failed to find menuentry for kernel version {kernel_ver!r}")

    @classmethod
    def _fixup_menuentry(cls, _entry: str, *, slot_boot_id: OTASlotBootID) -> str:
        """Fix up a raw menuentry block for OTA slot boot.

        Performs the following rewrites on the menuentry block:
            1. Replaces the menuentry title and id with `slot_boot_id`.
            2. Prefixes the linux and initrd file paths with `/<slot_boot_id>/`.

        Args:
            _entry (str): The raw menuentry block string to fix up.
            slot_boot_id (OTASlotBootID): The OTA slot boot identifier to apply.

        Returns:
            str: The rewritten menuentry block string.
        """
        _entry = MENUENTRY_TITLE_PA.sub(
            lambda ma: ma.group().replace(ma.group("entry_title"), slot_boot_id, 1),
            _entry,
        )
        _entry = MENUENTRY_ID_PA.sub(
            lambda ma: ma.group().replace(ma.group("entry_id"), slot_boot_id, 1),
            _entry,
        )

        _entry = LINUX_PA_MULTILINE.sub(
            lambda ma: ma.group().replace(
                ma.group("linux_fpath"), f"/{slot_boot_id}{ma.group('linux_fpath')}", 1
            ),
            _entry,
        )
        _entry = INITRD_PA_MULTILINE.sub(
            lambda ma: ma.group().replace(
                ma.group("initrd_fpath"),
                f"/{slot_boot_id}{ma.group('initrd_fpath')}",
                1,
            ),
            _entry,
        )
        return _entry

    @classmethod
    def generate_menuentry(
        cls, _in: str, *, slot_boot_id: OTASlotBootID, kernel_ver: str
    ) -> Self:
        """Generate an OTA-ready menuentry from grub-mkconfig output.

        Finds the menuentry block matching `kernel_ver`, then rewrites its
        title, id, and file paths for the given `slot_boot_id`.

        Args:
            _in (str): The full grub-mkconfig output string.
            slot_boot_id (OTASlotBootID): The OTA slot boot identifier to apply.
            kernel_ver (str): The kernel version to match (e.g. "5.19.0-50-generic").

        Returns:
            _BootMenuEntry: A `_BootMenuEntry` instance with the fixed-up menuentry.

        Raises:
            ValueError: If no matching non-recovery menuentry is found.
        """
        # NOTE: for specific kernel version, we should have exactly one
        #       boot entry(non-recovery entry) for it.
        return cls(
            raw_entry=cls._fixup_menuentry(
                cls._find_menuentry(_in, kernel_ver=kernel_ver),
                slot_boot_id=slot_boot_id,
            ),
            slot_boot_id=slot_boot_id,
            kernel_ver=kernel_ver,
        )


@dataclass
class _SlotInfo:
    slot_id: OTASlotBootID
    dev: str
    uuid: str


@dataclass
class _ABPartition:
    """
    Supported partition layout:
        (system boots with UEFI)
        /dev/<main_boot_disk>
            - p1: /boot/uefi
            - p2: /boot
            - p3: ota-slot_a
            - p4: ota-slot_b
    """

    boot_partition: _SlotInfo
    slot_a: _SlotInfo
    slot_b: _SlotInfo
    current_slot: OTASlotBootID

    # fmt: off
    @property
    def current_slot_info(self) -> _SlotInfo:
        return self.slot_a if self.current_slot == OTASlotBootID.slot_a else self.slot_b

    @property
    def standby_slot_info(self) -> _SlotInfo:
        return self.slot_b if self.current_slot == OTASlotBootID.slot_a else self.slot_a

    @property
    def standby_slot(self) -> OTASlotBootID:
        return OTASlotBootID.slot_b if self.current_slot == OTASlotBootID.slot_a else OTASlotBootID.slot_a
    # fmt: on

