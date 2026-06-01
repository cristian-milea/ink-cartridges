# CLAUDE.md

## What this is

The public, curated catalog of installable Ink Cartridges — alternate-screen
apps for a pwnagotchi's e-ink display. Published to
`github.com/cristian-milea/ink-cartridges`; the companion app fetches
`https://raw.githubusercontent.com/cristian-milea/ink-cartridges/main/index.json`
on Browse and downloads each entry's files for one-tap install.

There are no built-in cartridges on the device any more — this catalog is the
only source.

This repo also vendors a **public, open-source copy of the host plugin** under
`pwnagotchi-plugin/` (readable source in `src/` + a reproducible single-file
build `ink-cartridge.py`, GPLv3) so users have a public, auditable place to get
the plugin they must install. Keep `pwnagotchi-plugin/src/` in sync with its
upstream and rebuild with `python3 pwnagotchi-plugin/build.py` when it changes.

> Machine-specific details — peer-repo paths, the private upstream repos, and the
> schema-doc locations — live in the git-ignored `CLAUDE.local.md`.

## Layout

- `apps/<name>/<stem>.py` + `<stem>.manifest.json` + optional
  `<stem>.ui.json` (+ optional `icon.png`) — one folder per cartridge.
- `index.json` — **generated** from the manifests by `build_index.py`. Do not
  hand-edit it.

**The manifest is the single source of truth.** Everything intrinsic to a
cartridge — `name`, `icon` (2-char monogram), `version`, `author`,
`description`, `category`, `long_description`, `requires`, `data_source` — lives
in its `<stem>.manifest.json`. `index.json` is a denormalised cache of those
manifests plus registry-only fields the script computes (`files.*`, `icon_url`,
`size_bytes`, `updated_at`). This is the npm/registry model: edit the manifest,
regenerate the index.

## Schema

The catalog/index contract (index shape, validation, URL resolution) and the
cartridge UI contract (manifest fields, widget vocabulary, the `data_source`
block, secret-slug namespacing, the auto-SyncCard convention) are vendored
publicly under `docs/`:

- `docs/ink-cartridge-catalog-schema.md` — index/catalog contract.
- `docs/ink-cartridge-ui-schema.md` — cartridge (manifest + `ui.json`) contract.

These are scrubbed copies of the upstream schema docs (`CLAUDE.local.md` has the
upstream location). For a working reference, copy an existing cartridge under
`apps/` (start from `apps/hello`, the minimal example).

## Conventions

- File-stem uses underscores (Python module names); manifest `name` uses
  hyphens. The host normalises both before comparing
  (e.g. `tide_sun.py` ↔ `name: "tide-sun"`).
- `icon` is canonical in the manifest. The device reads it from there (falling
  back to the Python class `.icon` only when a manifest is absent), so the class
  attribute is now just a safety net — keep it matching the manifest.
- Bump `version` in BOTH the .py class and the manifest when you change
  anything in the package; the companion's "Update available" badge uses the
  manifest version.

## Publishing

Regenerate the index, then commit. CI (`.github/workflows/index.yml`) runs
`build_index.py --check` and fails if you forget.

```
python3 build_index.py     # regenerate index.json from manifests
git add -A
git commit -m "<msg>"
git push
```

raw.githubusercontent.com has a 5-minute CDN cache; the Browse screen
shows the older index for up to that long after a push.
