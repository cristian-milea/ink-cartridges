# Ink Cartridge UI schema (v1)

The contract for "alternate-screen apps" that ship a phone-side UI alongside the
device-side Python. An app is now a *package*: a Python plugin file plus a
declarative JSON description of its Android interface. The companion app renders
the JSON natively in Jetpack Compose — no app-supplied code ever runs on the
phone.

This document is the source of truth. Both the device-side host plugin and the
Android companion implement against it.

## Package layout

An installable app is a small set of files identified by a common stem
(`<stem>` below). On the device they sit side-by-side in the apps dir:

```
/usr/local/share/pwnagotchi/ink-cartridge/
├── magic8.py                # device plugin (required)
├── magic8.manifest.json     # metadata + capability declarations (required)
├── magic8.ui.json           # phone-side UI tree (optional; missing = no UI)
└── magic8.icon.png          # 8×8 e-ink glyph (optional; falls back to .icon)
```

The phone fetches these as plain JSON via new endpoints:

```
GET /plugins/ink-cartridge/manifest/<name>   → application/json (the manifest)
GET /plugins/ink-cartridge/ui/<name>         → application/json (the ui tree)
```

If `<name>.ui.json` is absent, the phone shows a default panel (icon + name +
Activate/Stop only). This keeps existing built-ins compatible during rollout.

## manifest.json

The manifest is the **single source of truth** for everything intrinsic to a
cartridge. The catalog `index.json` is *generated* from it (see
`ink-cartridge-catalog-schema.md`) — never the other way round.

```jsonc
{
  "name": "magic8",                  // must match the device-side .name
  "icon": "8",                       // 1–2 char monogram (launcher tile + e-ink taskbar)
  "version": "1.2.0",
  "author": "cristian-milea",
  "description": "Magic 8-Ball — shake the phone, get an answer.",

  // Store-presentation fields (optional). These describe the app, so they
  // live here, not in the catalog index.
  "category": "games",               // games | weather | utilities | entertainment | lifestyle | reference
  "long_description": "Markdown allowed. Shown on the detail screen.",
  "homepage": "https://...",         // optional
  "license": "MIT",                  // optional

  // Optional declarations the phone honours BEFORE activation.
  "requires": {
    "permissions": ["location"],     // see allowlist below
    "secrets": [                     // rich object form (v1); see Secrets below
      {
        "key": "openweather",
        "label": "OpenWeather API key",
        "description": "Free key from openweathermap.org.",
        "optional": false
      }
    ]
  },

  // Optional. Bumps when the schema this manifest targets changes.
  "schema_version": 1
}
```

**`icon`** is the canonical 1–2 char monogram. The device reads it from the
manifest, falling back to the Python class's `.icon` attribute only when the
manifest is absent — keep the two matching.

**Permission allowlist (v1):** `location`, `notifications`, `network`.
Anything else causes the manifest to be rejected on install. The phone maps
each name to a concrete Android runtime permission (e.g. `location` →
`ACCESS_COARSE_LOCATION`). Apps cannot request raw Android permissions.

**Secrets** are values the player supplies once on the phone (Cartridges →
Settings → Secrets), stored AES-encrypted in `EncryptedSharedPreferences`. Each
`requires.secrets[]` entry is an **object** (the bare-string form is no longer
accepted):

| Field         | Required | Meaning                                                            |
| ------------- | -------- | ------------------------------------------------------------------ |
| `key`         | yes      | Identifier the cartridge references as `{{secret.<key>}}`.         |
| `label`       | yes      | Player-facing name shown in the settings list and edit dialog.     |
| `description` | no       | One-line explanation shown in the edit dialog.                     |
| `optional`    | no       | Default `false`. When `false`, **activation is blocked** until the player sets a value, and the launcher tile shows a CONFIG action instead of PLUG IN. When `true`, the cartridge runs without it (`{{secret.<key>}}` resolves to empty). |

**Storage namespacing (the slug).** The player never types a storage key — they
reference the bare `{{secret.<key>}}`. The phone namespaces each secret by the
*cartridge's identity* so two cartridges asking for the same `key` never
collide. The storage slug is:

```
s_<icon>_<author>_<key>
```

each component lowercased with every run of non-alphanumerics collapsed to a
single `_`, and a fixed `s_` prefix (guarantees a leading letter even for a
numeric monogram like `"8"`). Example: icon `TS`, author `cristian-milea`, key
`worldtides` → `s_ts_cristian_milea_worldtides`. A consequence: a key shared by
two different cartridges is entered once per cartridge — that's the isolation,
by design. This algorithm is implemented in the companion app; the device never
sees or resolves secrets.

## ui.json — widget tree

A single root node, recursively containing children. Each node is an object
with a `"type"` field plus type-specific fields.

