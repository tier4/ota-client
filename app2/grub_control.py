class GrubControl:
    def __init__(self):
        self._grub_cfg_file = "/boot/grub/grub.cfg"
        self._custom_cfg_file = "/boot/grub/custom.cfg"

    def create_custom_cfg_and_reboot(active_device, standby_device):
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

    def _get_booted_menu_entry(active_device):
        # find menuentry from current grub.cfg w/ kernel_release and (UUID or device).
        grub_cfg = open(self._grub_cfg_file).read()
        menus = GrubCfgParser(grub_cfg).parse()
        return find_linux_entry(menus, active_device)

    def _update_menu_entry(menu_entry, standby_device):
        # TODO:
        # replace linux and (UUID or device)
        # replace initrd and (UUID or device)
        return menu_entry

    def _grub_reboot_cmd(num):
        cmd = f"grub-reboot {num}"
        return subprocess.check_output(shlex.split(cmd))

    def _grub_reboot():
        # count grub menu entry number
        grub_cfg = open(self._grub_cfg_file).read()
        menus = GrubCfgParser(grub_cfg).parse()
        _grub_reboot_cmd(len(menus))
