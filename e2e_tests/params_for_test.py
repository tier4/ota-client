# os status
BANKA_UUID, BANKA_DEV = "3a1c99e7-46d9-41b1-8b0a-b07bceef1d02", "/dev/sda3"
BANKB_UUID, BANKB_DEV = "ad0cd79a-1752-47bb-9274-f9aa4e289cb9", "/dev/sda4"
BOOT_UUID, BOOT_DEV = "cc59073d-9e5b-41e1-b724-576259341132", "/dev/sda2"

BANK_INFO = f"""\
banka: {BANKA_DEV}
bankb: {BANKB_DEV}
"""

# initial status
ECUID = "1\n"
ECUINFO_YAML = """\
main_ecu:
  ecu_name: 'autoware_ecu' 
  ecu_type: 'autoware'
  ecu_id: '1'
  version: '0.0.0'
  independent: True
  ip_addr: ''
"""

GRUB_DEFAULT = """\
GRUB_DEFAULT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_TIMEOUT=0
GRUB_DISTRIBUTOR=`lsb_release -i -s 2> /dev/null || echo Debian`
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"
GRUB_CMDLINE_LINUX=""
"""

GRUB_CUSTOM_CFG = f"""
menuentry 'GNU/Linux' {{
        linux   /vmlinuz-5.4.0-74-generic root=UUID={UUID_A} ro  quiet splash $vt_handoff
        initrd  /initrd.img-5.4.0-74-generic
}}"""

FSTAB_BY_UUID_BANKA = f"""\
# /etc/fstab: static file system information.
#
# Use 'blkid' to print the universally unique identifier for a
# device; this may be used with UUID= as a more robust way to name devices
# that works even if disks are added and removed. See fstab(5).
#
# <file system> <mount point>   <type>  <options>       <dump>  <pass>
# / was on /dev/nvme0n1p1 during installation
UUID={BOOT_UUID} /               ext4    errors=remount-ro 0       1
UUID={BANKA_UUID} /boot           ext4    errors=remount-ro 0       1
/swapfile                                 none            swap    sw              0       0
"""

# updated status
# status switched from NORMAL to SWITCHB
UPDATED_OTA_STATUS="SWITCHB"

UPDATED_ECUINFO_YAML = """\
main_ecu:
  ecu_name: 'autoware_ecu'
  ecu_type: 'autoware'
  ecu_id: '1'
  version: '0.5.1'
  independent: True
"""

FSTAB_BY_UUID_BANKB = """\
# /etc/fstab: static file system information.
#
# Use 'blkid' to print the universally unique identifier for a
# device; this may be used with UUID= as a more robust way to name devices
# that works even if disks are added and removed. See fstab(5).
#
# <file system> <mount point>   <type>  <options>       <dump>  <pass>
# / was on /dev/nvme0n1p1 during installation
UUID={BOOT_UUID} /               ext4    errors=remount-ro 0       1
UUID={BANKB_UUID} /boot           ext4    errors=remount-ro 0       1
/swapfile                                 none            swap    sw              0       0
"""

GRUB_CUSTOM_CFG_BANKB = f"""
menuentry 'GNU/Linux' {{
        linux   /vmlinuz-5.4.0-74-generic root=UUID={UUID_B} ro  quiet splash $vt_handoff
        initrd  /initrd.img-5.4.0-74-generic
}}"""
