"""Exact floor-plan drawing for the manager rooms page.

The building is drawn as ONE closed path, so the outline never shows walls crossing
each other. Inside it there are only three things: the corridor (one path), the room
cards and the lift core. Coordinates are computed, never laid out by CSS.

Shape: an H. Two full-height legs carry the outer room columns; the inner columns
stop above and below a waist that joins the legs and holds the corridor and lifts.
A floor with too few rooms for a waist is drawn as two plain columns instead.

Each room card links to the room form.
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

HATCH = (
    '<pattern id="hatch" width="10" height="10" patternUnits="userSpaceOnUse" '
    'patternTransform="rotate(45)">'
    '<rect width="10" height="10" fill="#f5f8f6"/>'
    '<line x1="0" y1="0" x2="0" y2="10" stroke="#e8ede9" stroke-width="3.5"/>'
    "</pattern>"
)


def _text(x, y, value, cls, anchor="start"):
    return f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{value}</text>'


def _inner(body, rooms, x, first, start_slot, states, types_by_id, href_for, matched):
    for index, room in enumerate(rooms):
        body.append(room_card(room, x, first + (start_slot + index) * PITCH,
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
    else:
        link_slot = 0
        band_top = band_bottom = 0
        for x in (left_x, right_x):
            body.append(f'<rect x="{x}" y="{top}" width="{LEG_W}" height="{bottom - top}" class="shell"/>')
            body.append(f'<rect x="{x + PAD + ROOM_W}" y="{top}" width="{CORR}" '
                        f'height="{bottom - top}" fill="url(#hatch)"/>')

    # rooms
    for x_leg, leg, mirrored in ((left_x, west, False), (right_x, east, True)):
        if mirrored:
            outer_x = x_leg + LEG_W - PAD - ROOM_W
            inner_x = x_leg + PAD
        else:
            outer_x = x_leg + PAD
            inner_x = x_leg + LEG_W - PAD - ROOM_W
        for index, room in enumerate(leg["outer"][:slots]):
            body.append(room_card(room, outer_x, first + index * PITCH,
                                  states, types_by_id, href_for, matched))
        if link:
            _inner(body, leg["inner_up"][:link_slot], inner_x, first, 0,
                   states, types_by_id, href_for, matched)
            _inner(body, leg["inner_down"][:slots - link_slot - LINK_SLOTS], inner_x, first,
                   link_slot + LINK_SLOTS, states, types_by_id, href_for, matched)
        else:
            _inner(body, leg["inner_up"][:slots], inner_x, first, 0,
                   states, types_by_id, href_for, matched)

    if link:
        core_h = 38
        core_y = band_top + (band_h - core_h) / 2
        usable = east_corr + CORR - west_corr - 2 * CORR
        labels = ("Stairs", "Lift A", "Lift B", "Linen")
        cell_w = 86
        spacing = (usable - 4 * cell_w) / 5
        for index, label in enumerate(labels):
            cx = west_corr + CORR + spacing * (index + 1) + cell_w * index
            body.append(f'<rect x="{cx:.1f}" y="{core_y:.1f}" width="{cell_w}" height="{core_h}" '
                        f'rx="5" class="core"/>')
            body.append(_text(cx + cell_w / 2, core_y + core_h / 2 + 4, label, "core-label", "middle"))

        notch_x = left_x + LEG_W + BETWEEN / 2
        body.append(_text(notch_x, top + 46, "outside", "outside-label", "middle"))
        body.append(_text(notch_x, bottom - 30, "outside", "outside-label", "middle"))

    body.append(_text(left_x, MARGIN + 2, f"Floor plan · {slots} rooms per outer column", "cap"))

    return Markup(
        f'<svg class="plan-svg" viewBox="0 0 {WIDTH} {height}" role="img" aria-label="Floor plan">'
        f"<defs>{HATCH}</defs>{''.join(body)}</svg>"
    )


def room_card(room, x, y, states, types_by_id, href_for, matched=None):
    state = states.get(room.id, "available")
    room_type = types_by_id[room.room_type_id]
    note = "" if room.has_window else " · no window"
    dim = " dim" if matched is not None and room.id not in matched else ""
    return (
        f'<a href="{href_for(room)}" class="sv-room{dim}">'
        f"<title>{room.number} · {room_type.name} · {state.replace('_', ' ')}</title>"
        f'<rect x="{x}" y="{y}" width="{ROOM_W}" height="{ROOM_H}" rx="6" class="r {state}"/>'
        f'<text x="{x + 11}" y="{y + 24}" class="num">{room.number}</text>'
        f'<text x="{x + 11}" y="{y + 40}" class="bed">{room_type.bed_type.capitalize()}{note}</text>'
        "</a>"
    )
