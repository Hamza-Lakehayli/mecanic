"""The app surface (CLI for now — the mobile UI comes later; the pipeline is
UI-agnostic and this exercises the same 9-step loop).

  mecanic run --scenario scenarios/p0171_vacuum_leak.yaml [--complaint "..."]
  mecanic benchmark [--dir scenarios]          # doc 17 §4.1 CI: agreement >= 80%
  mecanic replay <session.jsonl>               # free regression (doc 14 §6.2)
  mecanic outcome --session <id> [--part ...]   # the outcome form (flywheel)
  mecanic connect tcp://127.0.0.1:35000        # real ELM327-emulator / adapter
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .emulator import SCENARIOS_DIR, load_scenario, open_scenario_transport
from .ledger import SESSIONS_DIR, new_session, replay_file
from .session import Session


def _print_dashboard(state, ranking, cards, nba, result) -> None:
    print("\n" + "=" * 72)
    print("RETOUR AU VÉHICULE — dossier", state.session_id)
    print("=" * 72)
    verdict = result["verdict"]
    icon = {"verdict": "✅ VERDICT", "ranking": "🟡 CLASSEMENT", "escalate": "⚪ À ÉCLAIRCIR"}[verdict["status"]]
    print(f"\n{icon}  {verdict['statement']}")
    if verdict.get("confidence"):
        print(f"   confiance {verdict['confidence']}%")
    for c in verdict.get("citations", [])[:6]:
        print(f"   [{c['class']}] {c['text']}")
    if nba:
        label = "▶ NBA" if verdict["status"] != "verdict" else "▶ CONFIRMATION (optionnel)"
        print(f"\n{label} ({nba.get('minutes', '?')} min): {nba['title_fr']}")
        print(f"   {' / '.join(nba.get('steps_fr', [])[:2])}")
    if cards:
        print("\nFile de tests (jamais bloquante):")
        for c in cards[:5]:
            print(f"   - {c['title_fr']}  [gain {c['gain_score']}]")
    for f in state.triage_findings:
        mark = {"danger": "🔴", "warn": "🟠", "info": "🔵"}.get(f.get("severity", "info"), "·")
        print(f"\n{mark} {f['title']}")
        print(f"   {f['detail']}")
    if result.get("trends"):
        print("\nMémoire VIN (tendances):")
        for t in result["trends"][:3]:
            print(f"   - {t}")
    print(f"\nRapport complet: {result['report_path']}")
    print(f"Registre preuves: {SESSIONS_DIR / (state.session_id + '.jsonl')}")


def cmd_run(args) -> int:
    scenario = load_scenario(args.scenario)
    transport = open_scenario_transport(args.scenario, seed=args.seed)
    ledger = new_session(args.session_id)
    session = Session(ledger, transport, scenario, mode=args.mode)
    complaint = args.complaint or (scenario.get("complaint", {}) or {}).get("text")
    chips = args.chips or (scenario.get("complaint", {}) or {}).get("chips", [])
    session.quick_start(text=complaint, chips=chips,
                        recent_work=(scenario.get("complaint", {}) or {}).get("recent_work"))
    print(f"Session {ledger.session_id} — branché, l'agent démarre (mode {session.mode}).")
    session.connect_and_intake()
    n = session.live_watch(poll_dt=args.poll_dt)
    state, ranking, cards, nba, digest = session.analyze()
    if args.simulate is None:
        args.simulate = scenario.get("simulate", [])
    if args.simulate:
        session.simulate_steps(args.simulate)
        state, ranking, cards, nba, digest = session.analyze()
    result = session.conclude(state, ranking, cards, nba, digest)
    _print_dashboard(state, ranking, cards, nba, result)
    ledger.close()
    return 0


def cmd_benchmark(args) -> int:
    """Doc 17 §4.1: replay every labeled scenario; verdict agreement target
    >= 80%; token budget check (<10K/dossier). Exit code 1 below target.

    Deterministic by default (hermetic CI). Pass --llm to run the full §4.1
    matrix with the Nemotron floor-test in the loop (live API calls: the
    complaint text goes through LLM extraction instead of the lexicon)."""
    from .funnel import token_estimate
    from .ledger import new_session as _new_session
    from .llm import NemotronClient

    directory = Path(args.dir)
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        print(f"no scenarios in {directory}")
        return 1
    agree = 0
    rows = []
    # offline client unless --llm: the pipeline must not depend on the LLM
    llm = NemotronClient() if args.llm else NemotronClient(api_key="")
    for p in paths:
        scenario = load_scenario(p)
        transport = open_scenario_transport(p)
        ledger = _new_session(persist=False)
        session = Session(ledger, transport, scenario, llm=llm)
        complaint = (scenario.get("complaint", {}) or {}).get("text")
        chips = (scenario.get("complaint", {}) or {}).get("chips", [])
        session.quick_start(text=complaint, chips=chips,
                            recent_work=(scenario.get("complaint", {}) or {}).get("recent_work"))
        session.connect_and_intake()
        session.live_watch()
        state, ranking, cards, nba, digest = session.analyze()
        session.simulate_steps(scenario.get("simulate", []))
        if scenario.get("simulate"):
            state, ranking, cards, nba, digest = session.analyze()
        result = session.conclude(state, ranking, cards, nba, digest)
        ledger.close()

        expected = scenario.get("expected", {}) or {}
        exp_cause = expected.get("root_cause")
        top = ranking["ranking"][0]["cause"] if ranking["ranking"] else None
        conf = ranking["ranking"][0]["confidence"] if ranking["ranking"] else 0
        if exp_cause is None:
            # no expected cause: the agent must NOT guess a verdict (docs 02 §7,
            # 12) — pass iff no verdict was produced.
            ok_cause = ranking["status"] != "verdict"
        else:
            ok_cause = top == exp_cause
        ok_conf = (not expected.get("min_confidence")) or conf >= expected["min_confidence"]
        exp_status = expected.get("status")
        ok_status = (not exp_status) or ranking["status"] == exp_status
        flag_rules = {f["rule"] for f in ledger.fold().flags}
        ok_flags = all(mf in flag_rules for mf in expected.get("must_flags", []))
        finding_ids = {f["id"] for f in state.triage_findings}
        ok_triage = all(t in finding_ids for t in expected.get("triage_contains", []))
        tokens = token_estimate(digest)
        ok_tokens = tokens < 10000
        passed = ok_cause and ok_conf and ok_status and ok_flags and ok_triage and ok_tokens
        agree += int(ok_cause)
        rows.append((p.stem, exp_cause, top, conf, ranking["status"], tokens,
                     "PASS" if passed else "FAIL",
                     [] if passed else [x for x, ok in
                                        zip(("cause", "conf", "status", "flags", "triage", "tokens"),
                                            (ok_cause, ok_conf, ok_status, ok_flags, ok_triage, ok_tokens))
                                        if not ok]))
    print(f"{'scénario':28} {'attendu':22} {'prédit':22} {'conf':>4} {'état':9} {'tok':>5}  résultat")
    for r in rows:
        print(f"{r[0]:28} {str(r[1]):22} {str(r[2]):22} {r[3]:>4} {r[4]:9} {r[5]:>5}  {r[6]}"
              + (f"  ({','.join(r[7])})" if r[7] else ""))
    rate = agree / len(rows) * 100
    print(f"\nAccord verdict (top-1): {agree}/{len(rows)} = {rate:.0f}%  (cible doc 17 §4.1: ≥80%)")
    return 0 if rate >= 80 and all(r[6] == "PASS" for r in rows) else 1


def cmd_replay(args) -> int:
    state = replay_file(args.path)
    print(f"replayed {args.path}: {len(state.samples)} échantillons, "
          f"{len(state.dtcs)} codes, {len(state.triage_findings)} constats, "
          f"verdict = {(state.verdict or {}).get('cause')}")
    return 0


def cmd_outcome(args) -> int:
    from .record import record_outcome

    path = SESSIONS_DIR / f"{args.session}.jsonl"
    if not path.exists():
        print(f"session introuvable: {path}")
        return 1
    state = replay_file(path)
    rec = record_outcome(args.session, state.vin,
                         {"fix_part": args.part or "?", "fix_action": args.action or "replace",
                          "verified": args.verified, "comeback": args.comeback,
                          "note": args.note or ""})
    print("outcome enregistré (la moulinette apprend):", rec)
    return 0


def cmd_can(args) -> int:
    """CAN frames -> bus survey (+ optional DBC decode) — doc 17 §4.5.
    Listen-only by construction: reads frames, emits events, never sends."""
    from .canproducer import decode_frames, parse_can_csv, survey

    frames = parse_can_csv(args.path)
    ids = {f[1] for f in frames}
    print(f"{len(frames)} trames, {len(ids)} IDs")
    if args.survey_only or not args.dbc:
        for s in survey(frames)[:20]:
            kinds = [b["kind"] for b in s["bytes"]]
            print(f"  {s['id']:>6} ×{s['frames']:>6}  {s['mean_period_ms']} ms"
                  f"  octets: {kinds}")
    if args.dbc:
        events = decode_frames(frames, args.dbc)
        print(f"\ndecode ({Path(args.dbc).name}): {len(events)} événements signaux")
        by_name = {}
        for e in events:
            by_name.setdefault(e["name"], []).append(e["value"])
        for name, vals in list(by_name.items())[:12]:
            print(f"  {name}: min={min(vals):.2f} max={max(vals):.2f} n={len(vals)}")
    return 0


def cmd_soak(args) -> int:
    """Doc 17 §4.2: run the funnel over an entire public dataset (VED) —
    false-flag rate, threshold calibration vs the real parc, flagged-trip
    review table."""
    from .soak import render_soak_report, soak

    p = Path(args.path[0] if isinstance(args.path, list) else args.path)
    raw_paths = args.path if isinstance(args.path, list) else [args.path]
    paths: list[Path] = []
    for rp in raw_paths:
        rp_path = Path(rp)
        paths.extend(sorted(rp_path.glob("VED_*.csv")) if rp_path.is_dir() else [rp_path])
    base = Path(raw_paths[0])
    base = base if base.is_dir() else base.parent
    statics = [Path(args.static)] if args.static else sorted(base.glob("*.xlsx"))

    if args.diagnose:
        from .soak import diagnose_vehicle
        results = diagnose_vehicle(paths, args.diagnose, static_files=statics,
                                   max_trips=args.max_trips)
        if not results:
            print(f"aucun trajet trouvé pour le véhicule {args.diagnose}")
            return 1
        veh = results[0].get("vehicle") or {}
        print(f"\n=== Diagnostic réel — véhicule {args.diagnose} "
              f"({veh.get('vtype', '?')}, {veh.get('displacement_l', '?')} L) ===")
        for r in results:
            print(f"\n--- Trajet {r['key']} ({r['duration_s']} s) — flags: {r['flags']}")
            for f in r["findings"]:
                print(f"  [{'danger' if f['severity'] == 'danger' else 'info'}] {f['title']}")
            ranking = r["ranking"]
            print(f"  Statut: {ranking['status']} — playbooks actifs: "
                  f"{ranking.get('active_playbooks', [])}")
            for row in ranking["ranking"][:3]:
                print(f"  {row['rank']}. {row['label']} — confiance "
                      f"{row['confidence']}% (playbook {row['playbook']})")
        return 0

    agg = soak(paths, static_files=statics, limit=args.limit)
    md = render_soak_report(agg, paths)
    print(md)
    if args.out:
        out = Path(args.out)
        out.write_text(md, encoding="utf-8")
        print(f"\nRapport sauvegardé: {out}")
    return 0


def cmd_confiance(args) -> int:
    """Rapport Confiance — the buyer-facing pre-purchase report (doc 07 wedge
    #1), rendered from a stored session ledger (bring-your-own-evidence:
    the report is derived, never hand-written)."""
    from .ledger import SESSIONS_DIR, replay_file
    from .report import render_confiance_report, save_report

    path = Path(args.session)
    if not path.exists():
        path = SESSIONS_DIR / f"{args.session}.jsonl"
    if not path.exists():
        print(f"session introuvable: {args.session}")
        return 1
    state = replay_file(path)
    markdown = render_confiance_report(state)
    out = save_report(state.session_id or path.stem, markdown,
                      path.parent if str(path.parent) else SESSIONS_DIR,
                      suffix=".confiance.md")
    print(markdown)
    print(f"\nRapport sauvegardé: {out}")
    return 0


def cmd_connect(args) -> int:
    """Live intake against a real ELM327-emulator server or WiFi adapter."""
    from .elm327 import ELM327Driver, open_transport

    transport = open_transport(args.spec)
    ledger = new_session()
    session = Session(ledger, transport)
    session.quick_start(text=args.complaint)
    session.driver = ELM327Driver(transport)
    session.connect_and_intake()
    state = ledger.fold()
    print(f"VIN {state.vin} · codes: {state.dtc_codes()} · readiness: "
          f"{(state.readiness or {}).get('monitors')}")
    ledger.close()
    return 0


def main(argv=None) -> int:
    # Windows consoles default to legacy codepages — force UTF-8 with
    # graceful replacement so French/Darija output never crashes the app.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="mecanic",
                                 description="Mecanic Study — agent de diagnostic véhicule (doc 00)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="dérouler la boucle 9 étapes sur un scénario")
    p_run.add_argument("--scenario", required=True)
    p_run.add_argument("--complaint", help="plainte une ligne (FR/Darija)")
    p_run.add_argument("--chips", nargs="*", help="chips symptômes")
    p_run.add_argument("--mode", default="AUTO", choices=["AUTO", "INTERACTIVE"])
    p_run.add_argument("--session-id", default=None)
    p_run.add_argument("--poll-dt", type=float, default=1.0)
    p_run.add_argument("--seed", type=int, default=42)
    p_run.add_argument("--simulate", nargs="*", default=None,
                       help="cards to simulate (default: scenario 'simulate' block)")
    p_run.set_defaults(fn=cmd_run)

    p_bm = sub.add_parser("benchmark", help="CI: replay des scénarios étiquetés (§4.1)")
    p_bm.add_argument("--dir", default=str(SCENARIOS_DIR))
    p_bm.add_argument("--llm", action="store_true",
                      help="Nemotron floor-test in the loop (live API; default: offline deterministic)")
    p_bm.set_defaults(fn=cmd_benchmark)

    p_rp = sub.add_parser("replay", help="replid un registre JSONL (doc 14 §6.2)")
    p_rp.add_argument("path")
    p_rp.set_defaults(fn=cmd_replay)

    p_oc = sub.add_parser("outcome", help="enregistrer l'issue (la moulinette)")
    p_oc.add_argument("--session", required=True)
    p_oc.add_argument("--part")
    p_oc.add_argument("--action", choices=["replace", "clean", "repair", "adjust", "none"])
    p_oc.add_argument("--verified", default="yes", choices=["yes", "no", "unknown"])
    p_oc.add_argument("--comeback", default="no", choices=["yes", "no"])
    p_oc.add_argument("--note", default="")
    p_oc.set_defaults(fn=cmd_outcome)

    p_can = sub.add_parser("can", help="trames CAN -> survey bus (+ décode DBC) — doc 17 §4.5, écoute seule")
    p_can.add_argument("path", help="CSV de trames (timestamp, id, data)")
    p_can.add_argument("--dbc", default=None, help="DBC (opendbc vendored dans data/dbc/)")
    p_can.add_argument("--survey-only", action="store_true")
    p_can.set_defaults(fn=cmd_can)

    p_sk = sub.add_parser("soak", help="soak du funnel sur un dataset public (VED, doc 17 §4.2)")
    p_sk.add_argument("path", nargs="+", help="fichier(s) VED_*.csv ou dossier")
    p_sk.add_argument("--limit", type=int, default=None, help="limite de trajets (smoke run)")
    p_sk.add_argument("--static", default=None, help="xlsx statique (auto-détecté sinon)")
    p_sk.add_argument("--out", default=None, help="chemin du rapport markdown")
    p_sk.add_argument("--diagnose", default=None,
                      help="diagnostic complet des trajets d'un véhicule (ex: 249)")
    p_sk.add_argument("--max-trips", type=int, default=6,
                      help="nb de trajets à diagnostiquer (avec --diagnose)")
    p_sk.set_defaults(fn=cmd_soak)

    p_cf = sub.add_parser("confiance", help="rapport acheteur (Rapport Confiance) depuis un registre de session")
    p_cf.add_argument("session", help="session id ou chemin .jsonl")
    p_cf.set_defaults(fn=cmd_confiance)

    p_ct = sub.add_parser("connect", help="connexion live (ELM327-emulator TCP ou adaptateur)")
    p_ct.add_argument("--spec", default="tcp://127.0.0.1:35000")
    p_ct.add_argument("--complaint", default="")
    p_ct.set_defaults(fn=cmd_connect)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
