#!/usr/bin/env python3
"""#1287: ``--escalation board`` without the project writeback floors every
rule the project leaves unset at what KiCad grades it at.

``board_floors_from_rules`` reads an unset ``rules.min_*`` key as "no board
floor", so a descent for that key fell through to the fab tier. With the
writeback the output project's floor is lowered to what was routed and KiCad
grades the board clean. Without it (``--no-fix-drc-settings``) the input
project ships unchanged and KiCad grades the key at pcbnew's factory value:
a rescue laid 0.0889 mm track on a project with no ``min_track_width`` and
kicad-cli reported 43 ``track_width`` violations against 0.2 mm.

Checks:
  1. writeback off: every unset rule is floored at KICAD_FACTORY_FLOORS.
  2. writeback off: a declared rule is kept as declared.
  3. writeback off: a request already below the factory value is the floor
     (no descent below it), like #1262's clearance rule.
  4. writeback on: unset rules stay unset (the writeback lowers the project).
  5. non-board policies pass the floors through.
  6. KICAD_FACTORY_FLOORS matches pcbnew's constants when pcbnew imports.

    python3 tests/test_1287_board_unset_floors_kicad_default.py
"""
import contextlib
import io
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from fab_tiers import (KICAD_FACTORY_FLOORS, board_floors_from_rules,  # noqa: E402
                       board_policy_floors)

failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def floors(policy, rules, requested, writeback):
    declared = board_floors_from_rules(rules, 0.2)
    with contextlib.redirect_stdout(io.StringIO()):
        return board_policy_floors(policy, declared, requested, 0.2,
                                   writeback=writeback)


def main():
    # The Wavelet Rev B project: vias declared, no track width, no edge.
    rules = {'min_via_diameter': 0.45, 'min_via_annular_width': 0.125,
             'min_through_hole_diameter': 0.2, 'min_hole_to_hole': 0.25,
             'min_clearance': 0.2}
    request = {'track_width': 0.2, 'clearance': 0.2, 'via_size': 0.45,
               'via_drill': 0.2}

    off = floors('board', rules, request, writeback=False)
    check('1. writeback off: unset track width floored at KiCad 0.2',
          off.get('track_width') == 0.2, str(off))
    check('1. writeback off: unset copper-to-edge floored at KiCad 0.5',
          off.get('board_edge') == 0.5, str(off))
    check('2. declared via size / drill / annular / hole-to-hole kept',
          (off.get('via_diameter'), off.get('via_drill'), off.get('annular'),
           off.get('hole_to_hole')) == (0.45, 0.2, 0.125, 0.25), str(off))

    bare = floors('board', {}, {'track_width': 0.15, 'via_size': 0.45},
                  writeback=False)
    check('3. a request below the factory value is the floor',
          bare.get('track_width') == 0.15 and bare.get('via_diameter') == 0.45,
          str(bare))
    check('3. a key with no request is floored at the factory value',
          bare.get('via_drill') == 0.3 and bare.get('annular') == 0.1,
          str(bare))

    on = floors('board', rules, request, writeback=True)
    check('4. writeback on: unset rules stay unset',
          'track_width' not in on and 'board_edge' not in on, str(on))

    fab = floors('fab', rules, request, writeback=False)
    check('5. fab policy passes the floors through',
          'track_width' not in fab, str(fab))

    try:
        from fix_kicad_drc_settings import pcbnew_rule_defaults
        factory = pcbnew_rule_defaults()
    except Exception:                                           # noqa: BLE001
        factory = {}
    if not factory:
        print('  [skip] 6. pcbnew not importable; table not cross-checked')
    else:
        rule_of = {'track_width': 'min_track_width',
                   'via_diameter': 'min_via_diameter',
                   'via_drill': 'min_through_hole_diameter',
                   'hole_to_hole': 'min_hole_to_hole',
                   'annular': 'min_via_annular_width',
                   'board_edge': 'min_copper_edge_clearance'}
        got = {k: factory.get(rule_of[k]) for k in KICAD_FACTORY_FLOORS}
        check('6. KICAD_FACTORY_FLOORS matches pcbnew',
              all(abs((got[k] or -1) - v) < 1e-9
                  for k, v in KICAD_FACTORY_FLOORS.items()),
              f'{got} vs {KICAD_FACTORY_FLOORS}')

    print('FAILED: ' + ', '.join(failures) if failures else 'PASS')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
