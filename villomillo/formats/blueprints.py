"""Blueprints and recipes: load them, check them, and turn a recipe into a plan for one clip.

A blueprint is one small building block (a framing, a text layer, a motion, a brand mark). A recipe stacks them for
one kind of video. The resolver drops blueprints a clip cannot support, applies the channel's filters, and refuses a
plan that adds nothing beyond a crop and captions.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml

from ..config import CONFIG_DIR

PATH = CONFIG_DIR / "blueprints.yaml"
LAYERS = ("framing", "text", "motion", "brand")


class BlueprintError(ValueError):
    """The blueprint file or a recipe is wrong. Raised when it is loaded, never half way through a render."""


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
    recipe: str
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


def load(path: pathlib.Path | None = None) -> tuple[dict[str, Blueprint], dict[str, dict]]:
    """Read and validate the blueprint file. Returns (blueprints by id, recipes by id)."""
    data = yaml.safe_load((path or PATH).read_text(encoding="utf-8")) or {}
    library: dict[str, Blueprint] = {}
    for bid, spec in (data.get("blueprints") or {}).items():
        if spec.get("layer") not in LAYERS:
            raise BlueprintError(f"blueprint {bid!r} has an unknown layer {spec.get('layer')!r}")
        library[bid] = Blueprint(bid, spec["layer"], tuple(spec.get("needs") or ()), tuple(spec.get("conflicts") or ()),
                                 bool(spec.get("added_value")), dict(spec.get("params") or {}))
    for bp in library.values():
        for other in bp.conflicts:
            if other not in library:
                raise BlueprintError(f"blueprint {bp.id!r} conflicts with unknown {other!r}")
    recipes = data.get("recipes") or {}
    for name, recipe in recipes.items():
        chosen = _recipe_ids(name, recipe, library)
        for bid in chosen:
            clash = set(library[bid].conflicts) & set(chosen)
            if clash:
                raise BlueprintError(f"recipe {name!r}: {bid!r} conflicts with {sorted(clash)[0]!r}")
    return library, recipes


def _recipe_ids(name: str, recipe: dict, library: dict[str, Blueprint]) -> list[str]:
    framing = recipe.get("framing")
    if not isinstance(framing, str):
        raise BlueprintError(f"recipe {name!r} needs exactly one framing blueprint")
    chosen = [framing]
    for layer in LAYERS[1:]:
        chosen += list(recipe.get(layer) or [])
    for bid in chosen:
        if bid not in library:
            raise BlueprintError(f"recipe {name!r} uses unknown blueprint {bid!r}")
    for layer in LAYERS:
        wanted = [framing] if layer == "framing" else list(recipe.get(layer) or [])
        for bid in wanted:
            if library[bid].layer != layer:
                raise BlueprintError(f"recipe {name!r}: {bid!r} is a {library[bid].layer} blueprint, not {layer}")
    return chosen


def resolve(recipe: str, available: set[str], *, forbid: set[str] | frozenset[str] = frozenset(),
            overrides: dict | None = None, path: pathlib.Path | None = None) -> Plan:
    """The plan for one clip. `available` names what the clip supplies: words, title, points, takeaway."""
    library, recipes = load(path)
    if recipe not in recipes:
        raise BlueprintError(f"no recipe called {recipe!r}")
    kept, dropped = [], []
    for bid in _recipe_ids(recipe, recipes[recipe], library):
        bp = library[bid]
        missing = [n for n in bp.needs if n not in available]
        if bid in forbid:
            dropped.append((bid, "not allowed for this channel"))
        elif missing:
            dropped.append((bid, f"the clip has no {missing[0]}"))
        else:
            kept.append(bp)
    if not any(b.layer == "framing" for b in kept):
        raise BlueprintError(f"recipe {recipe!r} lost its framing ({dropped[0][1] if dropped else 'unknown'})")
    if not any(b.added_value for b in kept):
        raise OriginalityError(
            f"recipe {recipe!r} would add nothing but a crop and captions "
            f"(dropped: {', '.join(f'{b}: {why}' for b, why in dropped) or 'nothing'})")
    return Plan(recipe, kept, dropped, overrides or {})
