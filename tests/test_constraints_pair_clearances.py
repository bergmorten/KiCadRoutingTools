"""Contract v1.1 ``pairClearances``: loader strictness, symmetric first-match
resolution, and pair-aware obstacle pricing where the routed net is known.

A pair rule prices a matched NET PAIR at its clearanceMm INSTEAD OF
max(classA, classB) (the LP50 half-bridge case: all HV nets share a 0.6 mm
creepage class against ground but sit one diode drop apart from each other,
and the board's .kicad_dru grants the HV-HV pair 0.2 mm).

Pytest-style (conftest.py puts the repo root on sys.path). The __main__ block
lets tests/run_all.py execute this file as a script too.
"""
import os as _os
import sys as _sys

# ROOT_BOOTSTRAP: run_all.py executes this file as a bare script, where
# sys.path[0] is tests/ — put the repo root first so top-level modules
# import (conftest.py does this only for pytest runs).
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
_ROOT = _os.path.dirname(_TESTS_DIR)
_sys.path.insert(0, _ROOT)
_sys.path.insert(1, _TESTS_DIR)                              # synth builders
_sys.path.insert(2, _os.path.join(_ROOT, 'rust_router'))     # GridObstacleMap

import copy
import sys

import pytest

from constraints_contract import ContractError, parse_contract

# Minimal valid document to graft pairClearances variants onto.
BASE_DOC = {
    "contract": 1,
    "defaults": {"trackWidthMm": 0.2, "clearanceMm": 0.2,
                 "viaSizeMm": 0.6, "viaDrillMm": 0.3},
    "netClasses": [
        {"name": "TX_HV", "appliesTo": ["+48V", "HV_*"], "clearanceMm": 0.6},
    ],
}

HV_PATTERNS = ["+48V", "HV_SW", "HV_TANK", "HV_HO", "HV_LO", "HV_HB"]


def _doc(pair_clearances):
    d = copy.deepcopy(BASE_DOC)
    d["pairClearances"] = pair_clearances
    return d


def _load(pair_clearances):
    return parse_contract(_doc(pair_clearances))


# --------------------------------------------------------- loader strictness

def test_spec_example_parses():
    c = _load([{"a": HV_PATTERNS, "b": HV_PATTERNS, "clearanceMm": 0.2}])
    assert len(c.pair_clearances) == 1
    r = c.pair_clearances[0]
    assert r.a == tuple(HV_PATTERNS) and r.b == tuple(HV_PATTERNS)
    assert r.clearance_mm == 0.2


def test_absent_field_means_no_rules():
    c = parse_contract(copy.deepcopy(BASE_DOC))
    assert c.pair_clearances == []
    assert c.resolve_pair("HV_SW", "HV_TANK") is None


def test_not_an_array_rejected():
    with pytest.raises(ContractError, match='"pairClearances" must be an array'):
        _load({"a": ["X"], "b": ["Y"], "clearanceMm": 0.2})


def test_entry_not_object_rejected():
    with pytest.raises(ContractError, match=r'pairClearances\[0\]'):
        _load(["not-an-object"])


def test_unknown_key_rejected():
    with pytest.raises(ContractError, match='unknown key "clearance"'):
        _load([{"a": ["X"], "b": ["Y"], "clearanceMm": 0.2, "clearance": 0.2}])


@pytest.mark.parametrize("missing", ["a", "b", "clearanceMm"])
def test_missing_required_key_rejected(missing):
    entry = {"a": ["X"], "b": ["Y"], "clearanceMm": 0.2}
    del entry[missing]
    with pytest.raises(ContractError, match=f'"{missing}"'):
        _load([entry])


@pytest.mark.parametrize("bad_a", [[], "X", [""], ["X", 3], None])
def test_pattern_arrays_must_be_nonempty_strings(bad_a):
    with pytest.raises(ContractError, match='"a"'):
        _load([{"a": bad_a, "b": ["Y"], "clearanceMm": 0.2}])


@pytest.mark.parametrize("bad_mm", [0, -0.2, "0.2", True, float("nan"),
                                    float("inf"), None])
def test_clearance_mm_must_be_positive_finite_number(bad_mm):
    with pytest.raises(ContractError, match='"clearanceMm"'):
        _load([{"a": ["X"], "b": ["Y"], "clearanceMm": bad_mm}])


# --------------------------------------------- resolve_pair (name patterns)

def test_resolve_pair_symmetric():
    c = _load([{"a": ["+48V"], "b": ["HV_*"], "clearanceMm": 0.2}])
    # One side matches 'a', the other 'b' -- in EITHER assignment.
    assert c.resolve_pair("+48V", "HV_SW") == 0.2
    assert c.resolve_pair("HV_SW", "+48V") == 0.2
    # Both sides from the same pattern list is NOT a match for an a/b rule.
    assert c.resolve_pair("HV_SW", "HV_TANK") is None
    assert c.resolve_pair("+48V", "GND") is None


