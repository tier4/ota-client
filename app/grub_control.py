import re
import subprocess
import shlex
import tempfile
import shutil
from pathlib import Path

from ota_error import OtaErrorUnrecoverable
import configs as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class GrubCfgParser:
    def __init__(self, grub_cfg):
        self._grub_cfg = grub_cfg

    def parse(self):
        menu, _ = self._parse(self._grub_cfg, False)
        return menu

    def _parse(self, cfg, in_submenu):
        """
        returns [
          {}, # menuentry
          {}, # menuentry
          [ # submenu
            {}, # menuentry
            {}, # menuentry
          ],
          [ # submenu
            {}, # menuentry
          ],
          {}, # menuentry
        """
        pos = 0
        braces = []
        menus = []
        while True:
            m = re.search(r"(menuentry\s.*{|submenu\s.*{|})", cfg[pos:])
            if m:
                if m.group(1).startswith("menuentry"):
                    braces.append(pos + m.span()[0])
                pos += m.span()[1]
                if m.group(1).startswith("submenu"):
                    menu, sub_pos = self._parse(cfg[pos:], True)
                    pos += sub_pos
                    menus.append(menu)
                if m.group(1) == "}":
                    try:
                        begin = braces.pop()
                        # parse [begin:end]
                        linux = re.search(r"[ \t]*linux\s+/vmlinuz.*", cfg[begin:pos])
                        initrd = re.search(r"[ \t]*initrd\s+/initrd.*", cfg[begin:pos])
                        entry = {}
                        entry["entry"] = cfg[begin:pos]
                        entry["linux"] = None if linux is None else linux.group(0)
                        entry["initrd"] = None if initrd is None else initrd.group(0)
                        menus.append(entry)
                    except IndexError:
                        if in_submenu:
                            return menus, pos
                        else:
                            pass  # just ignore
            else:
                return menus, pos


