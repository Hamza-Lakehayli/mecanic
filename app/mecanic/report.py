"""The report generator (doc 15 stage 6/7/8 outputs, doc 12 section 4): the
billable artifact — a dossier, in French, evidence-cited, exportable at any
moment (partial evidence = partial dossier + "what would sharpen this" —
the mid-state Rapport Confiance sellable, doc 12 section 2).
"""

from __future__ import annotations

from pathlib import Path

from .funnel import token_estimate
from .verdict import render_citations


def _fmt_code_list(state) -> str:
    groups = []
    for kind, label in (("stored", "mémorisés"), ("pending", "en attente"),
                       ("permanent", "permanents")):
        codes = state.dtc_codes(kind)
        if codes:
            groups.append(f"{label}: {', '.join(codes)}")
    return " · ".join(groups) if groups else "aucun"


def render_report(state, verdict_data: dict, fix_plan: dict | None,
                  verification: dict | None, cards: list[dict], nba: dict | None,
                  digest: dict | None) -> str:
    v = state.vehicle or {}
    partial = verdict_data.get("status") != "verdict"
    L: list[str] = []
    L.append(f"# Dossier Mécanic — {state.session_id}")
    L.append("")
    L.append(f"**Véhicule:** {v.get('make', '?')} {v.get('model', '')} "
             f"{v.get('year', '')} · {v.get('engine', '')} · VIN `{state.vin or '?'}`")
    complaint = (state.complaint or {}).get("text", "")
    L.append(f"**Plainte:** {complaint or '(non renseignée — dossier produit quand même, doc 12)'}")
    chips = state.chips + state.condition_chips
    if chips:
        L.append(f"**Indices déclarés:** {', '.join(chips)}")
    L.append(f"**Mode:** {state.mode} · **Codes:** {_fmt_code_list(state)}")
    if partial:
        L.append("")
        L.append("> **Dossier partiel** — le verdict n'est pas encore atteint. "
                 "Ce qui l'affinerait : la carte NBA ci-dessous, 2 minutes sur la voiture.")
    L.append("")

    if state.freeze_frames:
        L.append("## Trame de gel")
        for ff in state.freeze_frames:
            vals = ", ".join(f"{k} = {val}" for k, val in ff.get("values", {}).items())
            L.append(f"- Au moment du code {ff.get('dtc', '?')} : {vals}")
        L.append("")

    if state.mode06:
        L.append("## Mode $06 (auto-tests usine)")
        for m in state.mode06:
            mark = {"pass": "OK", "near_limit": "⚠ dérive", "fail": "ÉCHEC"}[m["verdict"]]
            L.append(f"- {m['name']} : {m['value']} (usine {m['min']}–{m['max']}) — {mark}")
        L.append("")

    if state.readiness:
        mon = state.readiness.get("monitors", {})
        inc = [k for k, s in mon.items() if s == "incomplete"]
        comp = [k for k, s in mon.items() if s == "complete"]
        L.append("## Readiness")
        L.append(f"- Terminés : {', '.join(comp) if comp else '—'}")
        L.append(f"- Incomplets : {', '.join(inc) if inc else '—'}")
        L.append("")

    if getattr(state, "module_scan", None):
        ms = state.module_scan
        L.append("## Carte de santé des modules (doc 12 §2)")
        for m in ms.get("modules", []):
            icon = {"ok": "🟢", "codes": "🟠", "missing": "⚫"}.get(m.get("status"), "⚪")
            L.append(f"- {icon} {m.get('addr')} — {m.get('status')}")
        if not ms.get("modules"):
            L.append("- ⚫ Aucun module ne répond — vérifier le contact (ignition ON).")
        L.append("")

    if state.triage_findings:
        L.append("## Triage — constats")
        for f in state.triage_findings:
            icon = {"danger": "🔴", "warn": "🟠", "info": "🔵"}.get(f.get("severity", "info"), "⚪")
            L.append(f"- {icon} **{f['title']}** — {f['detail']}")
        L.append("")

    L.append("## Hypothèses classées")
    ranking_rows = state.rankings[-1]["ranking"] if state.rankings else []
    if ranking_rows:
        for r in ranking_rows:
            L.append(f"- **{r['label']}** — confiance {r['confidence']}% "
                     f"(playbook {r['playbook']}, part {r['share']*100:.0f}%)")
            for ev in r.get("evidence_for", [])[:4]:
                L.append(f"  - [{ev.get('class')}] {ev.get('text')}")
    else:
        L.append("- Aucune hypothèse active (aucun code ni flag discriminant).")
    L.append("")

    L.append("## Verdict")
    L.append(f"{verdict_data.get('statement', '')}")
    citations = render_citations(verdict_data)
    if citations:
        L.append("")
        L.append("**Preuves citées :**")
        L.append("```")
        L.append(citations)
        L.append("```")
    L.append("")

    if nba:
        if partial:
            L.append("## Prochaine meilleure action (NBA)")
            L.append(f"**{nba['title_fr']}** (~{nba.get('minutes', '?')} min) — sépare les "
                     f"finalistes en une étape.")
        else:
            L.append("## Confirmation recommandée (optionnel)")
            L.append(f"**{nba['title_fr']}** (~{nba.get('minutes', '?')} min) — le verdict "
                     f"est posé; ce test le confirme sur le véhicule avant la sortie.")
        for s in nba.get("steps_fr", [])[:3]:
            L.append(f"1. {s}")
        L.append("")

    if cards:
        L.append("## File d'attente de tests (jamais bloquante)")
        for c in cards:
            L.append(f"- [ ] {c['title_fr']} (gain {c['gain_score']}, ~{c.get('minutes', '?')} min)")
        L.append("")

    if fix_plan:
        L.append("## Plan de réparation")
        for p in fix_plan["parts"]:
            price = f"~{p['price_mad_est']} MAD" if p.get("price_mad_est") else "prix à sourcer"
            L.append(f"- {p['qty']}× {p['name_fr']} ({price})")
        L.append(f"- Main d'œuvre estimée : {fix_plan['labor_h']} h "
                 f"(~{fix_plan['labor_total_mad_est']} MAD)")
        for step in fix_plan.get("procedure", []):
            L.append(f"  - {step}")
        if fix_plan.get("post_repair_steps"):
            L.append("  - **Après la réparation — configuration requise (doc 02 §3):**")
            for step in fix_plan["post_repair_steps"]:
                L.append(f"    - ⚠️ {step}")
        if fix_plan.get("pricing_disclaimer"):
            L.append(f"\n  *{fix_plan['pricing_disclaimer']}*")
        L.append("")

    if verification:
        L.append("## Vérification")
        for step in verification["checklist"]:
            L.append(f"- [ ] {step}")
        L.append(f"\n*{verification.get('consent_note', '')}*")
        L.append("")

    if digest:
        L.append("## Budget attention (doc 16)")
        L.append(f"- Digest : ~{token_estimate(digest)} tokens (budget 5–10K/dossier atteignable).")
        L.append("")

    L.append("---")
    L.append("*Rapport généré par l'agent Mécanic — chaque affirmation cite sa preuve typée : "
             "[OBD] [INTAKE] [PHYSICAL] [MEASUREMENT] [PHOTO].*")
    return "\n".join(L)


