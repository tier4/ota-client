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
import os
import stat
from pathlib import Path

from otaclient._utils.path import replace_root
from otaclient.app.create_standby.common import PersistFilesHandler


def create_files(tmp_path: Path):
    """
    20231222: this function create <src> and <dst> folders as preserved src rootfs and save destination rootfs.
    """

    dst = tmp_path / "dst"
    dst.mkdir()

    src = tmp_path / "src"
    src.mkdir()

    """
    src/
    src/a
    src/to_a -> a
    src/to_broken_a -> broken_a
    src/A
    src/A/b
    src/A/to_b -> b
    src/A/to_broken_b -> broken_b
    src/A/B/
    src/A/B/c
    src/A/B/to_c -> c
    src/A/B/to_broken_c -> broken_c
    src/A/B/C/
    """

    a = src / "a"
    a.write_text("a")
    to_a = src / "to_a"
    to_a.symlink_to("a")
    to_broken_a = src / "to_broken_a"
    to_broken_a.symlink_to("broken_a")
    A = src / "A"
    A.mkdir()

    b = A / "b"
    b.write_text("b")
    to_b = A / "to_b"
    to_b.symlink_to("b")
    to_broken_b = A / "to_broken_b"
    to_broken_b.symlink_to("broken_b")
    B = A / "B"
    B.mkdir()

    c = B / "c"
    c.write_text("c")
    to_c = B / "to_c"
    to_c.symlink_to("c")
    to_broken_c = B / "to_broken_c"
    to_broken_c.symlink_to("broken_c")
    C = B / "C"
    C.mkdir()

    os.chown(src, 0, 1, follow_symlinks=False)
    os.chown(a, 2, 3, follow_symlinks=False)
    os.chown(to_a, 4, 5, follow_symlinks=False)
    os.chown(to_broken_a, 6, 7, follow_symlinks=False)
    os.chown(A, 8, 9, follow_symlinks=False)
    os.chown(b, 10, 13, follow_symlinks=False)
    os.chown(to_b, 33, 34, follow_symlinks=False)
    os.chown(to_broken_b, 38, 39, follow_symlinks=False)
    os.chown(B, 41, 65534, follow_symlinks=False)
    os.chown(c, 100, 102, follow_symlinks=False)
    os.chown(to_c, 12345678, 87654321, follow_symlinks=False)  # id can't be converted
    os.chown(to_broken_c, 104, 104, follow_symlinks=False)
    os.chown(C, 105, 105, follow_symlinks=False)

    os.chmod(src, 0o111)
    os.chmod(a, 0o112)
    # os.chmod(to_a, 0o113)
    # os.chmod(to_broken_a, 0o114)
    os.chmod(A, 0o115)
    os.chmod(b, 0o116)
    # os.chmod(to_b, 0o117)
    # os.chmod(to_broken_b, 0o121)
    os.chmod(B, 0o122)
    os.chmod(c, 0o123)
    # os.chmod(to_c, 0o124)
    # os.chmod(to_broken_c, 0o125)
    os.chmod(C, 0o126)

    return (
        dst,
        src,
        a,
        to_a,
        to_broken_a,
        A,
        b,
        to_b,
        to_broken_b,
        B,
        c,
        to_c,
        to_broken_c,
        C,
    )


def uid_gid_mode(path):
    st = os.stat(path, follow_symlinks=False)
    return st[stat.ST_UID], st[stat.ST_GID], stat.S_IMODE(st[stat.ST_MODE])


def assert_uid_gid_mode(path, uid, gid, mode):
    _uid, _gid, _mode = uid_gid_mode(path)
    assert _uid == uid
    assert _gid == gid
    assert _mode == mode


