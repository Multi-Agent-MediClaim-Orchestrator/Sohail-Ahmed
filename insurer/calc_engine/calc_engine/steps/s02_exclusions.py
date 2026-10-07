"""S2 — exclusions run before caps so excluded amounts never consume a sub-limit (decision D-1)."""

from __future__ import annotations

from ..models import CalcInput
from ..state import EngineState


def s02_exclusions(st: EngineState, inp: CalcInput) -> EngineState:
    st = st.clone()
    before = st.net_total()
    ex, adm = inp.rules.exclusions, inp.admission
    primary = adm.diagnosis_codes[0] if adm.diagnosis_codes else None
    icd_hit = primary is not None and any(primary.startswith(p) for p in ex.icd_prefixes)
    tags = set(ex.tags)
    affected: list[str] = []
    for ls in st.lines:
        if ls.allowed <= 0:
            continue
        ln = ls.line
        if icd_hit and (adm.procedure_group is None or ln.procedure_group == adm.procedure_group):
            st.disallow_line(ls, "R-EXCL-01", "S2", f"Diagnosis {primary} is excluded")
        elif set(ln.exclusion_tags) & tags:
            st.disallow_line(ls, "R-EXCL-02", "S2", f"Excluded tag(s): {sorted(set(ln.exclusion_tags) & tags)}")
        elif (ln.is_non_medical and ex.non_medical_policy == "exclude_all") or (
            ex.non_medical_policy == "exclude_listed" and "non_medical" in ln.exclusion_tags
        ):
            st.disallow_line(ls, "R-EXCL-03", "S2", "Non-medical item")
        else:
            continue
        affected.append(ls.ref)
    st.record("S2", "Exclusions", before, "R-EXCL-0x" if affected else None, affected)
    return st
