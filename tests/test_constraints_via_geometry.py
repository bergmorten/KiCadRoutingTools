"""Per-net via geometry channel (constraints contract).

(d) Two nets in different classes emit DIFFERENT via sizes at a layer change,
    through the real emission funnel (_path_to_segments_vias -> _emit_via_size),
    on a tiny synthetic board.
(e) The fab-floor step-down ladder still shrinks a via BELOW its class size
    when foreign copper forces it (fitting_pad_via) -- never above.
(f) Planning/obstacle geometry follows the ROUTED net's via pair: per-net
    where the routed net is known (same-path via guard, diff-pair via
    spacing), max-over-routed-nets for the shared base obstacle map.

Pytest-style; the __main__ block lets tests/run_all.py run it as a script.
"""
import os as _os
import sys as _sys

# ROOT_BOOTSTRAP: run_all.py executes this file as a bare script, where
# sys.path[0] is tests/ — put the repo root first so top-level modules
# import (conftest.py does this only for pytest runs).
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import sys
import tempfile

import pytest

from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig, GridCoord, get_net_via_geometry
from single_ended_routing import _path_to_segments_vias
from stub_layer_switching import fitting_pad_via
from synth import make_net, make_pcb, make_seg

BOARD = """(kicad_pcb (version 20240108) (generator test)
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG_A")
  (net 2 "HV_TANK")
  (net 3 "PLAIN")
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 20) (end 0 20) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (width 0.1))
)
"""

LAYERS = ['F.Cu', 'B.Cu']
# One layer change at cell (100, 50) -> a via at (10.0, 5.0).
PATH = [(50, 50, 0), (100, 50, 0), (100, 50, 1), (150, 50, 1)]


def _board():
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb', prefix='cvg_')
    os.close(fd)
    try:
        with open(path, 'w') as f:
            f.write(BOARD)
        return parse_kicad_pcb(path)
    finally:
        os.unlink(path)


def _config():
    return GridRouteConfig(
        track_width=0.2, clearance=0.2, via_size=0.45, via_drill=0.2,
        grid_step=0.1, layers=LAYERS,
        net_via_geometry={1: (0.6, 0.3), 2: (0.25, 0.15)})


def _emit(net_id, config, pcb):
    segs, vias = _path_to_segments_vias(
        PATH, GridCoord(config.grid_step), LAYERS, net_id, config,
        None, None, None, pcb)
    return segs, vias


# ------------------------------------------------------------- helper

def test_get_net_via_geometry_fallback():
    config = _config()
    assert get_net_via_geometry(config, 1) == (0.6, 0.3)
    assert get_net_via_geometry(config, 2) == (0.25, 0.15)
    assert get_net_via_geometry(config, 3) == (0.45, 0.2)   # global pair
    assert get_net_via_geometry(config, None) == (0.45, 0.2)
    # A config without the field (older constructors) resolves to the globals.
    class _Legacy:
        via_size, via_drill = 0.5, 0.3
    assert get_net_via_geometry(_Legacy(), 1) == (0.5, 0.3)


# ------------------------------------------------ (d) emitted via sizes

def test_two_nets_emit_different_via_sizes_at_layer_change():
    pcb = _board()
    config = _config()

    _, vias_a = _emit(1, config, pcb)
    assert len(vias_a) == 1
    assert (vias_a[0].size, vias_a[0].drill) == (0.6, 0.3)
    assert vias_a[0].net_id == 1

    _, vias_b = _emit(2, config, pcb)
    assert len(vias_b) == 1
    assert (vias_b[0].size, vias_b[0].drill) == (0.25, 0.15)

    # A net with no per-net entry keeps the config globals -- the historical
    # single-pair behaviour.
    _, vias_c = _emit(3, config, pcb)
    assert len(vias_c) == 1
    assert (vias_c[0].size, vias_c[0].drill) == (0.45, 0.2)


def test_empty_map_is_byte_identical_to_legacy():
    pcb = _board()
    config = _config()
    config.net_via_geometry = {}
    for nid in (1, 2, 3):
        _, vias = _emit(nid, config, pcb)
        assert [(v.size, v.drill) for v in vias] == [(0.45, 0.2)]


# ------------------------------- (e) ladder steps down below class size