def create_passwd_group_files(tmp_path):
    src_passwd = """\
root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
bin:x:2:2:bin:/bin:/usr/sbin/nologin
sys:x:3:3:sys:/dev:/usr/sbin/nologin
sync:x:4:65534:sync:/bin:/bin/sync
games:x:5:60:games:/usr/games:/usr/sbin/nologin
man:x:6:12:man:/var/cache/man:/usr/sbin/nologin
lp:x:7:7:lp:/var/spool/lpd:/usr/sbin/nologin
mail:x:8:8:mail:/var/mail:/usr/sbin/nologin
news:x:9:9:news:/var/spool/news:/usr/sbin/nologin
uucp:x:10:10:uucp:/var/spool/uucp:/usr/sbin/nologin
proxy:x:13:13:proxy:/bin:/usr/sbin/nologin
www-data:x:33:33:www-data:/var/www:/usr/sbin/nologin
backup:x:34:34:backup:/var/backups:/usr/sbin/nologin
list:x:38:38:Mailing List Manager:/var/list:/usr/sbin/nologin
irc:x:39:39:ircd:/var/run/ircd:/usr/sbin/nologin
gnats:x:41:41:Gnats Bug-Reporting System (admin):/var/lib/gnats:/usr/sbin/nologin
nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin
systemd-network:x:100:102:systemd Network Management,,,:/run/systemd:/usr/sbin/nologin
systemd-resolve:x:101:103:systemd Resolver,,,:/run/systemd:/usr/sbin/nologin
systemd-timesync:x:102:104:systemd Time Synchronization,,,:/run/systemd:/usr/sbin/nologin
messagebus:x:103:106::/nonexistent:/usr/sbin/nologin
syslog:x:104:110::/home/syslog:/usr/sbin/nologin
_apt:x:105:65534::/nonexistent:/usr/sbin/nologin
"""

    # dst_passwd uid is converted with f"1{src_passwd gid}"
    dst_passwd = """\
root:x:10:0:root:/root:/bin/bash
daemon:x:11:1:daemon:/usr/sbin:/usr/sbin/nologin
bin:x:12:2:bin:/bin:/usr/sbin/nologin
sys:x:13:3:sys:/dev:/usr/sbin/nologin
sync:x:14:65534:sync:/bin:/bin/sync
games:x:15:60:games:/usr/games:/usr/sbin/nologin
man:x:16:12:man:/var/cache/man:/usr/sbin/nologin
lp:x:17:7:lp:/var/spool/lpd:/usr/sbin/nologin
mail:x:18:8:mail:/var/mail:/usr/sbin/nologin
news:x:19:9:news:/var/spool/news:/usr/sbin/nologin
uucp:x:110:10:uucp:/var/spool/uucp:/usr/sbin/nologin
proxy:x:113:13:proxy:/bin:/usr/sbin/nologin
www-data:x:133:33:www-data:/var/www:/usr/sbin/nologin
backup:x:134:34:backup:/var/backups:/usr/sbin/nologin
list:x:138:38:Mailing List Manager:/var/list:/usr/sbin/nologin
irc:x:139:39:ircd:/var/run/ircd:/usr/sbin/nologin
gnats:x:141:41:Gnats Bug-Reporting System (admin):/var/lib/gnats:/usr/sbin/nologin
nobody:x:165534:65534:nobody:/nonexistent:/usr/sbin/nologin
systemd-network:x:1100:102:systemd Network Management,,,:/run/systemd:/usr/sbin/nologin
systemd-resolve:x:1101:103:systemd Resolver,,,:/run/systemd:/usr/sbin/nologin
systemd-timesync:x:1102:104:systemd Time Synchronization,,,:/run/systemd:/usr/sbin/nologin
messagebus:x:1103:106::/nonexistent:/usr/sbin/nologin
syslog:x:1104:110::/home/syslog:/usr/sbin/nologin
_apt:x:1105:65534::/nonexistent:/usr/sbin/nologin
"""

    src_group = """\
root:x:0:
daemon:x:1:
bin:x:2:
sys:x:3:
adm:x:4:syslog
tty:x:5:syslog
disk:x:6:
lp:x:7:
mail:x:8:
news:x:9:
uucp:x:10:
man:x:12:
proxy:x:13:
kmem:x:15:
dialout:x:20:
fax:x:21:
voice:x:22:
cdrom:x:24:
floppy:x:25:
tape:x:26:
sudo:x:27:
audio:x:29:pulse
dip:x:30:
www-data:x:33:
backup:x:34:
operator:x:37:
list:x:38:
irc:x:39:
src:x:40:
gnats:x:41:
shadow:x:42:
utmp:x:43:
video:x:44:
sasl:x:45:
plugdev:x:46:
staff:x:50:
games:x:60:
users:x:100:
nogroup:x:65534:
systemd-journal:x:101:
systemd-network:x:102:
systemd-resolve:x:103:
systemd-timesync:x:104:
crontab:x:105:
"""

    # dst_group gid is converted with f"2{src_group gid}"
    dst_group = """\
root:x:20:
daemon:x:21:
bin:x:22:
sys:x:23:
adm:x:24:syslog
tty:x:25:syslog
disk:x:26:
lp:x:27:
mail:x:28:
news:x:29:
uucp:x:210:
man:x:212:
proxy:x:213:
kmem:x:215:
dialout:x:220:
fax:x:221:
voice:x:222:
cdrom:x:224:
floppy:x:225:
tape:x:226:
sudo:x:227:
audio:x:229:pulse
dip:x:230:
www-data:x:233:
backup:x:234:
operator:x:237:
list:x:238:
irc:x:239:
src:x:240:
gnats:x:241:
shadow:x:242:
utmp:x:243:
video:x:244:
sasl:x:245:
plugdev:x:246:
staff:x:250:
games:x:260:
users:x:2100:
nogroup:x:265534:
systemd-journal:x:2101:
systemd-network:x:2102:
systemd-resolve:x:2103:
systemd-timesync:x:2104:
crontab:x:2105:
"""
    src_passwd_file = tmp_path / "etc" / "src_passwd"
    dst_passwd_file = tmp_path / "etc" / "dst_passwd"
    src_group_file = tmp_path / "etc" / "src_group"
    dst_group_file = tmp_path / "etc" / "dst_group"
    (tmp_path / "etc").mkdir()

    src_passwd_file.write_text(src_passwd)
    dst_passwd_file.write_text(dst_passwd)
    src_group_file.write_text(src_group)
    dst_group_file.write_text(dst_group)
    return src_passwd_file, dst_passwd_file, src_group_file, dst_group_file


