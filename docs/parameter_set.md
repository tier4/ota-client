# Parameter Sets on the ECU

otaclient lays a parameter set (spec: [ota-image-libs `parameter_set.md`](https://github.com/tier4/ota-image-libs/blob/main/spec/parameter_set.md)) over the running image without writing the standby slot.
It does not reboot, unless a changed slot's `apply` is `reboot`: then the same slot reboots once the `SUCCESS` is reported.
This page is what otaclient keeps on the ECU for it, and which part of that others may read.
Only file-based layouts (the A/B rootfs updated file by file) take a parameter set; other layouts refuse one with `E463`.

## The Store

Under `/opt/ota/parameter-set/` (`0700`, root only), in the running slot:

| Path | What it is |
| --- | --- |
| `applied/record.json` | The set this slot runs with: [the record](#the-record). Absent when no set is applied. |
| `applied/tree/<path>` | The set's files at their paths relative to `/`, bind-mounted read-only (`nosuid,nodev`; executables run) over each slot. |
| `applied/files.json` | The files otaclient unpacked from the set's layer and checked: what the tree is checked against at each start. |
| `applied/manifest.json`, `config.json` | The set's own metadata, as downloaded. |
| `previous/` | The set before, only while a switch is in progress. |
| `discard/` | The set before, only while it is being deleted after a switch. |
| `staging/` | A set being prepared. |
| `history.jsonl` (`.1`) | One JSON object per line per event (`applied`, `running`, `dropped`, `tree_mismatch`, `apply_failed`, `apply_not_root_only`, `mount_point_made`, …), rotated at 1 MiB. |
| `last-running-boot` | What was last recorded as `running`, so that it is recorded once per boot. |

The mounts are also written into a managed block of `/etc/fstab` (`bind,ro,nosuid,nodev,nofail,x-systemd.before=local-fs.target`), so that a reboot lays the set again before anything reads the slots; a slot that cannot be mounted does not stop the boot, and otaclient mounts it again when it starts.
A full OTA writes a new slot from its image: the set is not carried over, only the history is.

A slot is never over what otaclient uses for itself: besides what every consumer reads (`/boot`, `/etc/fstab`, `/opt/ota`, `/usr/share/ota`), `/proc`, `/sys`, `/dev`, `/run`, `/host_root`, `/ota-cache` and otaclient's stores at `/.ota-*`; such a set is refused with `E463`.

A slot directory the running image does not have is made (empty, root's, with its missing parents) as the mount point, recorded as `mount_point_made`, and left in place when the slot is taken away; one the image has as a file, or behind a file or a symbolic link, refuses the set with `E463`.
A slot's mount is taken away lazily: a program that runs from the slot's files keeps them until it exits.

A slot's `apply` runs as root, in the host's root, once the slot holds the set's new contents: it is the file at its path then, the set's own when the path is inside one of the set's slots (the set must carry it, executable), the running image's otherwise (which must have it, or the set is refused with `E463`).
Nothing runs when a slot is taken away (an empty set, a set without that slot, a set dropped at startup): that takes effect when what reads the slot restarts. When a switch fails, the set before is put back and the `apply` of its slots runs again; with no set before, nothing runs.
An `apply` of the running image that users other than root can change (the file, or a directory on its way, writable by them) is logged and recorded as `apply_not_root_only`, but not refused.

## The Record

`applied/record.json` is a JSON object. Others may read these fields, whose meaning does not change within a `schema_version`:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | int | `1`. |
| `image_id` | string | The parameter image's ID, as the update request named it. |
| `set_name` | string | The set's name (its config's `name`). |
| `set_version` | string | The set's version (its config's `version`). |
| `base_image_id` | string | The running image's ID (`/usr/share/ota/version.json`) when the set was applied; empty when the image had none. |
| `applied_at` | string | When it was applied, ISO 8601 UTC. |

Any other field is otaclient's own and may change. The store is root's: a reader runs as root.

The file is replaced as a whole (written to a temporary file and renamed), and the directory holding it is renamed during a switch: a reader reads the whole file, takes a missing file as "no set applied", and reads it again when it changes.
A record whose `base_image_id` is not the running image's ID is not this slot's set: otaclient drops such a set at its next start, and a reader takes it as "no set applied".

## Status

While a set is applied, otaclient reports the parameter release (`release_name`, `release_id`) and the parameter image's ID as the ECU's `image_id`.
When the set is dropped, the release recorded before it is reported again.
The running image's own ID is in its version file, which a set does not change.

From the start of the download until the set is in place, the current slot records `FAILURE` with `E544` ("did not finish"): an update cut short by a stop of otaclient or a power loss is reported as failed, and the store is put back to the set before (or forward to the new one, when its release was already recorded) at the next start.
