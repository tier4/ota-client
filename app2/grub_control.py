import re
import platform
import subprocess
import shlex


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
    def __init__(self):
        self._grub_cfg_file = "/boot/grub/grub.cfg"
        self._custom_cfg_file = "/boot/grub/custom.cfg"

    def create_custom_cfg_and_reboot(self, active_device, standby_device):
        # custom.cfg
        booted_menu_entry = self._get_booted_menu_entry(active_device)
        # count custom.cfg menuentry number
        custom_cfg = self._update_menu_entry(booted_menu_entry, standby_device)
        # store custom.cfg
        with tempfile.NamedTemporaryFile("w", delete=False, prefix=__name__) as f:
            temp_name = f.name
            f.write(custom_cfg)
            shutil.move(temp_name, self._custom_cfg_file)

        # grub-reboot
        self._grub_reboot()
        # reboot
        self.reboot()

    def update_grub_cfg():
        # update /etc/default/grub w/ GRUB_DISABLE_SUBMENU
        # grub-mkconfig temporally
        # count menuentry number
        # grub-mkconfig w/ the number counted
        pass

    def reboot():
        cmd = f"reboot"
        return subprocess.check_output(shlex.split(cmd))

    """ private from here """

    def find_linux_entry(self, menus, uuid, device, kernel_release):
        for menu in menus:
            if type(menu) is dict:
                if menu["linux"].find(kernel_release) >= 0 and (
                    menu["linux"].find(uuid) >= 0 or menu["linux"].find(device) >= 0
                ):
                    return menu
            elif type(menu) is list:
                ret = find_linux_entry(menu, uuid, device, kernel_release)
                if ret is not None:
                    return ret
        return None

    def _get_booted_menu_entry(self, active_device):
        # find menuentry from current grub.cfg w/ kernel_release and (UUID or device).
        grub_cfg = open(self._grub_cfg_file).read()
        menus = GrubCfgParser(grub_cfg).parse()
        kernel_release = platform.release()  # same as `uname -r`
        active_uuid = self._get_uuid(active_device)
        return self.find_linux_entry(menus, active_uuid, active_device, kernel_release)

    def _update_menu_entry(self, menu_entry, standby_device):
        # TODO:
        # replace linux and (UUID or device)
        # replace initrd and (UUID or device)
        return menu_entry

    def _grub_reboot_cmd(self, num):
        cmd = f"grub-reboot {num}"
        return subprocess.check_output(shlex.split(cmd))

    def _grub_reboot(self):
        # count grub menu entry number
        grub_cfg = open(self._grub_cfg_file).read()
        menus = GrubCfgParser(grub_cfg).parse()
        _grub_reboot_cmd(len(menus))

    def _get_uuid(self, device):
        cmd = f"lsblk -ino UUID /dev/{device}"
        return subprocess.check_output(shlex.split(cmd))