def test_copy_tree_src_dir(mocker, tmp_path):
    (
        dst,
        src,
        a,
        to_a,
        to_broken_a,
        A,
        b,
        to_b,
        to_broken_b,
        B,
        c,
        to_c,
        to_broken_c,
        C,
    ) = create_files(tmp_path)

    (
        src_passwd_file,
        dst_passwd_file,
        src_group_file,
        dst_group_file,
    ) = create_passwd_group_files(tmp_path)

    # NOTE: persist entry must be canonical and starts with /
    persist_entry = replace_root(B, src, "/")
    PersistFilesHandler(
        src_passwd_file=src_passwd_file,
        src_group_file=src_group_file,
        dst_passwd_file=dst_passwd_file,
        dst_group_file=dst_group_file,
        src_root=src,
        dst_root=dst,
    ).preserve_persist_entry(persist_entry, skip_invalid=False)

    # src/A
    assert (dst / A.relative_to(src)).is_dir()
    assert not (dst / A.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / A.relative_to(src), 18, 29, 0o115)

    # src/A/B/
    assert (dst / B.relative_to(src)).is_dir()
    assert not (dst / B.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / B.relative_to(src), 141, 265534, 0o122)

    # src/A/B/c
    assert (dst / c.relative_to(src)).is_file()
    assert not (dst / c.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / c.relative_to(src), 1100, 2102, 0o123)

    # src/A/B/to_c
    assert (dst / to_c.relative_to(src)).is_file()
    assert (dst / to_c.relative_to(src)).is_symlink()
    # uid, gid can't be converted so original uid, gid is used.
    assert_uid_gid_mode(dst / to_c.relative_to(src), 12345678, 87654321, 0o777)

    # src/A/B/to_broken_c
    assert not (dst / to_broken_c.relative_to(src)).is_file()
    assert (dst / to_broken_c.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / to_broken_c.relative_to(src), 1104, 2104, 0o777)

    # src/A/B/C/
    assert (dst / C.relative_to(src)).is_dir()
    assert not (dst / C.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / C.relative_to(src), 1105, 2105, 0o126)

    # followings should not exist
    # src/a
    assert not (dst / a.relative_to(src)).exists()
    assert not (dst / a.relative_to(src)).is_symlink()
    # src/to_a
    assert not (dst / to_a.relative_to(src)).exists()
    assert not (dst / to_a.relative_to(src)).is_symlink()
    # src/to_broken_a
    assert not (dst / to_broken_a.relative_to(src)).exists()
    assert not (dst / to_broken_a.relative_to(src)).is_symlink()
    # src/A/b
    assert not (dst / b.relative_to(src)).exists()
    assert not (dst / b.relative_to(src)).is_symlink()
    # src/A/to_b
    assert not (dst / to_b.relative_to(src)).exists()
    assert not (dst / to_b.relative_to(src)).is_symlink()
    # src/A/to_broken_b
    assert not (dst / to_broken_b.relative_to(src)).exists()
    assert not (dst / to_broken_b.relative_to(src)).is_symlink()


