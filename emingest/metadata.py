# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Metadata of a TEM data set for NXem (pynxtools) and the NOMAD ELN.

The fields follow the NXem application definition (NeXus, as shipped with pynxtools) and the keys of the NOMAD Oasis
ELN file ``eln_data.yaml`` that the ``em`` reader of pynxtools-em reads:

=========================  ==========================================  ============
eln_data.yaml              NXem                                        in NXem
=========================  ==========================================  ============
entry/start_time           /ENTRY/start_time                           required
entry/end_time             /ENTRY/end_time                             recommended
entry/experiment_alias     /ENTRY/experiment_alias                     optional
entry/experiment_description /ENTRY/experiment_description             optional
project/name               /ENTRY/project/name                         optional
sample/is_simulation       /ENTRY/sample/is_simulation                 required
sample/preparation_date    /ENTRY/sample/preparation_date              required
sample/chemical_formula | atom_types  /ENTRY/sample/atom_types         required
sample/name                /ENTRY/sample/name                          recommended
sample/thickness           /ENTRY/sample/thickness                     optional
user[]/name, affiliation, address, email, telephone_number, role, orcid   /ENTRY/userID (identifier = ORCID)
instrument/vendor, model   /ENTRY/measurement/instrument/fabrication   required (with measurement data)
instrument/name, type      /ENTRY/measurement/instrument               recommended
instrument/emitter_type    .../ebeam_column/electron_source            recommended
=========================  ==========================================  ============