def test_ladder_steps_below_class_size_when_pad_forces_it():
    # Foreign net-2 track 0.48mm from the pad centre:
    #   class via 0.6 (r 0.30): needs 0.30 + 0.2 clr + 0.1 half-width = 0.60 > 0.48 -> blocked
    #   fab rung 0.45 (r 0.225): needs 0.525 > 0.48 -> still blocked
    #   fab rung 0.25 (r 0.125): needs 0.425 < 0.48 -> fits
    pcb = make_pcb(
        nets={2: make_net(2, 'OTHER')},
        segments=[make_seg(0.48, -5.0, 0.48, 5.0, layer='B.Cu', net_id=2,
                           width=0.2)])
    config = GridRouteConfig(track_width=0.2, clearance=0.2,
                             via_size=0.45, via_drill=0.2, layers=LAYERS,
                             net_via_geometry={1: (0.6, 0.3)})
    fit = fitting_pad_via(0.0, 0.0, 1, pcb, config, set())
    assert fit is not None
    v_dia, v_drill = fit
    assert v_dia < 0.6          # below the class size: geometry forced it
    assert (v_dia, v_drill) == (0.25, 0.15)   # 2-layer advanced fab rung


def test_ladder_starts_from_class_size_when_clear():
    # No foreign copper at all: the net's CLASS geometry ships unshrunk, even
    # though it is larger than the config global.
    pcb = make_pcb(nets={})
    config = GridRouteConfig(track_width=0.2, clearance=0.2,
                             via_size=0.45, via_drill=0.2, layers=LAYERS,
                             net_via_geometry={1: (0.6, 0.3)})
    assert fitting_pad_via(0.0, 0.0, 1, pcb, config, set()) == (0.6, 0.3)
    # And a net without an entry starts from the config global.
    assert fitting_pad_via(0.0, 0.0, 3, pcb, config, set()) == (0.45, 0.2)


# --------------------------------------- GND stitching vias (route_planes #4)

GND_BOARD = """(kicad_pcb (version 20240108) (generator test)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "GND")
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 20) (end 0 20) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (width 0.1))
  (via (at 15 10) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1))
)
"""


def test_gnd_stitching_vias_size_from_gnd_nets_geometry():
    from obstacle_map import build_base_obstacle_map
    from add_gnd_vias import add_gnd_vias_to_existing_board

    fd, path = tempfile.mkstemp(suffix='.kicad_pcb', prefix='cvg_gnd_')
    os.close(fd)
    try:
        with open(path, 'w') as f:
            f.write(GND_BOARD)
        pcb = parse_kicad_pcb(path)
    finally:
        os.unlink(path)

    config = GridRouteConfig(track_width=0.2, clearance=0.2,
                             via_size=0.45, via_drill=0.2, grid_step=0.1,
                             layers=LAYERS,
                             net_via_geometry={2: (0.8, 0.4)})
    obstacles = build_base_obstacle_map(pcb, config, [])
    vias = add_gnd_vias_to_existing_board(pcb, 'GND', 3.0, config,
                                          obstacles, GridCoord(0.1))
    assert [(v.net_id, v.size, v.drill) for v in vias] == [(2, 0.8, 0.4)]
    # The via-via minimum grows with the GND ring: (0.8+0.45)/2 + 0.2 = 0.825
    # (vs the legacy 0.65 at uniform geometry). Candidates snap to the 0.1
    # grid (pre-existing), so allow up to a half-cell diagonal of snap noise.
    v = vias[0]
    dist = ((v.x - 15.0) ** 2 + (v.y - 10.0) ** 2) ** 0.5
    assert dist >= 0.825 - 0.1 * 2 ** 0.5 / 2


def test_gnd_stitching_vias_default_geometry_unchanged():
    from obstacle_map import build_base_obstacle_map
    from add_gnd_vias import add_gnd_vias_to_existing_board

    fd, path = tempfile.mkstemp(suffix='.kicad_pcb', prefix='cvg_gnd_')
    os.close(fd)
    try:
        with open(path, 'w') as f:
            f.write(GND_BOARD)
        pcb = parse_kicad_pcb(path)
    finally:
        os.unlink(path)

    config = GridRouteConfig(track_width=0.2, clearance=0.2,
                             via_size=0.45, via_drill=0.2, grid_step=0.1,
                             layers=LAYERS)
    obstacles = build_base_obstacle_map(pcb, config, [])
    vias = add_gnd_vias_to_existing_board(pcb, 'GND', 3.0, config,
                                          obstacles, GridCoord(0.1))
    assert [(v.net_id, v.size, v.drill) for v in vias] == [(2, 0.45, 0.2)]


# ---------------- (f) planning geometry is per-net / max-over-routed
# Finding 1: obstacle/planning expansions must reflect the ROUTED net's via
# geometry -- per-net where the routed net is known, max over the nets being
# routed for structures shared by the whole pass.