def test_resolve_pair_first_match_wins():
    c = _load([
        {"a": ["HV_SW"], "b": ["HV_TANK"], "clearanceMm": 0.3},
        {"a": HV_PATTERNS, "b": HV_PATTERNS, "clearanceMm": 0.2},
    ])
    # Pair covered by both rules -> the FIRST in array order wins.
    assert c.resolve_pair("HV_SW", "HV_TANK") == 0.3
    assert c.resolve_pair("HV_TANK", "HV_SW") == 0.3
    # Pair covered only by the second rule.
    assert c.resolve_pair("HV_SW", "HV_HB") == 0.2


def test_pair_rules_to_ids_preserves_order_and_globs():
    c = _load([
        {"a": ["HV_SW"], "b": ["HV_TANK"], "clearanceMm": 0.3},
        {"a": HV_PATTERNS, "b": HV_PATTERNS, "clearanceMm": 0.2},
    ])
    names = {1: "+48V", 2: "HV_SW", 3: "HV_TANK", 4: "GND", 5: "SIG1"}
    rules = c.pair_rules_to_ids(names)
    assert rules == [
        (frozenset({2}), frozenset({3}), 0.3),
        (frozenset({1, 2, 3}), frozenset({1, 2, 3}), 0.2),
    ]


# ------------------------------------------ config-side id-pair resolution

def _mk_config(pair_rules, net_clearances, routed):
    from routing_config import GridRouteConfig
    c = GridRouteConfig(grid_step=0.1, clearance=0.15, via_size=0.6,
                        track_width=0.2, via_drill=0.3,
                        hole_to_hole_clearance=0.0, layers=['F.Cu', 'B.Cu'])
    c.set_net_clearances(net_clearances, list(routed))
    c.pair_clearances = list(pair_rules)
    return c


def test_config_pair_clearance_first_match_and_symmetry():
    cfg = _mk_config([(frozenset({1}), frozenset({2}), 0.3),
                      (frozenset({1, 2, 3}), frozenset({1, 2, 3}), 0.2)],
                     {1: 0.6, 2: 0.6, 3: 0.6}, routed=(1, 2, 3))
    assert cfg.pair_clearance(1, 2) == 0.3
    assert cfg.pair_clearance(2, 1) == 0.3
    assert cfg.pair_clearance(1, 3) == 0.2
    assert cfg.pair_clearance(3, 4) is None
    # A net never pair-rules against ITSELF (1 is in both sides of rule 2).
    assert cfg.pair_clearance(1, 1) is None
    # pair_obstacle_clearance: rule mm replaces the class max; unruled pairs
    # get this exact pair's max(classA, classB).
    assert cfg.pair_obstacle_clearance(1, 2) == 0.3
    assert cfg.pair_obstacle_clearance(3, 4) == 0.6


# --------------------------------------------------- obstacle-map pricing
#
# Geometry: obstacle via size 0.8, route via 0.6 -> a new via must keep
# center-to-center 0.4 + 0.3 + clearance. At the 0.6 class that is 1.3 mm;
# at the 0.2 pair rule, 0.9 mm. A probe at 1.0 mm is therefore BLOCKED under
# class pricing and FREE under the pair rule; a probe at 0.8 mm stays blocked
# under both (the relaxation is not a free-for-all).

N_A, N_B, N_C = 1, 2, 3           # A routes; B has a pair rule with A; C has none
HV_CLASSES = {N_A: 0.6, N_B: 0.6, N_C: 0.6}
RULE_AB = [(frozenset({N_A}), frozenset({N_B}), 0.2)]
B_XY, C_XY = (50.0, 50.0), (50.0, 60.0)


def _mk_pcb(vias_for):
    from synth import make_pcb, make_via, make_net
    from kicad_parser import BoardInfo
    pos = {N_A: (20.0, 20.0), N_B: B_XY, N_C: C_XY}
    return make_pcb(
        nets={n: make_net(n, f'NET{n}') for n in vias_for},
        vias=[make_via(*pos[n], net_id=n, size=0.8, drill=0.4) for n in vias_for],
        board_info=BoardInfo(layers={}, copper_layers=['F.Cu', 'B.Cu'],
                             board_bounds=None))


def _probes(cfg):
    from routing_config import GridCoord
    coord = GridCoord(cfg.grid_step)
    return {
        'near_B': coord.to_grid(B_XY[0] - 1.0, B_XY[1]),   # 1.0 mm from B
        'close_B': coord.to_grid(B_XY[0] - 0.8, B_XY[1]),  # 0.8 mm from B
        'near_C': coord.to_grid(C_XY[0] - 1.0, C_XY[1]),   # 1.0 mm from C
    }


