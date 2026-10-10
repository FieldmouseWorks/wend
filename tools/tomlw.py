"""A TOML writer just big enough for wend files (fixes, routes, boxes). tomllib reads; this writes."""
import json
import re


def tval(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        if "\n" in v and "'''" not in v:
            return "'''\n" + v + "'''"
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(map(tval, v)) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{tkey(k)} = {tval(x)}" for k, x in v.items()) + " }"
    raise TypeError(v)


def tkey(k):
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def toml(doc, header=""):
    out = [header] if header else []
    tables, arrays = {}, {}
    for k, v in doc.items():
        if isinstance(v, dict) and k in ("meta", "roles", "debconf"):
            tables[k] = v
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v) and k in ("pre", "post", "fixes"):
            arrays[k] = v
        else:
            out.append(f"{tkey(k)} = {tval(v)}")
    for k, rows in arrays.items():
        for r in rows:
            out.append(f"\n[[{k}]]")
            out += [f"{tkey(a)} = {tval(b)}" for a, b in r.items()]
    for k, t in tables.items():
        out.append(f"\n[{k}]")
        out += [f"{tkey(a)} = {tval(b)}" for a, b in t.items()]
    return "\n".join(out) + "\n"