```json
{
  "type": "column",
  "padding": 16,
  "children": [
    { "type": "text", "value": "Magic 8-Ball", "style": "headline" },
    { "type": "state_text", "binding": "last_answer",
      "default": "shake to ask", "style": "body" },
    { "type": "button", "label": "Shake",
      "action": { "type": "push", "payload": { "action": "shake" } } },
    { "type": "spacer", "height": 8 },
    { "type": "state_text", "binding": "shake_count",
      "format": "Shaken {} times", "style": "caption" }
  ]
}
```

### Widget vocabulary

| Type           | Required fields                       | Optional fields                          |
| -------------- | ------------------------------------- | ---------------------------------------- |
| `column`       | `children: []`                        | `padding` (dp), `spacing` (dp)           |
| `row`          | `children: []`                        | `padding`, `spacing`, `align` (`start`/`center`/`end`) |
| `spacer`       | one of `width` / `height` (dp)        |                                          |
| `divider`      |                                       | `thickness` (dp)                         |
| `text`         | `value` (template)                    | `style` (`headline`/`title`/`body`/`caption`), `align` |
| `state_text`   | `binding` (key in published_state)    | `default`, `format` (`{}` substituted), `style`, `align` |
| `image`        | `source` (url or `state.<key>`)       | `width`, `height`                        |
| `chart`        | `binding` (list of numbers)           | `kind` (`bar`/`line`), `height`          |
| `button`       | `label` (template), `action`          | `style` (`primary`/`secondary`)          |
| `switch`       | `local` (key in local state), `label` | `default` (bool), `action_on`, `action_off` |
| `slider`       | `local`, `min`, `max`                 | `step`, `default`, `label`, `action`     |
| `text_field`   | `local`, `label`                      | `default`, `kind` (`text`/`number`)      |
| `select`       | `local`, `options: [{value,label}]`   | `default`, `label`                       |
| `when`         | `if` (JsonLogic rule), `then` (node or `[]`) | `else` (node or `[]`)             |

Template strings (`value`, `label`, `format`, `payload` leaf values, and the
`data_source.url`) accept `{{state.X}}`, `{{local.X}}`, `{{secret.X}}`, and
`{{location.lat|lon|label}}` placeholders. Missing keys substitute as empty
string; missing secret renders a "Set up secret" badge next to the widget.

### Conditional rendering — the `when` widget

