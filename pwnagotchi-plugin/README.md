# Ink Cartridge — pwnagotchi host plugin

This is the **device-side plugin** that makes the Ink Cartridge catalog work. It
runs inside your pwnagotchi and does two things:

1. **Hosts cartridges.** It takes over the e-ink with one "cartridge" at a time
   (a game, a clock, weather…) while pwnagotchi keeps hunting underneath.
   Deactivate and the normal pwnagotchi face comes right back.
2. **Talks to the companion app** over a local Bluetooth link (RFCOMM for
   Android, BLE for iOS). The link never touches the Pi's Wi-Fi radio, so
   pwnagotchi never stops pwning while you browse and install cartridges.

You install this **once**. After that, every cartridge in the catalog installs
over Bluetooth from the app — no more SSH.

> **License:** GPLv3 (see [`LICENSE`](LICENSE)). The plugin links pwnagotchi,
> which is GPLv3, so it's GPLv3 too. The cartridge catalog in the parent repo is
> MIT — different scope, different license.

## Install it on your device

Full step-by-step (for someone who just flashed pwnagotchi) is in the
[main README's **Connect your device** section](../README.md#connect-your-device).
Short version:

```sh
# 1. Copy the plugin onto the Pi (replace the host with your device's name)
scp ink-cartridge.py pi@pwnagotchi.local:/tmp/

# 2. Move it into the custom-plugins dir and enable it
ssh pi@pwnagotchi.local '
  sudo mv /tmp/ink-cartridge.py /usr/local/share/pwnagotchi/custom-plugins/ &&
  echo -e "\n[main.plugins.ink-cartridge]\nenabled = true" | sudo tee -a /etc/pwnagotchi/config.toml &&
  sudo systemctl restart pwnagotchi
'
```

**Device prerequisites:** `python3-dbus` and `python3-gi` must be importable in
pwnagotchi's Python (they back the BlueZ D-Bus calls the Bluetooth link uses).
Recent jayofelony images ship them; if the plugin logs a dbus/gi import error,
`sudo apt install python3-dbus python3-gi`.

### Config options

All optional — the defaults match a stock jayofelony image.

```toml
[main.plugins.ink-cartridge]
enabled   = true
transport = "both"   # rfcomm (Android) | ble (iOS) | both (default)
apps_dir  = "/usr/local/share/pwnagotchi/ink-cartridge"   # where cartridges land
```

## Audit it before you trust it

It's your device — read the code first. This is the **only** file you put on the
Pi, and it is **plain, readable Python** (no minification, no base64 blobs):

```sh
less ink-cartridge.py            # the file you actually install
```

It's bundled into one file because pwnagotchi loads exactly one `.py` from
`custom-plugins/`. The bundle is **reproducible** — rebuild it from source and it
comes out byte-for-byte identical, so you can verify the shipped file matches
the source:

```sh
python3 build.py                 # regenerates ink-cartridge.py from src/
git diff --exit-code ink-cartridge.py   # no diff = the shipped file is honest
```

## Develop

- **Source of truth:** `src/*.py` — the plugin as readable modules.
- **`build.py`** inlines `src/` into `ink-cartridge.py`. It embeds each module
  verbatim under `#:IC-MODULE:` sentinels and, at import time, exec's each into
  its own namespace (three modules define `make_handlers`, so they can't share a
  flat namespace). The build **fails** unless the result imports cleanly and
  registers all expected commands — so a committed `ink-cartridge.py` always
  works.
- After editing `src/`, run `python3 build.py` and commit **both** `src/` and the
  regenerated `ink-cartridge.py`.
