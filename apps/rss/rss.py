# RSS / Atom headline reader with paginated auto-scroll.
#
# Sync envelope (from the phone, via manifest data_source with format:"xml"):
#   {"location": null, "fetched": <raw feed XML as a STRING> | null}
# Unlike the default "json" format, the phone does NOT parse the feed for us —
# it hands over the raw response body and this file parses it on-device with
# stdlib xml.etree.ElementTree. That keeps the companion app feed-agnostic.
#
# on_data receives two different shapes and tells them apart by payload keys,
# not by a fixed schema:
#   - a *config command* from the ui.json Apply button:
#       {"action": "set_config", "orientation": "...", "scroll_seconds": "...",
#        "feed": "https://..."}
#     (legacy "set_layout"/"set_scroll" single-field forms also accepted).
#   - the *sync envelope* above (has "fetched", no "action").
#
# Display: the device holds every item from the feed (up to MAX_ITEMS) and
# paginates through them a page at a time, auto-advancing every
# `scroll_seconds` (0 = off). The host re-renders us on that cadence because we
# expose `interval_seconds` (see the plugin's _active_interval). The current
# page is derived from the wall clock, so any repaint (auto-tick, push, banner)
# shows the page the elapsed time selects — no per-render mutation to get wrong.
#
# Feed source: the feed URL is user-configurable from the phone (a plain text
# field, not a secret). The chosen URL is surfaced back to the phone via
# published_state as {{state.feed_url}}, and the manifest's data_source.url is
# "{{state.feed_url}}" — so the phone fetches whatever feed the device holds.
# Defaults to Hacker News so the cartridge works out of the box.
#
# Settings persistence: the host only remembers the LAST payload it pushed per
# app and replays that single slot on reboot, so we can't rely on it to hold
# the feed URL AND orientation/scroll. All three are self-persisted to a small
# JSON sidecar here; feed *items* are transient (re-synced from the network) —
# whichever the replayed payload happens to be repopulates, the rest are read
# from the sidecar.

import json
import os
import time
import xml.etree.ElementTree as ET

from PIL import ImageFont

from ink_cartridge_host import draw_wrapped, wrap_text

ATOM_NS = "{http://www.w3.org/2005/Atom}"
MAX_FETCHED_BYTES = 2_000_000
MAX_ITEMS = 30            # hold a full feed page; the display paginates through them

COLUMNS = 3               # side-by-side headlines per page in the columns layout
ROW_MAX_LINES = 2         # cap each headline at 2 lines in the rows layout (#2)
LINE_SPACING = -1         # tighten line height for density (#3)

DEFAULT_SCROLL_SECONDS = 60
SCROLL_CHOICES = (0, 30, 60, 120, 300)   # allowed auto-scroll periods (0 = off)

DEFAULT_FEED = "https://hnrss.org/frontpage"   # works out of the box; user-overridable

SIDECAR = os.path.join(os.path.expanduser("~"), ".ink-cartridge-rss.json")


