"""Plane-served nets are not loop failures.

kad excludes plane-poured nets from track routing with '!NET' patterns. route.py
skips them and its summary then reports them as failed / pad-deficit, so
place_route_loop counted phantom failures: LP50 Rev A reported "7 failures" of
which 5 were GND and the power rails doing exactly what the planes stage poured
them to do. The loop then spent rounds quenching parts anchoring nets that were
never going to carry a track.
"""
import os as _os
import sys as _sys

# ROOT_BOOTSTRAP: run_all.py executes this file as a bare script, where
# sys.path[0] is tests/ — put the repo root first so top-level modules
# import (conftest.py does this only for pytest runs).
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import tempfile

import pytest

from place_route_loop import merge_route_summaries, plane_served_nets, run_route
import place_route_loop


ROUTE_ARGS = ("'*' '!GND' '!+48V' --layers F.Cu B.Cu --ordering mps")


def test_plane_served_nets_parses_exclusions():
    assert plane_served_nets(ROUTE_ARGS) == {'GND', '+48V'}


def test_plane_served_nets_tolerates_junk():
    assert plane_served_nets('') == set()
    assert plane_served_nets('--layers F.Cu') == set()
    # A lone '!' is not a net name.
    assert plane_served_nets('!') == set()
    # Unbalanced quotes must not explode the round.
    assert plane_served_nets("'unclosed") == set()


def _summary(**over):
    s = {
        'failed_single': ['GND', '+48V', 'HV_SW'],
        'failed_multipoint': [{'net_name': 'GND', 'failed_pads': 4}],
        'multipoint_nets': 2,
        'multipoint_pads_connected': 8,
        'multipoint_pads_total': 12,
        'successful': 5,
        'failed': 3,
        'total_iterations': 100,
        'total_vias': 7,
    }
    s.update(over)
    return s


def _canned_run(tmpdir, summary, route_args):
    """Drive run_route's tally against a canned log, no child process.

    run_route returns its metrics DICT (failures / failed_nets / blockers / …).
    """
    log_path = _os.path.join(tmpdir, 'route.log')
    with open(log_path, 'w', encoding='utf-8') as f:
        f.write('Routing 3 nets\n')
        f.write('JSON_SUMMARY: ' + json.dumps(summary) + '\n')
    original = place_route_loop._run_route_cmd
    place_route_loop._run_route_cmd = lambda cmd, lf: 0
    try:
        return run_route('in.kicad_pcb', 'out.kicad_pcb', route_args, log_path)
    finally:
        place_route_loop._run_route_cmd = original


def test_excluded_nets_leave_the_failure_tally():
    with tempfile.TemporaryDirectory() as td:
        metrics = _canned_run(td, _summary(), ROUTE_ARGS)
    # GND and +48V were excluded on purpose: only HV_SW is a real failure,
    # and GND's 4 unconnected multipoint pads leave the deficit too.
    assert metrics['failed_nets'] == ['HV_SW'], metrics['failed_nets']
    assert metrics['failures'] == 1, metrics


def test_without_exclusions_everything_still_counts():
    with tempfile.TemporaryDirectory() as td:
        metrics = _canned_run(td, _summary(), '--layers F.Cu B.Cu')
    assert set(metrics['failed_nets']) == {'GND', '+48V', 'HV_SW'}
    # 3 failed single + 4-pad deficit, exactly the pre-fix behaviour.
    assert metrics['failures'] == 3 + 4, metrics


def test_pad_deficit_never_goes_negative():
    # A malformed summary claiming more excluded failed pads than the total
    # deficit must not produce a negative (and thus flattering) tally.
    s = _summary(multipoint_pads_connected=11, multipoint_pads_total=12,
                 failed_multipoint=[{'net_name': 'GND', 'failed_pads': 9}])
    with tempfile.TemporaryDirectory() as td:
        metrics = _canned_run(td, s, ROUTE_ARGS)
    assert metrics['failures'] >= 1
    assert metrics['failures'] == 1  # HV_SW only; deficit floored at 0


if __name__ == '__main__':
    _sys.exit(pytest.main([__file__, '-q']))