def test_single_net_base_map_prices_pair_rule():
    """The rescue-path build (nets_to_route == [A]): B priced at the 0.2 pair
    rule, C stays at the 0.6 class -- from the SAME build."""
    from obstacle_map import build_base_obstacle_map
    pcb = _mk_pcb([N_B, N_C])
    p = _probes(_mk_config([], {}, ()))

    # Without the rule: both HV neighbours block the 1.0 mm probes.
    cfg0 = _mk_config([], HV_CLASSES, routed=(N_A,))
    m0 = build_base_obstacle_map(pcb, cfg0, [N_A], net_clearances=HV_CLASSES)
    assert m0.is_via_blocked(*p['near_B'])
    assert m0.is_via_blocked(*p['near_C'])

    # With the A-B rule at 0.2: A may approach B to 0.2 (probe at 1.0 mm is
    # free, probe at 0.8 mm still blocked); C keeps the full 0.6 class.
    cfg1 = _mk_config(RULE_AB, HV_CLASSES, routed=(N_A,))
    m1 = build_base_obstacle_map(pcb, cfg1, [N_A], net_clearances=HV_CLASSES)
    assert not m1.is_via_blocked(*p['near_B'])
    assert m1.is_via_blocked(*p['close_B'])
    assert m1.is_via_blocked(*p['near_C'])


def test_precompute_clearance_override_channel():
    """The per-net cache twin: clearance_override re-prices ONLY the priced-in
    clearance, everything else identical."""
    from obstacle_cache import precompute_net_obstacles, add_net_obstacles_from_cache
    from grid_router import GridObstacleMap
    pcb = _mk_pcb([N_B])
    cfg = _mk_config(RULE_AB, HV_CLASSES, routed=(N_A, N_B, N_C))
    p = _probes(cfg)

    m_std = GridObstacleMap(2)
    add_net_obstacles_from_cache(m_std, precompute_net_obstacles(pcb, N_B, cfg))
    assert m_std.is_via_blocked(*p['near_B'])  # class price 0.6

    m_pair = GridObstacleMap(2)
    add_net_obstacles_from_cache(m_pair, precompute_net_obstacles(
        pcb, N_B, cfg, clearance_override=0.2))
    assert not m_pair.is_via_blocked(*p['near_B'])
    assert m_pair.is_via_blocked(*p['close_B'])


def test_prepare_restore_swap_is_pair_aware_and_balanced():
    """The main-loop path: routing A swaps B's cache to the 0.2 pair price
    (C stays at 0.6), and restore returns the working map to its exact
    pre-prepare state (ref-count symmetry, #208/#309)."""
    from obstacle_map import build_base_obstacle_map
    from obstacle_cache import precompute_all_net_obstacles, build_working_obstacle_map
    from routing_context import prepare_obstacles_inplace, restore_obstacles_inplace

    pcb = _mk_pcb([N_A, N_B, N_C])
    cfg = _mk_config(RULE_AB, HV_CLASSES, routed=(N_A, N_B, N_C))
    p = _probes(cfg)
    layer_map = {'F.Cu': 0, 'B.Cu': 1}
    all_ids = [N_A, N_B, N_C]

    base = build_base_obstacle_map(pcb, cfg, all_ids, net_clearances=HV_CLASSES)
    caches = precompute_all_net_obstacles(pcb, all_ids, cfg)
    working = build_working_obstacle_map(base, caches)

    # Shared working map: everything at the conservative class price.
    assert working.is_via_blocked(*p['near_B'])
    assert working.is_via_blocked(*p['near_C'])
    stats_before = (working.get_stats()[0], working.get_stats()[1])

    # Routing A: the A-B pair rule opens B to 0.2; C (no rule) stays 0.6.
    _, via_cells = prepare_obstacles_inplace(
        working, pcb, cfg, N_A, all_ids, [], {}, layer_map, caches)
    assert not working.is_via_blocked(*p['near_B'])
    assert working.is_via_blocked(*p['close_B'])
    assert working.is_via_blocked(*p['near_C'])

    restore_obstacles_inplace(working, N_A, caches, via_cells)
    assert working.is_via_blocked(*p['near_B'])
    assert (working.get_stats()[0], working.get_stats()[1]) == stats_before

    # Routing C: no rule against B -> B stays at the full class price.
    _, via_cells = prepare_obstacles_inplace(
        working, pcb, cfg, N_C, all_ids, [], {}, layer_map, caches)
    assert working.is_via_blocked(*p['near_B'])
    restore_obstacles_inplace(working, N_C, caches, via_cells)
    assert (working.get_stats()[0], working.get_stats()[1]) == stats_before


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