def test_max_routing_via_geometry():
    from routing_config import max_routing_via_geometry
    config = _config()   # net 1: (0.6, 0.3), net 2: (0.25, 0.15), globals 0.45/0.2
    # Floored at the globals: net 2's smaller pair never shrinks the result.
    assert max_routing_via_geometry(config, [2]) == (0.45, 0.2)
    assert max_routing_via_geometry(config, [1, 2]) == (0.6, 0.3)
    assert max_routing_via_geometry(config, [3]) == (0.45, 0.2)
    assert max_routing_via_geometry(config, []) == (0.45, 0.2)
    config.net_via_geometry = {}
    assert max_routing_via_geometry(config, [1, 2, 3]) == (0.45, 0.2)


def test_base_map_via_keepout_reflects_routed_net_geometry():
    """A foreign via's via-to-via keep-out in the shared base map must grow
    when a net with 2x default via geometry is among the nets being routed:
    via_via_mm = obstacle/2 + ROUTING/2 + clearance = 0.65mm at the globals,
    0.875mm with the (0.9, 0.4) class. A probe cell 0.8mm from the obstacle
    via is open at default geometry and blocked for the big-via pass."""
    from obstacle_map import build_base_obstacle_map

    fd, path = tempfile.mkstemp(suffix='.kicad_pcb', prefix='cvg_keep_')
    os.close(fd)
    try:
        with open(path, 'w') as f:
            f.write(GND_BOARD)   # via at (15, 10) on net 1 SIG
        pcb = parse_kicad_pcb(path)
    finally:
        os.unlink(path)

    coord = GridCoord(0.1)
    probe = coord.to_grid(15.8, 10.0)   # 0.8mm from the via centre

    def _build(geometry):
        config = GridRouteConfig(track_width=0.2, clearance=0.2,
                                 via_size=0.45, via_drill=0.2, grid_step=0.1,
                                 hole_to_hole_clearance=0.2, layers=LAYERS,
                                 net_via_geometry=geometry)
        # Route net 2 (GND): the SIG via at (15, 10) is a foreign obstacle.
        return build_base_obstacle_map(pcb, config, [2])

    # Default geometry: keep-out radius 0.65mm < 0.8mm -> open.
    assert not _build({}).is_via_blocked(*probe)
    # Routed net carries a 2x class: radius 0.875mm > 0.8mm -> blocked.
    assert _build({2: (0.9, 0.4)}).is_via_blocked(*probe)
    # A big class on a net NOT being routed must not inflate the map.
    assert not _build({1: (0.9, 0.4)}).is_via_blocked(*probe)


def test_same_path_via_guard_uses_net_drill():
    from single_ended_routing import (_path_has_close_vias,
                                      _via_drill_exclusion_radius)
    config = GridRouteConfig(track_width=0.2, clearance=0.2,
                             via_size=0.45, via_drill=0.2, grid_step=0.1,
                             hole_to_hole_clearance=0.3, layers=LAYERS,
                             net_via_geometry={1: (0.6, 0.3)})
    # Two same-net vias 5 cells (0.5mm) apart: min c-c is (drill + h2h).
    # Globals: 0.2 + 0.3 = 0.5mm -> NOT closer than the floor.
    # Net 1:   0.3 + 0.3 = 0.6mm -> a violation for its larger drill.
    path = [(0, 0, 0), (0, 0, 1), (5, 0, 1), (5, 0, 0)]
    assert not _path_has_close_vias(path, config)
    assert not _path_has_close_vias(path, config, net_id=3)
    assert _path_has_close_vias(path, config, net_id=1)
    # The guard radius follows: ceil((drill + h2h)/step/2).
    assert _via_drill_exclusion_radius(config) == 3
    assert _via_drill_exclusion_radius(config, net_id=1) == 3
    config2 = GridRouteConfig(track_width=0.2, clearance=0.2,
                              via_size=0.45, via_drill=0.2, grid_step=0.1,
                              hole_to_hole_clearance=0.35, layers=LAYERS,
                              net_via_geometry={1: (0.6, 0.4)})
    assert _via_drill_exclusion_radius(config2) == 3       # ceil(0.55/0.1/2)
    assert _via_drill_exclusion_radius(config2, net_id=1) == 4  # ceil(0.75/0.1/2)


