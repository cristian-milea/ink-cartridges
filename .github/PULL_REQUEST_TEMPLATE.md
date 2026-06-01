<!-- Thanks for contributing! Please fill this in and tick the checklist. -->

## What this PR does



## Type

- [ ] New cartridge
- [ ] Change to an existing cartridge
- [ ] Host plugin (`pwnagotchi-plugin/`)
- [ ] Docs / tooling / other

## Checklist

- [ ] I read [`CONTRIBUTING.md`](../CONTRIBUTING.md).
- [ ] **Cartridge changes:** I ran `python3 build_index.py` and committed the
      regenerated `index.json`.
- [ ] **Cartridge changes:** I bumped `version` (semver) in **both** the `.py`
      class and the `manifest.json`.
- [ ] `category` is one of `info` / `fun` / `tools` / `system`, and any
      `requires.permissions` are within `location` / `notifications` / `network`.
- [ ] No hard-coded secrets/API keys — they're declared in `requires.secrets`
      and referenced as `{{secret.<key>}}`.
- [ ] **Host plugin changes:** I edited `pwnagotchi-plugin/src/`, ran
      `python3 pwnagotchi-plugin/build.py`, and committed the rebuilt
      `ink-cartridge.py`.

## Notes for reviewers


