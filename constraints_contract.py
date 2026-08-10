"""Routing-constraints contract, version 1 (shared with kad).

A single JSON document is the only constraint channel for route.py /
route_planes.py when ``--constraints`` is given. Both sides (kad emits, this
module consumes) apply IDENTICAL resolution rules:

- Net -> class: the FIRST class in ``netClasses`` array order whose
  ``appliesTo`` glob matches the net name (fnmatch-style ``*`` and ``?``,
  case-sensitive); no match -> ``defaults``.
- Per-net clearance / track width / via geometry: the class value when the
  class sets it, else ``defaults``. A ``netOverrides[net]`` value wins over
  the class (case-sensitive exact net name).
- ``neck``: "forbid" puts the net in the no-neck set (#541); a per-net
  override wins. Default "allow".
- ``clearanceCeilingMm`` (optional top-level) reproduces route.py's
  --clearance CEILING semantics (#439): PRESENT -> every class is capped at
  min(class, ceiling); ABSENT -> classes are honored in full.
- ``pairClearances`` (optional top-level, v1.1): symmetric per-NET-PAIR
  clearance rules. For a pair where one net fnmatch-matches any ``a`` pattern
  and the other any ``b`` pattern (also with a/b swapped), the required
  clearance is ``clearanceMm`` INSTEAD OF max(classA, classB). The FIRST rule
  in array order that matches a pair wins. Intended use is RELAXATION (e.g.
  half-bridge nets sharing a creepage class but sitting one diode drop apart);
  a value above the class max is NOT rejected (KiCad's .kicad_dru allows
  either direction), but validation does require a positive finite mm and
  non-empty pattern arrays.
- Fab-floor step-down ladders (via-in-pad, BGA escape) may still shrink a
  via BELOW its class size when geometry forces it -- never above.

Validation is STRICT: ``contract`` must be exactly 1, ANY unknown key at any
level is a hard error naming the key and its location, every *Mm value must
be a positive finite number, ``appliesTo`` must be a non-empty array of
strings. Fail loud, never default-and-continue. Via geometry must resolve to
a positive annular ring at EVERY resolved level: ``viaDrillMm < viaSizeMm``
for the defaults, for each class after inheriting the defaults, and for each
netOverrides entry after inheriting its class/defaults -- a cross-level
inversion (a class overriding one field against an inherited other) is a
hard error at load, never a silently skipped via downstream.

``signalLayers`` and ``planes`` are v1 FORWARD-COMPAT fields: they are
accepted and strictly validated (shape, non-empty strings) but intentionally
consumed by nothing -- the CLIs' own ``--layers`` / ``--nets`` /
``--plane-layers`` argv stays authoritative for layer and plane selection.
They exist so a kad-emitted document can carry the full board intent without
version-gating; consuming them is a future contract bump, not a v1 behavior.

Board-presence validation (``validate_against_board``): ``netOverrides``
keys are EXACT net names -- the typo channel -- so a key matching no board
net is a hard ContractError; a class whose ``appliesTo`` matches zero board
nets is returned for the caller to warn about loudly (a class may
legitimately target nets a board variant does not carry).

Pure stdlib by design (mirrors fab_tiers.py) so lightweight CLIs can import
it without the PCB parser.
"""
from __future__ import annotations

import fnmatch
import json
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

CONTRACT_VERSION = 1

# route.py flags that --constraints replaces outright; giving any of them
# alongside --constraints is a parser error naming the flag. route_planes.py
# uses the subset it actually defines.
CONSTRAINT_EXCLUSIVE_FLAGS = (
    '--track-width', '--clearance', '--via-size', '--via-drill',
    '--net-clearances', '--power-nets', '--power-nets-widths',
    '--no-neck-nets', '--fab-tier', '--fab-overrides',
)

# contract key -> fab_tiers.FLOOR_KEYS key (the --fab-overrides vocabulary).
_FAB_FLOOR_KEY_MAP = {
    'trackWidthMm': 'track_width',
    'clearanceMm': 'clearance',
    'viaDiameterMm': 'via_diameter',
    'viaDrillMm': 'via_drill',
    'boardEdgeMm': 'board_edge',
}

_TOP_KEYS = ('contract', 'defaults', 'netClasses', 'netOverrides', 'fabFloors',
             'signalLayers', 'planes', 'clearanceCeilingMm', 'pairClearances')
