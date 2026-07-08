# RSS / Atom headline reader.
#
# Sync envelope (from the phone, via manifest data_source with format:"xml"):
#   {"location": null, "fetched": <raw feed XML as a STRING> | null}
# Unlike the default "json" format, the phone does NOT parse the feed for us —
# it hands over the raw response body and this file parses it on-device with
# stdlib xml.etree.ElementTree. That keeps the companion app feed-agnostic.
#
# on_data receives two different shapes and must tell them apart by payload
# keys, not by a fixed schema:
#   - a *command* pushed by the ui.json select: {"action": "set_layout",
#     "value": "vertical"|"horizontal"}
#   - the *sync envelope* above (has "fetched", no "action").
#
# Orientation persistence caveat: the host only remembers the LAST payload it
# pushed to an app and replays that single payload on reboot (there's no
# per-app payload history). If we let the orientation command and the sync
# envelope share that one slot, whichever happened last would clobber the
# other on replay — a sync would forget the chosen layout, or a layout change
# would forget the feed. So orientation is self-persisted to a small sidecar
# file here, and feed items are treated as transient/refetched: on reboot the
# replayed payload repopulates whichever of the two it happens to be, and the
# other is either re-read from the sidecar (orientation) or re-synced from the
# network (items) rather than assumed durable.

import os
import xml.etree.ElementTree as ET

from PIL import ImageFont

from ink_cartridge_host import draw_wrapped

ATOM_NS = "{http://www.w3.org/2005/Atom}"
MAX_FETCHED_BYTES = 2_000_000
MAX_ITEMS = 5

# How many headlines each layout shows (see render()).
ROW_ITEMS = 3     # horizontal/rows: stacked, 1px rule between each
COL_ITEMS = 3     # vertical/columns: side-by-side, 1px rule between each

LAYOUT_SIDECAR = os.path.join(os.path.expanduser("~"), ".ink-cartridge-rss-layout")


def _load_orientation():
    try:
        with open(LAYOUT_SIDECAR, encoding="utf-8") as f:
            value = f.read().strip()
    except Exception:
        return "vertical"
    return value if value in ("vertical", "horizontal") else "vertical"


def _save_orientation(value):
    try:
        with open(LAYOUT_SIDECAR, "w", encoding="utf-8") as f:
            f.write(value)
    except Exception:
        pass


def _first_text(elem, tags):
    """First non-empty, stripped findtext() among tags (checked in order)."""
    for tag in tags:
        text = elem.findtext(tag)
        if text:
            return text.strip()
    return ""


def _extract_items(elements, title_tags, date_tags):
    items = []
    for elem in elements:
        items.append({
            "title": _first_text(elem, title_tags),
            "date": _first_text(elem, date_tags),
        })
        if len(items) >= MAX_ITEMS:
            break
    return items


def _parse_feed(xml_text):
    """Parse RSS or Atom XML text → (feed_title, items[:MAX_ITEMS]) or None on error."""
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None

    # RSS 2.0: <rss><channel><title>..</title><item>..</item>...</channel></rss>
    channel = root.find("channel")
    if channel is not None:
        title = _first_text(channel, ("title",))
        items = _extract_items(channel.findall("item"), ("title",), ("pubDate",))
        return title, items

    # Atom: <feed><title>..</title><entry>..</entry>...</feed>
    if root.tag in (f"{ATOM_NS}feed", "feed"):
        title = _first_text(root, (f"{ATOM_NS}title", "title"))
        entries = root.findall(f"{ATOM_NS}entry") or root.findall("entry")
        items = _extract_items(entries, (f"{ATOM_NS}title", "title"), (f"{ATOM_NS}updated", "updated"))
        return title, items

    return None


class Rss:
    name = "rss"
    icon = "RS"
    version = "1.0.0"

    def __init__(self):
        self._items = []
        self._feed_title = ""
        self._orientation = _load_orientation()

    def published_state(self):
        return {
            "feed_title": self._feed_title,
            "item_count": len(self._items),
        }

    def on_data(self, payload):
        if not isinstance(payload, dict):
            return

        if payload.get("action") == "set_layout":
            value = payload.get("value")
            if value in ("vertical", "horizontal"):
                self._orientation = value
                _save_orientation(value)
            return

        # Sync envelope.
        fetched = payload.get("fetched")
        if not isinstance(fetched, str) or not fetched:
            return
        if len(fetched) > MAX_FETCHED_BYTES:
            return

        parsed = _parse_feed(fetched)
        if parsed is None:
            # Parse error (or unrecognised shape) — keep previous items so a
            # transient bad fetch doesn't blank a working screen.
            return
        title, items = parsed
        self._feed_title = title
        self._items = items

    def render(self, draw, w, h):
        title_font = ImageFont.truetype("DejaVuSansMono-Bold", 10)
        body_font = ImageFont.truetype("DejaVuSansMono-Bold", 10)

        header = self._feed_title or "RSS"
        header_end = draw_wrapped(draw, (4, 2), header, title_font, w - 8,
                                  line_spacing=0)
        underline_y = header_end + 1
        draw.line((4, underline_y, w - 4, underline_y), fill=0)
        content_top = underline_y + 4

        if not self._items:
            draw_wrapped(draw, (4, content_top), "no items yet",
                         body_font, w - 8)
            return

        if self._orientation == "vertical":
            self._render_columns(draw, body_font, content_top, w, h)
        else:
            self._render_rows(draw, body_font, content_top, w, h)

    def _render_rows(self, draw, font, top, w, h):
        # horizontal/rows: headlines stacked top-to-bottom, each wrapped, with a
        # 1px horizontal rule between consecutive articles.
        items = self._items[:ROW_ITEMS]
        y = top
        for i, item in enumerate(items):
            title = item.get("title") or ""
            y = draw_wrapped(draw, (4, y), title, font, w - 8)
            if i < len(items) - 1:
                y += 3
                draw.line((4, y, w - 4, y), fill=0)
                y += 4
            if y >= h:
                break

    def _render_columns(self, draw, font, top, w, h):
        # vertical/columns: headlines side-by-side in equal columns, each wrapped
        # to its column width, with a 1px vertical rule between columns.
        items = self._items[:COL_ITEMS]
        n = len(items)
        col_w = (w - 8) // n
        for i, item in enumerate(items):
            x = 4 + i * col_w
            title = item.get("title") or ""
            # +2 gutter keeps text off the divider rule
            draw_wrapped(draw, (x + 2, top), title, font, col_w - 6)
            if i < n - 1:
                rule_x = x + col_w
                draw.line((rule_x, top, rule_x, h - 2), fill=0)
