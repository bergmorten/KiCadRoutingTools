"""Routing-constraints contract (version 1): loader strictness + resolution.

Pytest-style (conftest.py puts the repo root on sys.path). The __main__ block
lets tests/run_all.py execute this file as a script too.
"""
import os as _os
import sys as _sys

# ROOT_BOOTSTRAP: run_all.py executes this file as a bare script, where
# sys.path[0] is tests/ — put the repo root first so top-level modules
# import (conftest.py does this only for pytest runs).
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import copy
import json
import os
import sys
import tempfile

import pytest

from constraints_contract import (Contract, ContractError, conflicting_flag,
                                  load_contract, parse_contract,
                                  validate_against_board)

# The spec example, verbatim from the contract definition.
SPEC_EXAMPLE = {
    "contract": 1,
    "defaults": {"trackWidthMm": 0.2, "clearanceMm": 0.2,
                 "viaSizeMm": 0.3, "viaDrillMm": 0.15},
    "netClasses": [
        {"name": "TX_HV_POWER", "appliesTo": ["+48V", "HV_SW", "HV_TANK"],
         "trackWidthMm": 1.0, "clearanceMm": 0.6, "viaSizeMm": 0.6,
         "viaDrillMm": 0.3, "neck": "forbid"}
    ],
    "netOverrides": {"SOME_NET": {"clearanceMm": 0.3, "neck": "allow"}},
    "fabFloors": {"trackWidthMm": 0.0889, "clearanceMm": 0.0889,
                  "viaDiameterMm": 0.25, "viaDrillMm": 0.15,
                  "boardEdgeMm": 0.2},
    "signalLayers": ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"],
    "planes": [{"net": "GND", "layer": "In1.Cu"}],
}


def _load(doc):
    fd, path = tempfile.mkstemp(suffix='.json', prefix='contract_')
    os.close(fd)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        return load_contract(path)
    finally:
        os.unlink(path)


def _expect_error(doc, *needles):
    with pytest.raises(ContractError) as ei:
        parse_contract(doc)
    msg = str(ei.value)
    for n in needles:
        assert n in msg, f"error message {msg!r} does not name {n!r}"


# ---------------------------------------------------------------- loader (a)

def test_spec_example_loads():
    c = _load(SPEC_EXAMPLE)
    assert isinstance(c, Contract)
    assert c.defaults.track_width_mm == 0.2
    assert c.defaults.via_drill_mm == 0.15
    assert len(c.net_classes) == 1
    assert c.net_classes[0].name == "TX_HV_POWER"
    assert c.net_classes[0].neck == "forbid"
    assert c.net_overrides["SOME_NET"].clearance_mm == 0.3
    assert c.signal_layers == ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]
    assert c.planes == [{"net": "GND", "layer": "In1.Cu"}]
    assert c.clearance_ceiling_mm is None
    # fabFloors translate to the fab_tiers --fab-overrides vocabulary.
    assert c.fab_floor_overrides() == {
        'track_width': 0.0889, 'clearance': 0.0889,
        'via_diameter': 0.25, 'via_drill': 0.15, 'board_edge': 0.2}


def test_missing_contract_version_rejected():
    doc = copy.deepcopy(SPEC_EXAMPLE)
    del doc["contract"]
    _expect_error(doc, "contract")


def test_wrong_contract_version_rejected():
    doc = copy.deepcopy(SPEC_EXAMPLE)
    doc["contract"] = 2
    _expect_error(doc, "contract", "2")
    doc["contract"] = True  # bool is not version 1
    _expect_error(doc, "contract")


def test_unknown_top_level_key_rejected():
    doc = copy.deepcopy(SPEC_EXAMPLE)
    doc["frobnicate"] = 1
    _expect_error(doc, "frobnicate")


def test_unknown_class_key_rejected_with_location():
    doc = copy.deepcopy(SPEC_EXAMPLE)
    doc["netClasses"][0]["widthMm"] = 0.5
    _expect_error(doc, "widthMm", "netClasses[0]")


def test_unknown_override_key_rejected():
    doc = copy.deepcopy(SPEC_EXAMPLE)
    doc["netOverrides"]["SOME_NET"]["clr"] = 0.1
    _expect_error(doc, "clr", 'netOverrides["SOME_NET"]')


