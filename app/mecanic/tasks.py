"""Stage 5 — Task engine & test cards (docs 10/11/12/14).

Pattern trees decompose into:
  * AUTO tasks — monitoring, warm-up curve, idle trim snapshot, O2 switching,
    windowed drive-data mining, Mode $06 watching, report drafting, re-ranking.
  * NEEDS-YOU test cards — "hold 2500 RPM", spray/wiggle/tap, gauge widgets,
    photos — queued, NEVER blocking; the agent keeps working meanwhile.
  * Tier C actuation cards — only when the capability contract allows
    (filtered by construction on read-only adapters), behind consent gates.

The NBA (Next Best Action) card: exactly one prominent card — the highest
information-gain NEEDS-YOU task (v1 heuristic: lookup per pattern, doc 17
gap #10). Anti-fatigue: at most 5 visible cards queued (doc 12 §4).
"""

from __future__ import annotations

from pathlib import Path

from .schema import CapabilityContract
from .yamlmini import load_file

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MAX_VISIBLE_CARDS = 5  # anti-fatigue guard (doc 12)

# ---------------------------------------------------------------------------
# The card catalog (doc 11 §3 assisted tests + doc 02 §2 confirming tests +
# doc 12 measurement widgets with specs built in).
# ---------------------------------------------------------------------------

