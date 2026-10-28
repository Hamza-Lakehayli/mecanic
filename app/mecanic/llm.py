"""The LLM layer (docs 16 L4 / 17 §4.1) — optional by construction.

Locked starting choice: NVIDIA-hosted Nemotron 3 Ultra 550B (A55B) as the
floor-test baseline. If the pipeline reasons well on it, every future model
upgrade is pure margin; if it can't, no frontier model will rescue a broken
pipeline. Cheap-per-token protects the 49–99 MAD/dossier economics.

Design rules it obeys:
  * never reads the stream — only the digest (+ drill-down tools, doc 16);
  * free text (FR/Darija) and photos are its *only* inputs of record;
  * offline -> deterministic fallback, calls queue, nothing blocks (doc 12);
  * its output merges into the same schema (chips/priors), never a special
    path (doc 12 §3 "two paths, one ledger").
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"  # floor-test baseline (doc 17 §4.1)
NVIDIA_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file() -> None:
    """Load KEY=VALUE lines from app/.env (gitignored) into os.environ if the
    variables aren't already set. Stdlib-only secrets handling: keys live in
    the environment or .env, never in code."""
    if _ENV_FILE.exists():
        try:
            for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            pass


_load_env_file()


class NemotronClient:
    """Minimal OpenAI-compatible client for the NVIDIA API (stdlib only).

    Nemotron 3 Ultra returns a separate `reasoning_content` field — the
    final answer lands in `content`, so JSON extraction stays clean. The
    first request can cold-start >30 s on the server; warm turns are ~1 s
    (measured live, §4.1 latency budget: INTERACTIVE turns <3 s holds warm).
    """

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 timeout: float = 90.0):
        # api_key=None -> env/.env lookup; api_key="" -> FORCED OFFLINE
        # (an explicit empty string must not fall through to the env key —
        # that made "offline" tests silently live)
        if api_key is None:
            api_key = os.environ.get("MECANIC_NVIDIA_API_KEY") or os.environ.get("NVIDIA_API_KEY")
        self.api_key = api_key
        self.model = model or os.environ.get("MECANIC_LLM_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self.last_usage: dict = {}
        self.last_latency: float = 0.0

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict], temperature: float = 0.2) -> str | None:
        if not self.available:
            return None
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            # 4000: reasoning models spend tokens on thinking BEFORE the
            # answer — a 1500 cap truncated Lightning's extraction mid-reasoning
            # (empty content, the §4.1 probe caught it; only the fallback ran)
            "max_tokens": 4000,
        }).encode("utf-8")
        req = urllib.request.Request(
            NVIDIA_CHAT_URL, data=body,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        for attempt in (0, 1):  # one retry — cold starts & transient 5xx
            try:
                t0 = time.monotonic()
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.last_latency = time.monotonic() - t0
                self.last_usage = payload.get("usage", {}) or {}
                return payload["choices"][0]["message"]["content"]
            except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError) as e:
                if attempt == 1:
                    return None  # offline/bad response -> deterministic fallback (doc 12)
                time.sleep(1.0)
        return None

    # -- intake extraction: FR/Darija free text -> chips ------------------------

    def extract_complaint(self, text: str, symptom_chips: list[str],
                          condition_chips: list[str],
                          gloss: dict[str, str] | None = None,
                          mechanical_chips: list[str] | None = None) -> dict | None:
        """Free text (French or Darija, any script — the real garage register
        is FR+Darija MIXED IN ONE SENTENCE: "la porte dyal tomobil makat7elsh")
        -> structured chips. Returns None when unavailable — the caller uses
        the lexicon fallback.

        `gloss` maps chip id -> short FR/Darija description. Bare ids are not
        enough for Darija ("machi 9wa" != "power_loss" without a hint) — the
        measured fix for gap #4: the prompt must explain each chip.
        `mechanical_chips` is the route-out bank (doc 00 scope honesty):
        body/mechanical complaints map there, never to engine symptoms."""
        mechanical_chips = mechanical_chips or []

        def describe(chips):
            out = []
            for c in chips:
                if gloss and c in gloss:
                    out.append(f"{c} ({gloss[c]})")
                else:
                    out.append(c)
            return ", ".join(out)

        system = (
            "Tu es le module d'extraction d'un agent de diagnostic automobile marocain. "
            "Extrais de la plainte (français et/ou darija, souvent MELANGÉS dans la même "
            "phrase, script latin ou arabe) les chips valides et renvoie UNIQUEMENT du "
            "JSON: "
            '{"chips": [], "condition_chips": [], "mechanical_chips": [], '
            '"severity": "info|warn|danger", "language": "fr|darija|mix", "notes": "..."}. '
            "Chips symptômes valides: " + describe(symptom_chips) + ". "
            "Chips conditions valides: " + describe(condition_chips) + ". "
            + ("Chips mécaniques/carrosserie valides (symptômes NON détectables par les "
               "données — portes, freins, embrayage, suspension, direction): "
               + describe(mechanical_chips) + ". "
               if mechanical_chips else "")
            + "Exemples darija: 'msha do/daw men l motor' = le voyant moteur s'est allumé "
            "(chip mil_on); 'machi 9wa' = manque de puissance; 'dkhan k7el' = fumée noire; "
            "'3end ralenti' = au ralenti; 'f cote' = en côte; 'makat7elsh' = ne s'ouvre pas; "
            "'dyal' = 'de/du' possessif. "
            "Ne devine jamais une chip non listée. Un problème de porte/frein/embrayage va "
            "dans mechanical_chips, JAMAIS dans chips. Si aucune chip ne couvre, laisse "
            "vide et explique dans notes."
        )
        content = self.chat([{"role": "system", "content": system},
                             {"role": "user", "content": text}])
        if not content:
            return None
        try:
            parsed = json.loads(content)
        except ValueError:
            start, end = content.find("{"), content.rfind("}")
            if start < 0 or end <= start:
                return None
            try:
                parsed = json.loads(content[start:end + 1])
            except ValueError:
                return None
        chips = [c for c in parsed.get("chips", []) if c in symptom_chips]
        conds = [c for c in parsed.get("condition_chips", []) if c in condition_chips]
        mech = [c for c in parsed.get("mechanical_chips", []) if c in mechanical_chips]
        return {"chips": chips, "condition_chips": conds, "mechanical_chips": mech,
                "severity": parsed.get("severity", "info"),
                "language": parsed.get("language", "?"),
                "notes": parsed.get("notes", "")}
