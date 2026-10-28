"""Stage 3 — Triage (doc 15): instant findings read *together with* the
intake hints. Three kinds of finding + the trust layer:

  * fault      — each real code, titled in French, dangerous ones flagged
  * drift      — Mode $06 monitors passing but drifting toward limits
  * tamper     — Rapport Confiance forensics: recently cleared codes,
                 permanent DTCs surviving a wipe, DPF/EGR-delete fingerprints
  * paired statements — intake hints correlated with data ("vous avez dit
                 sifflement — ça colle avec les trims +19% à vide").

Everything is deterministic (rules), evidence-cited, and re-run on every
ledger fold — triage is cheap and exact (doc 16 L1).
"""

from __future__ import annotations

from pathlib import Path

from .yamlmini import load_file

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Non-continuous monitors (incomplete for a few drives after a code clear)
NON_CONTINUOUS = ("catalyst", "heated_catalyst", "evap", "o2_sensor", "o2_heater",
                  "egr", "secondary_air", "ac_system")


def load_chip_labels() -> dict[str, str]:
    """chip id -> FR label (priors.yaml banks: symptoms/conditions/mechanical)."""
    priors = load_file(str(DATA_DIR / "priors.yaml")) or {}
    out = {}
    for section in ("symptoms", "conditions", "mechanical"):
        for chip, spec in (priors.get(section) or {}).items():
            if isinstance(spec, dict) and spec.get("label_fr"):
                out[chip] = spec["label_fr"]
    return out


def load_dtc_titles() -> dict:
    p = DATA_DIR / "dtc_titles.yaml"
    return load_file(str(p)) if p.exists() else {}


def _title(code: str, titles: dict) -> str:
    e = titles.get(code)
    if isinstance(e, dict):
        return e.get("fr", code)
    return code