def test_copy_tree_src_file(mocker, tmp_path):
    (
        dst,
        src,
        a,
        to_a,
        to_broken_a,
        A,
        b,
        to_b,
        to_broken_b,
        B,
        c,
        to_c,
        to_broken_c,
        C,
    ) = create_files(tmp_path)

    (
        src_passwd_file,
        dst_passwd_file,
        src_group_file,
        dst_group_file,
    ) = create_passwd_group_files(tmp_path)

    PersistFilesHandler(
        src_passwd_file=src_passwd_file,
        src_group_file=src_group_file,
        dst_passwd_file=dst_passwd_file,
        dst_group_file=dst_group_file,
        src_root=src,
        dst_root=dst,
    ).preserve_persist_entry(replace_root(to_b, src, "/"))

    # src/A
    assert (dst / A.relative_to(src)).is_dir()
    assert not (dst / A.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / A.relative_to(src), 18, 29, 0o115)

    # src/A/to_b (src/A/b is not copied, so to_b.is_file() is False)
    assert not (dst / to_b.relative_to(src)).is_file()
    assert (dst / to_b.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / to_b.relative_to(src), 133, 234, 0o777)

    # followings should not exist
    # src/a
    assert not (dst / a.relative_to(src)).exists()
    assert not (dst / a.relative_to(src)).is_symlink()
    # src/to_a
    assert not (dst / to_a.relative_to(src)).exists()
    assert not (dst / to_a.relative_to(src)).is_symlink()
    # src/to_broken_a
    assert not (dst / to_broken_a.relative_to(src)).exists()
    assert not (dst / to_broken_a.relative_to(src)).is_symlink()
    # src/A/b
    assert not (dst / b.relative_to(src)).exists()
    assert not (dst / b.relative_to(src)).is_symlink()
    # src/A/to_broken_b
    assert not (dst / to_broken_b.relative_to(src)).exists()
    assert not (dst / to_broken_b.relative_to(src)).is_symlink()
    # src/A/B
    assert not (dst / B.relative_to(src)).exists()
    assert not (dst / B.relative_to(src)).is_symlink()
    # src/A/B/c
    assert not (dst / c.relative_to(src)).exists()
    assert not (dst / c.relative_to(src)).is_symlink()
    # src/A/B/to_c
    assert not (dst / to_c.relative_to(src)).exists()
    assert not (dst / to_c.relative_to(src)).is_symlink()
    # src/A/B/to_broken_c
    assert not (dst / to_broken_c.relative_to(src)).exists()
    assert not (dst / to_broken_c.relative_to(src)).is_symlink()
    # src/A/B/C/
    assert not (dst / C.relative_to(src)).exists()
    assert not (dst / C.relative_to(src)).is_symlink()