def save_report(session_id: str, markdown: str, sessions_dir: Path,
                suffix: str = ".report.md") -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{session_id}{suffix}"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    return path


# ---------------------------------------------------------------------------
# Rapport Confiance — the buyer-facing pre-purchase report (doc 07 wedge #1,
# 199–399 MAD/report). Same evidence, different reader: trust language,
# zero jargon, scope honesty up front (what this report CANNOT see).
# ---------------------------------------------------------------------------

_KIND_LABELS = {
    "fault": "Défaut",
    "tamper": "Manipulation probable",
    "drift": "Usure à surveiller",
    "readiness": "Préparation contrôle technique",
    "connectivity": "Connexion",
    "paired": "Correspondance symptôme/données",
    "refer_out": "Hors périmètre des données",
}

_KIND_ORDER = {"tamper": 0, "fault": 1, "drift": 2, "readiness": 3,
               "connectivity": 4, "paired": 5, "refer_out": 6}


def confiance_tier(state) -> tuple[str, str]:
    """The one-line verdict of trust: green / amber / orange / red / incomplete.
    Forensics first (the buyer's whole question), then faults, then wear."""
    findings = state.triage_findings
    ids = {f["id"] for f in findings}
    ms = getattr(state, "module_scan", None) or {}
    if "ignition_off_suspect" in ids or ms.get("responding") == 0:
        return ("incomplete",
                "Scan incomplet — véhicule NON évalué (aucun calculateur ne répond: "
                "contact coupé ?).")
    if any(f.get("kind") == "tamper" for f in findings):
        return ("red",
                "Signaux de manipulation détectés — les codes ont probablement été "
                "effacés avant la mise en vente.")
    if state.dtc_codes("stored") or state.dtc_codes("permanent"):
        return ("orange",
                "Défauts actifs mémorisés par le véhicule — à réparer avant l'achat "
                "ou à négocier.")
    if any(f.get("kind") == "drift" for f in findings):
        return ("amber",
                "Aucun défaut actif, mais un organe s'use et approche sa limite usine.")
    incomplete = [m for m, s in (state.readiness or {}).get("monitors", {}).items()
                  if s == "incomplete"]
    if incomplete:
        return ("amber",
                "Aucun défaut actif — mais des auto-tests ne sont pas terminés depuis "
                "le dernier effacement des codes.")
    return ("green",
            "Aucun défaut détecté au branchement: codes, auto-tests usine et moniteurs "
            "sont propres.")


