# Protected snapshot and isolated restore drill (revision 1)

Owner-invoked commands for the pinned Paperless 3.2.1 container. No service is
started by importing this package. A snapshot uses the native directory
`document_exporter`, then SQLite's online `Connection.backup` for the Library
queue. A drill imports the native export into fresh named data/media volumes on
an internal Docker network and checks every document through the actual Django
ORM. The drill never mounts a production database, media volume, runtime
directory, service config, or credential file.

## Private configuration and commands

Create an **external** JSON file, for example
`/ABSOLUTE/PRIVATE/maintenance.json`, mode `0600`. Create its `external_root`
directory first, mode `0700`, as a direct child of the host directory already
bound to `/usr/src/paperless/export`. The invoking host user must own the root
and have read access to the Library runtime and native media. Use the exact
running container name from this deployment; do not use a service alias.

```json
{
  "host_export_bind": "/ABSOLUTE/EXISTING/PAPERLESS_RUNTIME/export",
  "external_root": "/ABSOLUTE/EXISTING/PAPERLESS_RUNTIME/export/protected-snapshots",
  "library_config": "/ABSOLUTE/PRIVATE/library.json",
  "native_container": "EXACT_RUNNING_CONTAINER_NAME"
}
```

The Library config is read only when either command runs. Its `runtime_dir`
identifies `jobs.sqlite` and `staging`; its existing `allowed_file_roots` list
defines accepted local source files. The command copies that config **verbatim**
into the private snapshot. It may contain credentials. Do not place either
config, snapshots, drill directories, or receipts in Git or shared logs.
The native exporter can put mail or social-account credentials in its manifest
when no passphrase is supplied. Treat the entire snapshot as secret material;
any future off-device copy needs owner-approved encryption and a separate
verification receipt.

```sh
chmod 600 /ABSOLUTE/PRIVATE/maintenance.json /ABSOLUTE/PRIVATE/library.json
chmod 700 /ABSOLUTE/EXISTING/PAPERLESS_RUNTIME/export/protected-snapshots
PYTHONPYCACHEPREFIX=/ABSOLUTE/PRIVATE/pycache /ABSOLUTE/PYTHON -m extensions.library.maintenance.backup --config /ABSOLUTE/PRIVATE/maintenance.json
PYTHONPYCACHEPREFIX=/ABSOLUTE/PRIVATE/pycache /ABSOLUTE/PYTHON -m extensions.library.maintenance.restore --config /ABSOLUTE/PRIVATE/maintenance.json --receipt /ABSOLUTE/EXISTING/PAPERLESS_RUNTIME/export/protected-snapshots/snapshot-YYYYMMDDTHHMMSSZ-XXXXXXXXXXXX/receipt.json
```

Run from this repository root. The snapshot command defaults to retaining the
latest **7 successful** owned snapshot directories; `--retain N` changes that
positive count. Failed snapshots remain. Retention inspects an ownership marker
and rejects links, devices, hardlinks, unknown entries, and unsafe paths before
deleting. It does not prune drill receipts. Back up the entire protected root to
an owner-approved off-device destination separately; these receipts always say
`offsite_verified: false`.

The exporter target is precreated under the existing export bind and is invoked
without `--delete`, `--data-only`, `--no-archive`, or `--no-thumbnail`. All
exported originals, thumbnails, archives, metadata, manifest, and share-link
bundles are checked against manifest references and SHA-256 inventories. The
private inventory names the files; the public receipt contains only totals and
the private inventory hash. Files are `0600`, directories `0700`. The queue
backup checks SQLite integrity, batch/item counts, staging spool references,
and recovery-eligible `file` locators, supplement files, and OCR spool paths,
including blocked and `needs_ocr` items. Pending text with no
spool fails unless it is an explicit reanalyze item. Local files must be under
a canonical configured root, with no symlink in any ancestor, and pass the
source file suffix and sensitive-component checks. Missing, hardlinked,
nonregular, oversized, or changed sources fail the snapshot. The private
`reference-map.json` identifies each item and field, original absolute path,
and collision-safe copy. It and the SHA-256/size inventory stay inside the
private snapshot; the public receipt exposes counts and hashes only. URLs are
never fetched as source files.

The drill requires a successful receipt, rechecks every file/hash/count/marker,
and checks that the current native container and local image match the snapshot
identity. It creates a unique internal network, Redis broker, and fresh named
data/media volumes, with **no published ports**. The native export is mounted
read-only at `/snapshot`. It runs native `migrate` and `document_importer`
without `--data-only`, then compares all exported documents via Django ORM:
content SHA-256, original bytes SHA-256, every serialized document field other
than `modified` and `content`, tags, taxonomy, custom-field definitions and
instances (including reading states), users, groups, and object permissions.
`content` is compared by hash; `modified` is excluded because it is an
`auto_now` value; the generated `content_length` field is omitted by Django's
serializer. Any other unsupported field difference fails. The queue SQLite is
backed up again into a private drill copy. Every mapped source and spool is
checked against the original snapshot rows and inventory, copied to fresh
private drill storage, and rewritten in the drill SQLite. Hashes and counts
are checked again after rewrite. This validates the local recovery material;
it does not replay the Library queue. A production queue recovery would need
explicit owner configuration of accepted source roots and a separate recovery
procedure. Failed drills retain their exact owned resources and a private
`resources.json` for operator review. CLI failure output gives only the drill
ID and resource kinds with short container/network ID prefixes or drill-generated
volume names. Successful drills write a success receipt before removing
only their labeled container, volume, and network IDs; cleanup failure remains
visible as `cleanup: pending`.

## Optional launchd schedule template

This is a template, not an installed job. Replace every placeholder, keep the
config and output outside Git, and have the owner install it only after an
authorized live smoke check. The scheduled job runs snapshots only; restore
drills remain manual.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>OWNER.LABEL.paperless-library-snapshot</string>
  <key>WorkingDirectory</key><string>/ABSOLUTE/REPO/ROOT</string>
  <key>ProgramArguments</key><array>
    <string>/ABSOLUTE/PYTHON</string><string>-m</string>
    <string>extensions.library.maintenance.backup</string>
    <string>--config</string><string>/ABSOLUTE/PRIVATE/maintenance.json</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONPYCACHEPREFIX</key><string>/ABSOLUTE/PRIVATE/pycache</string>
  </dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
</dict></plist>
```

Limits: source export and queue backup are sequential rather than one atomic
cross-system transaction; concurrent ingress can make a queue snapshot reflect
a later instant than the native export. The drill proves local, isolated
same-version import and comparisons only. It does not prove an off-device copy,
production recovery time, or a real production restore. No live operation was
performed while developing revision 1.