def test_copy_tree_B_exists(mocker, tmp_path):
    (
        dst,
        src,
        a,
        to_a,
        to_broken_a,
        A,
        b,
        to_b,
        to_broken_b,
        B,
        c,
        to_c,
        to_broken_c,
        C,
    ) = create_files(tmp_path)

    dst_A = dst / "A"
    dst_A.mkdir(parents=True)
    print(f"dst_A {dst_A}")
    dst_B = dst_A / "B"
    dst_B.mkdir()

    os.chown(dst_A, 0, 1, follow_symlinks=False)
    os.chown(dst_B, 1, 2, follow_symlinks=False)
    os.chmod(dst_A, 0o765)
    os.chmod(dst_B, 0o654)
    st = os.stat(dst_A, follow_symlinks=False)

    (
        src_passwd_file,
        dst_passwd_file,
        src_group_file,
        dst_group_file,
    ) = create_passwd_group_files(tmp_path)

    PersistFilesHandler(
        src_passwd_file=src_passwd_file,
        src_group_file=src_group_file,
        dst_passwd_file=dst_passwd_file,
        dst_group_file=dst_group_file,
        src_root=src,
        dst_root=dst,
    ).preserve_persist_entry(replace_root(C, src, "/"))

    # src/A
    assert (dst / A.relative_to(src)).is_dir()
    assert not (dst / A.relative_to(src)).is_symlink()
    # 'A' is created by this function before hand
    # NOTE(20231222): if src dir exists in the dest, we should
    #                 remove the dst and preserve src to dest.
    assert_uid_gid_mode(dst / A.relative_to(src), 18, 29, 0o115)

    # src/A/B
    assert (dst / B.relative_to(src)).is_dir()
    assert not (dst / B.relative_to(src)).is_symlink()
    # 'B' is created by this function before hand
    assert_uid_gid_mode(dst / B.relative_to(src), 141, 265534, 0o122)

    # src/A/B/C/
    assert (dst / C.relative_to(src)).is_dir()
    assert not (dst / C.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / C.relative_to(src), 1105, 2105, 0o126)

    # followings should not exist
    # src/a
    assert not (dst / a.relative_to(src)).exists()
    assert not (dst / a.relative_to(src)).is_symlink()
    # src/to_a
    assert not (dst / to_a.relative_to(src)).exists()
    assert not (dst / to_a.relative_to(src)).is_symlink()
    # src/to_broken_a
    assert not (dst / to_broken_a.relative_to(src)).exists()
    assert not (dst / to_broken_a.relative_to(src)).is_symlink()
    # src/A/b
    assert not (dst / b.relative_to(src)).exists()
    assert not (dst / b.relative_to(src)).is_symlink()
    # src/A/to_b (src/A/b is not copied, so to_b.is_file() is False)
    assert not (dst / to_b.relative_to(src)).exists()
    assert not (dst / to_b.relative_to(src)).is_symlink()
    # src/A/to_broken_b
    assert not (dst / to_broken_b.relative_to(src)).exists()
    assert not (dst / to_broken_b.relative_to(src)).is_symlink()
    # src/A/B/c
    assert not (dst / c.relative_to(src)).exists()
    assert not (dst / c.relative_to(src)).is_symlink()
    # src/A/B/to_c
    assert not (dst / to_c.relative_to(src)).exists()
    assert not (dst / to_c.relative_to(src)).is_symlink()
    # src/A/B/to_broken_c
    assert not (dst / to_broken_c.relative_to(src)).exists()
    assert not (dst / to_broken_c.relative_to(src)).is_symlink()


