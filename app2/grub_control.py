import re
import subprocess
import shlex
import tempfile
import shutil
from pathlib import Path


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
    GRUB_CFG_FILE = Path("/boot/grub/grub.cfg")
    CUSTOM_CFG_FILE = Path("/boot/grub/custom.cfg")
    FSTAB_FILE = Path("/etc/fstab")

    def __init__(self):
        self._grub_cfg_file = GrubControl.GRUB_CFG_FILE
        self._custom_cfg_file = GrubControl.CUSTOM_CFG_FILE
        self._fstab_file = GrubControl.FSTAB_FILE

    def create_custom_cfg_and_reboot(
        self, active_device, standby_device, vmlinuz_file, initrd_img_file
    ):
        # pick up booted menuentry to create custom.cfg
        booted_menuentry = self._get_booted_menuentry(active_device)
        # modify booted menuentry for custom.cfg
        custom_cfg = self._update_menuentry(
            booted_menuentry, standby_device, vmlinuz_file, initrd_img_file
        )
        # store custom.cfg
        with tempfile.NamedTemporaryFile("w", delete=False, prefix=__name__) as f:
            f.write(custom_cfg)
            shutil.move(f.name, self._custom_cfg_file)

        # grub-reboot
        self._grub_reboot()
        # reboot
        self.reboot()

    def update_grub_cfg():
        # TODO:
        # update /etc/default/grub w/ GRUB_DISABLE_SUBMENU
        # grub-mkconfig temporally
        # count menuentry number
        # grub-mkconfig w/ the number counted
        pass

    def reboot():
        cmd = f"reboot"
        return subprocess.check_output(shlex.split(cmd))

    def update_fstab(self, mount_point, active_device, standby_device):
        active_uuid = self._get_uuid(active_device)
        standby_uuid = self._get_uuid(standby_device)
        lines = open(self._fstab_file).readlines()

        updated_fstab = []
        for line in lines:
            if line.startswith("#"):
                updated_fstab.append(line)
                continue
            line_split = line.split()
            if line_split[1] == "/":
                # TODO
                # replace UUID=... or device file
                # if line_split[0].find("UUID="):
                #     line.replace()
                # elif line_split[0].find(device):
                #     line.replace()
                # ...
                pass
            else:
                updated_fstab.append(line)

        # NOTE: For pytest, `self._fstab_file.relative_to("/")` should not be used
        # since FSTAB_FILE is mocked and mount_point is also mocked.
        with open(mount_point / "etc" / "fstab", "w") as f:
            f.writelines(updated_fstab)

    """ private from here """

    def _find_menuentry(self, menus, uuid, device, vmlinuz):
        for menu in menus:
            if type(menu) is dict:
                if menu["linux"].find(vmlinuz) >= 0 and (
                    menu["linux"].find(uuid) >= 0 or menu["linux"].find(device) >= 0
                ):
                    return menu
            elif type(menu) is list:
                ret = self._find_menuentry(menu, uuid, device, vmlinuz)
                if ret is not None:
                    return ret
        return None

    def _get_booted_menuentry(self, device):
        """
        find grub.cfg menuentry from active device or uuid and BOOT_IMAGE specified by /proc/cmdline
        """
        # grub.cfg
        grub_cfg = open(self._grub_cfg_file).read()
        menus = GrubCfgParser(grub_cfg).parse()

        # booted vmlinuz and initrd.img
        cmdline = self._get_cmdline()
        m = re.match(r"BOOT_IMAGE=/(.*)\s*root=UUID=(.*)\s", cmdline)
        vmlinuz = m.group(1)
        uuid = m.group(2)
        return self._find_menuentry(menus, uuid, device, vmlinuz)["entry"]

    def _update_menuentry(self, menuentry, standby_device, vmlinuz, initrd_img):
        uuid = self._get_uuid(standby_device)
        # NOTE: Only UUID sepcifier is supported.
        replaced = re.sub(
            r"(.*\slinux\s+/).*(\sroot=UUID=)\S*(\s.*)",
            rf"\g<1>{vmlinuz}\g<2>{uuid}\g<3>",  # NOTE: use \g<1> instead of \1
            menuentry,
        )
        replaced = re.sub(
            r"(.*\sinitrd\s+/)\S*(\s.*)",
            rf"\g<1>{initrd_img}\g<2>",
            replaced,
        )
        return replaced

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
