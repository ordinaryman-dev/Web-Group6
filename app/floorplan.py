"""Architectural floor plan for the manager rooms page.

Drawn as a real plan rather than a card grid:

* one closed path for the building outline and one for the corridor, so no wall is
  drawn twice or crosses another;
* rooms are plain rectangles with their number and bed type centred inside — the
  text never runs past a wall;
* each room has a door gap with a door leaf on the corridor side, and a window line
  on the facade side; a room without a window simply has no window line, so no
  label is needed;
* the lift core is drawn with the usual crossed-shaft symbol.

Each room rectangle links to the room form.
"""

from markupsafe import Markup

# ---- geometry -------------------------------------------------------------
ROOM_W = 150
ROOM_H = 56
GAP = 7
CORR = 44
PAD = 10
MARGIN = 14
LABEL = 26
BETWEEN = 300
LINK_SLOTS = 2
MIN_SLOTS_FOR_LINK = 4

PITCH = ROOM_H + GAP
LEG_W = PAD * 2 + ROOM_W * 2 + CORR
WIDTH = LEG_W * 2 + BETWEEN + MARGIN * 2

DOOR = 20          # door opening on the corridor side
WINDOW_INSET = 14  # window line inset from the room ends

HATCH = (
    '<pattern id="hatch" width="9" height="9" patternUnits="userSpaceOnUse" '
    'patternTransform="rotate(45)">'
    '<rect width="9" height="9" fill="#f6f9f7"/>'
    '<line x1="0" y1="0" x2="0" y2="9" stroke="#e9eeeb" stroke-width="3"/>'
    "</pattern>"
)


def _text(x, y, value, cls, anchor="start"):
    return f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{value}</text>'


def _inner(body, rooms, x, first, start_slot, side, states, types_by_id, href_for, matched):
    for index, room in enumerate(rooms):
        body.append(room_card(room, x, first + (start_slot + index) * PITCH, side,
                              states, types_by_id, href_for, matched))