def test_copy_tree_with_symlink_overwrite(mocker, tmp_path):
    (
        dst,
        src,
        a,
        to_a,
        to_broken_a,
        A,
        b,
        to_b,
        to_broken_b,
        B,
        c,
        to_c,
        to_broken_c,
        C,
    ) = create_files(tmp_path)

    (
        src_passwd_file,
        dst_passwd_file,
        src_group_file,
        dst_group_file,
    ) = create_passwd_group_files(tmp_path)

    ct = PersistFilesHandler(
        src_passwd_file=src_passwd_file,
        src_group_file=src_group_file,
        dst_passwd_file=dst_passwd_file,
        dst_group_file=dst_group_file,
        src_root=src,
        dst_root=dst,
    )

    ct.preserve_persist_entry(replace_root(to_a, src, "/"))
    ct.preserve_persist_entry(replace_root(to_broken_a, src, "/"))

    # followings should exist
    # src/to_a
    assert (dst / to_a.relative_to(src)).is_symlink()
    # src/to_broken_a
    assert (dst / to_broken_a.relative_to(src)).is_symlink()

    # overwrite symlinks
    ct.preserve_persist_entry(replace_root(to_a, src, "/"))
    ct.preserve_persist_entry(replace_root(to_broken_a, src, "/"))

    # followings should exist
    # src/to_a
    assert (dst / to_a.relative_to(src)).is_symlink()
    # src/to_broken_a
    assert (dst / to_broken_a.relative_to(src)).is_symlink()


def test_copy_tree_src_dir_dst_file(mocker, tmp_path):
    (
        dst,
        src,
        a,
        to_a,
        to_broken_a,
        A,
        b,
        to_b,
        to_broken_b,
        B,
        c,
        to_c,
        to_broken_c,
        C,
    ) = create_files(tmp_path)

    (
        src_passwd_file,
        dst_passwd_file,
        src_group_file,
        dst_group_file,
    ) = create_passwd_group_files(tmp_path)

    ct = PersistFilesHandler(
        src_passwd_file=src_passwd_file,
        src_group_file=src_group_file,
        dst_passwd_file=dst_passwd_file,
        dst_group_file=dst_group_file,
        src_root=src,
        dst_root=dst,
    )

    (dst / src.relative_to("/") / "A").mkdir(parents=True)
    # NOTE: create {dst}/{src}/A/B as *file* before hand
    (dst / src.relative_to("/") / "A" / "B").write_text("B")

    ct.preserve_persist_entry(replace_root(B, src, "/"))

    # src/A
    assert (dst / A.relative_to(src)).is_dir()
    assert not (dst / A.relative_to(src)).is_symlink()
    # NOTE: {dst}/{src}/A exists before copy so uid, gid and mode are unchanged.
    assert_uid_gid_mode(
        dst / A.relative_to(src), *uid_gid_mode(dst / src.relative_to(src) / "A")
    )

    # src/A/B/
    assert (dst / B.relative_to(src)).is_dir()
    assert not (dst / B.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / B.relative_to(src), 141, 265534, 0o122)

    # src/A/B/c
    assert (dst / c.relative_to(src)).is_file()
    assert not (dst / c.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / c.relative_to(src), 1100, 2102, 0o123)

    # src/A/B/to_c
    assert (dst / to_c.relative_to(src)).is_file()
    assert (dst / to_c.relative_to(src)).is_symlink()
    # uid, gid can't be converted so original uid, gid is used.
    assert_uid_gid_mode(dst / to_c.relative_to(src), 12345678, 87654321, 0o777)

    # src/A/B/to_broken_c
    assert not (dst / to_broken_c.relative_to(src)).is_file()
    assert (dst / to_broken_c.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / to_broken_c.relative_to(src), 1104, 2104, 0o777)

    # src/A/B/C/
    assert (dst / C.relative_to(src)).is_dir()
    assert not (dst / C.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / C.relative_to(src), 1105, 2105, 0o126)

    # followings should not exist
    # src/a
    assert not (dst / a.relative_to(src)).exists()
    assert not (dst / a.relative_to(src)).is_symlink()
    # src/to_a
    assert not (dst / to_a.relative_to(src)).exists()
    assert not (dst / to_a.relative_to(src)).is_symlink()
    # src/to_broken_a
    assert not (dst / to_broken_a.relative_to(src)).exists()
    assert not (dst / to_broken_a.relative_to(src)).is_symlink()
    # src/A/b
    assert not (dst / b.relative_to(src)).exists()
    assert not (dst / b.relative_to(src)).is_symlink()
    # src/A/to_b
    assert not (dst / to_b.relative_to(src)).exists()
    assert not (dst / to_b.relative_to(src)).is_symlink()
    # src/A/to_broken_b
    assert not (dst / to_broken_b.relative_to(src)).exists()
    assert not (dst / to_broken_b.relative_to(src)).is_symlink()


