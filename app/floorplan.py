"""Exact floor-plan drawing for the manager rooms page.

Coordinates are computed here and emitted as SVG, so walls and rooms cannot drift
the way a flex or grid layout can. Each room is a link to the room form.

A floor is laid out as a true H: two legs whose outer room columns run the full
height, a central link carrying the corridor and the lift core, and the inner
columns of each leg stopping above and below that link. The notches of the H are
the space between the legs above and below the link.

Per leg the outer column holds ``slots`` rooms (7 for a 24-room floor) and the
inner column holds ``slots - 2``, because the link takes two room slots.
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

LEG_W = PAD * 2 + ROOM_W * 2 + CORR
WIDTH = LEG_W * 2 + BETWEEN + MARGIN * 2

HATCH = (
    '<pattern id="hatch" width="10" height="10" patternUnits="userSpaceOnUse" '
    'patternTransform="rotate(45)">'
    '<rect width="10" height="10" fill="#f4f7f5"/>'
    '<line x1="0" y1="0" x2="0" y2="10" stroke="#e7ece9" stroke-width="4"/>'
    "</pattern>"
)


def _text(x, y, value, cls, anchor="start"):
    return f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{value}</text>'


def _line(x1, y1, x2, y2, cls="wall"):
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="{cls}"/>'


def render_floor_plan(plan, states, types_by_id, href_for, matched=None):
    """Return the SVG markup for one floor, or Markup() when there is nothing to draw."""

    if not plan:
        return Markup()
    west, east = plan["west"], plan["east"]
    slots = max(len(west["outer"]), len(east["outer"]))
    if not slots:
        return Markup()

    top = MARGIN + LABEL
    leg_h = PAD * 2 + slots * ROOM_H + (slots - 1) * GAP
    height = top + leg_h + MARGIN

    link_slot = max(0, (slots - LINK_SLOTS) // 2)
    link_y = top + PAD + link_slot * (ROOM_H + GAP)
    link_h = LINK_SLOTS * ROOM_H + (LINK_SLOTS - 1) * GAP

    left_x = MARGIN
    right_x = MARGIN + LEG_W + BETWEEN
    west_corr = left_x + PAD + ROOM_W
    east_corr = right_x + PAD + ROOM_W
    link_x1 = west_corr
    link_x2 = east_corr + CORR

    body = []
    for x in (left_x, right_x):
        body.append(f'<rect x="{x}" y="{top}" width="{LEG_W}" height="{leg_h}" class="fill"/>')
    body.append(f'<rect x="{link_x1}" y="{link_y}" width="{link_x2 - link_x1}" '
                f'height="{link_h}" class="fill"/>')

    for x in (west_corr, east_corr):
        body.append(f'<rect x="{x}" y="{top}" width="{CORR}" height="{link_y - top}" fill="url(#hatch)"/>')
        body.append(f'<rect x="{x}" y="{link_y + link_h}" width="{CORR}" '
                    f'height="{top + leg_h - link_y - link_h}" fill="url(#hatch)"/>')
    body.append(f'<rect x="{link_x1}" y="{link_y}" width="{link_x2 - link_x1}" '
                f'height="{link_h - 34}" fill="url(#hatch)"/>')
    body.append(f'<rect x="{link_x1}" y="{link_y + link_h - 34}" width="{link_x2 - link_x1}" '
                f'height="34" class="service"/>')

    for x in (left_x, right_x):
        corr_l = x + PAD + ROOM_W
        corr_r = corr_l + CORR
        body += [
            _line(x, top, x + LEG_W, top),
            _line(x, top + leg_h, x + LEG_W, top + leg_h),
            _line(x, top, x, top + leg_h),
            _line(x + LEG_W, top, x + LEG_W, top + leg_h),
            _line(corr_l, top, corr_l, link_y, "inner"),
            _line(corr_l, link_y + link_h, corr_l, top + leg_h, "inner"),
            _line(corr_r, top, corr_r, link_y, "inner"),
            _line(corr_r, link_y + link_h, corr_r, top + leg_h, "inner"),
        ]

    body += [
        _line(link_x1 + CORR, link_y, link_x2 - CORR, link_y),
        _line(link_x1 + CORR, link_y + link_h, link_x2 - CORR, link_y + link_h),
    ]

    for x_leg, leg, mirrored in ((left_x, west, False), (right_x, east, True)):
        if mirrored:
            outer_x = x_leg + PAD + ROOM_W + CORR
            inner_x = x_leg + PAD
        else:
            outer_x = x_leg + PAD
            inner_x = x_leg + PAD + ROOM_W + CORR
        body.append(_column(leg["outer"], outer_x, top + PAD, slots, states, types_by_id,
                            href_for, matched))
        for index, room in enumerate(leg["inner_up"]):
            body.append(room_card(room, inner_x, top + PAD + index * (ROOM_H + GAP),
                                  states, types_by_id, href_for, matched))
        for index, room in enumerate(leg["inner_down"]):
            slot = link_slot + LINK_SLOTS + index
            body.append(room_card(room, inner_x, top + PAD + slot * (ROOM_H + GAP),
                                  states, types_by_id, href_for, matched))

    core_y = link_y + link_h - 34
    span = link_x2 - link_x1 - 2 * CORR - 120
    for label, ratio in (("Stairs", 0.12), ("Lift A", 0.34), ("Lift B", 0.52), ("Linen", 0.74)):
        cx = link_x1 + CORR + span * ratio
        body.append(f'<rect x="{cx:.1f}" y="{core_y + 5}" width="88" height="24" rx="4" class="core"/>')
        body.append(_text(cx + 44, core_y + 21, label, "core-label", "middle"))

    mid = (link_x1 + link_x2) / 2
    body.append(_text((link_x1 + CORR + mid) / 2, link_y + 21, "cross corridor", "corr-label", "middle"))
    body.append(_text((mid + link_x2 - CORR) / 2, link_y + 21,
                      "Lift lobby · doors onto the corridor", "corr-strong", "middle"))

    notch_x = left_x + LEG_W + BETWEEN / 2
    body.append(_text(notch_x, top + 40, "outside", "outside-label", "middle"))
    body.append(_text(notch_x, top + leg_h - 24, "outside", "outside-label", "middle"))
    body.append(_text(left_x, MARGIN + 2, f"Floor plan · {slots} slots per leg", "cap"))

    return Markup(
        f'<svg class="plan-svg" viewBox="0 0 {WIDTH} {height}" role="img" aria-label="Floor plan">'
        f"<defs>{HATCH}</defs>{''.join(body)}</svg>"
    )


def _column(rooms, x, y, slots, states, types_by_id, href_for, matched=None):
    return "".join(
        room_card(room, x, y + index * (ROOM_H + GAP), states, types_by_id, href_for, matched)
        for index, room in enumerate(rooms[:slots])
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

