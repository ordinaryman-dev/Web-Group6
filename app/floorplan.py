# 房态图布局：Python 计算房间、走廊和门窗位置，shared 模板输出 SVG；展示不改变数据库中的房态。
from flask import render_template
from markupsafe import Markup


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

def floor_plan_geometry(plan):
    # 统一计算走廊和画布尺寸，两个翼楼使用同一个连接带位置。
    west, east = plan["west"], plan["east"]
    slots = plan.get("slots") or max(len(west["outer"]), len(east["outer"]))
    if not slots:
        return None
    link = plan.get("link", slots >= MIN_SLOTS_FOR_LINK)
    top = MARGIN + LABEL
    first = top + PAD
    bottom = first + (slots - 1) * PITCH + ROOM_H + PAD
    left_x = MARGIN
    right_x = MARGIN + LEG_W + BETWEEN
    west_corr = left_x + PAD + ROOM_W
    east_corr = right_x + PAD + ROOM_W
    link_slot = max(0, min(slots - LINK_SLOTS, (slots - LINK_SLOTS) // 2)) if link else 0
    band_top = first + link_slot * PITCH if link else 0
    band_h = LINK_SLOTS * ROOM_H + (LINK_SLOTS - 1) * GAP
    band_bottom = band_top + band_h if link else 0
    band_x1, band_x2 = west_corr, east_corr + CORR
    outline = corridor = None
    centre_lines = []
    cores = []
    if link:
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
        for cx in (band_x1 + CORR / 2, east_corr + CORR / 2):
            centre_lines.extend([(cx, top, cx, band_top), (cx, band_bottom, cx, bottom)])
        centre_lines.append((band_x1, band_top + band_h / 2, band_x2, band_top + band_h / 2))
        cell_w, core_h = 84, 40
        core_y = band_top + (band_h - core_h) / 2
        spacing = (band_x2 - band_x1 - 2 * CORR - 4 * cell_w) / 5
        for index, label in enumerate(("Stairs", "Lift A", "Lift B", "Linen")):
            cx = band_x1 + CORR + spacing * (index + 1) + cell_w * index
            cores.append({"x": round(cx, 1), "y": round(core_y, 1), "width": cell_w,
                          "height": core_h, "lift": index in (1, 2), "label": label})
    return {
        "slots": slots, "link": link, "top": top, "first": first, "bottom": bottom,
        "height": bottom + MARGIN, "left_x": left_x, "right_x": right_x,
        "link_slot": link_slot, "outline": outline, "corridor": corridor,
        "centre_lines": centre_lines, "cores": cores,
        "notch_x": left_x + LEG_W + BETWEEN / 2,
    }


def placed_rooms(plan, geometry):
    # 把每一间房映射到唯一坐标；只安排显示位置，不选择客户实际入住的房间。
    positions = []
    slots, link_slot = geometry["slots"], geometry["link_slot"]
    for x_leg, leg, mirrored in ((geometry["left_x"], plan["west"], False),
                                 (geometry["right_x"], plan["east"], True)):
        outer_x = x_leg + LEG_W - PAD - ROOM_W if mirrored else x_leg + PAD
        inner_x = x_leg + PAD if mirrored else x_leg + LEG_W - PAD - ROOM_W
        outer_door, inner_door = ("left", "right") if mirrored else ("right", "left")
        columns = [(leg["outer"][:slots], outer_x, 0, outer_door)]
        if geometry["link"]:
            columns.extend([
                (leg["inner_up"][:link_slot], inner_x, 0, inner_door),
                (leg["inner_down"][:slots-link_slot-LINK_SLOTS], inner_x, link_slot+LINK_SLOTS, inner_door),
            ])
        else:
            columns.append((leg["inner_up"][:slots], inner_x, 0, inner_door))
        for rooms, x, start_slot, door_side in columns:
            for index, room in enumerate(rooms):
                positions.append({"room": room, "x": x,
                                  "y": geometry["first"] + (start_slot + index) * PITCH,
                                  "door_side": door_side})
    return positions


def render_floor_plan(plan, states, types_by_id, href_for, matched=None, *, focused=None):
    # 用户可变文本交给 HTML 模板自动转义；仅最终渲染结果标记为可信 HTML。
    if not plan:
        return Markup()
    geometry = floor_plan_geometry(plan)
    if geometry is None:
        return Markup()
    rooms = []
    for position in placed_rooms(plan, geometry):
        room = position["room"]
        room_type = types_by_id[room.room_type_id]
        state = states.get(room.id, "available")
        rooms.append({
            **position, "state": state, "href": href_for(room),
            "label": f"{room.number} · {room_type.name} · {state.replace('_', ' ')}",
            "bed": room_type.bed_type.capitalize(),
            "dim": matched is not None and room.id not in matched,
            "focused": room.id == focused,
            "blocked": state in ("out_of_service", "maintenance", "maintenance_conflict"),
        })
    return Markup(render_template(
        "shared/_floorplan.html", geometry=geometry, rooms=rooms,
        room_w=ROOM_W, room_h=ROOM_H, leg_w=LEG_W, corridor_w=CORR,
        pad=PAD, margin=MARGIN, width=WIDTH, door=DOOR, window_inset=WINDOW_INSET,
    ))


def build_floor_plan(rooms):
    # 先确定两侧外列和内列，再统一预留走廊占用的两行，防止奇数或较大楼层漏画房间。
    """Use one shared corridor position so uneven columns never clip rooms."""
    rooms = list(rooms)
    if not rooms:
        return None
    half = (len(rooms) + 1) // 2
    halves = (rooms[:half], rooms[half:])
    legs = []
    link = min(map(len, halves)) >= 6
    if not link:
        for leg in halves:
            legs.append({"outer": leg[::2], "inner_up": leg[1::2], "inner_down": [], "link": False})
        slots = max(len(leg["outer"]) for leg in legs)
        return {"west": legs[0], "east": legs[1], "slots": slots, "link": False}

    for index, leg in enumerate(halves):
        outer_count = len(leg) // 2 + 1
        if index == 0:
            outer, inner = leg[:outer_count], leg[outer_count:]
        else:
            outer, inner = leg[-outer_count:], leg[:-outer_count]
        legs.append({"outer": outer, "inner": inner, "link": True})
    slots = max(max(len(leg["outer"]), len(leg["inner"]) + LINK_SLOTS)
                for leg in legs)
    link_slot = (slots - LINK_SLOTS) // 2
    for leg in legs:
        inner = leg.pop("inner")
        leg["inner_up"], leg["inner_down"] = inner[:link_slot], inner[link_slot:]
    return {"west": legs[0], "east": legs[1], "slots": slots, "link": True}
