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
        # flat_hierarchy has exactly one DATA-* net (DATA-RB7).
        assert m.group(1) == '1' and m.group(2) == '1' and m.group(3) == '1'

        sys.path.insert(0, ROOT)
        from kicad_parser import parse_kicad_pcb
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


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