def test_unknown_fab_floor_key_rejected():
    doc = copy.deepcopy(SPEC_EXAMPLE)
    doc["fabFloors"]["holeToHoleMm"] = 0.2
    _expect_error(doc, "holeToHoleMm", "fabFloors")


def test_nonpositive_and_nonfinite_numbers_rejected():
    for bad in (0, -0.2, float('nan'), float('inf')):
        doc = copy.deepcopy(SPEC_EXAMPLE)
        doc["defaults"]["clearanceMm"] = bad
        _expect_error(doc, "clearanceMm")


def test_non_numeric_mm_rejected():
    doc = copy.deepcopy(SPEC_EXAMPLE)
    doc["defaults"]["trackWidthMm"] = "0.2"
    _expect_error(doc, "trackWidthMm")
    doc["defaults"]["trackWidthMm"] = True
    _expect_error(doc, "trackWidthMm")


def test_missing_defaults_rejected():
    doc = copy.deepcopy(SPEC_EXAMPLE)
    del doc["defaults"]
    _expect_error(doc, "defaults")
    doc = copy.deepcopy(SPEC_EXAMPLE)
    del doc["defaults"]["viaSizeMm"]
    _expect_error(doc, "viaSizeMm")


def test_empty_applies_to_rejected():
    doc = copy.deepcopy(SPEC_EXAMPLE)
    doc["netClasses"][0]["appliesTo"] = []
    _expect_error(doc, "appliesTo")
    doc["netClasses"][0]["appliesTo"] = "HV_*"  # not an array
    _expect_error(doc, "appliesTo")
    doc["netClasses"][0]["appliesTo"] = ["HV_*", 3]
    _expect_error(doc, "appliesTo")


def test_bad_neck_value_rejected():
    doc = copy.deepcopy(SPEC_EXAMPLE)
    doc["netClasses"][0]["neck"] = "never"
    _expect_error(doc, "neck")


def test_invalid_json_file_rejected():
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    try:
        with open(path, 'w') as f:
            f.write("{not json")
        with pytest.raises(ContractError):
            load_contract(path)
    finally:
        os.unlink(path)
    with pytest.raises(ContractError):
        load_contract(path + ".does-not-exist")


# ------------------------------------------------------------ resolution (b)

def _two_class_contract():
    return parse_contract({
        "contract": 1,
        "defaults": {"trackWidthMm": 0.2, "clearanceMm": 0.2,
                     "viaSizeMm": 0.3, "viaDrillMm": 0.15},
        "netClasses": [
            {"name": "FIRST", "appliesTo": ["NET*"], "clearanceMm": 0.5,
             "viaSizeMm": 0.6, "viaDrillMm": 0.3, "neck": "forbid"},
            {"name": "SECOND", "appliesTo": ["NET1"], "clearanceMm": 0.9,
             "trackWidthMm": 1.0},
        ],
        "netOverrides": {
            "NET1": {"clearanceMm": 0.3},
            "NET2": {"neck": "allow"},
        },
    })


def test_first_matching_class_wins():
    c = _two_class_contract()
    # NET9 matches only FIRST's glob.
    r = c.resolve_net("NET9")
    assert r == {'clearance': 0.5, 'track_width': 0.2,
                 'via_size': 0.6, 'via_drill': 0.3, 'neck': 'forbid'}
    # NET1 matches BOTH; FIRST is first in array order, so SECOND's 0.9 / 1.0
    # never apply -- and the override then beats the class clearance.
    r = c.resolve_net("NET1")
    assert r['clearance'] == 0.3          # netOverrides beats the class
    assert r['track_width'] == 0.2        # SECOND's 1.0 must NOT leak in
    assert r['via_size'] == 0.6
    assert r['neck'] == 'forbid'


def test_override_beats_class_neck():
    c = _two_class_contract()
    assert c.resolve_net("NET2")['neck'] == 'allow'   # class says forbid


def test_no_match_falls_back_to_defaults():
    c = _two_class_contract()
    r = c.resolve_net("GND")
    assert r == {'clearance': 0.2, 'track_width': 0.2,
                 'via_size': 0.3, 'via_drill': 0.15, 'neck': 'allow'}


