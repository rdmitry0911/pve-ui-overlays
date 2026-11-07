#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""pve-ui-overlays: one owner of Proxmox VE's index.html.tpl, any number of overlays.

Proxmox VE has no plug-in API for its web UI.  Packages that add a script to
the PVE page (QSM Direct's console entry, qdev-host's Hardware panel, ...) used
to divert ``/usr/share/pve-manager/index.html.tpl`` themselves, which cannot
work for two of them: a file has exactly one dpkg diversion, so the second
package found a "foreign diversion" and the result depended on the install
order.  This package owns that single diversion and renders every registered
overlay into the template.  A client package only describes itself.

Contract for clients
--------------------
* Ship a descriptor ``/usr/share/pve-ui-overlays/overlays.d/ID.json`` and the
  script(s) it names under ``/usr/share/pve-manager/js/`` (served as
  ``/pve2/js/``), and ``Depends: pve-ui-overlays``.  dpkg file triggers make
  this package re-render the page whenever a descriptor, a script or
  pve-manager's template changes; nothing has to be called from maintainer
  scripts (calling ``pve-ui-overlays reconcile`` there is harmless).
* Configuration that must reach the browser is published through
  ``globals``: a literal value, or selected keys of a JSON file the client
  owns.  After changing that file, call ``pve-ui-overlays reconcile`` (or
  :func:`reconcile` from Python).
* The same operations are available as a Python API (``import
  pve_ui_overlays``): :func:`register`, :func:`unregister`, :func:`enable`,
  :func:`disable`, :func:`reconcile`, :func:`status`, :func:`list_overlays`,
  :func:`check`.  ``register`` stores a descriptor under
  ``/var/lib/pve-ui-overlays/overlays.d`` for overlays created at run time.

Descriptor (JSON object)::

    {
      "id": "qdev-host",                     # [a-z0-9][a-z0-9-]{0,31}, equals the file stem
      "order": 50,                           # lower loads first, default 50
      "description": "qdev-host devices in the VM Hardware panel",
      "scripts": ["/pve2/js/qdev-host-hardware.js"],
      "globals": {
        "QDEV_HOST_ENDPOINT": {"file": "/etc/qdev-host/ui.json", "keys": ["port"],
                               "default": {"port": 8017}}
        # or  "NAME": {"value": <any JSON>}
      },
      "requires": ["/usr/lib/qdev-host/pve/qdev_host_api.py"]   # optional: skip the overlay if absent
    }

Rendered after pvemanagerlib.js, in ``order``::

    <!-- pve-ui-overlays begin ... -->
    <!-- overlay qdev-host -->
    <script type="text/javascript">window.QDEV_HOST_ENDPOINT={"port":8017};</script>
    <script type="text/javascript" src="/pve2/js/qdev-host-hardware.js?ver=[% version %]-qdev-host-<sha256[:16]>"></script>
    <!-- pve-ui-overlays end -->

