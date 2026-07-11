# Ink Cartridge catalog schema (v1)

A curated public index of installable Ink Cartridges. Hosted at
[`cristian-milea/ink-cartridges`](https://github.com/cristian-milea/ink-cartridges).
The companion app fetches `index.json` over HTTPS, lets the user browse apps, and
downloads the file set for one-tap install.

Catalog URL:
`https://raw.githubusercontent.com/cristian-milea/ink-cartridges/main/index.json`

This document is the source of truth for the index schema and the repo
layout; the companion app implements against it.

> **index.json is generated, not hand-written.** Each cartridge's
> `<stem>.manifest.json` is the single source of truth for its intrinsic
> metadata (`name`, `icon`, `version`, `author`, `description`, `category`,
> `long_description`, `requires`, …). The catalog's `build_index.py` ingests
> every manifest and emits `index.json`, adding only registry-scoped fields
> (`files.*`, `icon_url`, `size_bytes`, `updated_at`). Editing a manifest and
> re-running the generator is the only supported way to change the index; a CI
> check (`build_index.py --check`) fails on a stale index. This is the
> npm/registry model — the package manifest is canonical, the index is a cache.

## Repo layout

```
ink-cartridges/                    (public GitHub repo)
├── index.json                       (required — the catalog index)
├── apps/
│   ├── hello/
│   │   ├── hello.py
│   │   ├── hello.manifest.json
│   │   └── hello.ui.json
│   ├── magic8/
│   │   ├── magic8.py
│   │   ├── magic8.manifest.json
│   │   └── magic8.ui.json
│   ├── tide-sun/
│   │   ├── tide_sun.py
│   │   ├── tide_sun.manifest.json
│   │   ├── tide_sun.ui.json
│   │   └── icon.png                (optional, ~96×96 PNG for store)
│   └── weather/
│       └── ...
└── README.md
```

The phone fetches:
```
https://raw.githubusercontent.com/<owner>/ink-cartridges/main/index.json
```

All `files.*` and `icon_url` paths in entries are **relative to the index
URL's directory**. The phone resolves them with standard URL joining.

## index.json

```jsonc
{
  "schema_version": 1,
  "updated_at": "2026-05-25T10:00:00Z",
  "apps": [
    {
      // ---- copied verbatim from the manifest (canonical) ----
      "name":              "weather",                      // matches device-side .name
      "icon":              "W",                            // 1–2 char monogram
      "version":           "1.2.0",                        // semver
      "author":            "cristian-milea",
      "description":       "Current conditions + 12h ...", // ~1 sentence
      "long_description":  "Markdown allowed. Optional.",  // shown on detail screen
      "category":          "weather",                      // store-group slug; see below
      "homepage":          "https://...",                  // optional
      "license":           "MIT",                          // optional
      "requires": {
        "permissions": ["location"],
        "secrets": [                                       // object form; see cartridge schema
          { "key": "openweather", "label": "OpenWeather API key",
            "description": "Free key from openweathermap.org.", "optional": false }
        ]
      },
      // ---- added by the generator (registry-scoped) ----
      "icon_url":          "apps/weather/icon.png",        // present only if icon.png exists
      "screenshots":       [],                             // optional, array of URLs
      "min_host_schema":   1,                              // optional, host schema floor
      "min_app_version":   "1.1",                          // floor on the companion app version
      "files": {
        "py":       "apps/weather/weather.py",
        "manifest": "apps/weather/weather.manifest.json",
        "ui":       "apps/weather/weather.ui.json"        // omitted when no ui.json
      },
      "size_bytes": 8507    // sum across files
    }
    // ...
  ]
}
```

### Field rules

- All **canonical** fields above are copied from the cartridge's
  `manifest.json` by the generator; never edit them in `index.json`. The
  **registry-scoped** fields are computed from the repo layout.
- `name` MUST match the Python class's `.name` attribute (existing Ink Cartridge
  invariant). It is also the install key on the device.
- `requires.secrets` entries are objects (`key`, `label`, `description?`,
  `optional?`) — see the cartridge schema's Secrets section. The device
  re-validates the downloaded manifest on install.
- `version` MUST be semver (`MAJOR.MINOR.PATCH`). The phone compares it to
  the installed app's manifest `version` to decide whether to badge
  "Update available."
- `category` is a lowercase store-grouping slug. The companion app groups
  cartridges by this raw string and renders it as the section header, so keep
  the catalog's values consistent. The curated set is `games`, `weather`,
  `utilities`, `entertainment`, `lifestyle`, `reference`.
- `requires.permissions` MUST be a subset of the runtime allowlist
  (`location`, `notifications`, `network`). It's a *promise* — at install
  time the device-side host re-validates against the downloaded
  manifest.json. They must agree.
- `files.py` MUST end in `.py`. The phone validates extensions before POST
  to avoid uploading a stray binary.
- `files.ui` is optional. Absent = the device still gets the .py +
  manifest; on the phone, the launcher shows the default panel.
- `min_app_version` is a `>=` floor (semver) on the *companion app's own*
  version, not the device — it's the answer to "can this phone's app render
  this cartridge's `ui.json`?" (new widget types are invisible to older app
  builds, since the phone — not the device — parses `ui.json`). The generator
  stamps it from the manifest's `schema_version` via a fixed table (see
  `ink-cartridge-ui-schema.md`'s "schema_version → min_app_version"); a
  manifest may also set `min_app_version` explicitly, which wins over the
  table. Unlike `version` (compared with strict `>` for the update badge),
  this is a floor: an app newer than the floor is always compatible.

### URL resolution

Given index URL `https://raw.githubusercontent.com/u/r/main/index.json`,
a `files.py` of `apps/weather/weather.py` resolves to
`https://raw.githubusercontent.com/u/r/main/apps/weather/weather.py`.

Absolute URLs are allowed but discouraged — keep everything inside the repo
so the catalog stays atomically updatable in one PR.

## Update detection

On Browse refresh, the phone:
1. Fetches `index.json`.
2. For each installed app, compares semver(catalog) > semver(installed).
3. Sets an "Update available" badge on the launcher icon and in Browse.

No automatic updates. The user must tap Update — same pipeline as Install.

**`min_app_version` gate.** Before offering Install/Update for a catalog
entry, the phone also checks `min_app_version` against its own app version.
If the installed companion app is older than the entry's floor, Install/Update
is disabled and Browse shows "Requires app {min_app_version}+ — update"
instead of the action button. This is independent of the version-compare
above: a cartridge can be a *newer* version than what's installed and still be
blocked, if the phone's app itself hasn't been updated far enough to render it.

## Failure modes

- `index.json` unreachable → Browse shows "Catalog unavailable" with retry.
  Installed apps still work normally; the launcher does not depend on the
  catalog at runtime.
- A file in `files.*` is 404 → install aborts, nothing is sent to the
  device.
- Total downloaded size exceeds 256 KB (device host install cap) → install
  aborts before POST.
- `category` is an unfamiliar string → it simply forms its own store section
  (the app groups by the raw value); a missing `category` defaults to `other`.

## Submission flow (community contributions — v2)

Out of scope for v1. The curated repo starts with maintainer-only PRs.
Once it stabilises, a `CONTRIBUTING.md` plus a `validate.py` CI check on
the repo can open it up.