def test_globs_are_case_sensitive():
    c = _two_class_contract()
    assert c.resolve_net("net1")['clearance'] == 0.2  # 'NET*' must not match


def test_clearance_ceiling_parsed():
    doc = {"contract": 1,
           "defaults": {"trackWidthMm": 0.2, "clearanceMm": 0.2,
                        "viaSizeMm": 0.3, "viaDrillMm": 0.15},
           "clearanceCeilingMm": 0.25}
    assert parse_contract(doc).clearance_ceiling_mm == 0.25


# ---------------------------------------- annular ring (resolved levels)

def test_annular_inversion_in_defaults_rejected():
    doc = copy.deepcopy(SPEC_EXAMPLE)
    doc["defaults"]["viaDrillMm"] = 0.3   # == viaSizeMm 0.3 -> ring 0
    _expect_error(doc, "defaults", "viaDrillMm")
    doc["defaults"]["viaDrillMm"] = 0.4   # > viaSizeMm
    _expect_error(doc, "defaults", "viaDrillMm")


def test_annular_inversion_in_class_rejected_cross_level():
    # The class sets ONLY the drill; the size is inherited from defaults
    # (0.3) -> resolved 0.4 drill / 0.3 size is an inversion.
    doc = copy.deepcopy(SPEC_EXAMPLE)
    del doc["netClasses"][0]["viaSizeMm"]
    doc["netClasses"][0]["viaDrillMm"] = 0.4
    _expect_error(doc, "netClasses[0]", "TX_HV_POWER")


def test_annular_inversion_in_override_rejected_cross_level():
    # The override sets ONLY the drill against the CLASS's inherited size
    # (0.6): 0.7 inverts. The override key names a net the class matches.
    doc = copy.deepcopy(SPEC_EXAMPLE)
    doc["netOverrides"]["HV_TANK"] = {"viaDrillMm": 0.7}
    _expect_error(doc, 'netOverrides["HV_TANK"]')


def test_override_drill_valid_through_class_not_defaults():
    # 0.45 drill would invert against the DEFAULTS size (0.3) but is valid
    # against the class size it actually inherits (0.6): resolution runs
    # through the class chain, so this must LOAD.
    doc = copy.deepcopy(SPEC_EXAMPLE)
    doc["netOverrides"]["HV_TANK"] = {"viaDrillMm": 0.45}
    c = _load(doc)
    r = c.resolve_net("HV_TANK")
    assert (r['via_size'], r['via_drill']) == (0.6, 0.45)
    # And the same drill on an UNCLASSED net inherits the defaults size ->
    # inversion -> rejected.
    doc = copy.deepcopy(SPEC_EXAMPLE)
    doc["netOverrides"]["UNCLASSED"] = {"viaDrillMm": 0.45}
    _expect_error(doc, 'netOverrides["UNCLASSED"]')


# ------------------------------------------- board-presence validation

def test_unresolvable_override_key_is_hard_error():
    c = _load(SPEC_EXAMPLE)   # has netOverrides["SOME_NET"]
    with pytest.raises(ContractError) as ei:
        validate_against_board(c, ["+48V", "HV_TANK", "GND"])
    assert "SOME_NET" in str(ei.value)


def test_class_matching_zero_nets_is_returned_not_fatal():
    c = _load(SPEC_EXAMPLE)
    # SOME_NET present -> overrides resolve; no TX_HV_POWER net on board ->
    # the class name comes back for the caller's loud warning.
    dead = validate_against_board(c, ["SOME_NET", "GND", "SIG1"])
    assert dead == ["TX_HV_POWER"]
    # Everything matched -> nothing to warn about.
    assert validate_against_board(c, ["SOME_NET", "HV_TANK"]) == []


# ------------------------------------------------- flag conflict helper (c)

def test_conflicting_flag_detection():
    assert conflicting_flag(['in.kicad_pcb', '--constraints', 'c.json',
                             '--track-width', '0.3']) == '--track-width'
    assert conflicting_flag(['--fab-tier=advanced']) == '--fab-tier'
    assert conflicting_flag(['--constraints', 'c.json', '--grid-step',
                             '0.05']) is None
    # substrings must not false-positive
    assert conflicting_flag(['--clearance-x', '1']) is None


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