_DEFAULTS_KEYS = ('trackWidthMm', 'clearanceMm', 'viaSizeMm', 'viaDrillMm')
_CLASS_KEYS = ('name', 'appliesTo', 'trackWidthMm', 'clearanceMm',
               'viaSizeMm', 'viaDrillMm', 'neck')
_OVERRIDE_KEYS = ('trackWidthMm', 'clearanceMm', 'viaSizeMm', 'viaDrillMm', 'neck')
_PLANE_KEYS = ('net', 'layer')
_PAIR_KEYS = ('a', 'b', 'clearanceMm')
_NECK_VALUES = ('allow', 'forbid')


class ContractError(ValueError):
    """A constraints document failed strict validation."""


def _err(where: str, msg: str) -> 'ContractError':
    return ContractError(f"{where}: {msg}")


def _require_dict(obj, where: str) -> dict:
    if not isinstance(obj, dict):
        raise _err(where, f"must be an object, got {type(obj).__name__}")
    return obj


def _reject_unknown(obj: dict, allowed, where: str) -> None:
    for k in obj:
        if k not in allowed:
            raise _err(where, f'unknown key "{k}" (allowed: {", ".join(allowed)})')


def _mm(obj: dict, key: str, where: str, required: bool = False) -> Optional[float]:
    if key not in obj:
        if required:
            raise _err(where, f'missing required key "{key}"')
        return None
    v = obj[key]
    # bool is an int subclass; a JSON true/false is never a length.
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _err(where, f'"{key}" must be a number, got {v!r}')
    v = float(v)
    if not math.isfinite(v) or v <= 0.0:
        raise _err(where, f'"{key}" must be a positive finite number, got {v!r}')
    return v


def _check_annular(size: float, drill: float, where: str) -> None:
    """Positive annular ring: drill strictly below the via diameter."""
    if drill >= size:
        raise _err(where, f'resolved viaDrillMm {drill!r} must be smaller than '
                          f'resolved viaSizeMm {size!r} (annular ring <= 0)')


def _neck(obj: dict, where: str) -> Optional[str]:
    if 'neck' not in obj:
        return None
    v = obj['neck']
    if v not in _NECK_VALUES:
        raise _err(where, f'"neck" must be one of {_NECK_VALUES}, got {v!r}')
    return v


def _str_list(v, where: str, key: str, allow_empty: bool = True) -> List[str]:
    if not isinstance(v, list):
        raise _err(where, f'"{key}" must be an array of strings, got {type(v).__name__}')
    if not allow_empty and not v:
        raise _err(where, f'"{key}" must be a non-empty array of strings')
    for i, s in enumerate(v):
        if not isinstance(s, str) or not s:
            raise _err(where, f'"{key}"[{i}] must be a non-empty string, got {s!r}')
    return list(v)


@dataclass(frozen=True)
class Defaults:
    track_width_mm: float
    clearance_mm: float
    via_size_mm: float
    via_drill_mm: float


@dataclass(frozen=True)
class NetClass:
    name: str
    applies_to: Tuple[str, ...]
    track_width_mm: Optional[float] = None
    clearance_mm: Optional[float] = None
    via_size_mm: Optional[float] = None
    via_drill_mm: Optional[float] = None
    neck: Optional[str] = None

    def matches(self, net_name: str) -> bool:
        # fnmatchcase: appliesTo globs are case-sensitive exact strings
        # apart from * and ? (spec) -- fnmatch.fnmatch would normcase on
        # some platforms.
        return any(fnmatch.fnmatchcase(net_name, g) for g in self.applies_to)


@dataclass(frozen=True)
class PairRule:
    """One ``pairClearances`` entry: a symmetric per-pair clearance override.

    Matches a NET PAIR when one side fnmatch-matches any ``a`` pattern and the
    other any ``b`` pattern -- in either assignment (symmetric). The matched
    pair's required clearance is ``clearance_mm`` INSTEAD OF the class
    max(classA, classB)."""
    a: Tuple[str, ...]
    b: Tuple[str, ...]
    clearance_mm: float

    @staticmethod
    def _any(net_name: str, globs: Tuple[str, ...]) -> bool:
        return any(fnmatch.fnmatchcase(net_name, g) for g in globs)

    def matches(self, net_a: str, net_b: str) -> bool:
        return ((self._any(net_a, self.a) and self._any(net_b, self.b))
                or (self._any(net_a, self.b) and self._any(net_b, self.a)))


