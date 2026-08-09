"""--constraints CLI wiring: mutual exclusion, strict-load exit 2, and an
end-to-end route.py run where per-net widths/clearances come from the contract.

Integration (subprocess): skipped by run_all.py --fast; runs under plain pytest.
"""
import os as _os
import sys as _sys

# ROOT_BOOTSTRAP: run_all.py executes this file as a bare script, where
# sys.path[0] is tests/ — put the repo root first so top-level modules
# import (conftest.py does this only for pytest runs).
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import json
import os
import re
import subprocess
import sys
import tempfile

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
BOARD = os.path.join(ROOT, 'kicad_files', 'flat_hierarchy.kicad_pcb')

CONTRACT = {
    "contract": 1,
    "defaults": {"trackWidthMm": 0.25, "clearanceMm": 0.2,
                 "viaSizeMm": 0.45, "viaDrillMm": 0.2},
    "netClasses": [
        {"name": "DATA", "appliesTo": ["DATA-*"],
         "trackWidthMm": 0.5, "clearanceMm": 0.3, "neck": "forbid"}
    ],
}


def _write(doc):
    fd, path = tempfile.mkstemp(suffix='.json', prefix='contract_cli_')
    os.close(fd)
    with open(path, 'w') as f:
        json.dump(doc, f)
    return path


def _run(script, *argv):
    return subprocess.run([sys.executable, os.path.join(ROOT, script)]
                          + list(argv), cwd=ROOT, capture_output=True,
                          text=True, timeout=600)


@pytest.mark.integration
def test_route_mutual_exclusion_names_the_flag():
    cpath = _write(CONTRACT)
    try:
        for flag, extra in (('--track-width', ['--track-width', '0.3']),
                            ('--fab-tier', ['--fab-tier', 'advanced']),
                            ('--no-neck-nets', ['--no-neck-nets', 'GND']),
                            ('--power-nets', ['--power-nets', '*GND*',
                                              '--power-nets-widths', '0.5'])):
            r = _run('route.py', BOARD, '/tmp/out.kicad_pcb',
                     '--constraints', cpath, *extra)
            assert r.returncode == 2, (flag, r.returncode, r.stderr)
            assert flag in r.stderr, (flag, r.stderr)
    finally:
        os.unlink(cpath)


@pytest.mark.integration
def test_route_planes_mutual_exclusion():
    cpath = _write(CONTRACT)
    try:
        r = _run('route_planes.py', BOARD, '--nets', 'GND',
                 '--plane-layers', 'B.Cu', '--constraints', cpath,
                 '--via-size', '0.6')
        assert r.returncode == 2, (r.returncode, r.stderr)
        assert '--via-size' in r.stderr
    finally:
        os.unlink(cpath)


@pytest.mark.integration
def test_invalid_contract_exits_2_naming_the_key():
    doc = dict(CONTRACT)
    doc['surpriseKey'] = 1
    cpath = _write(doc)
    try:
        r = _run('route.py', BOARD, '/tmp/out.kicad_pcb',
                 '--constraints', cpath)
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        assert 'surpriseKey' in (r.stdout + r.stderr)
    finally:
        os.unlink(cpath)


@pytest.mark.integration
def test_route_with_contract_applies_per_net_width_and_clearance():
    """Route two nets on the tracked flat_hierarchy board: the DATA-* class
    net must come out at its class width (0.5), the unclassed net at the
    contract default (0.25). Also asserts the run reports the contract
    resolution (clearance map + no-neck set)."""
    cpath = _write(CONTRACT)
    out = tempfile.mktemp(suffix='.kicad_pcb', prefix='contract_routed_')
    try:
        r = _run('route.py', BOARD, out, '--constraints', cpath,
                 '--nets', 'DATA-RB7', 'CLOCK-RB6')
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        assert 'Contract resolution:' in r.stdout
        m = re.search(r'Contract resolution: (\d+) net clearance\(s\).*?'
                      r'(\d+) width\(s\).*?(\d+) no-neck net\(s\)',
                      r.stdout, re.S)
        assert m, r.stdout[-2000:]
        sys.path.insert(0, ROOT)
        from kicad_parser import parse_kicad_pcb
        # The clearance map covers EVERY named net, not just the ones whose
        # class differs from the defaults: obstacle pricing and the rescue
        # ladder read a foreign net's clearance FROM the map, so an absent
        # entry forfeits the net's class value under a necked rescue (the
        # LP50 Rev A regression: 117 clearance violations in one stage).
        _pcb_in = parse_kicad_pcb(BOARD)
        _named = sum(1 for _nid, _n in _pcb_in.nets.items()
                     if _nid != 0 and _n.name)
        assert m.group(1) == str(_named), (m.group(1), _named)
        # flat_hierarchy has exactly one DATA-* net (DATA-RB7).
        assert m.group(2) == '1' and m.group(3) == '1'
        pcb = parse_kicad_pcb(out)
        name_by_id = {nid: n.name for nid, n in pcb.nets.items()}
        widths = {}
        for s in pcb.segments:
            widths.setdefault(name_by_id.get(s.net_id), set()).add(s.width)
        assert widths.get('DATA-RB7'), 'DATA-RB7 not routed'
        assert widths.get('CLOCK-RB6'), 'CLOCK-RB6 not routed'
        assert max(widths['DATA-RB7']) == 0.5, widths['DATA-RB7']
        assert max(widths['CLOCK-RB6']) == 0.25, widths['CLOCK-RB6']
    finally:
        os.unlink(cpath)
        for p in (out, out.replace('.kicad_pcb', '.kicad_pro')):
            if os.path.exists(p):
                os.unlink(p)