The keys below ``instrument`` and the sample fields ``identifier_sample``, ``physical_form`` and ``description``
are not read by pynxtools-em; the ``tem`` reader of this package (:mod:`emingest.nx_reader`) adds them to NXem
(``/ENTRY/measurement/instrument``, ``/ENTRY/sample``).  The HTML of the two rich-text fields goes to the NOMAD ELN
as it is and to NXem as plain text.
"""
from __future__ import annotations

import copy
import datetime as dt
import re

import yaml

from . import richtext

REQUIRED, RECOMMENDED, OPTIONAL = "required", "recommended", "optional"

# (section, key, label, level, type, help) -- type: str | datetime | date | bool | float | unit | choice:<a|b> | html
FIELDS = [
    ("entry", "experiment_alias", "Title / alias", RECOMMENDED, "str", "short name of the experiment (session)"),
    ("entry", "start_time", "Start", REQUIRED, "datetime", "start of the measurement (ISO 8601, with time zone)"),
    ("entry", "end_time", "End", RECOMMENDED, "datetime", "end of the measurement"),
    ("entry", "identifier_experiment", "Experiment ID", OPTIONAL, "str", "e.g. the ID in your lab book / ELN"),
    ("project", "name", "Project", OPTIONAL, "str", "project the data belong to"),
    ("sample", "name", "Sample name", RECOMMENDED, "str", ""),
    ("sample", "identifier_sample", "Sample ID", RECOMMENDED, "str", "ID of the sample (lab ID, sample database)"),
    ("sample", "identifier_owner", "Owner's sample ID", OPTIONAL, "str", "the ID given by the owner of the sample"),
    ("sample", "identifier_parent", "Parent sample ID", OPTIONAL, "str", "ID of the material / wafer it came from"),
    ("sample", "chemical_formula", "Chemical formula", REQUIRED, "str",
     "Hill notation, e.g. O3SrTi; or give the elements below"),
    ("sample", "atom_types", "Elements", REQUIRED, "str", "comma-separated, e.g. Sr, Ti, O (if no formula)"),
    ("sample", "is_simulation", "Simulated data", REQUIRED, "bool", "the data are a simulation, not a measurement"),
    ("sample", "preparation_date", "Preparation date", REQUIRED, "datetime", "when the specimen was prepared"),
    ("sample", "physical_form", "Physical form", RECOMMENDED,
     "choice:|bulk|foil|thin_film|powder|lamella|cross-section lamella|plan-view lamella|nanoparticles|flake|"
     "other", "NXem suggests bulk, foil, thin_film or powder (other values are allowed)"),
    ("sample", "thickness", "Thickness", OPTIONAL, "float", "specimen thickness"),
    ("sample", "thickness_unit", "Thickness unit", OPTIONAL, "choice:nm|um|A|m", ""),
    ("instrument", "name", "Microscope", RECOMMENDED, "str", "name of the instrument, e.g. 'TEM 2 (Titan)'"),
    ("instrument", "location", "Location", OPTIONAL, "str", "lab / institute"),
    ("instrument", "type", "Type", RECOMMENDED, "choice:|tem|sem|fib", "kind of microscope (NXem: tem, sem, fib)"),
    ("instrument", "vendor", "Vendor", REQUIRED, "str", "e.g. Thermo Fisher, JEOL, Nion, Hitachi, Zeiss"),
    ("instrument", "model", "Model", REQUIRED, "str", "e.g. Titan Themis, ARM200F, HERMES"),
    ("instrument", "serial_number", "Serial number", OPTIONAL, "str", ""),
    ("instrument", "identifier", "Instrument ID", OPTIONAL, "str", "persistent ID of the microscope (e.g. its "
                                                                   "inventory or eLabFTW resource)"),
    ("instrument", "voltage_kV", "High tension (kV)", RECOMMENDED, "float", "acceleration voltage"),
    ("instrument", "emitter_type", "Electron source", RECOMMENDED,
     "choice:|cold field emitter|Schottky field emitter|thermionic (LaB6)|thermionic (W)|other", ""),
    ("instrument", "operation_mode", "Mode", OPTIONAL,
     "choice:|TEM|STEM|HRTEM|diffraction|4D-STEM|EELS|EDS|holography|tomography|in situ|other", ""),
    ("instrument", "detectors", "Detectors", OPTIONAL, "str", "comma-separated, e.g. HAADF, K3, Merlin"),
]
USER_FIELDS = [("name", "Name", RECOMMENDED), ("affiliation", "Affiliation", OPTIONAL),
               ("email", "E-mail", OPTIONAL), ("orcid", "ORCID", RECOMMENDED), ("role", "Role", OPTIONAL),
               ("address", "Address", OPTIONAL), ("telephone_number", "Telephone", OPTIONAL)]
HTML_FIELDS = [("entry", "experiment_description", "Description of the experiment"),
               ("sample", "history", "Sample: history and preparation")]


def empty():
    m = {"entry": {}, "project": {}, "sample": {"is_simulation": False, "thickness_unit": "nm"}, "instrument": {},
         "users": [{}], "tags": [], "source": {}}
    for sec, key, _l in HTML_FIELDS:
        m[sec][key] = ""
    return m


def now_iso():
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def to_iso(v, end_of_day=False):
    """ISO 8601 with time zone from a date / datetime / string ('' if not parseable)."""
    if v in (None, ""):
        return ""
    if isinstance(v, dt.datetime):
        d = v
    elif isinstance(v, dt.date):
        d = dt.datetime.combine(v, dt.time(23, 59, 59) if end_of_day else dt.time())
    else:
        s = str(v).strip().replace("Z", "+00:00")
        d = None
        for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d", "%d.%m.%Y", "%d.%m.%Y %H:%M"):
            try:
                d = dt.datetime.fromisoformat(s) if fmt is None else dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if d is None:
            return ""
    if d.tzinfo is None:
        d = d.astimezone()
    return d.replace(microsecond=0).isoformat()


ORCID_RE = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])")


def norm_orcid(s):
    m = ORCID_RE.search(str(s or ""))
    return f"https://orcid.org/{m.group(1)}" if m else ""


def _filled(v):
    return v not in (None, "", [], {}) and not (isinstance(v, str) and not v.strip())


def check(meta):
    """(missing required, missing recommended): lists of labels.  The chemical formula and the elements count as one
    field (one of them is needed); a user needs a name."""
    req, rec = [], []
    for sec, key, label, level, typ, _h in FIELDS:
        if level == OPTIONAL or key in ("atom_types",):
            continue
        v = (meta.get(sec) or {}).get(key)
        if key == "chemical_formula":
            if not (_filled(v) or _filled((meta.get("sample") or {}).get("atom_types"))):
                req.append("chemical formula or elements")
            continue
        if typ == "bool":
            continue                                    # always has a value
        if not _filled(v):
            (req if level == REQUIRED else rec).append(label)
    users = [u for u in meta.get("users") or [] if _filled(u.get("name"))]
    if not users:
        rec.append("user name")
    elif not any(_filled(u.get("orcid")) for u in users):
        rec.append("ORCID of a user")
    if not richtext.to_text((meta.get("entry") or {}).get("experiment_description", "")).strip():
        rec.append("description")
    return req, rec


def atom_types_of(meta):
    """Elements of the formula (or of the elements field)."""
    from ase.data import chemical_symbols
    s = meta.get("sample") or {}
    src = " ".join(str(x) for x in [s.get("chemical_formula") or "", s.get("atom_types") or ""]
                   + [c.get("chemical_formula") or "" for c in s.get("components") or [] if isinstance(c, dict)])
    found = []
    for sym in re.findall(r"[A-Z][a-z]?", str(src)):
        if sym in chemical_symbols[1:] and sym not in found:
            found.append(sym)
    return found


def eln_data(meta):
    """The content of ``eln_data.yaml`` for pynxtools(-em): OASIS ELN keys + the instrument and extra sample keys
    (read by the ``tem`` reader).  HTML fields as plain text."""
    e, p, s, ins = (meta.get(k) or {} for k in ("entry", "project", "sample", "instrument"))
    out = {"entry": {}, "sample": {}}
    for k in ("experiment_alias", "identifier_experiment"):
        if _filled(e.get(k)):
            out["entry"][k] = str(e[k])
    out["entry"]["start_time"] = to_iso(e.get("start_time")) or now_iso()
    if _filled(e.get("end_time")):
        out["entry"]["end_time"] = to_iso(e.get("end_time"))
    desc = richtext.to_text(e.get("experiment_description", ""))
    if desc.strip():
        out["entry"]["experiment_description"] = desc
    if _filled(p.get("name")):
        out["project"] = {"name": str(p["name"])}
    sm = out["sample"]
    for k in ("name", "identifier_sample", "identifier_owner", "identifier_parent", "physical_form"):
        if _filled(s.get(k)):
            sm[k] = str(s[k])
    comps = [{k: str(v) for k, v in c.items() if _filled(v)} for c in s.get("components") or []
             if isinstance(c, dict) and _filled(c.get("name"))]
    if comps:
        sm["components"] = comps
    sm["is_simulation"] = bool(s.get("is_simulation"))
    sm["preparation_date"] = to_iso(s.get("preparation_date")) or out["entry"]["start_time"]
    if _filled(s.get("chemical_formula")):
        sm["chemical_formula"] = str(s["chemical_formula"]).replace(" ", "")
    at = atom_types_of(meta)
    if at:
        sm["atom_types"] = ", ".join(at)
    if _filled(s.get("thickness")):
        try:
            sm["thickness"] = {"value": float(s["thickness"]), "unit": s.get("thickness_unit") or "nm"}
        except (TypeError, ValueError):
            pass
    hist = richtext.to_text(s.get("history", ""))
    if hist.strip():
        sm["description"] = hist
    users = []
    for u in meta.get("users") or []:
        d = {k: str(u[k]).strip() for k, _l, _v in USER_FIELDS if _filled(u.get(k))}
        if "orcid" in d:
            d["orcid"] = norm_orcid(d["orcid"]) or d["orcid"]
        if d:
            users.append(d)
    if users:
        out["user"] = users
    insd = {}
    for k in ("name", "location", "type", "vendor", "model", "serial_number", "identifier", "operation_mode",
              "emitter_type"):
        if _filled(ins.get(k)):
            insd[k] = str(ins[k])
    if _filled(ins.get("voltage_kV")):
        try:
            insd["voltage"] = {"value": float(ins["voltage_kV"]), "unit": "kV"}
        except (TypeError, ValueError):
            pass
    if _filled(ins.get("detectors")):
        insd["detectors"] = [d.strip() for d in str(ins["detectors"]).split(",") if d.strip()]
    if insd:
        out["instrument"] = insd
    return out


def write_eln_data(meta, path):
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(eln_data(meta), f, sort_keys=False, allow_unicode=True)
    return path


def merge(meta, other, overwrite=False):
    """Fill the fields of ``meta`` from ``other`` (only empty ones unless ``overwrite``); users and tags are added."""
    out = copy.deepcopy(meta)
    for sec in ("entry", "project", "sample", "instrument", "source"):
        for k, v in (other.get(sec) or {}).items():
            if not _filled(v):
                continue
            cur = out.setdefault(sec, {}).get(k)
            if overwrite or not _filled(cur) or (sec, k) in (("sample", "is_simulation"),):
                out[sec][k] = v
    have = {(u.get("name") or "").strip().lower() for u in out.get("users") or []}
    out["users"] = [u for u in out.get("users") or [] if any(_filled(x) for x in u.values())]
    for u in other.get("users") or []:
        if (u.get("name") or "").strip().lower() not in have and any(_filled(x) for x in u.values()):
            out["users"].append(dict(u))
    out["users"] = out["users"] or [{}]
    out["tags"] = list(dict.fromkeys(list(out.get("tags") or []) + list(other.get("tags") or [])))
    return out


def from_file_hints(infos):
    """Metadata found in the data files (rosettasciio, Nion JSON): high tension, microscope, detector, date."""
    m = {"entry": {}, "instrument": {}}
    dates = sorted(i["metadata"]["date"] for i in infos if (i.get("metadata") or {}).get("date"))
    if dates:
        m["entry"]["start_time"] = to_iso(dates[0])
        if len(dates) > 1:
            m["entry"]["end_time"] = to_iso(dates[-1])
    for i in infos:
        h = i.get("metadata") or {}
        if h.get("voltage_kV") and "voltage_kV" not in m["instrument"]:
            v = float(h["voltage_kV"])
            m["instrument"]["voltage_kV"] = v / 1e3 if v > 1e4 else v      # eV -> kV if needed
        if h.get("microscope") and "model" not in m["instrument"]:
            m["instrument"]["model"] = str(h["microscope"])
        if h.get("vendor") and "vendor" not in m["instrument"]:
            m["instrument"]["vendor"] = str(h["vendor"])
        if h.get("detector") and "detectors" not in m["instrument"]:
            m["instrument"]["detectors"] = str(h["detector"])
    return m


def from_eln_data(d):
    """Metadata from the content of an ``eln_data.yaml`` (inverse of :func:`eln_data`)."""
    d = d or {}
    m = empty()
    e = d.get("entry") or {}
    for k in ("experiment_alias", "identifier_experiment", "start_time", "end_time"):
        if e.get(k):
            m["entry"][k] = str(e[k])
    if e.get("experiment_description"):
        m["entry"]["experiment_description"] = richtext.from_text(e["experiment_description"])
    if (d.get("project") or {}).get("name"):
        m["project"]["name"] = str(d["project"]["name"])
    s = d.get("sample") or {}
    for k in ("name", "identifier_sample", "identifier_owner", "identifier_parent", "physical_form",
              "chemical_formula", "atom_types", "preparation_date"):
        if s.get(k):
            m["sample"][k] = str(s[k])
    if s.get("components"):
        m["sample"]["components"] = [dict(c) for c in s["components"] if isinstance(c, dict)]
    m["sample"]["is_simulation"] = bool(s.get("is_simulation"))
    th = s.get("thickness")
    if isinstance(th, dict) and th.get("value") is not None:
        m["sample"]["thickness"] = th["value"]
        m["sample"]["thickness_unit"] = th.get("unit") or "nm"
    if s.get("description"):
        m["sample"]["history"] = richtext.from_text(s["description"])
    users = [{k: str(v) for k, v in u.items() if v} for u in d.get("user") or [] if isinstance(u, dict)]
    m["users"] = users or [{}]
    ins = d.get("instrument") or {}
    for k in ("name", "location", "type", "vendor", "model", "serial_number", "identifier", "operation_mode",
              "emitter_type"):
        if ins.get(k):
            m["instrument"][k] = str(ins[k])
    v = ins.get("voltage")
    if isinstance(v, dict) and v.get("value") is not None:
        m["instrument"]["voltage_kV"] = float(v["value"]) / (1e3 if str(v.get("unit", "kV")) == "V" else 1)
    if ins.get("detectors"):
        m["instrument"]["detectors"] = ", ".join(ins["detectors"]) if isinstance(ins["detectors"], list) \
            else str(ins["detectors"])
    return m


def from_nxem(path):
    """Metadata from an NXem file (e.g. one written by tem-provenance or by pynxtools-em): entry, sample (with
    identifiers and components), users, instrument."""
    import h5py

    def txt(g, k):
        if g is None or k not in g:
            return ""
        v = g[k][()]
        if hasattr(v, "decode"):
            v = v.decode()
        if hasattr(v, "tolist"):
            v = v.tolist()
        return v if not isinstance(v, (list, tuple)) else ", ".join(str(x) for x in v)

    m = empty()
    with h5py.File(path, "r") as f:
        entries = [f[k] for k in f if isinstance(f[k], h5py.Group) and txt(f[k], "definition") == "NXem"]
        if not entries:
            return m
        e = entries[0]
        for k in ("experiment_alias", "start_time", "end_time", "identifier_experiment"):
            if txt(e, k):
                m["entry"][k] = str(txt(e, k))
        if txt(e, "experiment_description"):
            m["entry"]["experiment_description"] = richtext.from_text(str(txt(e, "experiment_description")))
        smp = next((e[k] for k in e if isinstance(e[k], h5py.Group) and e[k].attrs.get("NX_class") in
                    (b"NXsample", "NXsample")), None)
        if smp is not None:
            s = m["sample"]
            for k in ("name", "identifier_sample", "identifier_parent", "chemical_formula", "atom_types",
                      "physical_form", "preparation_date"):
                if txt(smp, k):
                    s[k] = str(txt(smp, k))
            if txt(smp, "identifier_owner"):
                s["identifier_owner"] = str(txt(smp, "identifier_owner"))
            if txt(smp, "description"):
                s["history"] = richtext.from_text(str(txt(smp, "description")))
            if "is_simulation" in smp:
                s["is_simulation"] = bool(smp["is_simulation"][()])
            if "thickness" in smp:
                try:
                    v = float(smp["thickness"][()])
                    u = smp["thickness"].attrs.get("units", "m")
                    u = u.decode() if hasattr(u, "decode") else str(u)
                    fac = {"m": 1e9, "meter": 1e9, "nm": 1.0, "nanometer": 1.0, "um": 1e3, "angstrom": 0.1}.get(u)
                    s["thickness"], s["thickness_unit"] = (round(v * fac, 6), "nm") if fac else (v, u)
                except (TypeError, ValueError):
                    pass
            comps = []
            for k in smp:
                g = smp[k]
                if isinstance(g, h5py.Group) and g.attrs.get("NX_class") in (b"NXsample_component",
                                                                              "NXsample_component"):
                    c = {"name": str(txt(g, "name") or k)}
                    for kk in ("chemical_formula", "space_group"):
                        if txt(g, kk):
                            c[kk] = str(txt(g, kk))
                    comps.append(c)
            if comps:
                s["components"] = comps
        users = []
        for k in e:
            g = e[k]
            if isinstance(g, h5py.Group) and g.attrs.get("NX_class") in (b"NXuser", "NXuser"):
                u = {kk: str(txt(g, kk)) for kk in ("name", "affiliation", "role") if txt(g, kk)}
                orc = txt(g, "identifier_orcid") or txt(g, "orcid") or txt(g, "identifier")
                if orc:
                    u["orcid"] = norm_orcid(orc) or str(orc)
                if u:
                    users.append(u)
        m["users"] = users or [{}]
        ins = e.get("measurement/instrument")
        if ins is not None:
            if txt(ins, "name"):
                m["instrument"]["name"] = str(txt(ins, "name"))
            fab = ins.get("fabrication")
            for k in ("vendor", "model", "serial_number"):
                if fab is not None and txt(fab, k):
                    m["instrument"][k] = str(txt(fab, k))
    return m
