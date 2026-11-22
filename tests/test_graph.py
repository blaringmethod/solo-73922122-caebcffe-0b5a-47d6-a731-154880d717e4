"""图分析纯函数测试：连通性、方向取反、闭环矛盾。"""

from app.graph import Edge, analyze_components, zero_offset_between


def test_directed_offset_and_reverse_edge():
    # S1 零点 - S2 零点 = 15
    comps = analyze_components([Edge("S1", "S2", 15)])
    r = zero_offset_between(comps, "S1", "S2")
    assert r.ok and r.zero_offset_mm == 15
    # 反向查询自动取反
    r2 = zero_offset_between(comps, "S2", "S1")
    assert r2.ok and r2.zero_offset_mm == -15


def test_chained_offsets():
    # S1-S2 = 10, S2-S3 = 5 => S1-S3 = 15
    comps = analyze_components([Edge("S1", "S2", 10), Edge("S2", "S3", 5)])
    r = zero_offset_between(comps, "S1", "S3")
    assert r.ok and r.zero_offset_mm == 15
    r = zero_offset_between(comps, "S3", "S1")
    assert r.ok and r.zero_offset_mm == -15


def test_disconnected():
    comps = analyze_components([Edge("S1", "S2", 10), Edge("S3", "S4", 2)])
    r = zero_offset_between(comps, "S1", "S3")
    assert not r.ok and "未连通" in r.reason


def test_consistent_cycle_accepted():
    # 三角环闭合：S1-S2=10, S2-S3=5, S1-S3=15
    comps = analyze_components([Edge("S1", "S2", 10), Edge("S2", "S3", 5), Edge("S1", "S3", 15)])
    r = zero_offset_between(comps, "S1", "S3")
    assert r.ok and r.zero_offset_mm == 15


def test_contradictory_cycle_rejected():
    # 闭环矛盾：第三边应为 15，实际 14
    comps = analyze_components([Edge("S1", "S2", 10), Edge("S2", "S3", 5), Edge("S1", "S3", 14)])
    r = zero_offset_between(comps, "S1", "S2")
    assert not r.ok and "闭环矛盾" in r.reason
    # 矛盾组内任意两尺都不可用
    r2 = zero_offset_between(comps, "S2", "S3")
    assert not r2.ok


def test_unknown_station():
    comps = analyze_components([Edge("S1", "S2", 10)])
    r = zero_offset_between(comps, "S1", "SX")
    assert not r.ok and "未连通" in r.reason


def test_empty_graph():
    comps = analyze_components([])
    assert comps == {}
