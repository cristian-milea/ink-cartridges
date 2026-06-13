# Security Policy

Ink Cartridges install code that runs on people's devices — the **host plugin**
runs inside pwnagotchi, and **cartridges** run on the device under it. We take
reports seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Email **inkcartridge@cristimilea.ro** with:

- what the issue is and where (catalog cartridge, the host plugin, or the index),
- steps to reproduce or a proof of concept,
- the impact you think it has.

You can also use GitHub's **private vulnerability reporting** ("Report a
vulnerability" under the repo's Security tab) if you prefer.

We aim to acknowledge within a few days and will keep you updated as we work on a
fix. Please give us reasonable time to address it before any public disclosure.

## Scope

In scope:

- **Cartridges in `apps/`** — anything a published cartridge does that it
  shouldn't (network calls outside its declared `data_source`, reading/writing
  files it doesn't own, shelling out, leaking secrets, crashing the host).
- **The host plugin in `pwnagotchi-plugin/`** — the device-side code that hosts
  cartridges and serves the Bluetooth link.
- **The index** — anything in `index.json` / the catalog that could mislead the
  app into installing the wrong files.

Out of scope:

- The closed-source companion mobile app (report those through its own channel).
- The upstream pwnagotchi project itself.
- Vulnerabilities that require physical access to an already-unlocked device, or
  a device the reporter does not own.

## Verifying what you install

The host plugin is the one file you put on your device, and it's **plain,
readable Python** — no minified or encoded blobs. The build is reproducible, so
you can confirm the shipped file matches its source:

```sh
cd pwnagotchi-plugin
python3 build.py                      # regenerates ink-cartridge.py from src/
git diff --exit-code ink-cartridge.py # no diff = the shipped file is honest
```

## Good practice for cartridge authors

- Never hard-code an API key or secret. Declare it in `requires.secrets` and
  reference it as `{{secret.<key>}}` — secrets live encrypted on the phone and
  the device never stores them.
- Only fetch from a declared `data_source`. Don't open arbitrary sockets.
- Touch only your own cartridge's files and state.