@dataclass(frozen=True)
class NetOverride:
    track_width_mm: Optional[float] = None
    clearance_mm: Optional[float] = None
    via_size_mm: Optional[float] = None
    via_drill_mm: Optional[float] = None
    neck: Optional[str] = None


@dataclass
class Contract:
    defaults: Defaults
    net_classes: List[NetClass] = field(default_factory=list)
    net_overrides: Dict[str, NetOverride] = field(default_factory=dict)
    fab_floors: Dict[str, float] = field(default_factory=dict)  # contract-key -> mm
    signal_layers: List[str] = field(default_factory=list)
    planes: List[Dict[str, str]] = field(default_factory=list)
    clearance_ceiling_mm: Optional[float] = None
    pair_clearances: List[PairRule] = field(default_factory=list)

    def class_for(self, net_name: str) -> Optional[NetClass]:
        """FIRST class in array order whose appliesTo matches, else None."""
        for nc in self.net_classes:
            if nc.matches(net_name):
                return nc
        return None

    def resolve_net(self, net_name: str) -> Dict[str, object]:
        """{'clearance','track_width','via_size','via_drill','neck'} for one
        net, per the shared resolution rules (class order + netOverrides).
        The clearanceCeilingMm cap is applied by the CALLER (route.py's #439
        switch), not here -- this returns the honored-in-full values."""
        d = self.defaults
        nc = self.class_for(net_name)
        clearance = d.clearance_mm
        track_width = d.track_width_mm
        via_size, via_drill = d.via_size_mm, d.via_drill_mm
        neck = 'allow'
        if nc is not None:
            if nc.clearance_mm is not None:
                clearance = nc.clearance_mm
            if nc.track_width_mm is not None:
                track_width = nc.track_width_mm
            if nc.via_size_mm is not None:
                via_size = nc.via_size_mm
            if nc.via_drill_mm is not None:
                via_drill = nc.via_drill_mm
            if nc.neck is not None:
                neck = nc.neck
        ov = self.net_overrides.get(net_name)
        if ov is not None:
            if ov.clearance_mm is not None:
                clearance = ov.clearance_mm
            if ov.track_width_mm is not None:
                track_width = ov.track_width_mm
            if ov.via_size_mm is not None:
                via_size = ov.via_size_mm
            if ov.via_drill_mm is not None:
                via_drill = ov.via_drill_mm
            if ov.neck is not None:
                neck = ov.neck
        return {'clearance': clearance, 'track_width': track_width,
                'via_size': via_size, 'via_drill': via_drill, 'neck': neck}

    def resolve_pair(self, net_a: str, net_b: str) -> Optional[float]:
        """Pair clearance (mm) for (net_a, net_b): the FIRST rule in
        ``pairClearances`` array order that matches the pair (symmetric --
        a<->b swapped also matches), else None (-> the caller's normal
        max(classA, classB) rule applies)."""
        for r in self.pair_clearances:
            if r.matches(net_a, net_b):
                return r.clearance_mm
        return None

    def pair_rules_to_ids(self, net_names_by_id) -> List[Tuple[frozenset, frozenset, float]]:
        """Resolve the pattern rules to board net IDs, preserving array order:
        [(a_ids, b_ids, mm), ...]. First-match-wins over these triples
        reproduces resolve_pair() exactly (a pair matches triple k iff one id
        is in a_ids and the other in b_ids, either way around). Consumed by
        the router configs, which work in net ids, not names."""
        out: List[Tuple[frozenset, frozenset, float]] = []
        for r in self.pair_clearances:
            a_ids = frozenset(nid for nid, name in net_names_by_id.items()
                              if name and PairRule._any(name, r.a))
            b_ids = frozenset(nid for nid, name in net_names_by_id.items()
                              if name and PairRule._any(name, r.b))
            out.append((a_ids, b_ids, r.clearance_mm))
        return out

    def fab_floor_overrides(self) -> Dict[str, float]:
        """fabFloors translated to fab_tiers' --fab-overrides vocabulary
        (track_width / clearance / via_diameter / via_drill / board_edge),
        for set_default_fab_tier(). Empty when the contract sets none."""
        return {_FAB_FLOOR_KEY_MAP[k]: v for k, v in self.fab_floors.items()}