@pytest.mark.integration
def test_contract_defaults_beat_sibling_kicad_pro():
    """In contract mode the sibling .kicad_pro must NEVER supply geometry:
    the contract's defaults play the role the board Default net-class plays
    on the flag path. A .kicad_pro whose Default class carries a poison via
    (0.9/0.45) sits beside the board; the run must take the contract's
    0.45/0.2 and must not print the "not given; using the board Default
    net-class" auto-read line for any contract-carried parameter."""
    import shutil
    workdir = tempfile.mkdtemp(prefix='contract_pro_')
    board = os.path.join(workdir, 'poison.kicad_pcb')
    shutil.copyfile(BOARD, board)
    with open(os.path.join(ROOT, 'kicad_files', 'flat_hierarchy.kicad_pro'),
              encoding='utf-8') as f:
        pro = json.load(f)
    pro.setdefault('net_settings', {})['classes'] = [{
        'name': 'Default', 'clearance': 0.55, 'track_width': 0.9,
        'via_diameter': 0.9, 'via_drill': 0.45,
    }]
    with open(os.path.join(workdir, 'poison.kicad_pro'), 'w',
              encoding='utf-8') as f:
        json.dump(pro, f)
    cpath = _write(CONTRACT)
    out = os.path.join(workdir, 'poison_routed.kicad_pcb')
    try:
        r = _run('route.py', board, out, '--constraints', cpath,
                 '--nets', 'CLOCK-RB6')
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        # The auto-read fallback line must not fire for contract params.
        for flagline in ('--track-width not given', '--via-size not given',
                         '--via-drill not given', '--clearance not given'):
            assert flagline not in r.stdout, flagline
        # And the contract's defaults are what the run reports.
        assert 'defaults tw=0.25 clr=0.2 via=0.45/0.2' in r.stdout, \
            r.stdout[:2000]
        sys.path.insert(0, ROOT)
        from kicad_parser import parse_kicad_pcb
        pcb = parse_kicad_pcb(out)
        sizes = {(v.size, v.drill) for v in pcb.vias}
        assert (0.9, 0.45) not in sizes, sizes
        widths = {s.width for s in pcb.segments}
        assert 0.9 not in widths, widths
    finally:
        os.unlink(cpath)
        shutil.rmtree(workdir, ignore_errors=True)


@pytest.mark.integration
def test_route_planes_contract_clearance_map_covers_all_nets():
    """route_planes.py contract mode: the per-net clearance map covers every
    named net (same LP50 Rev A lesson as route.py — tap tracks and blocker
    reroutes price foreign obstacles from the map, and an absent entry
    forfeits that net's clearance under below-nominal work)."""
    cpath = _write(CONTRACT)
    out = tempfile.mktemp(suffix='.kicad_pcb', prefix='contract_planes_')
    try:
        r = _run('route_planes.py', BOARD, out, '--nets', 'GND',
                 '--plane-layers', 'B.Cu', '--constraints', cpath)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        m = re.search(r'Contract netclass clearances for (\d+) net\(s\)',
                      r.stdout)
        assert m, r.stdout[-2000:]
        sys.path.insert(0, ROOT)
        from kicad_parser import parse_kicad_pcb
        _named = sum(1 for _nid, _n in parse_kicad_pcb(BOARD).nets.items()
                     if _nid != 0 and _n.name)
        assert m.group(1) == str(_named), (m.group(1), _named)
    finally:
        os.unlink(cpath)
        for p in (out, out.replace('.kicad_pcb', '.kicad_pro')):
            if os.path.exists(p):
                os.unlink(p)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
