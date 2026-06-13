# host_alias.py — shared text-layout helpers for ink-cartridge, plus a
# sys.modules alias so installed cartridges can `from ink_cartridge_host import …`.
#
# Moved verbatim out of the old ink-cartridge.py monolith. Cartridge files
# import these helpers via the well-known module name `ink_cartridge_host`;
# `register_host_alias()` synthesises that module so the import works in both
# tests and production (pwnagotchi's plugin loader does not register the host
# module in sys.modules under any importable name).


def wrap_text(text, max_w, draw, font):
    """Word-wrap `text` into lines that each fit within `max_w` pixels.

    A single word that on its own is wider than `max_w` is hard-split at the
    character level so the line never overflows — preferable to letting a long
    URL or all-caps word spill off the screen.
    """
    if not text:
        return []
    lines = []
    for paragraph in text.splitlines() or [""]:
        cur = ""
        for word in paragraph.split(" "):
            trial = word if not cur else cur + " " + word
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
                continue
            # Trial overflows. Flush what we had…
            if cur:
                lines.append(cur)
                cur = ""
            # …then accept `word`, hard-splitting it if it alone is too wide.
            if draw.textlength(word, font=font) <= max_w:
                cur = word
            else:
                piece = ""
                for ch in word:
                    if draw.textlength(piece + ch, font=font) <= max_w:
                        piece += ch
                    else:
                        if piece:
                            lines.append(piece)
                        piece = ch
                cur = piece
        if cur:
            lines.append(cur)
    return lines


def draw_wrapped(draw, xy, text, font, max_w, fill=0, line_spacing=2):
    """Convenience: word-wrap then paint top-left at `xy`. Returns the y-coord
    just below the last painted line so callers can stack content beneath it."""
    x, y = xy
    lines = wrap_text(text, max_w, draw, font)
    if not lines:
        return y
    # Use font height (ascent+descent) for vertical spacing.
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + line_spacing
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h
    return y


def draw_wrapped_centered(draw, text, font, area, fill=0, line_spacing=2):
    """Word-wrap `text` and paint it centered within `area` = (x0, y0, x1, y1).
    Returns the (start_y, end_y) tuple."""
    x0, y0, x1, y1 = area
    max_w = x1 - x0
    lines = wrap_text(text, max_w, draw, font)
    if not lines:
        return (y0, y0)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + line_spacing
    total_h = line_h * len(lines) - line_spacing
    y = y0 + max(0, ((y1 - y0) - total_h) // 2)
    start_y = y
    for line in lines:
        lw = draw.textlength(line, font=font)
        lx = x0 + max(0, ((x1 - x0) - int(lw)) // 2)
        draw.text((lx, y), line, font=font, fill=fill)
        y += line_h
    return (start_y, y - line_spacing)


import sys as _sys, types as _types


def register_host_alias():
    """Expose text helpers to installed cartridges via `import ink_cartridge_host`."""
    if "ink_cartridge_host" in _sys.modules:
        return
    m = _types.ModuleType("ink_cartridge_host")
    m.wrap_text = wrap_text
    m.draw_wrapped = draw_wrapped
    m.draw_wrapped_centered = draw_wrapped_centered
    _sys.modules["ink_cartridge_host"] = m
