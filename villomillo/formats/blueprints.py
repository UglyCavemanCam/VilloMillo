"""Format blocks and templates: load the blocks, check a template's layers, and turn a template into a plan for one clip.

A block is one small building block (a framing, a text layer, a motion, a brand mark). A template stacks them: one
framing block plus any others. The resolver drops blocks a clip cannot support, applies the channel's filters, and
refuses a plan that adds nothing beyond a crop and captions.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml

from ..config import CONFIG_DIR

PATH = CONFIG_DIR / "blueprints.yaml"
LAYERS = ("framing", "text", "motion", "brand")


class BlueprintError(ValueError):
    """The block file or a template is wrong. Raised when it is loaded or saved, never half way through a render."""


class OriginalityError(BlueprintError):
    """A plan whose only additions are a crop and captions."""


@dataclass(frozen=True)
class Blueprint:
    id: str
    layer: str
    needs: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    added_value: bool = False
    params: dict = field(default_factory=dict)


@dataclass
class Plan:
    name: str
    blueprints: list[Blueprint]
    dropped: list[tuple[str, str]]
    overrides: dict = field(default_factory=dict)

    def ids(self) -> list[str]:
        return [b.id for b in self.blueprints]

    def has(self, blueprint_id: str) -> bool:
        return blueprint_id in self.ids()

    def param(self, blueprint_id: str, key: str, default=None):
        for b in self.blueprints:
            if b.id == blueprint_id:
                merged = {**b.params, **self.overrides.get(blueprint_id, {})}
                return merged.get(key, default)
        return default

    def framing(self) -> Blueprint:
        return next(b for b in self.blueprints if b.layer == "framing")

    def framing_param(self, key: str, default=None):
        return self.param(self.framing().id, key, default)


def load(path: pathlib.Path | None = None) -> dict[str, Blueprint]:
    """Read and validate the block file. Returns the blocks by id."""
    data = yaml.safe_load((path or PATH).read_text(encoding="utf-8")) or {}
    library: dict[str, Blueprint] = {}
    for bid, spec in (data.get("blueprints") or {}).items():
        if spec.get("layer") not in LAYERS:
            raise BlueprintError(f"block {bid!r} has an unknown layer {spec.get('layer')!r}")
        library[bid] = Blueprint(bid, spec["layer"], tuple(spec.get("needs") or ()), tuple(spec.get("conflicts") or ()),
                                 bool(spec.get("added_value")), dict(spec.get("params") or {}))
    for bp in library.values():
        for other in bp.conflicts:
            if other not in library:
                raise BlueprintError(f"block {bp.id!r} conflicts with unknown {other!r}")
    return library


def _as_list(value) -> list:
    return list(value) if isinstance(value, list) else []


def _ids(layers: dict) -> list[str]:
    """Every block a template names, framing first."""
    return [layers["framing"], *_as_list(layers.get("text")), *_as_list(layers.get("motion")), *_as_list(layers.get("brand"))]


def layers_problem(layers, library: dict[str, Blueprint]) -> str | None:
    """Why a template's layers cannot be used, in plain words, or None when they can."""
    if not isinstance(layers, dict):
        return "the template has no layers"
    framing = layers.get("framing")
    if not isinstance(framing, str):
        return "a template needs exactly one framing"
    for key in ("text", "motion", "brand"):
        if layers.get(key) is not None and not isinstance(layers[key], list):
            return f"{key} must be a list of blocks"
    ids = _ids(layers)
    for bid in ids:
        if bid not in library:
            return f"there is no block called {bid}"
    for layer in LAYERS:
        wanted = [framing] if layer == "framing" else _as_list(layers.get(layer))
        for bid in wanted:
            if library[bid].layer != layer:
                return f"{bid} is a {library[bid].layer} block, not {layer}"
    for bid in ids:
        clash = set(library[bid].conflicts) & set(ids)
        if clash:
            return f"{bid} cannot be used with {sorted(clash)[0]}"
    return None


def resolve(name: str, layers: dict, available: set[str], *, forbid: set[str] | frozenset[str] = frozenset(),
            overrides: dict | None = None, path: pathlib.Path | None = None) -> Plan:
    """The plan for one clip. `available` names what the clip supplies: words, title, points, takeaway."""
    library = load(path)
    problem = layers_problem(layers, library)
    if problem:
        raise BlueprintError(f"template {name!r}: {problem}")
    kept, dropped = [], []
    for bid in _ids(layers):
        bp = library[bid]
        missing = [n for n in bp.needs if n not in available]
        if bid in forbid:
            dropped.append((bid, "not allowed for this channel"))
        elif missing:
            dropped.append((bid, f"the clip has no {missing[0]}"))
        else:
            kept.append(bp)
    if not any(b.layer == "framing" for b in kept):
        raise BlueprintError(f"template {name!r} lost its framing ({dropped[0][1] if dropped else 'unknown'})")
    if not any(b.added_value for b in kept):
        raise OriginalityError(
            f"template {name!r} would add nothing but a crop and captions "
            f"(dropped: {', '.join(f'{b}: {why}' for b, why in dropped) or 'nothing'})")
    return Plan(name, kept, dropped, overrides or {})
