# pve-ui-overlays

One owner of Proxmox VE's `index.html.tpl`, any number of web UI overlays.

Proxmox VE has no plug-in API for its web UI. Packages that add a script to
the PVE page ([qsm-pve-direct](https://github.com/rdmitry0911/qsm-rd)'s
console entry, [qdev-host-pve](https://github.com/rdmitry0911/qdev-host)'s
Hardware panel, ...) used to divert `/usr/share/pve-manager/index.html.tpl`
themselves. A file has exactly one dpkg diversion, so the second such package
found a "foreign diversion" and the outcome depended on the install order.
`pve-ui-overlays` owns that single diversion and renders every registered
overlay into the page; a client package only describes itself and depends on
this package. Nothing is loaded into pveproxy or pvedaemon.

```text
/usr/share/pve-ui-overlays/overlays.d/qsm-pve-direct.json   ─┐
/usr/share/pve-ui-overlays/overlays.d/qdev-host.json        ─┤  pve-ui-overlays reconcile
/var/lib/pve-ui-overlays/overlays.d/*.json  (API-registered) ─┤  (dpkg triggers, path unit, API)
/etc/pve-ui-overlays/overlays.d/*.json      (administrator)  ─┘            │
                                                                            ▼
          stock template (diverted, allow-listed by pve-manager version + sha256)
          + <!-- pve-ui-overlays begin --> … one block per overlay … <!-- end -->
```

## Client contract

1. Ship a descriptor `/usr/share/pve-ui-overlays/overlays.d/ID.json` and the
   script(s) it names under `/usr/share/pve-manager/js/` (pveproxy serves that
   directory as `/pve2/js/`).
2. `Depends: pve-ui-overlays`.
3. Nothing else. dpkg file triggers on `overlays.d`, `manifest.d`,
   `/usr/share/pve-manager/js` and the template itself make this package
   re-render the page when a descriptor, a script or pve-manager changes, on
   install and on removal. Calling `pve-ui-overlays reconcile` from a
   maintainer script is harmless but not needed.
4. Configuration that must reach the browser goes through `globals`: a
   literal, or selected keys of a JSON file the client owns (never the whole
   file: it may hold secrets). After changing such a file, run
   `pve-ui-overlays reconcile` (or call `reconcile()` from Python).
5. A client may ship allow-list rows for newer pve-manager releases in
   `/usr/share/pve-ui-overlays/manifest.d/NAME.sha256` (same format as the
   main list) without waiting for a pve-ui-overlays release.

### Descriptor

```json
{
  "id": "qdev-host",
  "order": 50,
  "description": "qdev-host devices in the VM Hardware panel",
  "scripts": ["/pve2/js/qdev-host-hardware.js"],
  "globals": {
    "QDEV_HOST_ENDPOINT": {"file": "/etc/qdev-host/ui.json", "keys": ["port"], "default": {"port": 8017}},
    "QDEV_HOST_FLAGS": {"value": {"beta": true}}
  },
  "requires": ["/usr/lib/qdev-host/pve/qdev_host_api.py"]
}
```

| Key | Meaning |
|---|---|
| `id` | `[a-z0-9][a-z0-9-]{0,31}`, equal to the file name without `.json`; also the cache key suffix |
| `order` | 0..999, lower loads first (default 50); ties by id |
| `scripts` | `/pve2/js/NAME.js` paths; each must exist as a root-owned regular file under `/usr/share/pve-manager/js/` |
| `globals` | `window.NAME = …` before the scripts. `{"value": any}` or `{"file": "/abs.json", "keys": [...], "default": any}`: picked keys of the file merged over `default`; omitted when the file is absent and there is no default |
| `requires` | optional absolute paths; the overlay is skipped while any is missing (e.g. a service the UI needs) |
| `description` | shown by `pve-ui-overlays list` |

The rendered page, right after `pvemanagerlib.js`:

```html
    <!-- pve-ui-overlays begin: package-managed, do not edit (run `pve-ui-overlays reconcile`) -->
    <!-- overlay qdev-host -->
    <script type="text/javascript">window.QDEV_HOST_ENDPOINT={"port":8017};</script>
    <script type="text/javascript" src="/pve2/js/qdev-host-hardware.js?ver=[% version %]-qdev-host-3f1c…"></script>
    <!-- pve-ui-overlays end -->
```

The `?ver=` suffix carries the script's content hash, so a browser never
reuses an older script after a client upgrade.

## CLI

```text
pve-ui-overlays reconcile          render the installed overlays (idempotent; writes only on change)
pve-ui-overlays status [--json]    active | stale | inactive | alongside-legacy | disabled-unsupported-pve | foreign-diversion
pve-ui-overlays list [--json]      overlays, sources, problems
pve-ui-overlays check [FILE]       validate one descriptor, or all installed ones (exit 1 on problems)
pve-ui-overlays register FILE      install a run-time descriptor (/var/lib/pve-ui-overlays/overlays.d) and reconcile
pve-ui-overlays unregister ID      forget a run-time descriptor and reconcile
pve-ui-overlays enable|disable ID  switch an overlay without uninstalling its package
pve-ui-overlays remove             give PVE its stock page back (overlays stay installed)
pve-ui-overlays render             print the page as reconcile would write it
pve-ui-overlays template-hash      the allow-list row (version<TAB>sha256) for this node
```

## Python API

```python
import pve_ui_overlays as ui

ui.check({"id": "x", "scripts": ["/pve2/js/x.js"]})   # raises InvalidDescriptor
ui.register({...})                                     # /var/lib/pve-ui-overlays/overlays.d/x.json + reconcile
ui.unregister("x")
ui.disable("x"); ui.enable("x")
result = ui.reconcile()                                # Result(state, overlays, skipped, changed, message, pve_manager_version)
result = ui.status()                                   # same, without writing
for overlay in ui.list_overlays(): ...                 # Overlay(id, order, scripts, globals, source, enabled, problems)
version, sha = ui.template_hash()
```

Exceptions: `OverlayError` (base), `UnsupportedPve`, `UnsafeState`,
`InvalidDescriptor`. `Manager(root=Path(...))` gives a sandboxed instance for
tests (file-based diversion emulation, no dpkg).

## Safety rules

- The stock template is patched only when the installed `pve-manager`
  version **and** the template's SHA-256 are in the allow-list
  (`/usr/share/pve-ui-overlays/pve-manager-index-template.sha256` +
  `manifest.d/*.sha256`). After an unreviewed pve-manager upgrade the stock
  page is restored (`status`: `disabled-unsupported-pve`) until a row is
  added; `pve-ui-overlays template-hash` prints the row to review.
- Overlays with a missing script, a missing `requires` path or an invalid
  descriptor are skipped and reported; they never break the others.
- All files are read with the same checks (regular, root-owned, not
  group/world writable, bounded size); the template is written atomically and
  only when its content changes.
- Removing the last overlay, or this package, gives PVE its untouched stock
  template back.

## Coexistence with older clients

An installed `qsm-pve-direct` that still diverts the template itself is left
alone: the overlays are inserted into its rendered copy (`status`:
`alongside-legacy`), and `pve-ui-overlays-reconcile.path` re-inserts them
whenever it re-renders. A script the legacy render already carries is not
duplicated. When that qsm-pve-direct is upgraded to a descriptor-based
version its own prerm restores the stock template, and the next reconcile
takes the diversion. An older `qdev-host-pve` diversion is taken over
directly.

## Build

```bash
scripts/build-deb-in-chroot.sh     # dpkg-buildpackage in a trixie chroot (debhelper, dh-python)
python3 tests/test_overlays.py     # sandbox tests, no root needed
```
