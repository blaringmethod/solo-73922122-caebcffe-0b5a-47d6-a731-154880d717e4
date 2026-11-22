"""测线图的连通组与闭环矛盾判定（纯函数，便于单测）。

测线约定：
    测线 (upstream, downstream, d) 表示
        上游尺零点高程 - 下游尺零点高程 = d （毫米，有向）。
    图按无向连通处理；站号相同、方向相反的测线取相反符号。

判定方法：对每个连通组取一棵 BFS 生成树，令相对零点高 rel[v] 为
    rel[v] = 零点高程(v) - 零点高程(根)，
对每条非树边核对两端高差是否与边权一致；任一不一致即为闭环矛盾，
该连通组整体不可用于核发。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Edge:
    upstream: str
    downstream: str
    zero_offset_mm: int


@dataclass
class Component:
    stations: frozenset[str]
    consistent: bool
    # rel[v] = 零点高程(v) - 零点高程(根)；矛盾组为 None
    relative_zeros: dict[str, int] | None
    root: str | None


def _build_adjacency(lines: list[Edge]) -> dict[str, list[tuple[str, int]]]:
    adj: dict[str, list[tuple[str, int]]] = {}
    for e in lines:
        adj.setdefault(e.upstream, []).append((e.downstream, e.zero_offset_mm))
        # 反向边高差取反：零点(down) - 零点(up) = -d
        adj.setdefault(e.downstream, []).append((e.upstream, -e.zero_offset_mm))
    return adj


def analyze_components(lines: list[Edge]) -> dict[str, Component]:
    """返回每个站号所属的连通组分析结果。"""
    adj = _build_adjacency(lines)
    result: dict[str, Component] = {}
    visited: set[str] = set()

    for start in adj:
        if start in visited:
            continue
        rel: dict[str, int] = {start: 0}
        consistent = True
        queue: deque[str] = deque([start])
        visited.add(start)
        while queue:
            u = queue.popleft()
            for v, w in adj[u]:
                # 边 (u, v, w) 表示 零点(u) - 零点(v) = w
                expected_rel_v = rel[u] - w
                if v not in rel:
                    rel[v] = expected_rel_v
                    visited.add(v)
                    queue.append(v)
                elif rel[v] != expected_rel_v:
                    consistent = False
        stations = frozenset(rel.keys())
        comp = Component(
            stations=stations,
            consistent=consistent,
            relative_zeros=rel if consistent else None,
            root=start if consistent else None,
        )
        for s in stations:
            result[s] = comp
    return result


@dataclass
class OffsetResult:
    ok: bool
    zero_offset_mm: int | None = None
    reason: str | None = None


def zero_offset_between(
    components: dict[str, Component], upstream: str, downstream: str
) -> OffsetResult:
    """按当前测线图求两尺零点的有向高差（上游零点 - 下游零点，毫米）。"""
    cu = components.get(upstream)
    cd = components.get(downstream)
    if cu is None or cd is None or cu is not cd:
        return OffsetResult(False, reason="两尺未连通：当前测线图中两站不在同一连通组")
    if not cu.consistent or cu.relative_zeros is None:
        return OffsetResult(False, reason="所在连通组的测线闭环矛盾，无法确定两尺零点高差")
    return OffsetResult(True, zero_offset_mm=cu.relative_zeros[upstream] - cu.relative_zeros[downstream])