def test_copy_tree_src_file_dst_dir(mocker, tmp_path):
    (
        dst,
        src,
        a,
        to_a,
        to_broken_a,
        A,
        b,
        to_b,
        to_broken_b,
        B,
        c,
        to_c,
        to_broken_c,
        C,
    ) = create_files(tmp_path)

    (
        src_passwd_file,
        dst_passwd_file,
        src_group_file,
        dst_group_file,
    ) = create_passwd_group_files(tmp_path)

    ct = PersistFilesHandler(
        src_passwd_file=src_passwd_file,
        src_group_file=src_group_file,
        dst_passwd_file=dst_passwd_file,
        dst_group_file=dst_group_file,
        src_root=src,
        dst_root=dst,
    )

    # NOTE: create {dst}/{src}/A/B/c as *dir* before hand
    (dst / src.relative_to("/") / "A" / "B" / "c").mkdir(parents=True)

    ct.preserve_persist_entry(replace_root(B, src, "/"))

    # src/A
    assert (dst / A.relative_to(src)).is_dir()
    assert not (dst / A.relative_to(src)).is_symlink()
    # NOTE: {dst}/{src}/A exists before copy so uid, gid and mode are unchanged.
    assert_uid_gid_mode(
        dst / A.relative_to(src), *uid_gid_mode(dst / src.relative_to(src) / "A")
    )

    # src/A/B/
    assert (dst / B.relative_to(src)).is_dir()
    assert not (dst / B.relative_to(src)).is_symlink()
    # NOTE: {dst}/{src}/A/B exists before copy so uid, gid and mode are unchanged.
    assert_uid_gid_mode(
        dst / B.relative_to(src), *uid_gid_mode(dst / src.relative_to(src) / "A" / "B")
    )

    # src/A/B/c
    assert (dst / c.relative_to(src)).is_file()
    assert not (dst / c.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / c.relative_to(src), 1100, 2102, 0o123)

    # src/A/B/to_c
    assert (dst / to_c.relative_to(src)).is_file()
    assert (dst / to_c.relative_to(src)).is_symlink()
    # uid, gid can't be converted so original uid, gid is used.
    assert_uid_gid_mode(dst / to_c.relative_to(src), 12345678, 87654321, 0o777)

    # src/A/B/to_broken_c
    assert not (dst / to_broken_c.relative_to(src)).is_file()
    assert (dst / to_broken_c.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / to_broken_c.relative_to(src), 1104, 2104, 0o777)

    # src/A/B/C/
    assert (dst / C.relative_to(src)).is_dir()
    assert not (dst / C.relative_to(src)).is_symlink()
    assert_uid_gid_mode(dst / C.relative_to(src), 1105, 2105, 0o126)

    # followings should not exist
    # src/a
    assert not (dst / a.relative_to(src)).exists()
    assert not (dst / a.relative_to(src)).is_symlink()
    # src/to_a
    assert not (dst / to_a.relative_to(src)).exists()
    assert not (dst / to_a.relative_to(src)).is_symlink()
    # src/to_broken_a
    assert not (dst / to_broken_a.relative_to(src)).exists()
    assert not (dst / to_broken_a.relative_to(src)).is_symlink()
    # src/A/b
    assert not (dst / b.relative_to(src)).exists()
    assert not (dst / b.relative_to(src)).is_symlink()
    # src/A/to_b
    assert not (dst / to_b.relative_to(src)).exists()
    assert not (dst / to_b.relative_to(src)).is_symlink()
    # src/A/to_broken_b
    assert not (dst / to_broken_b.relative_to(src)).exists()
    assert not (dst / to_broken_b.relative_to(src)).is_symlink()
