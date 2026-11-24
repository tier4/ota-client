# Parameter Sets on the ECU

otaclient lays a parameter set (spec: [ota-image-libs `parameter_set.md`](https://github.com/tier4/ota-image-libs/blob/main/spec/parameter_set.md)) over the running image without writing the standby slot.
It does not reboot, unless a changed slot's `apply` is `reboot`: then the same slot reboots once the `SUCCESS` is reported.
This page is what otaclient keeps on the ECU for it, and which part of that others may read.

Every layout takes a parameter set, the file-based ones (the A/B rootfs updated file by file) and the partition-based ones (`grub-verity`, `jetson-dpi`, `driveos-dpi`) alike, by the same mechanism.
They differ only where the root filesystem is read-only, and in the boot controller:

- On a read-only root (a partition-based layout's dm-verity image), a slot directory the image does not have cannot be made: the image must have each slot's directory, or the set is refused with `E463`.
- A slot whose `apply` is `reboot` reboots the same slot the way the boot controller reboots: a plain reboot on file-based layouts, the DPI's reboot on partition-based ones (a plain reboot of the guest does not come back on DriveOS).
- On a partition-based layout, no set is taken in a boot in which a full update was staged (`E463`): the DPI may have armed the standby slot for a trial boot, which the next reboot would boot. Apply the set after the next reboot.

Every layout needs the boot-time unit enabled ([At Boot](#at-boot)): without it, a set is refused with `E463`.

## The Store

Under `/opt/data/ota/parameter-set/` (`0700`, root only); `/opt/data` and `/opt/data/ota` are made (`0755`) when missing.
`/opt/data` is the one place every layout can write and keep: on a partition-based layout it is a partition of its own, shared by both slots and not rolled back; on a file-based layout it is a directory of the running slot, unless the image mounts a partition there.

Every directory from the mount point the store is on down to the store that exists (`/opt/data` or `/opt`, `/opt/data/ota`, the store) must be root's, not writable by group or others, and not a symbolic link: other users write under `/opt/data` on some devices, and one who could rename the store away could plant a set whose `apply` runs as root.
Otherwise a new set is refused with `E463` (naming the directory, its owner and mode) before anything is written, and at boot and at otaclient's start nothing is laid from the store, nothing in it is deleted, and nothing is recorded in it; it is logged, and the slot reports its base's release, not the set's.
What otaclient makes itself (`0755`, the store `0700`) passes.

A store the slots share needs the base's image ID (`/usr/share/ota/version.json`) to tell the base a set was applied on: on a base without one, a new set is refused with `E463`.
On a store on the running slot's own root filesystem, a base without an image ID is told by its empty ID, as before.

| Path | What it is |
| --- | --- |
| `applied/record.json` | The set this slot runs with: [the record](#the-record). Absent when no set is applied. |
| `applied/tree/<path>` | The set's files at their paths relative to `/`, bind-mounted read-only (`nosuid,nodev`; executables run) over each slot. The bind is made read-only in a private staging mount (`/run/otaclient-parameter-set`) and then moved over the slot, so that a view that receives the host's mounts (a container's, a service's, another bind of `/`) gets it read-only too. |
| `applied/files.json` | The files otaclient unpacked from the set's layer and checked: what the tree is checked against at each start. |
| `applied/manifest.json`, `config.json` | The set's own metadata, as downloaded. |
| `previous/` | The set before, only while a switch is in progress. |
| `discard/` | The set before, only while it is being deleted after a switch. |
| `staging/` | A set being prepared. |
| `history.jsonl` (`.1`) | One JSON object per line per event (`applied`, `running`, `mounted_at_boot`, `not_mounted_at_boot`, `remounted`, `dropped`, `left_behind`, `base_change_not_taken`, `tree_mismatch`, `apply_failed`, `apply_not_root_only`, `mount_point_made`, `not_laid`, …), rotated at 1 MiB. |
| `last-<event>-boot` | What was last recorded as `running`, `not_laid` or `tree_mismatch`, so that each is recorded once per boot. |
| `base-change-staged.json` | Only in a store the slots share: a full update was staged since the set was applied ([A Full OTA](#a-full-ota)). |

A slot is never over what otaclient or the OTA platform uses for itself: besides what every consumer reads (the image library's `/boot`, `/etc/fstab`, `/etc/ota`, `/opt/ota`, `/opt/data/ota`, `/usr/lib/ota` and `/usr/share/ota`: a set with a slot over one of them, or an `apply` in one, does not parse, and the parameter image is refused as invalid with `E422`), `/proc`, `/sys`, `/dev`, `/run`, `/host_root`, `/ota-cache`, otaclient's stores at `/.ota-*`, the parameter set store itself (so neither `/opt/data` nor a path inside the store), what otaclient keeps on a partition-based layout's `/opt/data` (`ota-status`, with `staged-in-boot`, and `otaclient`, the staging directory), and the DPI's: on `/opt/data` its `ota-agent`, `ota-cache`, `ota-client`, `ota-du`, `ota-esync`, `ota-health` and `esync`, with the persist stores `appdata` and `bulk`, and in the image `/usr/local/lib/rootfs-ua`, `/usr/local/sbin` and the DPI `ecu_info.yaml` names (as named and as it resolves, with their directories); such a set is refused with `E463`.
Nor is an `apply` of the running image in one of them (`E463`): what runs there is otaclient's or the platform's; except in `/usr/local/sbin`, where only slots are kept out, as an `apply` there runs one of the base's own programs.

A slot directory the running image does not have is made (empty, root's, mode `0755` with each missing parent, whatever the umask) as the mount point, recorded as `mount_point_made`, and left in place when the slot is taken away; one the image has as a file, or behind a file or a symbolic link, or one that cannot be made there (a read-only filesystem), refuses the set with `E463`.
A slot's mount is taken away lazily: a program that runs from the slot's files keeps them until it exits. So is whatever is mounted below it; each mount taken away is logged.

A slot's `apply` runs as root, in the host's root and mount namespace, once the slot holds the set's new contents: it is the file at its path then, the set's own when the path is inside one of the set's slots (the set must carry it as an executable regular file that others cannot write, nor any directory from its slot down to it, or the parameter image is refused as invalid with `E422`), the running image's otherwise (which must have it, or the set is refused with `E463`).
Nothing runs when a slot is taken away (an empty set, a set without that slot, a set dropped at startup): that takes effect when what reads the slot restarts. When a switch fails, the set before is put back and the `apply` of its slots that change back runs again; with no set before, nothing runs, and what an `apply` of the failed set restarted reads the failed set's files until it restarts again.
Whenever a set is switched to, undone or dropped, the store's directories are renamed first and the mounts follow: `applied/` always says what is to be mounted, whatever fails after.
An `apply` that users other than root can change (the file, or a directory on its way, writable by them or not root's) is logged and recorded as `apply_not_root_only`, but not refused; for an `apply` the set carries, what is looked at is the running image's way to the slot that holds it.

## At Boot

The app image's boot-time unit, [`otaclient-parameter-set.service_app`](../samples/otaclient-parameter-set.service_app), runs `/otaclient/otaclient mount-parameter-set` once, before `local-fs.target`, so that each slot holds the set before anything that reads it starts.
It is required: install it as `otaclient-parameter-set.service` and enable it (`local-fs.target.wants/` under `/etc/systemd/system`, `/usr/lib/systemd/system` or `/lib/systemd/system`), or otaclient refuses a set with `E463`, as one would not be laid at boot while its release is reported.
It needs an otaclient app image that has the subcommand (released after v4.0.0rc6): with an older one, the unit would start otaclient itself.
It is skipped when the store holds no set (`ConditionPathExists=` on `applied/` and `previous/`).

It reads the store and the running image's version file (`/usr/share/ota/version.json`), needs neither the boot controller nor `ecu_info.yaml` (loading otaclient's configuration still parses it and `proxy_info.yaml`, and logs that they are missing when `/boot` is not mounted yet), and never runs an `apply`:

- With no set applied, it does nothing.
- With a store that is not root's alone, or a set whose record cannot be read, it mounts nothing, logs it and exits `1`.
- With a set this boot cannot tell is the running base's, it mounts nothing and leaves the store as it is (only logged): a set applied on another image, or on an image without an ID in a store the slots share, or any set after a full update was staged from a store the slots share ([A Full OTA](#a-full-ota)). This boot may be the trial of a new base, which may still fall back to the base the set was applied on; otaclient's start decides.
- With a record of a newer `schema_version` than this otaclient's, it mounts nothing and leaves it to otaclient's start.
- With `previous/` there (a switch did not finish), it mounts nothing: otaclient completes or rolls back the switch at its start, and runs the `apply` of the slots it mounts then.
- Otherwise, each slot that is not mounted gets its mount point (when the image has none and it can be made) and the read-only bind; a slot that cannot be mounted does not keep the others from being mounted.

For the running base's set it records one event (`mounted_at_boot`, or `not_mounted_at_boot` with the reason), and exits `0` when every slot is mounted (or nothing is to be), `1` when one is not; each command it runs is bounded in time, and so is the unit.
A slot on a filesystem of its own, other than `/` and the store's, needs that filesystem mounted first: add its path to the unit's `RequiresMountsFor=` (a drop-in).
So does a platform that prepares `/opt/data` at boot (who owns it included): order that before the unit (`After=`, a drop-in), or the unit finds the store's way not root's alone and lays nothing.

When otaclient starts, once the boot controller has concluded the boot, it checks the set the unit mounted. A slot that is not mounted (the unit could not mount it, or left it to this start) is mounted then, its `apply` runs, and the log says why (`remounted`); a slot that cannot be mounted then drops the set. This also lays a set at the start of an otaclient whose boot-time unit is missing, as a safety net: what reads the slots until then runs with the base's defaults.

A set that is not the running base's, or whose record cannot be read, is never laid. It is dropped only once the running base is committed (the slot booted at `SUCCESS` or `INITIALIZED`; never by the downloaded client of a client update, whose status is set to `SUCCESS` whatever it was), with nothing unmounted, as it was never laid on this base; at any other status (a trial boot that was not committed, say) it is kept as it is, recorded once per boot (`not_laid`), and the base it was applied on lays it again when the platform falls back to it.
Nor is such a set the set before when a new set is applied on the running base: until the base is committed, a new set is refused with `E463` (the platform may still fall back to the base the stored set belongs to); once it is, the stored set is dropped, every slot of the new set is applied, and a failure undoes the new set to the base's defaults, never to the other base's set.

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
A record whose `base_image_id` is not the running image's ID (or is empty, in a store the slots share) is not this slot's set: neither the boot-time unit nor otaclient lays it, otaclient drops it once the running base is committed, and a reader takes it as "no set applied".

## A Full OTA

A full OTA writes a new slot from its image, which runs with its slots' defaults: the set stays behind (`left_behind` in the history).

- A store on the running slot's own root filesystem is not carried over: only the history is copied into the new slot, with that line. The persist files never copy the store either, also when they list a directory that holds it (`/opt/data`, say).
- A store on a filesystem of its own (a partition shared by both slots, as on a partition-based layout) stays where it is, with that line (`shared`) and, when it holds a set, a mark that a full update was staged (`base-change-staged.json`): the new slot may run the very image the set was applied on (the base release delivered again, say), which the image ID cannot tell from the old slot. While the mark is there, the boot-time unit lays nothing, and otaclient's start tells by the slot's records: the slot that still records the set's release is the one it was applied on (the update fell back, or never switched), which lays the set again, applies its slots and removes the mark (`base_change_not_taken`); any other slot does not lay it, and drops it, with the mark, once its base is committed. Until then, a fallback to the old base finds its set and lays it.

## Status

While a set is applied, otaclient reports the parameter release (`release_name`, `release_id`) and the parameter image's ID as the ECU's `image_id`.
When the set is dropped, the release recorded before it is reported again.
The running image's own ID is in its version file, which a set does not change.

From the start of the download until the set is in place, the current slot records `FAILURE` with `E544` ("did not finish"): an update cut short by a stop of otaclient or a power loss is reported as failed, and the store is put back to the set before (or forward to the new one, when its release was already recorded) at the next start.

## Known Limitations

- Whether the running base is committed is what otaclient recorded for the boot: a root filesystem switch made without otaclient staging it (by an eSync campaign through the DPI, say) is not seen as a trial, and a set of the base before is dropped at the first start of the new base.
- On a file-based ECU whose `/opt/data` (or `/opt`, when `/opt/data` is not a mount point of its own) is not root's or is writable by others, the store is not trusted: a set is refused with `E463`.