class Triage:
    """Triage rules (deterministic; per-make precision is the open item —
    doc 15 stage 3)."""

    def __init__(self, dtc_titles: dict | None = None):
        self.dtc_titles = dtc_titles if dtc_titles is not None else load_dtc_titles()
        self.chip_labels = load_chip_labels()

    def analyze(self, state) -> list[dict]:
        findings: list[dict] = []
        stored = state.dtc_codes("stored")
        pending = state.dtc_codes("pending")
        permanent = state.dtc_codes("permanent")
        readiness = state.readiness or {}
        monitors = readiness.get("monitors", {})

        # 0 — ignition guard (doc 12 §2): a scan where NOTHING responds first
        # asks "is the key on?" — never "dead bus, dead ECU".
        flag_rules = {f["data"]["rule"] if "data" in f else f.get("rule", "")
                      for f in state.flags}
        if "no_ecu_response" in flag_rules:
            findings.append({
                "id": "ignition_off_suspect", "kind": "connectivity",
                "severity": "warn",
                "title": "Aucun module ne répond — vérifier le contact d'abord",
                "detail": ("L'adaptateur est alimenté (broche 16, batterie directe) "
                           "mais aucun calculateur ne répond. Cause n°1: contact "
                           "coupé — allumer le contact (ignition ON) et relancer le "
                           "scan. Ne JAMAIS conclure « bus mort / calculateur mort » "
                           "contact éteint. Contact déjà ON: vérifier la prise OBD, "
                           "les fusibles diagnostic, puis suspecter le bus (U-codes)."),
            })

        # 1 — real faults -----------------------------------------------------
        for kind, code_list in (("stored", stored), ("permanent", permanent)):
            for code in code_list:
                dangerous = code.startswith("P030") and kind == "permanent" and _has_load_freeze(state)
                findings.append({
                    "id": f"fault_{kind}_{code}",
                    "kind": "fault",
                    "severity": "danger" if dangerous else "warn",
                    "title": f"Code {code} — {_title(code, self.dtc_titles)}",
                    "detail": ("Ratés actifs sous charge — NE PAS CONDUIRE (doc 12)."
                               if dangerous else
                               ("Code permanent (survit à l'effacement des codes)."
                                if kind == "permanent" else "Code mémorisé actif.")),
                })
        for code in pending:
            findings.append({
                "id": f"pending_{code}",
                "kind": "fault", "severity": "info",
                "title": f"Code en attente {code} — {_title(code, self.dtc_titles)}",
                "detail": "Pas encore confirmé par deux cycles de conduite.",
            })

        # 2 — monitors drifting toward their factory limits (doc 00 step 3)
        for m in state.mode06:
            if m.get("verdict") == "near_limit":
                findings.append({
                    "id": f"drift_{m['mid']}_{m['tid']}",
                    "kind": "drift", "severity": "warn",
                    "title": f"{m['name']} passe encore, mais dérive vers sa limite",
                    "detail": (f"Valeur {m['value']} (limite usine {m['min']}–{m['max']}) — "
                               f"le code tombera bientôt. À surveiller d'une visite à l'autre."),
                })

        # 3 — tamper forensics (the Rapport Confiance engine, doc 10 §A)
        tamper = []
        if permanent and not stored:
            tamper.append(f"Codes effacés récemment: le(s) permanent(s) {', '.join(permanent)} "
                          f"survivent à l'effacement, mais plus aucun code mémorisé.")
        elif not stored and not pending and readiness.get("mil") is False:
            incomplete_nc = [m for m in NON_CONTINUOUS
                             if monitors.get(m) == "incomplete"]
            if incomplete_nc:
                cycles = readiness.get("drive_cycles_since_clear")
                when = f" (~{cycles} cycles de conduite)" if cycles is not None else ""
                tamper.append(f"Codes effacés{when}: {len(incomplete_nc)} moniteurs non-continus "
                              f"incomplets ({', '.join(incomplete_nc)}) et aucun code.")
        if tamper:
            findings.append({
                "id": "tamper_cleared_codes", "kind": "tamper", "severity": "danger",
                "title": "Effacement récent des codes détecté",
                "detail": " ".join(tamper) + " Signe classique d'une vente après masquage.",
            })

        fuel = (state.vehicle or {}).get("fuel", "").lower()
        if fuel == "diesel":
            egr_incomplete = monitors.get("egr") == "incomplete"
            cat_incomplete = monitors.get("catalyst") == "incomplete"
            no_emissions_codes = not any(c.startswith(("P040", "P042", "P246", "P203"))
                                         for c in stored + pending)
            if (egr_incomplete or cat_incomplete) and no_emissions_codes and not stored:
                which = "EGR" if egr_incomplete else "catalyseur"
                declared = ((state.history or {}).get("tuning_state") or {})
                note = " (déclaré par l'atelier)" if declared else ""
                findings.append({
                    "id": "tamper_delete_fingerprint", "kind": "tamper", "severity": "warn",
                    "title": f"Empreinte compatible avec une suppression {which}/DPF",
                    "detail": (f"Moniteur {which} jamais terminé, aucun code emissions, "
                               f"diesel{note}. Cohérent avec un véhicule dépollué "
                               f"(suppression EGR/DPF). Vérification visuelle requise."),
                })

        # 4 — readiness state (drive-cycle counters, CT prep)
        incomplete = [m for m, s in monitors.items() if s == "incomplete"]
        if incomplete and not tamper:
            findings.append({
                "id": "readiness_incomplete", "kind": "readiness", "severity": "info",
                "title": f"{len(incomplete)} moniteur(s) incomplet(s)",
                "detail": f"{', '.join(incomplete)} — cycles de conduite restants avant CT.",
            })

        # 5 — paired statements: intake hints read together with the data
        findings += self._paired_statements(state)

        # 6 — recent work = the comeback prior (doc 11 §2: highest-value question)
        recent = (state.history or {}).get("recent_work", [])

        # 7 — mechanical / body complaints: route OUT of data diagnosis
        # (doc 00 "what it is NOT", doc 10 §3C: honestly routing ~20% of jobs
        # away is what makes the other 80% of verdicts sellable)
        mech = sorted(set((state.complaint or {}).get("mechanical_chips", [])))
        if mech:
            labels = [self.chip_labels.get(c, c) for c in mech]
            findings.append({
                "id": "mechanical_refer_out",
                "kind": "refer_out", "severity": "info",
                "title": "Plainte mécanique/carrosserie — hors périmètre des données OBD",
                "detail": ("Symptômes déclarés: " + ", ".join(labels) + ". Ceci n'est pas "
                           "détectable via les données du véhicule — orienter vers un "
                           "contrôle physique (carrosserie/mécanique). Le scan des données "
                           "continue par ailleurs: un agent qui sait dire 'non détectable, "
                           "vérifiez physiquement' est précisément ce qui rend fiables ses "
                           "autres verdicts."),
            })

        # 8 — recent work finding
        if recent:
            last = recent[-1]
            part = last.get("part", "?")
            system = last.get("system", "?")
            findings.append({
                "id": f"comeback_{system}_{part}".replace(" ", "_"),
                "kind": "fault", "severity": "info",
                "title": f"Travaux récents sur {system} ({part})",
                "detail": ("Re-vérifier d'abord ce qui a été touché: durite débranchée, "
                           "connecteur mal clipsé, collier oublié — la première cause "
                           "d'une panne juste après une réparation."),
            })
        return findings

    def _paired_statements(self, state) -> list[dict]:
        """Correlate intake chips/conditions with data findings (doc 15)."""
        out = []
        chips = set(state.chips) | set(state.condition_chips)
        flags = {f["data"]["rule"] if "data" in f else f["rule"] for f in state.flags}
        flag_values = {}
        for f in state.flags:
            fd = f["data"] if "data" in f else f
            flag_values[fd["rule"]] = fd.get("values", {})

        def paired(id_, title, detail):
            out.append({"id": f"paired_{id_}", "kind": "paired", "severity": "info",
                        "title": title, "detail": detail})

        if "hissing" in chips and ("trim_lean_overall" in flags or "trim_idle_dominant" in flags):
            v = flag_values.get("trim_idle_dominant", flag_values.get("trim_lean_overall", {}))
            idle = v.get("idle_total", "?")
            paired("hissing_trims",
                   "Votre sifflement colle avec les données",
                   f"Vous avez signalé un sifflement — les trims totaux à vide sont "
                   f"à +{idle}% : de l'air non mesuré entre bien par là. "
                   f"Test de localisation par pulvérisation en file d'attente.")
        if "under_load" in chips and "trim_load_only" in flags:
            paired("load_trims", "Perte de puissance sous charge confirmée par les trims",
                   "Trims pauvres uniquement sous charge — le volume carburant ne suit "
                   "pas la demande (pompe/filtre/injecteurs).")
        if "cold_only" in chips and "warmup_slow" in flags:
            paired("cold_thermostat", "Symptôme à froid + montée en température lente",
                   "Le thermostat n'atteint pas sa température — cohérent avec votre "
                   "plainte 'seulement à froid'.")
        if "battery_light" in chips and "voltage_low" in flags:
            paired("battery_voltage", "Voyant batterie + tension faible confirmée",
                   "La tension au ralenti est sous 13,5 V — le voyant dit vrai.")
        if "black_smoke" in chips:
            paired("black_rich", "Fumée noire signalée",
                   "Fumée noire = mélange riche. Si les trims deviennent négatifs, "
                   "regarder régulateur de pression / injecteur qui fuit / débitmètre qui sur-estime.")
        if "after_refuel" in chips:
            paired("refuel_evap", "Apparu juste après le plein",
                   "Post-refueling → purge EVAP/bouchon en tête de liste (image gel de "
                   "trame: niveau de carburant au moment du code).")
        if "rain" in chips:
            paired("rain_ignition", "Apparaît sous la pluie",
                   "Humidité → arcage secondaire allumage / entrée d'eau connecteur. "
                   "Test de flexion (wiggle) + inspection visuelle conseillés.")
        return out


def _has_load_freeze(state) -> bool:
    for ff in state.freeze_frames:
        vals = ff.get("values", {})
        if vals.get("LOAD", 0) >= 40 or vals.get("RPM", 0) >= 2500:
            return True
    return False