def render_floor_plan(plan, states, types_by_id, href_for, matched=None):
    """Return the SVG markup for one floor, or Markup() when there is nothing to draw."""

    if not plan:
        return Markup()
    west, east = plan["west"], plan["east"]
    slots = plan.get("slots") or max(len(west["outer"]), len(east["outer"]))
    if not slots:
        return Markup()
    link = plan.get("link", slots >= MIN_SLOTS_FOR_LINK)

    top = MARGIN + LABEL
    first = top + PAD
    bottom = first + (slots - 1) * PITCH + ROOM_H + PAD
    height = bottom + MARGIN

    left_x = MARGIN
    right_x = MARGIN + LEG_W + BETWEEN
    west_corr = left_x + PAD + ROOM_W
    east_corr = right_x + PAD + ROOM_W

    body = []

    if link:
        link_slot = max(0, min(slots - LINK_SLOTS, (slots - LINK_SLOTS) // 2))
        band_top = first + link_slot * PITCH
        band_h = LINK_SLOTS * ROOM_H + (LINK_SLOTS - 1) * GAP
        band_bottom = band_top + band_h
        band_x1, band_x2 = west_corr, east_corr + CORR

        outline = (
            f"M{left_x},{top} H{left_x + LEG_W} V{band_top} H{right_x} V{top} "
            f"H{right_x + LEG_W} V{bottom} H{right_x} V{band_bottom} "
            f"H{left_x + LEG_W} V{bottom} H{left_x} Z"
        )
        corridor = (
            f"M{band_x1},{top} H{band_x1 + CORR} V{band_top} H{east_corr} V{top} "
            f"H{east_corr + CORR} V{bottom} H{east_corr} V{band_bottom} "
            f"H{band_x1 + CORR} V{bottom} H{band_x1} Z"
        )
        body += [f'<path d="{outline}" class="shell"/>',
                 f'<path d="{corridor}" fill="url(#hatch)"/>',
                 f'<path d="{corridor}" class="corridor-line"/>']
        # corridor centre line, the dotted line a real plan uses
        cx_w = band_x1 + CORR / 2
        cx_e = east_corr + CORR / 2
        body.append(f'<line x1="{cx_w}" y1="{top}" x2="{cx_w}" y2="{band_top}" class="centre-line"/>')
        body.append(f'<line x1="{cx_w}" y1="{band_bottom}" x2="{cx_w}" y2="{bottom}" class="centre-line"/>')
        body.append(f'<line x1="{cx_e}" y1="{top}" x2="{cx_e}" y2="{band_top}" class="centre-line"/>')
        body.append(f'<line x1="{cx_e}" y1="{band_bottom}" x2="{cx_e}" y2="{bottom}" class="centre-line"/>')
        cy = band_top + band_h / 2
        body.append(f'<line x1="{band_x1}" y1="{cy}" x2="{band_x2}" y2="{cy}" class="centre-line"/>')
    else:
        link_slot = 0
        band_top = band_bottom = 0
        for x in (left_x, right_x):
            body.append(f'<rect x="{x}" y="{top}" width="{LEG_W}" height="{bottom - top}" class="shell"/>')
            body.append(f'<rect x="{x + PAD + ROOM_W}" y="{top}" width="{CORR}" '
                        f'height="{bottom - top}" fill="url(#hatch)"/>')

    # rooms: doors face the corridor, windows face the facade
    for x_leg, leg, mirrored in ((left_x, west, False), (right_x, east, True)):
        if mirrored:
            outer_x = x_leg + LEG_W - PAD - ROOM_W
            inner_x = x_leg + PAD
            outer_door, inner_door = "left", "right"
        else:
            outer_x = x_leg + PAD
            inner_x = x_leg + LEG_W - PAD - ROOM_W
            outer_door, inner_door = "right", "left"
        for index, room in enumerate(leg["outer"][:slots]):
            body.append(room_card(room, outer_x, first + index * PITCH, outer_door,
                                  states, types_by_id, href_for, matched))
        if link:
            _inner(body, leg["inner_up"][:link_slot], inner_x, first, 0, inner_door,
                   states, types_by_id, href_for, matched)
            _inner(body, leg["inner_down"][:slots - link_slot - LINK_SLOTS], inner_x, first,
                   link_slot + LINK_SLOTS, inner_door, states, types_by_id, href_for, matched)
        else:
            _inner(body, leg["inner_up"][:slots], inner_x, first, 0, inner_door,
                   states, types_by_id, href_for, matched)

    if link:
        core_h = 40
        core_y = band_top + (band_h - core_h) / 2
        labels = ("Stairs", "Lift A", "Lift B", "Linen")
        usable = band_x2 - band_x1 - 2 * CORR
        cell_w = 84
        spacing = (usable - 4 * cell_w) / 5
        for index, label in enumerate(labels):
            cx = band_x1 + CORR + spacing * (index + 1) + cell_w * index
            body.append(f'<rect x="{cx:.1f}" y="{core_y:.1f}" width="{cell_w}" height="{core_h}" '
                        f'rx="2" class="core"/>')
            if index in (1, 2):  # lift shafts carry the crossed-shaft symbol
                body.append(f'<line x1="{cx:.1f}" y1="{core_y:.1f}" '
                            f'x2="{cx + cell_w:.1f}" y2="{core_y + core_h:.1f}" class="shaft"/>')
                body.append(f'<line x1="{cx + cell_w:.1f}" y1="{core_y:.1f}" '
                            f'x2="{cx:.1f}" y2="{core_y + core_h:.1f}" class="shaft"/>')
            body.append(_text(cx + cell_w / 2, core_y + core_h + 13, label, "core-label", "middle"))

        notch_x = left_x + LEG_W + BETWEEN / 2
        body.append(_text(notch_x, top + 46, "outside", "outside-label", "middle"))
        body.append(_text(notch_x, bottom - 30, "outside", "outside-label", "middle"))

    body.append(_text(left_x, MARGIN + 2, f"Floor plan · {slots} rooms per outer column", "cap"))

    return Markup(
        f'<svg class="plan-svg" viewBox="0 0 {WIDTH} {height}" role="img" aria-label="Floor plan">'
        f"<defs>{HATCH}</defs>{''.join(body)}</svg>"
    )


def room_card(room, x, y, door_side, states, types_by_id, href_for, matched=None):
    state = states.get(room.id, "available")
    room_type = types_by_id[room.room_type_id]
    dim = " dim" if matched is not None and room.id not in matched else ""
    parts = [
        f'<a href="{href_for(room)}" class="sv-room{dim}">',
        f"<title>{room.number} · {room_type.name} · {state.replace('_', ' ')}</title>",
        f'<rect x="{x}" y="{y}" width="{ROOM_W}" height="{ROOM_H}" class="room {state}"/>',
        _text(x + ROOM_W / 2, y + 25, room.number, "num", "middle"),
        _text(x + ROOM_W / 2, y + 41, room_type.bed_type.capitalize(), "bed", "middle"),
    ]
    # window: a thin double line on the facade side, absent for interior rooms
    if room.has_window:
        wy1, wy2 = y + WINDOW_INSET, y + ROOM_H - WINDOW_INSET
        if door_side == "left":  # facade is the other side
            fx = x + ROOM_W
            parts.append(f'<line x1="{fx - 1}" y1="{wy1}" x2="{fx - 1}" y2="{wy2}" class="window"/>')
            parts.append(f'<line x1="{fx - 4}" y1="{wy1}" x2="{fx - 4}" y2="{wy2}" class="window"/>')
        else:
            fx = x
            parts.append(f'<line x1="{fx + 1}" y1="{wy1}" x2="{fx + 1}" y2="{wy2}" class="window"/>')
            parts.append(f'<line x1="{fx + 4}" y1="{wy1}" x2="{fx + 4}" y2="{wy2}" class="window"/>')
    # door: erase part of the corridor-side wall and add the leaf swing
    mid = y + ROOM_H / 2
    half = DOOR / 2
    if door_side == "right":
        ex = x + ROOM_W
        parts.append(f'<rect x="{ex - 2}" y="{mid - half}" width="4" height="{DOOR}" class="door-gap"/>')
        parts.append(f'<path d="M{ex - 1},{mid - half} L{ex - 1 - DOOR},{mid - half}" class="door-leaf"/>')
        parts.append(f'<path d="M{ex - 1 - DOOR},{mid - half} A{DOOR},{DOOR} 0 0 0 {ex - 1},{mid + half}" '
                     f'class="door-arc"/>')
    else:
        ex = x
        parts.append(f'<rect x="{ex - 2}" y="{mid - half}" width="4" height="{DOOR}" class="door-gap"/>')
        parts.append(f'<path d="M{ex + 1},{mid - half} L{ex + 1 + DOOR},{mid - half}" class="door-leaf"/>')
        parts.append(f'<path d="M{ex + 1 + DOOR},{mid - half} A{DOOR},{DOOR} 0 0 0 {ex + 1},{mid + half}" '
                     f'class="door-arc"/>')
    parts.append("</a>")
    return "".join(parts)
