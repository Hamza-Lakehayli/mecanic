"""Cross-guard: the bundled YAML-subset loader must agree with PyYAML on
every YAML file in the repo (playbooks, scenarios, data) — the zero-dependency
fallback never drifts from the primary parser."""

from pathlib import Path

import pytest
import yaml

from mecanic.yamlmini import loads

APP_ROOT = Path(__file__).resolve().parent.parent

ALL_YAML = sorted(list((APP_ROOT / "patterns").glob("*.yaml"))
                  + list((APP_ROOT / "scenarios").glob("*.yaml"))
                  + list((APP_ROOT / "data").glob("*.yaml")))


@pytest.mark.parametrize("path", ALL_YAML, ids=[str(p.name) for p in ALL_YAML])
def test_yamlmini_matches_pyyaml(path):
    text = path.read_text(encoding="utf-8")
    ours = loads(text)
    theirs = yaml.safe_load(text)
    assert ours == theirs


def test_yamlmini_scalar_types():
    doc = 'i: 3\nf: 2.5\nb: true\ns: "quoted: yes"\nn: null\n'
    assert loads(doc) == {"i": 3, "f": 2.5, "b": True, "s": "quoted: yes", "n": None}


def test_yamlmini_nested_structures():
    doc = (
        "a:\n"
        "  - id: x\n"
        "    prior: 0.3\n"
        "    tags: [one, two]\n"
        "  - plain\n"
        "b:\n"
        "  c:\n"
        "    d: 1\n"
        "e: {x: 1, y: 'z'}\n"
    )
    assert loads(doc) == {
        "a": [{"id": "x", "prior": 0.3, "tags": ["one", "two"]}, "plain"],
        "b": {"c": {"d": 1}},
        "e": {"x": 1, "y": "z"},
    }