def parse_contract(doc: dict, where: str = 'contract document') -> Contract:
    """Validate a decoded JSON document and build the Contract. STRICT."""
    _require_dict(doc, where)
    _reject_unknown(doc, _TOP_KEYS, where)

    if 'contract' not in doc:
        raise _err(where, 'missing required key "contract" (must be 1)')
    ver = doc['contract']
    if isinstance(ver, bool) or ver != CONTRACT_VERSION:
        raise _err(where, f'"contract" must be {CONTRACT_VERSION}, got {ver!r}')

    dwhere = f'{where}: defaults'
    if 'defaults' not in doc:
        raise _err(where, 'missing required key "defaults"')
    ddoc = _require_dict(doc['defaults'], dwhere)
    _reject_unknown(ddoc, _DEFAULTS_KEYS, dwhere)
    defaults = Defaults(
        track_width_mm=_mm(ddoc, 'trackWidthMm', dwhere, required=True),
        clearance_mm=_mm(ddoc, 'clearanceMm', dwhere, required=True),
        via_size_mm=_mm(ddoc, 'viaSizeMm', dwhere, required=True),
        via_drill_mm=_mm(ddoc, 'viaDrillMm', dwhere, required=True))

    net_classes: List[NetClass] = []
    if 'netClasses' in doc:
        arr = doc['netClasses']
        if not isinstance(arr, list):
            raise _err(where, f'"netClasses" must be an array, got {type(arr).__name__}')
        seen_names = set()
        for i, c in enumerate(arr):
            cw = f'{where}: netClasses[{i}]'
            cd = _require_dict(c, cw)
            _reject_unknown(cd, _CLASS_KEYS, cw)
            name = cd.get('name')
            if not isinstance(name, str) or not name:
                raise _err(cw, f'"name" must be a non-empty string, got {name!r}')
            cw = f'{where}: netClasses[{i}] ({name})'
            if name in seen_names:
                raise _err(cw, 'duplicate class name')
            seen_names.add(name)
            if 'appliesTo' not in cd:
                raise _err(cw, 'missing required key "appliesTo"')
            applies = _str_list(cd['appliesTo'], cw, 'appliesTo', allow_empty=False)
            net_classes.append(NetClass(
                name=name, applies_to=tuple(applies),
                track_width_mm=_mm(cd, 'trackWidthMm', cw),
                clearance_mm=_mm(cd, 'clearanceMm', cw),
                via_size_mm=_mm(cd, 'viaSizeMm', cw),
                via_drill_mm=_mm(cd, 'viaDrillMm', cw),
                neck=_neck(cd, cw)))

    net_overrides: Dict[str, NetOverride] = {}
    if 'netOverrides' in doc:
        od = _require_dict(doc['netOverrides'], f'{where}: netOverrides')
        for net, ov in od.items():
            ow = f'{where}: netOverrides["{net}"]'
            ovd = _require_dict(ov, ow)
            _reject_unknown(ovd, _OVERRIDE_KEYS, ow)
            if not ovd:
                raise _err(ow, 'empty override object')
            net_overrides[net] = NetOverride(
                track_width_mm=_mm(ovd, 'trackWidthMm', ow),
                clearance_mm=_mm(ovd, 'clearanceMm', ow),
                via_size_mm=_mm(ovd, 'viaSizeMm', ow),
                via_drill_mm=_mm(ovd, 'viaDrillMm', ow),
                neck=_neck(ovd, ow))

    fab_floors: Dict[str, float] = {}
    if 'fabFloors' in doc:
        fw = f'{where}: fabFloors'
        fd = _require_dict(doc['fabFloors'], fw)
        _reject_unknown(fd, tuple(_FAB_FLOOR_KEY_MAP), fw)
        for k in fd:
            fab_floors[k] = _mm(fd, k, fw, required=True)

    signal_layers: List[str] = []
    if 'signalLayers' in doc:
        signal_layers = _str_list(doc['signalLayers'], where, 'signalLayers',
                                  allow_empty=False)

    planes: List[Dict[str, str]] = []
    if 'planes' in doc:
        arr = doc['planes']
        if not isinstance(arr, list):
            raise _err(where, f'"planes" must be an array, got {type(arr).__name__}')
        for i, p in enumerate(arr):
            pw = f'{where}: planes[{i}]'
            pd = _require_dict(p, pw)
            _reject_unknown(pd, _PLANE_KEYS, pw)
            for k in _PLANE_KEYS:
                if k not in pd:
                    raise _err(pw, f'missing required key "{k}"')
                if not isinstance(pd[k], str) or not pd[k]:
                    raise _err(pw, f'"{k}" must be a non-empty string, got {pd[k]!r}')
            planes.append({'net': pd['net'], 'layer': pd['layer']})

    ceiling = _mm(doc, 'clearanceCeilingMm', where)

    pair_clearances: List[PairRule] = []
    if 'pairClearances' in doc:
        arr = doc['pairClearances']
        if not isinstance(arr, list):
            raise _err(where, f'"pairClearances" must be an array, got {type(arr).__name__}')
        for i, p in enumerate(arr):
            pw = f'{where}: pairClearances[{i}]'
            pd = _require_dict(p, pw)
            _reject_unknown(pd, _PAIR_KEYS, pw)
            for k in ('a', 'b'):
                if k not in pd:
                    raise _err(pw, f'missing required key "{k}"')
            pair_clearances.append(PairRule(
                a=tuple(_str_list(pd['a'], pw, 'a', allow_empty=False)),
                b=tuple(_str_list(pd['b'], pw, 'b', allow_empty=False)),
                clearance_mm=_mm(pd, 'clearanceMm', pw, required=True)))

    contract = Contract(defaults=defaults, net_classes=net_classes,
                        net_overrides=net_overrides, fab_floors=fab_floors,
                        signal_layers=signal_layers, planes=planes,
                        clearance_ceiling_mm=ceiling,
                        pair_clearances=pair_clearances)

    # Annular-ring validation at every RESOLVED level: the defaults pair,
    # each class AFTER inheriting the defaults (catching a class that
    # overrides one field against an inherited other), and each netOverrides
    # entry AFTER inheriting through its (exact-name) class and the defaults.
    _check_annular(defaults.via_size_mm, defaults.via_drill_mm,
                   f'{where}: defaults')
    for i, nc in enumerate(net_classes):
        _eff_size = nc.via_size_mm if nc.via_size_mm is not None else defaults.via_size_mm
        _eff_drill = nc.via_drill_mm if nc.via_drill_mm is not None else defaults.via_drill_mm
        _check_annular(_eff_size, _eff_drill,
                       f'{where}: netClasses[{i}] ({nc.name})')
    for net in net_overrides:
        # netOverrides keys are exact net names, so their class is knowable
        # at parse time: resolve_net applies exactly the runtime chain.
        _r = contract.resolve_net(net)
        _check_annular(_r['via_size'], _r['via_drill'],
                       f'{where}: netOverrides["{net}"]')

    return contract