Safety: the stock template is patched only when the installed pve-manager
version and the template's SHA-256 are in the reviewed allow-list
(``/usr/share/pve-ui-overlays/pve-manager-index-template.sha256`` plus
``manifest.d/*.sha256`` rows shipped by clients); an unknown upgrade gets the
untouched stock template back until a row is added.  Overlays whose script is
missing are skipped, never fatal.  The template is written only when its
content changes.

Transition: an older qsm-pve-direct that still diverts the template itself is
left in place and the overlays are inserted into its rendered copy
("alongside-legacy" mode, re-applied by pve-ui-overlays-reconcile.path when it
re-renders); an older qdev-host-pve diversion is taken over.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

__version__ = "0.1.0"
PACKAGE = "pve-ui-overlays"
PVE_MANAGER = "pve-manager"

TEMPLATE = Path("usr/share/pve-manager/index.html.tpl")
JS_DIR = Path("usr/share/pve-manager/js")
JS_URL_PREFIX = "/pve2/js/"
SHARE = Path("usr/share/pve-ui-overlays")
VAR = Path("var/lib/pve-ui-overlays")
ETC = Path("etc/pve-ui-overlays")
OVERLAY_DIRS = (SHARE / "overlays.d", VAR / "overlays.d", ETC / "overlays.d")  # later wins on the same id
MANIFEST = SHARE / "pve-manager-index-template.sha256"
MANIFEST_DIRS = (SHARE / "manifest.d", ETC / "manifest.d")
DISABLED_DIR = VAR / "disabled"
STATE = VAR / "state.json"
DIVERSION_SUFFIX = f".{PACKAGE}-orig"

BEGIN = b"    <!-- pve-ui-overlays begin: package-managed, do not edit (run `pve-ui-overlays reconcile`) -->\n"
END = b"    <!-- pve-ui-overlays end -->\n"
ANCHOR = b'    <script type="text/javascript" src="/pve2/js/pvemanagerlib.js?ver=[% version %]"></script>\n'

# Older client versions that divert and render the template on their own.
LEGACY_RENDERERS: dict[str, dict[str, Any]] = {
    "qsm-pve-direct": {"suffix": ".qsm-pve-direct-orig",
                       "marker": b"<!-- qsm-pve-direct PVE 9 console overlay", "takeover": False},
    "qdev-host-pve": {"suffix": ".qdev-host-pve-orig",
                      "marker": b"<!-- qdev-host-pve PVE 9 hardware overlay begin", "takeover": True},
}

MAX_TEMPLATE = 256 * 1024
MAX_SCRIPT = 4 * 1024 * 1024
MAX_MANIFEST = 128 * 1024
MAX_DESCRIPTOR = 64 * 1024
MAX_GLOBAL_FILE = 64 * 1024
_ID = re.compile(r"\A[a-z0-9][a-z0-9-]{0,31}\Z")
_VERSION = re.compile(r"\A[A-Za-z0-9.+:~_-]{1,128}\Z")
_HASH = re.compile(r"\A[0-9a-f]{64}\Z")
_GLOBAL = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_SCRIPT_URL = re.compile(r"\A/pve2/js/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.js\Z")


class OverlayError(RuntimeError):
    """Base class: the page cannot be changed safely."""


class UnsupportedPve(OverlayError):
    """The installed pve-manager version/template is not in the allow-list."""


class UnsafeState(OverlayError):
    """A file or diversion is not in the expected, package-owned state."""


class InvalidDescriptor(OverlayError):
    """An overlay descriptor does not follow the contract."""


@dataclass
class Overlay:
    id: str
    order: int = 50
    description: str = ""
    scripts: list[str] = field(default_factory=list)
    globals: dict[str, dict[str, Any]] = field(default_factory=dict)
    requires: list[str] = field(default_factory=list)
    source: Path | None = None
    enabled: bool = True
    problems: list[str] = field(default_factory=list)   # why it is skipped (empty = rendered)
    warnings: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.enabled and not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "order": self.order, "description": self.description, "scripts": self.scripts,
                "globals": self.globals, "requires": self.requires, "source": str(self.source) if self.source else None,
                "enabled": self.enabled, "problems": self.problems, "warnings": self.warnings}


@dataclass
class Result:
    state: str                 # active | inactive | alongside-legacy | disabled-unsupported-pve | foreign-diversion | stale
    overlays: list[str]        # rendered overlay ids, in page order
    skipped: dict[str, str]    # id -> reason
    changed: bool = False
    message: str = ""
    pve_manager_version: str | None = None

    def __str__(self) -> str:
        parts = [self.state]
        if self.overlays:
            parts.append("overlays: " + ", ".join(self.overlays))
        for ident, why in self.skipped.items():
            parts.append(f"skipped {ident}: {why}")
        if self.message:
            parts.append(self.message)
        return "; ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "overlays": self.overlays, "skipped": self.skipped, "changed": self.changed,
                "message": self.message, "pve_manager_version": self.pve_manager_version}


# ----------------------------------------------------------------- helpers
def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_regular(path: Path, maximum: int, root_owner: bool) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise UnsafeState(f"{path}: {error.strerror}") from error
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or (root_owner and st.st_uid != 0) or st.st_size > maximum):
            raise UnsafeState(f"{path}: not a safe regular file (regular, not group/world writable, root-owned)")
        chunks = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise UnsafeState(f"{path}: too large")
        return data
    finally:
        os.close(fd)


def atomic_write(path: Path, content: bytes, mode: int, root_owner: bool) -> None:
    st = path.parent.lstat()
    if (not stat.S_ISDIR(st.st_mode) or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (root_owner and st.st_uid != 0)):
        raise UnsafeState(f"{path.parent}: unsafe directory")
    fd, tmp = tempfile.mkstemp(prefix=f".{PACKAGE}-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if root_owner:
            os.fchown(fd, 0, 0)
        os.write(fd, content)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as error:
        raise OverlayError(f"cannot write {path}: {error.strerror}") from error
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            Path(tmp).unlink()
        except FileNotFoundError:
            pass


def js_literal(value: Any) -> bytes:
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").encode("ascii")


def strip_block(template: bytes) -> bytes:
    while True:
        begin = template.find(BEGIN)
        if begin < 0:
            return template
        end = template.find(END, begin)
        if end < 0:
            raise UnsafeState("the pve-ui-overlays block in the PVE template is damaged (begin without end)")
        template = template[:begin] + template[end + len(END):]


# -------------------------------------------------------------- dpkg access
class Diversions:
    """dpkg's diversion database; the only backend used on a real root."""

    def __init__(self, executable: str = "/usr/bin/dpkg-divert") -> None:
        self.executable = executable

    def _run(self, args: list[str]) -> str:
        try:
            done = subprocess.run([self.executable, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True)
        except OSError as error:
            raise OverlayError("dpkg-divert is unavailable") from error
        if done.returncode != 0:
            raise OverlayError(f"dpkg-divert failed: {done.stderr.strip()}")
        return done.stdout.strip()

    def owner(self, target: Path) -> str | None:
        return self._run(["--listpackage", str(target)]) or None

    def add(self, target: Path, diverted: Path, package: str = PACKAGE) -> None:
        self._run(["--package", package, "--divert", str(diverted), "--rename", "--add", str(target)])

    def remove(self, target: Path, diverted: Path, package: str = PACKAGE) -> None:
        self._run(["--package", package, "--divert", str(diverted), "--rename", "--remove", str(target)])


class SandboxDiversions:
    """File-only diversion backend for --root tests; never touches the dpkg database."""

    def __init__(self, state: Path) -> None:
        self.state = state

    def owner(self, target: Path) -> str | None:
        try:
            return self.state.read_text().strip() or None
        except FileNotFoundError:
            return None

    def add(self, target: Path, diverted: Path, package: str = PACKAGE) -> None:
        if self.owner(target) is not None or diverted.exists():
            raise OverlayError("sandbox: diversion already exists")
        os.replace(target, diverted)
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(package + "\n")

    def remove(self, target: Path, diverted: Path, package: str = PACKAGE) -> None:
        if self.owner(target) != package:
            raise OverlayError("sandbox: diversion owner mismatch")
        if target.exists():
            raise OverlayError("sandbox: target still present")
        os.replace(diverted, target)
        self.state.unlink()


def pve_manager_version() -> str:
    try:
        done = subprocess.run(["/usr/bin/dpkg-query", "-W", "-f=${db:Status-Status}\t${Version}", PVE_MANAGER],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)
    except OSError as error:
        raise UnsupportedPve("pve-manager is unavailable") from error
    fields = done.stdout.strip().split("\t", 1)
    if (done.returncode != 0 or len(fields) != 2 or not _VERSION.match(fields[1])
            or fields[0] not in ("installed", "triggers-pending", "triggers-awaited")):
        raise UnsupportedPve("pve-manager is unavailable")
    return fields[1]


# ------------------------------------------------------------------ manager
class Manager:
    def __init__(self, root: Path = Path("/"), diversions: Any = None,
                 version_fn: Callable[[], str] = pve_manager_version, root_owner: bool | None = None) -> None:
        self.root = root
        self.testing = root != Path("/")
        self.div = diversions or (SandboxDiversions(root / VAR / "sandbox-diversion-owner") if self.testing
                                  else Diversions())
        self.version_fn = version_fn
        self.root_owner = (not self.testing) if root_owner is None else root_owner
        self.template = root / TEMPLATE
        self.diversion = Path(f"{self.template}{DIVERSION_SUFFIX}")

    # -- allow-list
    def manifest(self) -> dict[str, str]:
        files = [self.root / MANIFEST]
        for directory in MANIFEST_DIRS:
            files += sorted((self.root / directory).glob("*.sha256"))
        rows: dict[str, str] = {}
        for path in files:
            if not path.exists():
                continue
            try:
                text = read_regular(path, MAX_MANIFEST, self.root_owner).decode("ascii")
            except UnicodeDecodeError as error:
                raise UnsafeState(f"{path}: not ASCII") from error
            for line in text.splitlines():
                if not line or line.startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) != 2 or not _VERSION.match(fields[0]) or not _HASH.match(fields[1]):
                    raise UnsafeState(f"{path}: invalid manifest row {line!r}")
                rows.setdefault(fields[0], fields[1])
        if not rows:
            raise UnsafeState("no template allow-list installed")
        return rows

    def check_stock(self, stock_path: Path) -> tuple[str, bytes]:
        version = self.version_fn()
        want = self.manifest().get(version)
        if want is None:
            raise UnsupportedPve(f"pve-manager {version} is not in the template allow-list")
        stock = read_regular(stock_path, MAX_TEMPLATE, self.root_owner)
        if sha256(stock) != want:
            raise UnsupportedPve(f"stock template hash {sha256(stock)[:16]}... does not match the allow-list row for {version}")
        if stock.count(ANCHOR) != 1:
            raise UnsupportedPve("template layout unsupported: pvemanagerlib.js anchor not found exactly once")
        return version, stock

    def template_hash(self) -> tuple[str, str]:
        """(pve-manager version, sha256 of the stock template) of this node, for new allow-list rows."""
        owner = self.div.owner(self.template)
        if owner == PACKAGE:
            path = self.diversion
        elif owner in LEGACY_RENDERERS:
            path = Path(f"{self.template}{LEGACY_RENDERERS[owner]['suffix']}")
        else:
            path = self.template
        data = read_regular(path, MAX_TEMPLATE, self.root_owner)
        return self.version_fn(), sha256(data)

    # -- descriptors
    def parse_descriptor(self, data: Any, source: Path | None = None) -> Overlay:
        if not isinstance(data, dict):
            raise InvalidDescriptor("descriptor must be a JSON object")
        ident = data.get("id", source.stem if source else None)
        if not isinstance(ident, str) or not _ID.match(ident):
            raise InvalidDescriptor("id must match [a-z0-9][a-z0-9-]{0,31}")
        if source is not None and source.stem != ident:
            raise InvalidDescriptor(f"id {ident!r} must equal the file name {source.name!r} without .json")
        overlay = Overlay(id=ident, source=source)
        order = data.get("order", 50)
        if not isinstance(order, int) or isinstance(order, bool) or not 0 <= order <= 999:
            raise InvalidDescriptor("order must be an integer 0..999")
        overlay.order = order
        description = data.get("description", "")
        if not isinstance(description, str) or len(description) > 200:
            raise InvalidDescriptor("description must be a string of at most 200 characters")
        overlay.description = description
        scripts = data.get("scripts")
        if not isinstance(scripts, list) or not scripts or not all(isinstance(s, str) and _SCRIPT_URL.match(s) for s in scripts):
            raise InvalidDescriptor("scripts must be a non-empty list of /pve2/js/NAME.js paths")
        if len(set(scripts)) != len(scripts):
            raise InvalidDescriptor("scripts must not repeat")
        overlay.scripts = scripts
        globals_ = data.get("globals", {})
        if not isinstance(globals_, dict):
            raise InvalidDescriptor("globals must be an object")
        for name, spec in globals_.items():
            if not isinstance(name, str) or not _GLOBAL.match(name):
                raise InvalidDescriptor(f"global {name!r}: not a JavaScript identifier")
            if not isinstance(spec, dict) or ("value" in spec) == ("file" in spec):
                raise InvalidDescriptor(f"global {name}: give exactly one of \"value\" or \"file\"")
            if "file" in spec:
                if not isinstance(spec["file"], str) or not spec["file"].startswith("/"):
                    raise InvalidDescriptor(f"global {name}: file must be an absolute path")
                keys = spec.get("keys")
                if keys is not None and (not isinstance(keys, list) or not all(isinstance(k, str) for k in keys)):
                    raise InvalidDescriptor(f"global {name}: keys must be a list of strings")
            for extra in set(spec) - {"value", "file", "keys", "default"}:
                overlay.warnings.append(f"global {name}: unknown key {extra!r} ignored")
        overlay.globals = globals_
        requires = data.get("requires", [])
        if not isinstance(requires, list) or not all(isinstance(r, str) and r.startswith("/") for r in requires):
            raise InvalidDescriptor("requires must be a list of absolute paths")
        overlay.requires = requires
        for extra in set(data) - {"id", "order", "description", "scripts", "globals", "requires"}:
            overlay.warnings.append(f"unknown key {extra!r} ignored")
        return overlay

    def load_overlays(self) -> list[Overlay]:
        """All descriptors, later directories overriding earlier ones on the same id; problems recorded."""
        found: dict[str, Overlay] = {}
        for directory in OVERLAY_DIRS:
            for path in sorted((self.root / directory).glob("*.json")):
                try:
                    raw = read_regular(path, MAX_DESCRIPTOR, self.root_owner)
                    overlay = self.parse_descriptor(json.loads(raw.decode("utf-8")), path)
                except (OverlayError, ValueError, UnicodeDecodeError) as error:
                    overlay = Overlay(id=path.stem if _ID.match(path.stem) else f"invalid-{len(found)}", source=path,
                                      problems=[f"invalid descriptor: {error}"])
                found[overlay.id] = overlay
        for overlay in found.values():
            overlay.enabled = not (self.root / DISABLED_DIR / overlay.id).exists()
            if overlay.problems:
                continue
            for required in overlay.requires:
                if not (self.root / required.lstrip("/")).exists():
                    overlay.problems.append(f"missing {required}")
            for url in overlay.scripts:
                path = self.root / JS_DIR / url[len(JS_URL_PREFIX):]
                try:
                    read_regular(path, MAX_SCRIPT, self.root_owner)
                except UnsafeState as error:
                    overlay.problems.append(f"script {url}: {error}")
        return sorted(found.values(), key=lambda o: (o.order, o.id))

    # -- rendering
    def global_value(self, overlay: Overlay, name: str, spec: dict[str, Any]) -> tuple[bool, Any]:
        if "value" in spec:
            return True, spec["value"]
        path = self.root / spec["file"].lstrip("/")
        value: Any = None
        have_file = False
        try:
            raw = read_regular(path, MAX_GLOBAL_FILE, self.root_owner)
            value = json.loads(raw.decode("utf-8"))
            have_file = True
        except (UnsafeState, ValueError, UnicodeDecodeError) as error:
            if path.exists():
                overlay.warnings.append(f"global {name}: {error}")
        keys = spec.get("keys")
        if keys is not None:
            picked = {k: value[k] for k in keys if isinstance(value, dict) and k in value} if have_file else {}
            if isinstance(spec.get("default"), dict):
                merged = dict(spec["default"])
                merged.update(picked)
                return True, merged
            if picked or have_file:
                return True, picked
            return ("default" in spec), spec.get("default")
        if have_file:
            return True, value
        return ("default" in spec), spec.get("default")

    def render_block(self, overlays: list[Overlay]) -> bytes:
        out = [BEGIN]
        for overlay in overlays:
            out.append(f"    <!-- overlay {overlay.id} -->\n".encode("ascii"))
            for name, spec in overlay.globals.items():
                present, value = self.global_value(overlay, name, spec)
                if present:
                    out.append(b'    <script type="text/javascript">window.' + name.encode("ascii") + b"="
                               + js_literal(value) + b";</script>\n")
            for url in overlay.scripts:
                digest = sha256(read_regular(self.root / JS_DIR / url[len(JS_URL_PREFIX):], MAX_SCRIPT, self.root_owner))
                out.append(b'    <script type="text/javascript" src="' + url.encode("ascii")
                           + b"?ver=[% version %]-" + overlay.id.encode("ascii") + b"-" + digest[:16].encode("ascii")
                           + b'"></script>\n')
        out.append(END)
        return b"".join(out)

    def render(self, base: bytes, overlays: list[Overlay]) -> bytes:
        base = strip_block(base)
        if base.count(ANCHOR) != 1:
            raise UnsupportedPve("template layout unsupported: pvemanagerlib.js anchor not found exactly once")
        if not overlays:
            return base
        return base.replace(ANCHOR, ANCHOR + self.render_block(overlays), 1)

    # -- state
    def write_state(self, version: str, mode: str, overlays: list[Overlay]) -> None:
        (self.root / VAR).mkdir(parents=True, exist_ok=True)
        atomic_write(self.root / STATE, json.dumps({"pve_manager_version": version, "mode": mode,
                                                    "overlays": [o.id for o in overlays], "version": 1},
                                                   sort_keys=True).encode() + b"\n", 0o644, self.root_owner)

    def _plan(self) -> tuple[str, str | None, bytes, bytes | None]:
        """(mode, owner, base template, current template or None). mode: own | stock | legacy | takeover."""
        owner = self.div.owner(self.template)
        if owner == PACKAGE:
            return "own", owner, read_regular(self.diversion, MAX_TEMPLATE, self.root_owner), self._current()
        if owner is None:
            return "stock", None, self._current(), None
        legacy = LEGACY_RENDERERS.get(owner)
        if legacy is None:
            raise UnsafeState(f"PVE template is diverted by another package ({owner})")
        stock = read_regular(Path(f"{self.template}{legacy['suffix']}"), MAX_TEMPLATE, self.root_owner)
        if legacy["takeover"]:
            return "takeover", owner, stock, self._current()
        return "legacy", owner, stock, self._current()

    def _current(self) -> bytes:
        return read_regular(self.template, MAX_TEMPLATE, self.root_owner)

    def _usable(self, overlays: list[Overlay], base: bytes) -> tuple[list[Overlay], dict[str, str]]:
        """Overlays to render and the skip reasons of the rest (scripts already in a legacy render are skipped)."""
        rendered: list[Overlay] = []
        skipped: dict[str, str] = {}
        for overlay in overlays:
            if not overlay.enabled:
                skipped[overlay.id] = "disabled"
            elif overlay.problems:
                skipped[overlay.id] = "; ".join(overlay.problems)
            elif any((b'src="' + url.encode("ascii")) in base for url in overlay.scripts):
                skipped[overlay.id] = "already present in the legacy-rendered template"
            else:
                rendered.append(overlay)
        return rendered, skipped

    def reconcile(self) -> Result:
        """Bring the PVE page in line with the installed overlays. Idempotent; writes only on change."""
        overlays = self.load_overlays()
        try:
            mode, owner, base, current = self._plan()
            if mode == "takeover":
                legacy = LEGACY_RENDERERS[owner]
                if current is not None and legacy["marker"] not in current and BEGIN not in current:
                    raise UnsafeState(f"refusing to take over an unmanaged template diverted by {owner}")
                version, stock = self.check_stock(Path(f"{self.template}{legacy['suffix']}"))
                self.template.unlink()
                self.div.remove(self.template, Path(f"{self.template}{legacy['suffix']}"), package=owner)
                mode, owner, base, current = "stock", None, stock, None
            if mode == "legacy":
                version, _stock = self.check_stock(Path(f"{self.template}{LEGACY_RENDERERS[owner]['suffix']}"))
                rendered, skipped = self._usable(overlays, strip_block(current))
                new = self.render(current, rendered)
                changed = new != current
                if changed:
                    atomic_write(self.template, new, 0o644, self.root_owner)
                self.write_state(version, "alongside-legacy", rendered)
                return Result("alongside-legacy", [o.id for o in rendered], skipped, changed,
                              f"template is diverted by an older {owner}; overlays inserted into its copy",
                              version)
            version, stock = self.check_stock(self.diversion if mode == "own" else self.template)
            rendered, skipped = self._usable(overlays, b"")
            if not rendered:
                if mode == "own":
                    self._restore_own()
                    return Result("inactive", [], skipped, True, "no overlays to render; stock template restored", version)
                return Result("inactive", [], skipped, False, "no overlays to render", version)
            new = self.render(stock, rendered)
            if mode == "stock":
                self.div.add(self.template, self.diversion)
                try:
                    diverted = read_regular(self.diversion, MAX_TEMPLATE, self.root_owner)
                    if diverted != stock:
                        raise UnsafeState("template changed while diverting")
                    atomic_write(self.template, new, 0o644, self.root_owner)
                    self.write_state(version, "own", rendered)
                except BaseException:
                    try:
                        self._restore_own()
                    except OverlayError:
                        pass
                    raise
                return Result("active", [o.id for o in rendered], skipped, True, "", version)
            changed = new != current
            if changed:
                atomic_write(self.template, new, 0o644, self.root_owner)
            self.write_state(version, "own", rendered)
            return Result("active", [o.id for o in rendered], skipped, changed, "", version)
        except UnsupportedPve as error:
            # The important behaviour on a pve-manager upgrade: hand the stock template back untouched.
            self.remove()
            return Result("disabled-unsupported-pve", [], {o.id: "pve unsupported" for o in overlays}, True, str(error))

    def status(self) -> Result:
        """Like reconcile() but without writing: 'stale' means a reconcile would change the page."""
        overlays = self.load_overlays()
        try:
            mode, owner, base, current = self._plan()
        except UnsafeState as error:
            return Result("foreign-diversion", [], {}, False, str(error))
        try:
            version = self.version_fn()
            manifest_ok = self.manifest().get(version) is not None
        except OverlayError as error:
            return Result("disabled-unsupported-pve", [], {}, False, str(error))
        if not manifest_ok:
            return Result("disabled-unsupported-pve", [], {}, False, f"pve-manager {version} is not in the template allow-list", version)
        if mode in ("legacy", "takeover"):
            rendered, skipped = self._usable(overlays, strip_block(current or b""))
            expected = self.render(current or b"", rendered) if mode == "legacy" else None
            state = "alongside-legacy" if (mode == "legacy" and expected == current) else "stale"
            return Result(state, [o.id for o in rendered], skipped, False,
                          f"template is diverted by an older {owner}" + ("" if state != "stale" else "; run reconcile"), version)
        rendered, skipped = self._usable(overlays, b"")
        if mode == "stock":
            state = "inactive" if not rendered else "stale"
            return Result(state, [o.id for o in rendered], skipped, False,
                          "" if not rendered else "overlays installed but not rendered yet; run reconcile", version)
        expected = self.render(base, rendered) if rendered else base
        if not rendered:
            return Result("stale", [], skipped, False, "no overlays left; reconcile restores the stock template", version)
        return Result("active" if expected == current else "stale", [o.id for o in rendered], skipped, False,
                      "" if expected == current else "page differs from the installed overlays; run reconcile", version)

    def _restore_own(self) -> None:
        if self.div.owner(self.template) != PACKAGE:
            return
        patched = None
        if self.template.exists():
            patched = self._current()
            if BEGIN not in patched:
                raise UnsafeState("refusing to remove an unmanaged PVE template")
            self.template.unlink()
        try:
            self.div.remove(self.template, self.diversion)
        except OverlayError:
            if patched is not None:
                atomic_write(self.template, patched, 0o644, self.root_owner)
            raise
        try:
            (self.root / STATE).unlink()
        except FileNotFoundError:
            pass

    def remove(self) -> Result:
        """Give PVE its stock page back (own mode) or drop our block from a legacy render."""
        owner = self.div.owner(self.template)
        if owner == PACKAGE:
            self._restore_own()
            return Result("inactive", [], {}, True, "stock template restored")
        if owner in LEGACY_RENDERERS and not LEGACY_RENDERERS[owner]["takeover"] and self.template.exists():
            current = self._current()
            new = strip_block(current)
            if new != current:
                atomic_write(self.template, new, 0o644, self.root_owner)
            try:
                (self.root / STATE).unlink()
            except FileNotFoundError:
                pass
            return Result("inactive", [], {}, new != current, f"overlays removed from the {owner} template")
        return Result("inactive", [], {}, False, "")

    # -- registration API
    def register(self, descriptor: dict[str, Any] | os.PathLike[str] | str, reconcile: bool = True) -> Overlay:
        """Store a run-time descriptor under /var/lib/pve-ui-overlays/overlays.d and re-render."""
        if isinstance(descriptor, (str, os.PathLike)):
            path = Path(descriptor)
            data = json.loads(path.read_text(encoding="utf-8"))
            overlay = self.parse_descriptor(data, path if path.suffix == ".json" else None)
        else:
            data = descriptor
            overlay = self.parse_descriptor(data)
        target_dir = self.root / VAR / "overlays.d"
        target_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(target_dir / f"{overlay.id}.json", json.dumps(data, indent=2, sort_keys=True).encode() + b"\n",
                     0o644, self.root_owner)
        if reconcile:
            self.reconcile()
        return overlay

    def unregister(self, ident: str, reconcile: bool = True) -> bool:
        """Forget a run-time registration (package-shipped descriptors are dpkg's business). True if one existed."""
        if not _ID.match(ident):
            raise InvalidDescriptor("invalid overlay id")
        path = self.root / VAR / "overlays.d" / f"{ident}.json"
        existed = path.exists()
        if existed:
            path.unlink()
        if reconcile:
            self.reconcile()
        return existed

    def set_enabled(self, ident: str, enabled: bool, reconcile: bool = True) -> None:
        if not _ID.match(ident):
            raise InvalidDescriptor("invalid overlay id")
        flag = self.root / DISABLED_DIR / ident
        if enabled:
            flag.unlink(missing_ok=True)
        else:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
        if reconcile:
            self.reconcile()


# ----------------------------------------------------------- module API
_default: Manager | None = None


def manager() -> Manager:
    global _default
    if _default is None:
        _default = Manager()
    return _default


def reconcile() -> Result:
    return manager().reconcile()


def status() -> Result:
    return manager().status()


def remove() -> Result:
    return manager().remove()


def list_overlays() -> list[Overlay]:
    return manager().load_overlays()


def check(descriptor: dict[str, Any] | os.PathLike[str] | str) -> Overlay:
    """Validate a descriptor (dict or path) without installing it; raises InvalidDescriptor."""
    m = manager()
    if isinstance(descriptor, (str, os.PathLike)):
        path = Path(descriptor)
        return m.parse_descriptor(json.loads(path.read_text(encoding="utf-8")), path if path.suffix == ".json" else None)
    return m.parse_descriptor(descriptor)


def register(descriptor: dict[str, Any] | os.PathLike[str] | str, reconcile: bool = True) -> Overlay:
    return manager().register(descriptor, reconcile)


def unregister(ident: str, reconcile: bool = True) -> bool:
    return manager().unregister(ident, reconcile)


def enable(ident: str, reconcile: bool = True) -> None:
    manager().set_enabled(ident, True, reconcile)


def disable(ident: str, reconcile: bool = True) -> None:
    manager().set_enabled(ident, False, reconcile)


def template_hash() -> tuple[str, str]:
    return manager().template_hash()


# ------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pve-ui-overlays", description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)
    parser.add_argument("--package-version", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("reconcile", help="render the installed overlays into the PVE page (idempotent)")
    sub.add_parser("status", help="state of the page and the overlays, without writing")
    sub.add_parser("list", help="installed overlays, their sources and problems")
    p = sub.add_parser("check", help="validate a descriptor file (or all installed ones)")
    p.add_argument("file", nargs="?")
    p = sub.add_parser("register", help="install a run-time descriptor and reconcile")
    p.add_argument("file")
    p = sub.add_parser("unregister", help="remove a run-time descriptor and reconcile")
    p.add_argument("id")
    p = sub.add_parser("enable", help="enable an overlay and reconcile")
    p.add_argument("id")
    p = sub.add_parser("disable", help="disable an overlay (kept installed) and reconcile")
    p.add_argument("id")
    sub.add_parser("remove", help="give PVE its stock page back (overlays stay installed)")
    sub.add_parser("render", help="print the page as reconcile would write it")
    sub.add_parser("template-hash", help="print the allow-list row for this node's pve-manager")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    testing = root != Path("/")
    if args.package_version and not testing:
        print("pve-ui-overlays: --package-version is for --root tests only", file=sys.stderr)
        return 2
    if not testing and os.geteuid() != 0 and args.command not in ("status", "list", "check", "template-hash", "render"):
        print("pve-ui-overlays: must run as root", file=sys.stderr)
        return 2
    m = Manager(root, version_fn=(lambda: args.package_version) if args.package_version else pve_manager_version)
    try:
        if args.command in ("reconcile", "status", "remove"):
            result = getattr(m, args.command)()
            print(json.dumps(result.as_dict()) if args.json else f"pve-ui-overlays: {result}")
            return 0
        if args.command == "list":
            overlays = m.load_overlays()
            if args.json:
                print(json.dumps([o.as_dict() for o in overlays], indent=2))
            else:
                for o in overlays:
                    state = "disabled" if not o.enabled else ("skipped: " + "; ".join(o.problems) if o.problems else "ok")
                    print(f"{o.id:24s} order {o.order:3d}  {state}  {o.source}" + (f"\n{'':24s} {o.description}" if o.description else ""))
                    for w in o.warnings:
                        print(f"{'':24s} warning: {w}")
                if not overlays:
                    print("no overlays installed")
            return 0
        if args.command == "check":
            if args.file:
                path = Path(args.file)
                overlay = m.parse_descriptor(json.loads(path.read_text(encoding="utf-8")), path)
                for w in overlay.warnings:
                    print(f"warning: {w}")
                print(f"{overlay.id}: ok")
                return 0
            bad = [o for o in m.load_overlays() if o.problems]
            for o in bad:
                print(f"{o.id}: {'; '.join(o.problems)}")
            print("all overlays ok" if not bad else f"{len(bad)} overlay(s) with problems")
            return 1 if bad else 0
        if args.command == "register":
            overlay = m.register(args.file)
            print(f"pve-ui-overlays: registered {overlay.id}; {m.status()}")
            return 0
        if args.command == "unregister":
            existed = m.unregister(args.id)
            print(f"pve-ui-overlays: {'unregistered' if existed else 'no run-time registration for'} {args.id}; {m.status()}")
            return 0
        if args.command in ("enable", "disable"):
            m.set_enabled(args.id, args.command == "enable")
            print(f"pve-ui-overlays: {args.command}d {args.id}; {m.status()}")
            return 0
        if args.command == "render":
            mode, _owner, base, current = m._plan()
            overlays = m.load_overlays()
            rendered, _skipped = m._usable(overlays, strip_block(current) if mode == "legacy" else b"")
            sys.stdout.buffer.write(m.render(current if mode == "legacy" else base, rendered))
            return 0
        if args.command == "template-hash":
            version, digest = m.template_hash()
            print(f"{version}\t{digest}")
            return 0
    except OverlayError as error:
        print(f"pve-ui-overlays: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
