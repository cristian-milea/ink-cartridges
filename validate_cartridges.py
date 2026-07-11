#!/usr/bin/env python3
"""Validate every cartridge under apps/ against the Ink Cartridge contract.

Mirrors the checks the device-side host performs on install, plus the catalog
invariants, so a malformed cartridge fails CI instead of a maintainer's eyeballs.
See docs/ink-cartridge-ui-schema.md and docs/ink-cartridge-catalog-schema.md.

Usage:
    python3 validate_cartridges.py            # validate every cartridge in apps/
    python3 validate_cartridges.py apps/hello # validate one (dir path or name)

Exit 0 if all cartridges are valid, 1 otherwise.
"""
import ast
import glob
import json
import os
import re
import sys

# --- contract constants (keep in sync with the schema docs) ---------------
# Store-grouping buckets. The companion app groups by the raw category string
# (no hardcoded enum), so this set is a catalog convention, not an app contract;
# keep values lowercase. See docs/ink-cartridge-catalog-schema.md.
CATEGORIES = {"games", "weather", "utilities", "entertainment", "lifestyle", "reference"}
PERMISSIONS = {"location", "notifications", "network"}  # runtime allowlist
WIDGET_TYPES = {
    "column", "row", "spacer", "divider", "text", "state_text", "image",
    "chart", "button", "switch", "slider", "text_field", "select", "when",
    "dpad",
}
ACTION_TYPES = {"push", "sync", "set_local", "request_permission"}
DPAD_DIRECTIONS = {
    "up", "down", "left", "right",
    "up_left", "up_right", "down_left", "down_right",
}
SCHEMA_VERSIONS = {1, 2}
MAX_PACKAGE_BYTES = 256 * 1024  # device install cap, summed across files
REQUIRED_FIELDS = ("name", "icon", "version", "author", "description")
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")  # hyphenated manifest name
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _normalise(s):
    """Host normalisation: lowercase, every run of non-alphanumerics -> '-'.

    Lets `tide_sun` (module stem) compare equal to `tide-sun` (manifest name).
    """
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _class_attrs(py_source):
    """Return {name, version, icon} string literals from the first class that
    defines a `name` attribute. Missing keys are simply absent from the dict."""
    tree = ast.parse(py_source)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        attrs = {}
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if not (len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
                continue
            key = stmt.targets[0].id
            if key in ("name", "version", "icon") and isinstance(stmt.value, ast.Constant):
                attrs[key] = stmt.value.value
        if "name" in attrs:
            return attrs
    return {}


def _validate_action(action, errors, path):
    if not isinstance(action, dict):
        errors.append(f"{path}: action must be an object")
    elif action.get("type") not in ACTION_TYPES:
        errors.append(f"{path}: unknown action type {action.get('type')!r}; allowed: {sorted(ACTION_TYPES)}")


def _validate_ui_node(node, errors, path="ui"):
    """Validate a widget node, recursing only into real child slots.

    `children` (column/row) and a `when`'s `then`/`else` hold widgets; `action`,
    `payload`, `options`, etc. are data with their own vocabularies, so we don't
    treat every nested `type` as a widget type.
    """
    if not isinstance(node, dict):
        errors.append(f"{path}: widget node must be an object")
        return
    t = node.get("type")
    if t not in WIDGET_TYPES:
        errors.append(f"{path}: unknown widget type {t!r}")
        return
    for akey in ("action", "action_on", "action_off"):
        if akey in node:
            _validate_action(node[akey], errors, f"{path}.{akey}")
    if t in ("column", "row"):
        children = node.get("children", [])
        if not isinstance(children, list):
            errors.append(f"{path}.children must be a list")
        else:
            for i, c in enumerate(children):
                _validate_ui_node(c, errors, f"{path}.children[{i}]")
    elif t == "when":
        for branch in ("then", "else"):
            if branch in node:
                items = node[branch] if isinstance(node[branch], list) else [node[branch]]
                for i, c in enumerate(items):
                    _validate_ui_node(c, errors, f"{path}.{branch}[{i}]")
    elif t == "dpad":
        if not any(node.get(axis) for axis in ("vertical", "horizontal", "diagonal")):
            errors.append(f"{path}: dpad requires at least one of vertical/horizontal/diagonal to be true")
        actions = node.get("actions", {})
        if not isinstance(actions, dict):
            errors.append(f"{path}.actions must be an object")
        else:
            bad_keys = set(actions) - DPAD_DIRECTIONS
            if bad_keys:
                errors.append(f"{path}.actions has unknown direction keys {sorted(bad_keys)}; allowed: {sorted(DPAD_DIRECTIONS)}")
            for key, action in actions.items():
                if key in DPAD_DIRECTIONS:
                    _validate_action(action, errors, f"{path}.actions.{key}")
        if "center" in node:
            _validate_action(node["center"], errors, f"{path}.center")


def _validate_secrets(secrets, errors):
    if not isinstance(secrets, list):
        errors.append("requires.secrets must be a list")
        return
    for i, s in enumerate(secrets):
        if not isinstance(s, dict):
            errors.append(f"requires.secrets[{i}] must be an object (bare strings are rejected)")
            continue
        for f in ("key", "label"):
            if not isinstance(s.get(f), str) or not s[f]:
                errors.append(f"requires.secrets[{i}].{f} is required and must be a non-empty string")
        if "description" in s and not isinstance(s["description"], str):
            errors.append(f"requires.secrets[{i}].description must be a string")
        if "optional" in s and not isinstance(s["optional"], bool):
            errors.append(f"requires.secrets[{i}].optional must be a boolean")


def _validate_data_source(ds, errors):
    if not isinstance(ds, dict):
        errors.append("data_source must be an object")
        return
    if ds.get("type") != "http":
        errors.append('data_source.type must be "http"')
    if not isinstance(ds.get("url"), str) or not ds["url"]:
        errors.append("data_source.url is required and must be a non-empty string")
    if "method" in ds and ds["method"] != "GET":
        errors.append('data_source.method must be "GET" (POST is reserved)')
    needs = ds.get("needs", [])
    if not isinstance(needs, list) or not all(isinstance(n, str) for n in needs):
        errors.append("data_source.needs must be a list of strings")
    if "auto_sync" in ds and not isinstance(ds["auto_sync"], bool):
        errors.append("data_source.auto_sync must be a boolean")
    if "min_sync_seconds" in ds and not (isinstance(ds["min_sync_seconds"], int)
                                         and not isinstance(ds["min_sync_seconds"], bool)):
        errors.append("data_source.min_sync_seconds must be an integer")


def validate_cartridge(directory):
    """Return (name, [errors]) for one apps/<name>/ directory."""
    errors = []
    dirname = os.path.basename(directory.rstrip("/"))

    pys = [p for p in glob.glob(os.path.join(directory, "*.py"))
           if os.path.basename(p) != "__init__.py"]
    manifests = glob.glob(os.path.join(directory, "*.manifest.json"))
    uis = glob.glob(os.path.join(directory, "*.ui.json"))
    icon_png = os.path.join(directory, "icon.png")

    if len(pys) != 1:
        errors.append(f"expected exactly one cartridge .py, found {[os.path.basename(p) for p in pys]}")
    if len(manifests) != 1:
        errors.append(f"expected exactly one *.manifest.json, found {len(manifests)}")
    if len(uis) > 1:
        errors.append(f"at most one *.ui.json allowed, found {len(uis)}")
    if len(manifests) != 1:
        return dirname, errors  # can't go further without a single manifest

    # --- manifest ---------------------------------------------------------
    try:
        manifest = json.load(open(manifests[0], encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return dirname, errors + [f"manifest does not parse: {e}"]

    for f in REQUIRED_FIELDS:
        if not isinstance(manifest.get(f), str) or not manifest[f]:
            errors.append(f"manifest.{f} is required and must be a non-empty string")

    name = manifest.get("name")
    if isinstance(name, str):
        if not NAME_RE.match(name):
            errors.append(f"name {name!r} must be lowercase, hyphen-separated (a-z, 0-9, -)")
        if name != dirname:
            errors.append(f"name {name!r} must match its directory name {dirname!r}")
    if isinstance(manifest.get("icon"), str) and not 1 <= len(manifest["icon"]) <= 2:
        errors.append(f"icon {manifest['icon']!r} must be a 1-2 char monogram")
    if isinstance(manifest.get("version"), str) and not SEMVER_RE.match(manifest["version"]):
        errors.append(f"version {manifest['version']!r} must be semver MAJOR.MINOR.PATCH")
    if manifest.get("category") not in CATEGORIES:
        errors.append(f"category {manifest.get('category')!r} must be one of {sorted(CATEGORIES)}")
    for opt in ("homepage", "license", "long_description"):
        if opt in manifest and not isinstance(manifest[opt], str):
            errors.append(f"manifest.{opt} must be a string")
    if "schema_version" in manifest and manifest["schema_version"] not in SCHEMA_VERSIONS:
        errors.append(f"schema_version must be one of {sorted(SCHEMA_VERSIONS)}")

    req = manifest.get("requires")
    if req is not None:
        if not isinstance(req, dict):
            errors.append("requires must be an object")
        else:
            perms = req.get("permissions", [])
            if not isinstance(perms, list):
                errors.append("requires.permissions must be a list")
            else:
                bad = set(perms) - PERMISSIONS
                if bad:
                    errors.append(f"requires.permissions has unknown entries {sorted(bad)}; allowed: {sorted(PERMISSIONS)}")
            if "secrets" in req:
                _validate_secrets(req["secrets"], errors)

    if "data_source" in manifest:
        _validate_data_source(manifest["data_source"], errors)

    # --- python plugin ----------------------------------------------------
    if len(pys) == 1:
        src = open(pys[0], encoding="utf-8").read()
        try:
            attrs = _class_attrs(src)
        except SyntaxError as e:
            attrs = None
            errors.append(f"{os.path.basename(pys[0])} does not parse: {e}")
        if attrs is not None:
            if not attrs:
                errors.append(f"{os.path.basename(pys[0])}: no class with a `name` attribute found")
            else:
                if isinstance(name, str) and "name" in attrs and \
                        _normalise(attrs["name"]) != _normalise(name):
                    errors.append(f"class name {attrs['name']!r} != manifest name {name!r}")
                if isinstance(manifest.get("version"), str) and \
                        attrs.get("version") != manifest["version"]:
                    errors.append(f"class version {attrs.get('version')!r} != manifest version {manifest['version']!r} (bump both)")

    # --- ui.json ----------------------------------------------------------
    if len(uis) == 1:
        try:
            ui = json.load(open(uis[0], encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            errors.append(f"ui.json does not parse: {e}")
        else:
            _validate_ui_node(ui, errors)

    # --- icon.png (optional) ---------------------------------------------
    if os.path.isfile(icon_png):
        with open(icon_png, "rb") as f:
            if f.read(8) != PNG_MAGIC:
                errors.append("icon.png is not a valid PNG (bad signature)")

    # --- package size -----------------------------------------------------
    paths = pys + manifests + uis + ([icon_png] if os.path.isfile(icon_png) else [])
    total = sum(os.path.getsize(p) for p in paths)
    if total > MAX_PACKAGE_BYTES:
        errors.append(f"package is {total} bytes, over the {MAX_PACKAGE_BYTES}-byte install cap")

    return dirname, errors


def main(argv):
    if argv:
        dirs = [a if os.path.isdir(a) else os.path.join("apps", a) for a in argv]
    else:
        dirs = sorted(d for d in glob.glob("apps/*") if os.path.isdir(d))

    results = [validate_cartridge(d) for d in dirs]

    # cross-cartridge: names are install keys and must be unique.
    seen = {}
    for d, (name, _) in zip(dirs, results):
        seen.setdefault(name, []).append(d)
    dupes = {n: ds for n, ds in seen.items() if len(ds) > 1}

    ok = True
    for name, errors in results:
        if errors:
            ok = False
            print(f"✗ {name}")
            for e in errors:
                print(f"    - {e}")
        else:
            print(f"✓ {name}")
    for n, ds in dupes.items():
        ok = False
        print(f"✗ duplicate cartridge name {n!r} in {ds}")

    print()
    if ok:
        print(f"All {len(results)} cartridges valid.")
        return 0
    print("Validation failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