def test_diff_pair_via_spacing_uses_pair_geometry():
    from diff_pair_routing import (_min_via_center_distance,
                                   _pair_via_geometry, _pair_via_offset)
    config = GridRouteConfig(track_width=0.2, clearance=0.2,
                             via_size=0.45, via_drill=0.2, grid_step=0.1,
                             hole_to_hole_clearance=0.3, layers=LAYERS,
                             net_via_geometry={1: (0.6, 0.3),
                                               2: (0.25, 0.15)})
    # Pair geometry is the elementwise max over the two nets.
    assert _pair_via_geometry(config, 1, 3) == (0.6, 0.3)
    assert _pair_via_geometry(config, 3, 3) == (0.45, 0.2)
    # Copper rule 0.6+0.2=0.8 vs drill rule 0.3+0.3=0.6 -> 0.8 for net 1;
    # the globals give max(0.65, 0.5) = 0.65.
    assert _min_via_center_distance(config) == 0.65
    assert _min_via_center_distance(config, 0.6, 0.3) == 0.8
    # The perpendicular via offset grows with the pair's via size.
    off_default = _pair_via_offset(config, 0.15)
    off_big = _pair_via_offset(config, 0.15, 0.6, 0.3)
    assert off_big > off_default


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))


# A no-net SMD pad sitting in open board space. net 0 is KiCad's "no net":
# U321 pad 15 on LP50 Rev A is exactly this, and a 0.6 mm contract-class via
# landed 0.106 mm from it — through BOTH the 0.2 netclass clearance and the
# 0.25 hole floor — because the pad's via keep-out was priced at the run-global
# via size instead of the routed net's own geometry.
NC_PAD_BOARD = """(kicad_pcb (version 20240108) (generator test)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "GND")
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 20) (end 0 20) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (width 0.1))
  (footprint "nc" (layer "F.Cu") (at 15 10)
    (pad "15" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu"))
  )
)
"""


def test_no_net_pad_via_keepout_uses_routed_net_via_geometry():
    """A NO-NET pad must keep a via away by (routed via)/2 + clearance + pad
    half-size — priced at the ROUTED net's geometry, not the global.

    Pad half-width 0.5, clearance 0.2. At the 0.45 global via the keep-out
    reaches 0.5 + 0.2 + 0.225 = 0.925 mm from the pad centre; with a 0.9 mm
    class via it reaches 0.5 + 0.2 + 0.45 = 1.15 mm. A probe 1.05 mm out is
    therefore legal at the global and MUST be blocked once the routed net
    carries the bigger via.
    """
    from obstacle_map import build_base_obstacle_map

    fd, path = tempfile.mkstemp(suffix='.kicad_pcb', prefix='cvg_ncpad_')
    os.close(fd)
    try:
        with open(path, 'w') as f:
            f.write(NC_PAD_BOARD)
        pcb = parse_kicad_pcb(path)
    finally:
        os.unlink(path)

    coord = GridCoord(0.1)
    probe = coord.to_grid(16.05, 10.0)   # 1.05 mm right of the pad centre

    def _build(geometry):
        config = GridRouteConfig(track_width=0.2, clearance=0.2,
                                 via_size=0.45, via_drill=0.2, grid_step=0.1,
                                 hole_to_hole_clearance=0.2, layers=LAYERS,
                                 net_via_geometry=geometry)
        # Route net 1 (SIG); the no-net pad is a foreign obstacle.
        return build_base_obstacle_map(pcb, config, [1])

    assert not _build({}).is_via_blocked(*probe), \
        'global-geometry keep-out should leave 1.05 mm open'
    assert _build({1: (0.9, 0.4)}).is_via_blocked(*probe), \
        'a 0.9 mm class via must be blocked 1.05 mm from a no-net pad'


def test_no_net_pad_is_stamped_at_all():
    """The no-net pad must be an obstacle in the first place: net 0 is a real
    copper feature, and skipping it is how a via ends up on top of one."""
    from obstacle_map import build_base_obstacle_map

    fd, path = tempfile.mkstemp(suffix='.kicad_pcb', prefix='cvg_ncpad2_')
    os.close(fd)
    try:
        with open(path, 'w') as f:
            f.write(NC_PAD_BOARD)
        pcb = parse_kicad_pcb(path)
    finally:
        os.unlink(path)
    coord = GridCoord(0.1)
    config = GridRouteConfig(track_width=0.2, clearance=0.2,
                             via_size=0.45, via_drill=0.2, grid_step=0.1,
                             hole_to_hole_clearance=0.2, layers=LAYERS)
    om = build_base_obstacle_map(pcb, config, [1])
    centre = coord.to_grid(15.0, 10.0)
    assert om.is_blocked(centre[0], centre[1], 0), 'no-net pad copper must block tracks'
    assert om.is_via_blocked(*centre), 'no-net pad must block vias'