def _coerce_feed(value):
    """Accept a non-empty http(s) URL string; return it stripped, else None."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if url.startswith(("http://", "https://")):
        return url
    return None


def _load_settings():
    orientation, scroll, feed = "vertical", DEFAULT_SCROLL_SECONDS, DEFAULT_FEED
    try:
        with open(SIDECAR, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return orientation, scroll, feed
    if isinstance(data, dict):
        if data.get("orientation") in ("vertical", "horizontal"):
            orientation = data["orientation"]
        s = data.get("scroll_seconds")
        if isinstance(s, int) and s in SCROLL_CHOICES:
            scroll = s
        f = _coerce_feed(data.get("feed"))
        if f is not None:
            feed = f
    return orientation, scroll, feed


def _save_settings(orientation, scroll, feed):
    try:
        with open(SIDECAR, "w", encoding="utf-8") as f:
            json.dump({"orientation": orientation, "scroll_seconds": scroll,
                       "feed": feed}, f)
    except Exception:
        pass


def _coerce_scroll(value):
    """Accept an int or a numeric string; return it only if it's an allowed
    choice, else None (caller leaves the current value untouched)."""
    try:
        s = int(value)
    except (TypeError, ValueError):
        return None
    return s if s in SCROLL_CHOICES else None


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


def _line_h(font):
    ascent, descent = font.getmetrics()
    return ascent + descent + LINE_SPACING


def _wrap_capped(draw, text, font, max_w, max_lines):
    """Wrap `text` to at most `max_lines` lines within `max_w` px, ellipsizing
    the last line when content was dropped. Returns the list of lines."""
    lines = wrap_text(text, max_w, draw, font)
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    last = kept[-1]
    while last and draw.textlength(last + "…", font=font) > max_w:
        last = last[:-1]
    kept[-1] = (last + "…") if last else "…"
    return kept


def _draw_lines(draw, x, y, lines, font):
    lh = _line_h(font)
    for line in lines:
        draw.text((x, y), line, font=font, fill=0)
        y += lh
    return y


class Rss:
    name = "rss"
    icon = "RS"
    version = "1.2.0"

    def __init__(self):
        self._items = []
        self._feed_title = ""
        orientation, scroll, feed = _load_settings()
        self._orientation = orientation
        self._scroll_seconds = scroll
        self._feed_url = feed
        # Drives the host's repaint cadence for auto-scroll; None = push-driven.
        self.interval_seconds = scroll or None
        self._scroll_base = time.monotonic()

    def published_state(self):
        # orientation/scroll_seconds/feed_url are surfaced so the phone controls
        # can pre-select the device's current values ({{state.x}} in ui.json),
        # and so data_source.url = "{{state.feed_url}}" fetches the chosen feed.
        # scroll_seconds is a string to match the select option values.
        return {
            "feed_title": self._feed_title,
            "item_count": len(self._items),
            "orientation": self._orientation,
            "scroll_seconds": str(self._scroll_seconds),
            "feed_url": self._feed_url,
        }

    def on_data(self, payload):
        if not isinstance(payload, dict):
            return

        if payload.get("action") in ("set_config", "set_layout", "set_scroll"):
            self._apply_config(payload)
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
        self._scroll_base = time.monotonic()   # restart paging at page 1 on fresh data

    def _apply_config(self, payload):
        changed = False

        orientation = payload.get("orientation")
        if orientation is None and payload.get("action") == "set_layout":
            orientation = payload.get("value")   # legacy single-field form
        if orientation in ("vertical", "horizontal"):
            self._orientation = orientation
            changed = True

        raw_scroll = payload.get("scroll_seconds")
        if raw_scroll is None and payload.get("action") == "set_scroll":
            raw_scroll = payload.get("value")    # legacy single-field form
        scroll = _coerce_scroll(raw_scroll)
        if scroll is not None:
            self._scroll_seconds = scroll
            self.interval_seconds = scroll or None
            changed = True

        feed = _coerce_feed(payload.get("feed"))
        if feed is not None and feed != self._feed_url:
            self._feed_url = feed
            # Drop the old feed's items so the screen doesn't show stale content
            # from a different source until the next sync fetches the new URL.
            self._items = []
            self._feed_title = ""
            changed = True

        if changed:
            self._scroll_base = time.monotonic()
            _save_settings(self._orientation, self._scroll_seconds, self._feed_url)

    def _per_page(self, font, h):
        if self._orientation == "vertical":
            return COLUMNS
        # rows: how many capped-height items fit below a one-line header.
        lh = _line_h(font)
        header_h = lh + 6                      # header line + underline + gaps
        item_h = ROW_MAX_LINES * lh + 3        # 2 lines + (gap·rule·gap)
        return max(1, (h - header_h) // item_h)

    def _current_page(self, n_pages):
        if n_pages <= 1 or not self._scroll_seconds:
            return 0
        elapsed = time.monotonic() - self._scroll_base
        return int(elapsed // self._scroll_seconds) % n_pages

    def render(self, draw, w, h):
        font = ImageFont.truetype("DejaVuSansMono-Bold", 10)

        items = self._items
        per_page = self._per_page(font, h)
        n_pages = max(1, (len(items) + per_page - 1) // per_page) if items else 1
        page = self._current_page(n_pages)

        # Header: page indicator X/N (top-right) + feed title (left, wrapped
        # into the width the indicator leaves).
        indicator = f"{page + 1}/{n_pages}"
        ind_w = int(draw.textlength(indicator, font=font))
        draw.text((w - 4 - ind_w, 2), indicator, font=font, fill=0)

        header = self._feed_title or "RSS"
        header_end = draw_wrapped(draw, (4, 2), header, font,
                                  w - 12 - ind_w, line_spacing=LINE_SPACING)
        underline_y = header_end + 1
        draw.line((4, underline_y, w - 4, underline_y), fill=0)
        content_top = underline_y + 3

        if not items:
            draw_wrapped(draw, (4, content_top), "no items yet", font, w - 8,
                         line_spacing=LINE_SPACING)
            return

        page_items = items[page * per_page:(page + 1) * per_page]
        if self._orientation == "vertical":
            self._render_columns(draw, font, content_top, w, h, page_items)
        else:
            self._render_rows(draw, font, content_top, w, h, page_items)

    def _render_rows(self, draw, font, top, w, h, items):
        # horizontal/rows: headlines stacked top-to-bottom, each capped at
        # ROW_MAX_LINES, separated by a 1px rule with a 1px gap either side.
        y = top
        for i, item in enumerate(items):
            lines = _wrap_capped(draw, item.get("title") or "", font,
                                 w - 8, ROW_MAX_LINES)
            y = _draw_lines(draw, 4, y, lines, font)
            if i < len(items) - 1:
                y += 1
                draw.line((4, y, w - 4, y), fill=0)
                y += 2
            if y >= h:
                break

    def _render_columns(self, draw, font, top, w, h, items):
        # vertical/columns: headlines side-by-side in equal columns, each
        # wrapped to its column (capped to the column height), with a 1px
        # vertical rule between columns.
        n = len(items)
        col_w = (w - 8) // n
        max_lines = max(1, (h - 2 - top) // _line_h(font))
        for i, item in enumerate(items):
            x = 4 + i * col_w
            lines = _wrap_capped(draw, item.get("title") or "", font,
                                 col_w - 6, max_lines)
            _draw_lines(draw, x + 2, top, lines, font)
            if i < n - 1:
                rule_x = x + col_w
                draw.line((rule_x, top, rule_x, h - 2), fill=0)
