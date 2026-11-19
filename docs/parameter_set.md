# Parameter Sets on the ECU

otaclient lays a parameter set (spec: [ota-image-libs `parameter_set.md`](https://github.com/tier4/ota-image-libs/blob/main/spec/parameter_set.md)) over the running image without writing the standby slot and without a reboot.
This page is what otaclient keeps on the ECU for it, and which part of that others may read.
Only file-based layouts (the A/B rootfs updated file by file) take a parameter set; other layouts refuse one with `E463`.

## The Store

Under `/opt/ota/parameter-set/`, in the running slot:

| Path | What it is |
| --- | --- |
| `applied/record.json` | The set this slot runs with: [the record](#the-record). Absent when no set is applied. |
| `applied/tree/<path>` | The set's files at their paths relative to `/`, bind-mounted read-only (`nosuid,nodev`) over each slot. |
| `applied/files.json`, `manifest.json`, `config.json` | The set's file list and metadata as downloaded, what otaclient checks the tree against. |
| `previous/` | The set before, only while a switch is in progress. |
| `staging/` | A set being prepared. |
| `history.jsonl` (`.1`) | One JSON object per line per event (`applied`, `running`, `dropped`, `tree_mismatch`, …), rotated at 1 MiB. |

The mounts are also written into a managed block of `/etc/fstab` (`nofail`), so that a reboot lays the set again before anything reads the slots.
A full OTA writes a new slot from its image: the set is not carried over, only the history is.

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

Any other field is otaclient's own and may change.

The file is replaced as a whole (written to a temporary file and renamed), and the directory holding it is renamed during a switch: a reader reads the whole file, takes a missing file as "no set applied", and reads it again when it changes.
A record whose `base_image_id` is not the running image's ID is not this slot's set: otaclient drops such a set at its next start, and a reader takes it as "no set applied".

## Status

While a set is applied, otaclient reports the parameter release (`release_name`, `release_id`) and the parameter image's ID as the ECU's `image_id`.
When the set is dropped, the release recorded before it is reported again.
The running image's own ID is in its version file, which a set does not change.