def render_confiance_report(state) -> str:
    v = state.vehicle or {}
    tier, headline = confiance_tier(state)
    L: list[str] = []
    L.append(f"# Rapport Confiance — {v.get('make', '?')} {v.get('model', '')} "
             f"{v.get('year', '')}")
    L.append("")
    L.append(f"**VIN:** `{state.vin or '?'}` · **Moteur:** {v.get('engine', '?')} · "
             f"**Session:** {state.session_id}")
    L.append("")
    L.append(f"## Verdict ({tier})")
    L.append("")
    L.append(f"**{headline}**")
    L.append("")
    if state.triage_findings:
        L.append("## Points de vigilance")
        for f in sorted(state.triage_findings,
                        key=lambda x: _KIND_ORDER.get(x.get("kind"), 9)):
            icon = {"danger": "🔴", "warn": "🟠", "info": "🔵"}.get(f.get("severity"), "⚪")
            kind = _KIND_LABELS.get(f.get("kind"), "Constat")
            L.append(f"- {icon} **{kind} — {f['title']}:** {f['detail']}")
        L.append("")
    codes = (state.dtc_codes("stored") + state.dtc_codes("pending")
             + state.dtc_codes("permanent"))
    if codes:
        L.append(f"**Codes mémorisés:** {', '.join(sorted(set(codes)))}")
        L.append("")
    if state.mode06:
        L.append("## Auto-tests usine (Mode $06)")
        for m in state.mode06:
            mark = {"pass": "OK", "near_limit": "⚠ dérive", "fail": "ÉCHEC"}[m["verdict"]]
            L.append(f"- {m['name']} : {m['value']} {m.get('unit', '')} "
                     f"(usine {m['min']}–{m['max']}) — {mark}")
        L.append("")
    if state.readiness:
        mon = state.readiness.get("monitors", {})
        inc = [k for k, s in mon.items() if s == "incomplete"]
        L.append("## Préparation contrôle technique")
        L.append(f"- Moniteurs terminés: {len(mon) - len(inc)}/{len(mon)}"
                 + (f" — restent à rouler: {', '.join(inc)}" if inc else ""))
        L.append("")
    verdict = state.verdict or {}
    if verdict.get("status") == "verdict":
        L.append("## Diagnostic technique déjà réalisé par l'agent")
        L.append(f"**Cause probable:** {verdict.get('label')} "
                 f"(confiance {verdict.get('confidence')}%) — chaque affirmation est "
                 f"appuyée par une preuve typée du registre de la session.")
        L.append("")
    L.append("## Ce que ce rapport NE peut PAS vérifier")
    L.append("- L'état mécanique invisible aux calculateurs (embrayage, suspension, jeux).")
    L.append("- Le kilométrage réel physique (hors plausibilité par les données).")
    L.append("- La carrosserie, l'accidenté, l'intérieur — inspection sur place requise.")
    L.append("- Les pannes intermittentes sans code au moment du scan.")
    L.append("")
    L.append("---")
    L.append("*Rapport produit par l'agent Mécanic à partir d'une lecture directe des "
             "calculateurs du véhicule ([OBD] [INTAKE] [PHYSICAL] [MEASUREMENT]) — "
             "chaque constat cite sa preuve. Un agent honnête dit aussi ce qu'il ne "
             "peut pas voir.*")
    return "\n".join(L)