To show or hide a subtree based on the current `local`/`state` (or even
`secret`/`location`), wrap it in a `when` node. The `if` field is a
[JsonLogic](https://jsonlogic.com) rule evaluated by the companion app
against the same scopes the template engine sees, exposed under top-level
keys `state`, `local`, `secret`, `location`. There is **no expression mini-
language to parse and no host access** — JsonLogic only composes its
declared operators (`==`, `!=`, `and`, `or`, `!`, `var`, `<`, `>`, `<=`,
`>=`, `in`, `if`, …) over data.

```json
{ "type": "when",
  "if":   { "==": [ { "var": "local.bets_on" }, true ] },
  "then": [
    { "type": "select", "local": "bet", "label": "Bet",
      "options": [{ "value": "5", "label": "$5" }, { "value": "10", "label": "$10" }] },
    { "type": "button", "label": "Double",
      "action": { "type": "push", "payload": { "action": "double" } } }
  ],
  "else": [
    { "type": "text", "value": "Playing without chips.", "style": "caption" }
  ] }
```

Notes:
- `then` and `else` accept either a single node object or an array of nodes.
- `else` is optional — omit to render nothing when the condition is false.
- A malformed or unknown-operator rule renders the `else` branch (default
  fail-closed for visibility): better to hide than to show stale controls.
- The rule is re-evaluated on every recomposition driven by `local`/`state`
  change, so a `switch` flipping `local.bets_on` instantly toggles the
  subtree.

### Actions

Every input widget triggers an `action`. Action shape:

```json
{ "type": "push",   "payload": { "action": "shake" } }   // POST to device
{ "type": "sync" }                                       // fetch data_source + push
{ "type": "set_local", "key": "answer", "value": "" }    // mutate local state
{ "type": "request_permission", "name": "location" }     // OS prompt
```

`push` is the workhorse: payload values are templates and resolve before send.
The phone always wraps the payload as `{"app":"<name>","payload":<resolved>}`
and POSTs to `/plugins/ink-cartridge/push` with the standard CSRF flow.

`sync` implicitly targets the currently-active app and is fully app-agnostic.
It reads the app's manifest `data_source`, fetches the URL, and pushes the
envelope `{"location": {...}|null, "fetched": <body>}`. See **Data sources**
below.

## Data sources — declarative fetch

An app's manifest may declare a `data_source` block. The phone fetches the
URL (auto-sync OR a SyncCard tap), wraps the response in an envelope, and
POSTs to the device. **The app's Python `on_data` does the transformation**
from the external API shape to its display state — Android has zero
knowledge of which API the app talks to.

**Auto SyncCard convention:** if a manifest has `data_source`, the
companion auto-renders a control card with "Last synced X ago",
"Next auto-sync in N min" (when auto-sync is on), location indicator,
and a Sync button. The card appears *above* the app's `ui.json` if any.
**Apps whose sole control is sync don't need a `ui.json` at all** — the
SyncCard is the surface. (e-ink is the display; phone is the remote.)

```json
{
  "data_source": {
    "type": "http",
    "method": "GET",
    "url": "https://api.example.com/?lat={{location.lat}}&lon={{location.lon}}",
    "needs": ["location"],
    "auto_sync": true,
    "min_sync_seconds": 0
  }
}
```

Fields:
- `type`: only `"http"` for now.
- `url`: full URL. Placeholders resolved before fetch.
- `method`: `"GET"` (default) — POST is reserved for a future revision.
- `needs`: declarative input hints (`"location"`, `"secret:<name>"`). The
  phone refuses to sync if a need can't be satisfied.
- `auto_sync` *(optional, default `true`)*: when `false` the app is
  **manual-sync only** — the periodic feeder and on-activation auto-sync skip
  it; the SyncCard's Sync button still fetches on demand. Use for metered/
  quota-limited APIs where every fetch costs the user (e.g. a paid tide key).
- `min_sync_seconds` *(optional, default `0`)*: minimum seconds between
  *automatic* syncs. The phone throttles the feeder/activation sync to at most
  once per interval; manual Sync taps ignore it. Lets a cartridge that caches a
  long span device-side (e.g. 7 days of tide data) drop its auto-sync cadence
  from the global default to roughly once a week.

Both fields gate only *automatic* fetches; an explicit Sync tap always fetches.
Apps that omit them behave exactly as before (sync freely).

Envelope shape the device receives (in `on_data`):
```json
{ "location": {"lat": 50.82, "lon": -0.14, "label": "Brighton, UK"} | null,
  "fetched": <raw response body as JSON> | null }
```

## published_state — what the phone reads back

An app can optionally implement `published_state(self) -> dict` on its Python
class:

```python
class Magic8:
    name = "magic8"
    def published_state(self):
        return {
            "last_answer": self._answer or "",
            "shake_count": self._count,
        }
```

The host plugin calls this on every `GET /plugins/ink-cartridge/state` and includes
the result in the response:

```json
{
  "active": "magic8",
  "apps": [ ... ],
  "published": { "magic8": { "last_answer": "...", "shake_count": 42 } }
}
```

`state_text` widgets read from `published["<app>"]["<binding>"]`. This is the
*only* device-side state the phone surfaces — the goal is for app authors to
write `published_state()` deliberately rather than leaking arbitrary internals.

## Install flow

Phone uploads a package via `POST /plugins/ink-cartridge/install`:

```json
{
  "files": {
    "myapp.py":            "<python source>",
    "myapp.manifest.json": "<json>",
    "myapp.ui.json":       "<json>"     // optional
  }
}
```

The host validates:
1. `myapp.py` parses (existing check).
2. Source size ≤ 256 KB total across all files.
3. `manifest.json` parses; `name` matches the Python class `.name`.
4. `requires.permissions` ⊆ allowlist.
5. `ui.json` (if present) parses and root is a known widget type.

On success the files are written atomically; on any failure all three are
discarded. Legacy `{"filename","source"}` body is still accepted (no manifest,
no ui) so existing tooling keeps working.

## Apps screen layout (companion app)

The Apps tab shows **one app at a time**:

- Main pane: renders the active app's `ui.json`. If no app is active, shows an
  empty state ("No app active — tap Switch app to choose one").
- Top-right button **Switch app** → opens a `ModalBottomSheet`:
  - Active app pinned at top with **View** (close the sheet) and **Stop**.
  - Other apps listed with **Activate**.
  - Footer: Install (.pwnapp picker), Permissions & secrets, Location, Auto-sync.

This means one rendered UI per visible app — matching the device, which can
only show one app at a time on the e-ink. Switching apps on the phone activates
on the device too; the two are kept in lock-step.

## Permissions & secrets screen

A sub-screen reached from the Apps switcher footer (or Settings):

- **Permissions** — one row per name in the allowlist. Tap to toggle the
  underlying OS permission. Apps that declare a missing permission can't be
  activated until granted.
- **Secrets** — list of named secrets (`openweather`, `worldtides`, ...). Add,
  edit, delete. Apps that reference a missing secret show a placeholder and a
  "Set up" affordance pointing at this screen.

## Why this shape

- **No code crosses the trust boundary on the phone.** JSON is data; the
  renderer is fixed. An attacker can't ship a malicious UI that does anything
  the renderer doesn't already allow.
- **Permissions are global.** Apps describe needs declaratively; the user
  decides once. No "this app wants location" dialogue every activation.
- **Extensible.** Adding a widget = one Compose function + one parser branch.
  Adding an action = one dispatcher branch. Schema version bump if breaking.
- **Easy.** A new app is three small files. The simplest viable
  app is ~10 lines of Python + a manifest + a single `button` widget.
