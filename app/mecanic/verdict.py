"""Stage 6 — Verdict composer (doc 00 step 6, doc 15).

Root cause + typed evidence citations ([OBD] [INTAKE] [PHYSICAL]
[MEASUREMENT] [PHOTO]) + confidence. Below threshold -> the NBA instead of
guessing; escalate to the human expert rather than fabricate certainty
(doc 02 §7: "confidence below threshold → ask, don't guess").
"""

from __future__ import annotations

from .ranker import RANKING_THRESHOLD, VERDICT_THRESHOLD


def compose(state, ranking: dict, nba: dict | None = None) -> dict:
    """Build the verdict event data from the current ranking."""
    rows = ranking.get("ranking", [])
    status = ranking.get("status", "escalate")
    if not rows:
        return {
            "cause": None, "label": None, "playbook": None, "confidence": 0,
            "status": "escalate",
            "statement": ("Pas encore d'hypothèse exploitable — aucune donnée discriminante. "
                          "Brancher/rouler ou ajouter des indices."),
            "citations": [], "nba": (nba or {}).get("task_id"),
        }

    top = rows[0]
    citations = []
    for ev in top.get("evidence_for", []):
        citations.append({"class": ev.get("class", "AGENT"), "text": ev.get("text", "")})
    against = [{"class": ev.get("class", "AGENT"), "text": ev.get("text", "")}
               for ev in top.get("evidence_against", [])]

    if status == "verdict":
        statement = (f"Cause probable: {top['label']} "
                     f"(confiance {top['confidence']}%). "
                     f"Chaque affirmation ci-dessous est citée avec sa preuve typée.")
    elif status == "ranking":
        statement = (f"Pas encore de verdict — hypothèse de tête: {top['label']} "
                     f"({top['confidence']}%). Le test à faire maintenant sépare les finalistes "
                     f"en une étape: voir la carte NBA.")
    else:
        statement = ("Confiance insuffisante pour trancher — ne pas deviner. "
                     "Exécuter la carte NBA et/ou ajouter des mesures; "
                     "si ça bloque, escalader vers un expert.")

    return {
        "cause": top["cause"], "label": top["label"], "playbook": top["playbook"],
        "confidence": top["confidence"], "status": status,
        "statement": statement,
        "citations": citations,
        "citations_against": against,
        "runner_up": [{"cause": r["cause"], "label": r["label"], "confidence": r["confidence"]}
                      for r in rows[1:3]],
        "nba": (nba or {}).get("task_id"),
    }


def render_citations(verdict: dict) -> str:
    """Plain-text citation block for reports/console."""
    lines = []
    for c in verdict.get("citations", []):
        lines.append(f"[{c['class']}] {c['text']}")
    for c in verdict.get("citations_against", []):
        lines.append(f"[CONTRE] [{c['class']}] {c['text']}")
    return "\n".join(lines)
