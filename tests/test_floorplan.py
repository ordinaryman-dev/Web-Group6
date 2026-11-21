# 房态图显示与转义：不同楼层容量不漏房、不越界，用户文本不能变成脚本标记。
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from app.floorplan import build_floor_plan, render_floor_plan


@pytest.mark.parametrize("count", [1, 3, 8, 11, 12, 24, 25, 50])
def test_floorplan_keeps_every_room_once_and_inside_viewbox(app, count):
    rooms = [SimpleNamespace(id=i, number=str(300+i), room_type_id=1, has_window=i%2 == 0)
             for i in range(1, count+1)]
    room_type = SimpleNamespace(name="Twin", bed_type="twin")
    with app.test_request_context():
        markup = render_floor_plan(build_floor_plan(rooms), {}, {1: room_type},
                                   lambda room: f"/room/{room.id}", matched={1}, focused=1)
    svg = ElementTree.fromstring(str(markup))
    links = svg.findall('a')
    assert len(links) == count
    assert {link.get('href') for link in links} == {f"/room/{room.id}" for room in rooms}
    assert links[0].get('aria-current') == 'true'
    width, height = map(float, svg.get('viewBox').split()[2:])
    for link in links:
        rectangle = link.find('rect')
        assert 0 <= float(rectangle.get('x')) <= width-float(rectangle.get('width'))
        assert 0 <= float(rectangle.get('y')) <= height-float(rectangle.get('height'))


def test_floorplan_escapes_user_text_and_preserves_windows_and_blocked_state(app):
    room = SimpleNamespace(id=1, number='<script>alert("x")</script>', room_type_id=1, has_window=False)
    room_type = SimpleNamespace(name='Twin <unsafe>', bed_type='twin')
    with app.test_request_context():
        markup = str(render_floor_plan(build_floor_plan([room]), {1:'maintenance_conflict'},
                                       {1:room_type}, lambda room:'/room/1?q="&x=<tag>'))
    assert '<script>' not in markup and '&lt;script&gt;' in markup
    svg = ElementTree.fromstring(markup)
    link = svg.find('a')
    assert link.get('href') == '/room/1?q="&x=<tag>'
    assert not link.findall('.//script')
    assert link.find('title').text == '<script>alert("x")</script> · Twin <unsafe> · maintenance conflict'
    assert not [line for line in link.findall('line') if line.get('class') == 'window']
    assert [line for line in link.findall('line') if line.get('class') == 'out-slash']