CARD_CATALOG: dict[str, dict] = {
    # -- data tests: the tech holds a condition, the agent watches ------------
    "hold_2500_rpm": {
        "kind": "NEEDS_YOU", "class": "data_test",
        "title_fr": "Tenir 2500 tr/min pendant 30 s",
        "steps_fr": ["Moteur chaud, frein de stationnement serré.",
                     "Amener le régime à 2500 tr/min et le tenir 30 secondes.",
                     "L'agent compare les trims à vide contre 2500: chute = fuite d'admission."],
        "watch": ["STFT_B1", "LTFT_B1", "STFT_B2", "LTFT_B2", "MAF"],
        "targets": ["vacuum_leak", "maf_underreport", "fuel_delivery_weak"],
        "gain_weight": 30, "minutes": 2,
        "requires": [],
    },
    "spray_test": {
        "kind": "NEEDS_YOU", "class": "physical_test",
        "title_fr": "Test de pulvérisation autour de l'admission",
        "steps_fr": ["Moteur tournant au ralenti.",
                     "Pulvériser eau savonneuse (ou sonde fumée) autour des joints: collecteur, durites, PCV, purge.",
                     "L'agent surveille STFT: un coup de trim quand le spray touche = fuite LOCALISÉE."],
        "watch": ["STFT_B1", "LTFT_B1", "RPM"],
        "targets": ["vacuum_leak", "exhaust_leak_upstream"],
        "gain_weight": 34, "minutes": 3,
        "safety_fr": "Jamais de produit inflammable near-échappement chaud.",
        "requires": [],
    },
    "smoke_machine": {
        "kind": "NEEDS_YOU", "class": "physical_test",
        "title_fr": "Machine à fumée dans l'admission",
        "steps_fr": ["Mettre l'admission en légère surpression avec la machine à fumée.",
                     "Chercher les filaments de fumée qui sortent."],
        "watch": [],
        "targets": ["vacuum_leak"],
        "gain_weight": 30, "minutes": 10,
        "requires": [],
    },
    "wiggle_test": {
        "kind": "NEEDS_YOU", "class": "physical_test",
        "title_fr": "Test de flexion des faisceaux/connecteurs",
        "steps_fr": ["Moteur au ralenti, flexionner doucement les faisceaux et connecteurs autour de la zone suspecte.",
                     "L'agent enregistre tout décrochage RPM/signal corrélé au geste."],
        "watch": ["RPM", "MAP"],
        "targets": ["egr_valve_electrical", "ignition_coil", "ground_corrosion",
                    "bus_wire_intermittent", "module_power_ground"],
        "gain_weight": 24, "minutes": 3,
        "requires": [],
    },
    "tap_test": {
        "kind": "NEEDS_YOU", "class": "physical_test",
        "title_fr": "Tapoter le composant suspect",
        "steps_fr": ["Tapoter doucement le composant (vanne, sonde, relais) pendant que l'agent surveille son signal."],
        "watch": ["RPM"],
        "targets": ["o2_sensor_biased", "o2_sensor_aging", "egr_valve_electrical",
                    "o2_downstream_lazy"],
        "gain_weight": 18, "minutes": 1,
        "requires": [],
    },
    "unplug_maf": {
        "kind": "NEEDS_YOU", "class": "data_test",
        "title_fr": "Débrancher le débitmètre moteur tournant",
        "steps_fr": ["Débrancher le connecteur MAF au ralenti.",
                     "L'agent regarde le passage en cartographie de secours: si le moteur va MIEUX, le MAF ment."],
        "watch": ["RPM", "STFT_B1", "MAF"],
        "targets": ["maf_underreport", "maf_overreport", "maf_bias_gravity"],
        "gain_weight": 28, "minutes": 1,
        "requires": [],
    },
    "electrical_load_test": {
        "kind": "NEEDS_YOU", "class": "data_test",
        "title_fr": "Test de charge électrique",
        "steps_fr": ["Au ralenti: phares + ventilation habitacle + dégivrage arrière allumés.",
                     "Frein serré, véhicule arrêté.",
                     "L'agent surveille la tension: sag < 13,5 V = circuit de charge en cause."],
        "watch": ["BATV"],
        "targets": ["alternator_fault", "ground_corrosion"],
        "gain_weight": 30, "minutes": 2,
        "requires": [],
    },
    "fan_response_check": {
        "kind": "NEEDS_YOU", "class": "data_test",
        "title_fr": "Vérifier l'enclenchement du ventilateur",
        "steps_fr": ["Laisser monter en température au ralenti (ou clim en marche).",
                     "Le ventilateur doit s'enclencher: sinon circuit ventilateur, pas thermostat."],
        "watch": ["ECT", "BATV"],
        "targets": ["fan_always_on", "thermostat_stuck_open"],
        "gain_weight": 22, "minutes": 6,
        "requires": [],
    },
    # -- measurement widgets: the agent pre-fills the spec ---------------------
    "gauge_fuel_pressure": {
        "kind": "NEEDS_YOU", "class": "measurement",
        "title_fr": "Pression rail (jauge)",
        "steps_fr": ["Brancher la jauge au rail, lire à vide et à 2500 tr/min.",
                     "Renseigner la valeur — l'agent compare à la plage usine."],
        "widget": {"metric": "fuel_pressure_rail", "unit": "bar", "min": 3.0, "max": 4.5},
        "targets": ["fuel_delivery_weak", "injector_clogged", "fuel_filter_clogged",
                    "leak_off_lines", "fuel_pump_weak"],
        "gain_weight": 30, "minutes": 5,
        "requires": [],
    },
    "gauge_compression": {
        "kind": "NEEDS_YOU", "class": "measurement",
        "title_fr": "Compression par cylindre",
        "steps_fr": ["Moteur chaud, papillon ouvert, toutes les bougies retirées.",
                     "Noter la valeur par cylindre — variance > 15% signale le cylindre faible."],
        "widget": {"metric": "compression_variance", "unit": "%", "min": 0, "max": 15},
        "targets": ["compression_low"],
        "gain_weight": 32, "minutes": 20,
        "requires": [],
    },
    "gauge_battery_rest": {
        "kind": "NEEDS_YOU", "class": "measurement",
        "title_fr": "Tension batterie au repos",
        "steps_fr": ["Après 1 h de repos, mesurer aux bornes. <12,4 V = déchargée/usée."],
        "widget": {"metric": "battery_rest_v", "unit": "V", "min": 12.4, "max": 12.8},
        "targets": ["battery_worn"],
        "gain_weight": 22, "minutes": 1,
        "requires": [],
    },
    "gauge_cranking_volt": {
        "kind": "NEEDS_YOU", "class": "measurement",
        "title_fr": "Tension au démarrage",
        "steps_fr": ["Mesurer pendant le démarrage: <9,6 V = batterie faible."],
        "widget": {"metric": "cranking_v", "unit": "V", "min": 9.6, "max": 11.0},
        "targets": ["battery_worn"],
        "gain_weight": 24, "minutes": 1,
        "requires": [],
    },
    "ir_gun_ect_check": {
        "kind": "NEEDS_YOU", "class": "measurement",
        "title_fr": "Comparer sonde eau vs pistolet IR",
        "steps_fr": ["Pistolet IR sur le boîtier thermostat, comparer à la valeur sonde affichée."],
        "widget": {"metric": "ect_vs_ir_delta", "unit": "°C", "min": -5, "max": 5},
        "targets": ["coolant_temp_sensor_bias"],
        "gain_weight": 26, "minutes": 2,
        "requires": [],
    },
    # -- photos (multimodal later; stored + report material now) ---------------
    "photo_spark_plugs": {
        "kind": "NEEDS_YOU", "class": "photo",
        "title_fr": "Photo des bougies par cylindre",
        "steps_fr": ["Une photo par bougie, étiquetée par cylindre.",
                     "Blanc = pauvre · noir sec = riche · huileux = huile · tan = sain."],
        "targets": ["spark_plug_worn", "lean_misfire", "engine_root_cause"],
        "gain_weight": 22, "minutes": 5,
        "fuel": ["essence"],
        "requires": [],
    },
    "photo_maf_part_number": {
        "kind": "NEEDS_YOU", "class": "photo",
        "title_fr": "Photo du débitmètre + numéro de pièce",
        "steps_fr": ["Vérifier la référence: débitmètres et sondes O2 contrefaites sont monnaie courante sur le marché."],
        "targets": ["maf_underreport", "o2_sensor_biased", "o2_sensor_aging",
                    "o2_downstream_lazy"],
        "gain_weight": 16, "minutes": 1,
        "requires": [],
    },
    # -- swap logic (misfire) ----------------------------------------------------
    "swap_coil": {
        "kind": "NEEDS_YOU", "class": "physical_test",
        "title_fr": "Échanger la bobine du cylindre en raté",
        "steps_fr": ["Échanger la bobine du cylindre fautif avec celle d'un autre cylindre.",
                     "Le raté suit-il la bobine ? L'agent compare les compteurs avant/après."],
        "watch": ["RPM"],
        "targets": ["ignition_coil"],
        "gain_weight": 34, "minutes": 5,
        "fuel": ["essence"],
        "requires": [],
    },
    "air_filter_check": {
        "kind": "NEEDS_YOU", "class": "physical_test",
        "title_fr": "Inspecter le filtre à air",
        "steps_fr": ["Sortir le filtre, contrôler le colmatage et le conduit d'admission."],
        "targets": ["intake_restriction"],
        "gain_weight": 14, "minutes": 2,
        "requires": [],
    },
    "egr_visual_check": {
        "kind": "NEEDS_YOU", "class": "physical_test",
        "title_fr": "Inspection visuelle de la vanne EGR",
        "steps_fr": ["Déposer la vanne EGR: état du clapet, calamine, freedom de mouvement."],
        "targets": ["egr_passage_clogged", "egr_leaking"],
        "gain_weight": 24, "minutes": 15,
        "requires": [],
    },
    "ground_inspection": {
        "kind": "NEEDS_YOU", "class": "physical_test",
        "title_fr": "Contrôler les masses (oxydation)",
        "steps_fr": ["Nettoyer/inspecter les masses moteur et carrosserie; mesurer la chute de tension (>0,3 V = résistance)."],
        "targets": ["ground_corrosion", "module_power_ground"],
        "gain_weight": 20, "minutes": 10,
        "requires": [],
    },
    "tsb_check": {
        "kind": "NEEDS_YOU", "class": "research",
        "title_fr": "Vérifier les TSB publics pour ce modèle",
        "steps_fr": ["Chercher les bulletins de service constructeur pour ce code/moteur — plusieurs familles P0420 ont une reprogrammation ECM connue."],
        "targets": ["false_positive_tsb"],
        "gain_weight": 14, "minutes": 5,
        "requires": [],
    },
    "drive_cycle_recheck": {
        "kind": "NEEDS_YOU", "class": "data_test",
        "title_fr": "Rouler un cycle complet (l'agent enregistre tout)",
        "steps_fr": ["Branché, rouler le cycle de conduite indiqué — l'agent enregistre et analyse au retour."],
        "watch": [],
        "targets": ["catalyst_worn", "egr_passage_clogged", "dpf_interaction"],
        "gain_weight": 26, "minutes": 30,
        "requires": [],
    },
    # -- post-swap configuration check (the used-parts pattern, doc 02 §3) -----
    "verify_injector_coding": {
        "kind": "NEEDS_YOU", "class": "inspection",
        "title_fr": "L'injecteur remplacé a-t-il été codé ? (C2i/C3i)",
        "steps_fr": ["Vérifier si le codage de compensation de l'injecteur neuf a été écrit au calculateur:",
                     "— l'étiquette / la fiche livrée avec l'injecteur porte le code C2i/C3i — a-t-il été saisi ?",
                     "— ou demander directement à l'atelier qui a fait l'échange.",
                     "Non codé + ratés juste après l'échange = la cause la plus probable, avant toute autre pièce."],
        "targets": ["post_repair_config_missing", "injector_clogged"],
        "gain_weight": 32, "minutes": 2,
        "requires": [],
    },
    # -- fuel-gated cards (petrol-only / diesel-only — a dCi has no bobines) ---
    "check_gas_cap": {
        "kind": "NEEDS_YOU", "class": "physical_test",
        "title_fr": "Contrôler le bouchon de réservoir",
        "steps_fr": ["Dévisser le bouchon, inspecter le joint (fissuré? aplati?).",
                     "Revisser jusqu'aux 3 clics.",
                     "Un P0455 juste après le plein = bouchon dans 4 cas sur 5."],
        "watch": [],
        "targets": ["gas_cap_seal", "evap_leak_small"],
        "gain_weight": 20, "minutes": 1,
        "fuel": ["essence"],
    },
    "gauge_glow_plugs": {
        "kind": "NEEDS_YOU", "class": "measurement",
        "title_fr": "Résistance des bougies de préchauffage",
        "steps_fr": ["Débrancher le connecteur de chaque bougie, mesurer aux bornes (multimètre Ω).",
                     "Renseigner la valeur — l'agent compare à la plage usine (0,4–2 Ω)."],
        "widget": {"metric": "glow_plug_resistance", "unit": "Ω", "min": 0.4, "max": 2.0},
        "targets": ["glow_plug_worn", "glow_module_relay"],
        "gain_weight": 24, "minutes": 10,
        "fuel": ["diesel"],
    },
    "dpf_highway_regen": {
        "kind": "NEEDS_YOU", "class": "data_test",
        "title_fr": "Tentative de régénération: 30 min à 2500–3000 tr/min",
        "steps_fr": ["Routes dégagées, moteur chaud: maintenir 2500–3000 tr/min ~30 min (voie rapide).",
                     "L'agent enregistre tout le trajet.",
                     "Le défaut s'efface? → surcharge de suie (profil urbain), pas FAP mort. Persiste → colmatage, suite pro."],
        "watch": ["RPM", "LOAD", "ECT", "SPEED"],
        "targets": ["dpf_soot_overload", "dpf_clogged_blocked"],
        "gain_weight": 26, "minutes": 30,
        "fuel": ["diesel"],
    },
    "leak_off_inspection": {
        "kind": "NEEDS_YOU", "class": "physical_test",
        "title_fr": "Contrôler les durites de retour d'injecteurs (leak-off)",
        "steps_fr": ["Moteur tournant, inspecter les petits tuyaux de retour: bulles d'air = prise d'air amont.",
                     "Classique dCi: le réseau de retour fait chuter la pression rail sous charge."],
        "watch": [],
        "targets": ["leak_off_lines", "fuel_filter_clogged"],
        "gain_weight": 20, "minutes": 3,
        "fuel": ["diesel"],
    },
    # -- Tier C actuation (filtered out unless the capability contract allows) --
    "egr_sweep_tierC": {
        "kind": "NEEDS_YOU", "class": "actuation",
        "title_fr": "Balayage EGR commandé (VCI)",
        "steps_fr": ["Phase 2 — véhicule sécurisé, consentement requis.",
                     "L'agent commande la position EGR et surveille la réponse MAP/RPM."],
        "watch": ["MAP", "RPM", "EGR_ERR"],
        "targets": ["egr_passage_clogged", "egr_valve_electrical"],
        "gain_weight": 30, "minutes": 5,
        "requires": ["actuation:EGR_SWEEP"],
        "consent_required": True,
    },
    "dpf_check_tierC": {
        "kind": "NEEDS_YOU", "class": "actuation",
        "title_fr": "Lecture soot DPF + pression différentielle (VCI)",
        "steps_fr": ["Phase 2 — lecture des DIDs étendus via UDS 0x22."],
        "watch": [],
        "targets": ["dpf_interaction"],
        "gain_weight": 28, "minutes": 4,
        "requires": ["pid:dpf_soot_load"],
        "consent_required": True,
    },
    "injector_balance_tierC": {
        "kind": "NEEDS_YOU", "class": "actuation",
        "title_fr": "Équilibrage cylindres par coupure d'injecteurs (VCI)",
        "steps_fr": ["Phase 2 — coupure injecteur par injecteur, chute RPM comparée."],
        "watch": ["RPM"],
        "targets": ["injector_clogged"],
        "gain_weight": 32, "minutes": 6,
        "requires": ["actuation:INJ_CUTOUT"],
        "consent_required": True,
    },
}

