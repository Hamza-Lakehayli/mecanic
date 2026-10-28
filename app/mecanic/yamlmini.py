"""Strict YAML-subset loader.

Used only when PyYAML is not installed (the package has zero hard
dependencies by design — offline-first). Supports exactly the subset the
repo's own YAML files use:

    # comments
    key: value            # scalars: int / float / bool / str ("quoted ok")
    key:                  # nested block map or seq, indented deeper
      - item
      - key: value        # seq of maps
    key: [a, b, c]        # inline list of scalars
    key: {a: 1, b: 2}     # inline map of scalars

Not supported (deliberately): anchors, multi-doc, multiline strings, tabs.
All repo YAML is authored against this subset, and PyYAML parses it too.
"""

from __future__ import annotations


class YamlMiniError(ValueError):
    pass


def _scalar(text: str):
    t = text.strip()
    if t == "":
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    # PyYAML-parity null set: ~ and null (case-insensitive) — deliberately NOT
    # "none", which PyYAML keeps as the string "None" (parity is the contract;
    # the yamlmini tests enforce it).
    if low in ("null", "~"):
        return None
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _split_inline(inner: str):
    """Split on commas, respecting quotes AND nested brackets/braces."""
    parts, buf, quote, depth = [], "", None, 0
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch in "[{":
            depth += 1
            buf += ch
        elif ch in "]}":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return [p for p in parts if p]


def _inline(text: str):
    """Parse an inline [..] list or {..} map of scalars, else None."""
    t = text.strip()
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        return [] if not inner else [_scalar_or_inline(p) for p in _split_inline(inner)]
    if t.startswith("{") and t.endswith("}"):
        inner = t[1:-1].strip()
        out = {}
        if not inner:
            return out
        for part in _split_inline(inner):
            if ":" not in part:
                raise YamlMiniError(f"inline map entry missing ':': {part!r}")
            k, v = part.split(":", 1)
            out[k.strip().strip("\"'")] = _scalar_or_inline(v)  # recursive
        return out
    return None


def _scalar_or_inline(text: str):
    v = _inline(text)
    return _scalar(text) if v is None else v


def _strip_comment(line: str) -> str:
    out, quote = "", None
    for ch in line:
        if quote:
            out += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out += ch
        elif ch == "#":
            break
        else:
            out += ch
    return out.rstrip()


def _split_key(txt: str) -> tuple[str, str] | None:
    """Split 'key: value' — the key may itself contain ':' (e.g.
    'fuel_pressure_rail:BAD'), so split at the first ': ' or a trailing
    ':'. Returns (key, rest) with key quotes stripped, or None."""
    idx = txt.find(": ")
    if idx >= 0:
        key, rest = txt[:idx], txt[idx + 2:]
    elif txt.endswith(":"):
        key, rest = txt[:-1], ""
    else:
        return None
    return key.strip().strip("\"'"), rest.strip()


def loads(text: str):
    """Parse the YAML subset into Python objects."""
    lines = []
    for raw in text.splitlines():
        line = _strip_comment(raw.rstrip("\n"))
        if not line.strip():
            continue
        if "\t" in line:
            raise YamlMiniError("tabs not allowed in this YAML subset")
        lines.append((len(line) - len(line.lstrip(" ")), line.strip()))
    pos = [0]

    def parse_block(indent: int):
        if pos[0] >= len(lines):
            return None
        ind, txt = lines[pos[0]]
        if txt.startswith("- "):
            return parse_seq(ind)
        if txt[0] in "[{":
            # a flow map/list written on its own line (value of the key above)
            pos[0] += 1
            return _scalar_or_inline(txt)
        if ind != indent:
            raise YamlMiniError(f"unexpected indent: {txt!r}")
        return parse_map(indent)

    def parse_seq(indent: int):
        seq = []
        while pos[0] < len(lines):
            ind, txt = lines[pos[0]]
            if ind != indent or not txt.startswith("- "):
                break
            rest = txt[2:].strip()
            pos[0] += 1
            if rest:
                if rest[0] in "[{" or ":" not in rest or (rest[0] in "\"'" and rest[-1] == rest[0]):
                    # plain scalar item, inline list, or inline map
                    seq.append(_scalar_or_inline(rest))
                else:
                    # "- key: value" — start of a map item; deeper keys follow
                    kv = _split_key(rest)
                    if kv is None:
                        seq.append(_scalar_or_inline(rest))
                        continue
                    item = {kv[0]: _scalar_or_inline(kv[1]) if kv[1] else None}
                    seq.append(_map_item(item, indent + 2))
            else:
                if pos[0] < len(lines) and lines[pos[0]][0] > indent:
                    seq.append(parse_block(lines[pos[0]][0]))
                else:
                    seq.append(None)
        return seq

    def _map_item(item: dict, key_indent: int):
        """Continue a seq-item map with keys at `key_indent`."""
        while pos[0] < len(lines):
            ind, txt = lines[pos[0]]
            if ind != key_indent or txt.startswith("- "):
                break
            kv = _split_key(txt)
            if kv is None:
                raise YamlMiniError(f"expected 'key: value', got {txt!r}")
            key, rest2 = kv
            pos[0] += 1
            if rest2:
                item[key] = _scalar_or_inline(rest2)
            elif pos[0] < len(lines) and lines[pos[0]][0] > ind:
                item[key] = parse_block(lines[pos[0]][0])
            else:
                item[key] = None
        return item

    def parse_map(indent: int):
        mapping = {}
        while pos[0] < len(lines):
            ind, txt = lines[pos[0]]
            if ind < indent:
                break
            if ind > indent:
                raise YamlMiniError(f"unexpected indent: {txt!r}")
            kv = _split_key(txt)
            if kv is None:
                raise YamlMiniError(f"expected 'key: value', got {txt!r}")
            key, rest = kv
            pos[0] += 1
            if rest:
                mapping[key] = _scalar_or_inline(rest)
            elif pos[0] < len(lines) and lines[pos[0]][0] > indent:
                mapping[key.strip()] = parse_block(lines[pos[0]][0])
            elif pos[0] < len(lines) and lines[pos[0]][0] == indent and lines[pos[0]][1].startswith("- "):
                # seq at the same indent as its key is invalid in our subset
                raise YamlMiniError(f"sequence under key {key!r} must be indented")
            else:
                mapping[key.strip()] = None
        return mapping

    return parse_block(0)


def load_file(path: str):
    """Load YAML from a file — PyYAML if available, else the subset loader."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        return loads(text)
