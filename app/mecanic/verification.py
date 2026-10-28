"""Stage 8 — Verification (doc 02 §8, doc 15): consent-gated code clear ->
the exact drive cycle per model -> Mode $06 re-check -> the permanent DTC
clears itself only when the car agrees -> "did the fix hold" follow-up.
"""

from __future__ import annotations

from .patterns import load_drive_cycles

GENERIC_OBD2_KEY = "generic_obd2"


def build_verification(state, library, verdict_data: dict) -> dict:
    pb, _cause = library.find_cause(verdict_data.get("cause") or "")
    note = ""
    recheck: list = []
    cycle_key = GENERIC_OBD2_KEY
    if pb:
        v = pb.verification or {}
        cycle_key = v.get("drive_cycle", GENERIC_OBD2_KEY)
        recheck = list(v.get("recheck_mode06", []))
        note = v.get("note_fr", "")

    cycles = load_drive_cycles()
    cycle = cycles.get(cycle_key) or cycles.get(GENERIC_OBD2_KEY) or {}
    steps = list(cycle.get("steps", []))

    checklist = [
        "Réparation terminée? Prendre la photo de la pièce remplacée (traçabilité rapport).",
        "AVEC CONSENTEMENT: effacer les codes (bouton consentement — action irréversible).",
    ] + steps + [
        "Re-vérifier Mode $06 pour les tests listés ci-dessous (l'agent relit).",
        "Le code permanent s'effacera de LUI-MÊME quand la voiture confirmera le correctif.",
        "Relire les trims à vide: < ±5% attendus si mélange réparé.",
    ]
    return {
        "drive_cycle": {"key": cycle_key, "name_fr": cycle.get("name_fr", cycle_key),
                        "steps": steps},
        "recheck_mode06": recheck,
        "checklist": checklist,
        "consent_note": ("L'effacement des codes exige un consentement explicite (doc 02 §7). "
                         "Le permanent, lui, ne s'efface que par la voiture."),
        "note_fr": note,
    }