# AUTO tasks always scheduled (doc 12 §1) — they run without the human.
AUTO_TASKS = [
    {"task_id": "auto_full_scan", "class": "scan",
     "title_fr": "Scan complet toutes ECUs (codes, trames, Mode 06, readiness, VIN)"},
    {"task_id": "auto_window_mining", "class": "watch",
     "title_fr": "Analyse fenêtrée des données roulantes (trims à vide vs 2000–3000 tr/min)"},
    {"task_id": "auto_warmup_watch", "class": "watch",
     "title_fr": "Surveillance de la courbe de montée en température"},
    {"task_id": "auto_o2_watch", "class": "watch",
     "title_fr": "Analyse du taux de commutation de la sonde O2"},
    {"task_id": "auto_rerank", "class": "rerank",
     "title_fr": "Re-classement des hypothèses à chaque nouvelle preuve"},
    {"task_id": "auto_report_draft", "class": "report",
     "title_fr": "Préparation du dossier (rapport partiel à tout moment)"},
]


def visible_cards(capabilities: dict | CapabilityContract, ranking: dict,
                   performed: set[str] | None = None, vehicle: dict | None = None) -> list[dict]:
    """The NEEDS-YOU queue, filtered by the capability contract (doc 14 §4)
    and by the vehicle's fuel (petrol-only cards never appear on a diesel
    and vice-versa — a dCi has no ignition coils), ordered by expected
    information gain (v1 heuristic, doc 17 gap #10).
    Cards already performed this session drop out of the queue."""
    performed = performed or set()
    if isinstance(capabilities, dict):
        contract = CapabilityContract(producer=capabilities.get("producer", "?"),
                                      actuation=capabilities.get("actuation", []),
                                      enhanced_pids=capabilities.get("enhanced_pids", []),
                                      modes=capabilities.get("modes", []),
                                      max_poll_hz=capabilities.get("max_poll_hz", 4))
    else:
        contract = capabilities

    rows = ranking.get("ranking", [])
    fuel = str((vehicle or {}).get("fuel", "")).lower()
    top_causes = {r["cause"]: r for r in rows[:6]}
    cards: list[dict] = []
    for card_id, card in CARD_CATALOG.items():
        if card["kind"] != "NEEDS_YOU" or card_id in performed:
            continue
        if fuel and card.get("fuel") and fuel not in card["fuel"]:
            continue  # wrong fuel — a diesel has no bobines/bougies d'allumage
        if any(not contract.supports(req) for req in card.get("requires", [])):
            continue  # Tier C card on a read-only adapter — by construction, hidden
        # info-gain v1: card weight × how much the targeted causes matter now
        relevance = 0.0
        for cause_id in card.get("targets", []):
            row = top_causes.get(cause_id)
            if row:
                relevance = max(relevance, row["share"] * 100)
        if relevance <= 0:
            continue
        score = card["gain_weight"] * (0.4 + relevance / 100)
        cards.append({"task_id": card_id, **card, "gain_score": round(score, 1),
                      "status": "queued"})
    cards.sort(key=lambda c: -c["gain_score"])
    return cards[:MAX_VISIBLE_CARDS]


def nba_card(cards: list[dict]) -> dict | None:
    """The Next Best Action: exactly one prominent card (doc 12)."""
    for c in cards:
        if c.get("status") == "queued":
            return c
    return None