class GrubControl:
    GRUB_CFG_FILE = cfg.GRUB_CFG_FILE  # Path("/boot/grub/grub.cfg")
    CUSTOM_CFG_FILE = cfg.CUSTOM_CFG_FILE  # Path("/boot/grub/custom.cfg")
    FSTAB_FILE = cfg.FSTAB_FILE  # Path("/etc/fstab")
    DEFAULT_GRUB_FILE = cfg.DEFAULT_GRUB_FILE  # Path("/etc/default/grub")

    def __init__(self):
        self._grub_cfg_file = GrubControl.GRUB_CFG_FILE
        self._custom_cfg_file = GrubControl.CUSTOM_CFG_FILE
        self._fstab_file = GrubControl.FSTAB_FILE
        self._default_grub_file = GrubControl.DEFAULT_GRUB_FILE

    def create_custom_cfg_and_reboot(
        self, standby_device, vmlinuz_file, initrd_img_file
    ):
        # pick up booted menuentry to create custom.cfg
        booted_menuentry = self._get_booted_menuentry()
        # modify booted menuentry for custom.cfg
        custom_cfg = self._update_menuentry(
            booted_menuentry, standby_device, vmlinuz_file, initrd_img_file
        )
        # store custom.cfg
        with tempfile.NamedTemporaryFile("w", delete=False, prefix=__name__) as f:
            temp_name = f.name
            f.write(custom_cfg)
        # should not be called within the NamedTemporaryFile context
        shutil.move(temp_name, self._custom_cfg_file)

        # grub-reboot
        self._grub_reboot()
        # reboot
        self.reboot()

    def update_grub_cfg(self, device, default_vmlinuz):
        """
        This function updates /etc/default/grub and and /boot/grub/grub.cfg to
        boot from device with default_vmlinuz kernel.
        """
        # 1. update /etc/default/grub with:
        #    GRUB_TIMEOUT_STYLE=menu
        #    GRUB_TIMEOUT=10
        #    GRUB_DISABLE_SUBMENU=y
        patterns = {
            "GRUB_TIMEOUT_STYLE=": "menu",
            "GRUB_TIMEOUT=": "10",
            "GRUB_DISABLE_SUBMENU=": "y",
        }
        self._update_default_grub(patterns)

        # 2. create temporary grub.cfg with grub-mkconfig
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / "grub.cfg"
            self._grub_mkconfig_cmd(file_name)

            # 3. count a number of default_vmlinuz entry from temporary grub.cfg
            menus = GrubCfgParser(open(file_name).read()).parse()
            uuid = self._get_uuid(device)
            number = self._count_menuentry(menus, uuid, default_vmlinuz)
            if number < 0:
                raise OtaErrorUnrecoverable(
                    f"menuentry not found: UUID={uuid}, vmlinuz={default_vmlinuz}, menus={menus}"
                )

        # 4. update /etc/default/grub with:
        #    GRUB_DEFAULT={number}
        patterns = {
            "GRUB_DEFAULT=": str(number),
        }
        self._update_default_grub(patterns)

        # 5. update /boot/grub/grub.cfg with grub-mkconfig
        self._grub_mkconfig_cmd(self._grub_cfg_file)

    def reboot(self):
        cmd = f"reboot"
        return subprocess.check_output(shlex.split(cmd))

    def update_fstab(self, mount_point, active_device, standby_device):
        """
        NOTE:
        fstab operation might not be a part of grub, but uuid operation is only
        done in this class, so this function is implemented in this class.
        """
        active_uuid = self._get_uuid(active_device)
        standby_uuid = self._get_uuid(standby_device)

        fstab_active = open(self._fstab_file).readlines()  # active partition fstab

        # standby partition fstab (to be merged)
        fstab_standby = open(mount_point / "etc" / "fstab").readlines()

        fstab_standby_dict = {}
        for line in fstab_standby:
            if not line.startswith("#") and not line.startswith("\n"):
                path = line.split()[1]
                fstab_standby_dict[path] = line

        # merge entries
        merged = []
        for line in fstab_active:
            if line.startswith("#") or line.startswith("\n"):
                merged.append(line)
            else:
                path = line.split()[1]
                if path in fstab_standby_dict.keys():
                    merged.append(fstab_standby_dict[path])
                    del fstab_standby_dict[path]
                else:
                    merged.append(line.replace(active_uuid, standby_uuid))
        for v in fstab_standby_dict.values():
            merged.append(v)

        with open(mount_point / "etc" / "fstab", "w") as f:
            f.writelines(merged)

    def get_booted_vmlinuz_and_uuid(self):
        cmdline = self._get_cmdline()
        m = re.match(r"BOOT_IMAGE=/(\S*)\s*root=UUID=(\S*)", cmdline)
        vmlinuz = m.group(1)
        uuid = m.group(2)
        return vmlinuz, uuid

    """ private from here """

    def _find_menuentry(self, menus, uuid, vmlinuz):
        # NOTE: Only UUID sepcifier is supported.
        for menu in menus:
            if type(menu) is dict:
                m = re.match(r"\s*linux\s+/(\S*)\s+root=UUID=(\S+)\s*", menu["linux"])
                if m and m.group(1) == vmlinuz and m.group(2) == uuid:
                    return menu
            elif type(menu) is list:
                ret = self._find_menuentry(menu, uuid, vmlinuz)
                if ret is not None:
                    return ret
        return None

    def _count_menuentry(self, menus, uuid, vmlinuz):
        # NOTE: Only UUID sepcifier is supported.
        for i, menu in enumerate(menus):
            if type(menu) is dict:
                m = re.match(r"\s*linux\s+/(\S*)\s+root=UUID=(\S+)\s*", menu["linux"])
                if m and m.group(1) == vmlinuz and m.group(2) == uuid:
                    return i
            else:
                raise OtaErrorUnrecoverable("GRUB_DISABLE_SUBMENU=y should be set")
        return -1

    def _get_booted_menuentry(self):
        """
        find grub.cfg menuentry from uuid and BOOT_IMAGE specified by /proc/cmdline
        """
        # grub.cfg
        grub_cfg = open(self._grub_cfg_file).read()
        menus = GrubCfgParser(grub_cfg).parse()

        # booted vmlinuz and initrd.img
        vmlinuz, uuid = self.get_booted_vmlinuz_and_uuid()
        menuentry = self._find_menuentry(menus, uuid, vmlinuz)
        return f"{menuentry['entry']}\n"  # append newline

    def _update_menuentry(self, menuentry, standby_device, vmlinuz, initrd_img):
        uuid = self._get_uuid(standby_device)
        # NOTE: Only UUID sepcifier is supported.
        replaced = re.sub(
            r"(.*\slinux\s+/)\S*(\s+root=UUID=)\S*(\s.*)",
            rf"\g<1>{vmlinuz}\g<2>{uuid}\g<3>",  # NOTE: use \g<1> instead of \1
            menuentry,
        )
        replaced = re.sub(
            r"(.*\sinitrd\s+/)\S*(\s.*)",
            rf"\g<1>{initrd_img}\g<2>",
            replaced,
        )
        return replaced

    def _update_default_grub(self, patterns):
        lines = open(self._default_grub_file).readlines()
        patterns_found = {}
        updated = []
        for line in lines:
            found = False
            for k, v in patterns.items():
                m = re.match(rf"{k}.*", line)
                if m:
                    found = True
                    updated.append(f"{k}{v}\n")
                    patterns_found[k] = v
                    break
            if not found:
                updated.append(line)

        deltas = dict(patterns.items() - patterns_found.items())
        for k, v in deltas.items():
            updated.append(f"{k}{v}\n")

        with open(self._default_grub_file, "w") as f:
            f.writelines(updated)

    def _grub_reboot_cmd(self, num):
        cmd = f"grub-reboot {num}"
        return subprocess.check_output(shlex.split(cmd))

    def _grub_reboot(self):
        # count grub menu entry number
        grub_cfg = open(self._grub_cfg_file).read()
        menus = GrubCfgParser(grub_cfg).parse()
        self._grub_reboot_cmd(len(menus))

    def _get_uuid(self, device):
        cmd = f"lsblk -in -o UUID /dev/{device}"
        return subprocess.check_output(shlex.split(cmd)).decode().strip()

    def _get_cmdline(self):
        return open("/proc/cmdline").read()

    def _grub_mkconfig_cmd(self, outfile):
        cmd = f"grub-mkconfig -o {outfile}"
        return subprocess.check_output(shlex.split(cmd)).decode().strip()