def load_contract(path: str) -> Contract:
    """Read + strictly validate a constraints JSON file."""
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
    except OSError as e:
        raise ContractError(f"cannot read constraints file {path}: {e}")
    except ValueError as e:
        raise ContractError(f"{path} is not valid JSON: {e}")
    return parse_contract(doc, where=path)


def validate_against_board(contract: Contract, net_names,
                           where: str = 'constraints contract') -> List[str]:
    """Board-presence validation (call once per run, after the board parse).

    - A ``netOverrides`` key matching no board net raises ContractError:
      override keys are EXACT net names, so a non-match is the typo channel
      and must fail loud, never silently no-op.
    - Returns the names of classes whose ``appliesTo`` globs matched ZERO
      board nets, for the caller to warn about loudly (stderr). Not fatal: a
      class may legitimately target nets a board variant does not carry.
    """
    names = set(net_names)
    missing = sorted(k for k in contract.net_overrides if k not in names)
    if missing:
        raise ContractError(
            f"{where}: netOverrides name(s) matching no board net (override "
            f"keys are exact net names -- fix the typo or drop the entry): "
            f"{', '.join(missing)}")
    return [nc.name for nc in contract.net_classes
            if not any(nc.matches(n) for n in names)]


def conflicting_flag(argv, flags=CONSTRAINT_EXCLUSIVE_FLAGS) -> Optional[str]:
    """First flag in `flags` present in argv (as '--flag' or '--flag=v'),
    else None. Used for the --constraints mutual-exclusion parser error --
    these flags have no distinguishable 'given' state after argparse fills
    defaults (e.g. --fab-tier defaults to 'standard'), so presence is
    detected on the raw argv."""
    toks = list(argv)
    for flag in flags:
        for t in toks:
            if t == flag or t.startswith(flag + '='):
                return flag
    return None
