"""Deterministic category/keyword -> mapped_group rules (07 §5 task 7), shared with the crew's calc mapper.

The engine never maps with an LLM; unmapped lines become ``other`` and raise ``UNMAPPED_LINE``."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import MappedGroup

CATEGORY_DEFAULT: dict[str, MappedGroup] = {
    "room": MappedGroup.room_rent,
    "icu": MappedGroup.icu,
    "surgery": MappedGroup.surgeon_fees,
    "anaesthesia": MappedGroup.anaesthesia,
    "medicine": MappedGroup.medicine,
    "consumable": MappedGroup.consumable,
    "implant": MappedGroup.implant,
    "investigation": MappedGroup.investigation,
    "consultation": MappedGroup.doctor_fees,
}

# (regex on description, group, tags, is_non_medical, is_implant) — first match wins, evaluated before the category default
KEYWORD_RULES: list[tuple[str, MappedGroup, tuple[str, ...], bool, bool]] = [
    (r"\bambulance\b", MappedGroup.ambulance, (), False, False),
    (r"\b(ot|operation theat(re|er))\b.*\b(charge|fee)s?\b|\bot charges?\b", MappedGroup.ot_charges, (), False, False),
    (r"\bsurgeon\b|\bsurgical (fee|charge)s?\b", MappedGroup.surgeon_fees, (), False, False),
    (r"\banaesthe(sia|tist)\b|\banesthe", MappedGroup.anaesthesia, (), False, False),
    (r"\bnursing\b", MappedGroup.nursing, (), False, False),
    (r"\bicu\b|intensive care|\bccu\b", MappedGroup.icu, (), False, False),
    (r"\b(stent|screw|plate|prosthesis|lens|valve|pacemaker|mesh|implant)s?\b", MappedGroup.implant, (), False, True),
    (r"\b(registration|admission) (fee|charge)s?\b|\bservice charges?\b|\battendants?\b|\bgowns?\b|\bcaps?\b|\bmasks?\b|\bslippers?\b|\bdiet kits?\b|\bbelts?\b|\bthermometers?\b|\bTV\b|\bphone\b",
     MappedGroup.non_medical, ("non_medical",), True, False),
    (r"\bcosmetic\b|\bhair (transplant|removal)\b", MappedGroup.other, ("cosmetic",), False, False),
    (r"\b(pre|before) ?-?hospitali[sz]ation\b", MappedGroup.pre_hospitalisation, (), False, False),
    (r"\b(post|after) ?-?hospitali[sz]ation\b", MappedGroup.post_hospitalisation, (), False, False),
    (r"\b(package|pkg)\b", MappedGroup.procedure_package, (), False, False),
    (r"\b(x-?ray|mri|ct scan|ultrasound|usg|blood|culture|lft|kft|cbc|ecg|echo|biopsy|histopath)\w*", MappedGroup.investigation, (), False, False),
    (r"\b(tablet|capsule|syrup|injection|inj\.?|infusion|iv fluid|saline)s?\b", MappedGroup.medicine, (), False, False),
    (r"\b(syringe|gloves?|catheter|cannula|dressing|bandage|drape|suture|swab)s?\b", MappedGroup.consumable, (), False, False),
]
_COMPILED = [(re.compile(p, re.I), g, t, nm, imp) for p, g, t, nm, imp in KEYWORD_RULES]


@dataclass(frozen=True)
class Mapping:
    mapped_group: MappedGroup
    tags: tuple[str, ...] = field(default_factory=tuple)
    is_non_medical: bool = False
    is_implant: bool = False
    source: str = "rule"


def map_line(category: str, description: str) -> Mapping | None:
    """Return a rule-based mapping, or ``None`` when only an LLM could decide (caller marks ``source='agent'``)."""
    for rx, group, tags, nm, imp in _COMPILED:
        if rx.search(description):
            return Mapping(group, tags, nm, imp)
    g = CATEGORY_DEFAULT.get(category)
    if g is not None and category != "other":
        return Mapping(g, (), False, g == MappedGroup.implant)
    return None
