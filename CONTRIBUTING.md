# Contributing to Ink Cartridges

Thanks for wanting to add to the catalog! This repo is the public, curated list
of cartridges the companion app installs. Contributions are welcome and reviewed.

> **Scope of this repo:** the **cartridges** (under `apps/`) and the vendored
> **host plugin** (under `pwnagotchi-plugin/`). The companion mobile app is
> closed-source and lives elsewhere — issues about the app UI belong on its own
> tracker, not here.

## What a cartridge is

A cartridge is a small package that takes over the pwnagotchi e-ink while
pwnagotchi keeps hunting. It's just files:

```
apps/<name>/
├── <stem>.py             # the cartridge: a Python class with name, icon, render()
├── <stem>.manifest.json  # metadata + capability declarations (source of truth)
├── <stem>.ui.json        # optional: the phone-side controls, rendered from data
└── icon.png              # optional: ~96×96 PNG store icon
```

The companion app renders each cartridge's phone-side UI **from the `ui.json`
data** — no app code from a cartridge ever runs on the phone. Most cartridges
need only a button or two, or nothing at all (a sync-only app).

## The contracts (read these first)

- **[`docs/ink-cartridge-ui-schema.md`](docs/ink-cartridge-ui-schema.md)** — the
  cartridge contract: every `manifest.json` field, the `ui.json` widget
  vocabulary, actions, `data_source`, secrets, and `published_state()`.
- **[`docs/ink-cartridge-catalog-schema.md`](docs/ink-cartridge-catalog-schema.md)**
  — the index contract: what `index.json` looks like and how URLs resolve.

**The manifest is the single source of truth.** `index.json` is *generated* from
the manifests — never hand-edit it.

## Add a cartridge

1. **Start from a template.** Copy [`apps/hello`](apps/hello) (the minimal
   example) to `apps/<your-name>/` and rename the files to your stem.
2. **Write the cartridge.** Fill in `<stem>.py` (a class with `name`, `icon`,
   `render()`), `<stem>.manifest.json`, and an optional `<stem>.ui.json`. Follow
   the schema docs above.
3. **Regenerate the index:**
   ```sh
   python3 build_index.py        # rebuilds index.json from all manifests
   ```
4. **Open a PR** with both your `apps/<name>/` files **and** the regenerated
   `index.json`.

CI runs `python3 build_index.py --check` and **fails the PR if `index.json` is
stale**, so don't skip step 3.

## Conventions

- **Naming:** the file stem uses underscores (a valid Python module name); the
  manifest `name` uses hyphens. The host normalises both before comparing
  (e.g. `tide_sun.py` ↔ `"name": "tide-sun"`).
- **`icon`** is the canonical 1–2 char monogram in the manifest. Keep the Python
  class's `.icon` attribute matching it (it's a fallback).
- **Versioning:** bump `version` (semver) in **both** the `.py` class and the
  manifest whenever you change anything in the package — the app's "Update
  available" badge compares the manifest version.
- **`category`** must be one of `games`, `weather`, `utilities`,
  `entertainment`, `lifestyle`, `reference`.
- **`requires.permissions`** must be a subset of `location`, `notifications`,
  `network`. Secrets (API keys) are declared in `requires.secrets` and referenced
  as `{{secret.<key>}}` — never hard-code a key in your cartridge.
- **Size:** the whole package must stay under the device install cap (256 KB
  across all files).

## Review expectations

v1 is **maintainer-curated**. A cartridge should be self-contained, safe (no
network calls outside a declared `data_source`, no shelling out, no reading files
it doesn't own), and actually work on a 1-bit e-ink. Expect review comments —
they're about keeping the catalog trustworthy, since every entry installs onto
people's devices with one tap.

## Licensing of contributions

By contributing a cartridge you agree it's licensed under this repo's catalog
license, **[MIT](LICENSE)**. (The host plugin under `pwnagotchi-plugin/` is
separately **GPLv3** — see [its README](pwnagotchi-plugin/README.md).)

## Reporting bugs & vulnerabilities

- **Bugs / cartridge ideas:** open an issue (templates provided).
- **Security issues:** please *don't* file a public issue — see
  [`SECURITY.md`](SECURITY.md) for private disclosure.
