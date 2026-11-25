# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""TEM sessions recorded with the eLabFTW templates of `tem-provenance
<https://github.com/erotunno/tem-provenance>`_ (Materials Commons): Sample resource -> TEM Session experiment
(linking one Sample and one TEM Microscope, with start / end time and the ``Path`` of the acquisition folder).

Given the ID of a session, :func:`session_metadata` follows the links like ``export_tem_image.py`` of
tem-provenance and maps the records to the metadata of EMingest:

=============================================  =====================================================================
eLabFTW                                        EMingest / NXem
=============================================  =====================================================================
session title, ``elabid``                      title, ``identifier_experiment`` (persistent eLabFTW ID)
session ``start_time`` / ``end_time``          start / end, entered in facility-local time -> UTC (ambiguous or
                                               nonexistent local times are refused)
session ``experiment_description``, body,      description (rich text), with a provenance note (record IDs of
``Other_notes``, ``Technical_issues``,         session, sample and microscope)
``Contamination``, ``Plasma Cleaning``
session ``voltage``                            high tension (else the ``voltage`` of the microscope)
Sample: title, ``elabid``                      sample name, ``identifier_sample`` (persistent eLabFTW ID)
Sample ``identifier`` / ``Sample_id``          ``identifier_owner`` (the owner's ID, kept next to the eLabFTW ID)
Sample ``identifier_parent`` (resource link)   ``identifier_parent`` = elabid of the parent sample
Sample ``chemical_formula``, ``atom_types``,   formula; elements from all formulas; components (NXsample_component)
Materials ``<name>_formula`` /                 with formula and space group
``<name>_space_group``, ``substrate_formula``
Sample ``thickness`` ("40 nm"),                thickness, physical form, preparation date (date-only records:
``physical_form``, ``preparation_date``        noon local time, noted), sample history (rich text: body + method,
``Method``, ``Sample_provider``, reception     provider, reception, storage)
Microscope: title, ``vendor``, ``model``,      instrument name, fabrication, identifier, electron source, mode
``identifier``, ``emitter_type``, ``voltage``,
``capability``
session ``Path``                               the acquisition folder: all its files are the data of the session
=============================================  =====================================================================

The time window of the session is kept: acquisitions whose time stamp (from the file) lies outside it are flagged
(specimen attribution unverified), as in tem-provenance.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re

from . import richtext
from .elabftw import ElabError, _body_html, _tags

SESSION_FIELDS = ("Sample", "Microscope", "Path")
DEFAULT_TZ = "Europe/Rome"


def fields_of(entity):
    """{name: field dict} of the extra fields."""
    meta = entity.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta) if meta.strip() else {}
        except ValueError:
            meta = {}
    ef = (meta or {}).get("extra_fields") or {}
    return ef if isinstance(ef, dict) else {}


def values_of(entity):
    return {k: (v.get("value") if isinstance(v, dict) else v) for k, v in fields_of(entity).items()}


def groups_of(entity):
    meta = entity.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta) if meta.strip() else {}
        except ValueError:
            meta = {}
    return {str(g.get("id")): g.get("name") for g in ((meta or {}).get("elabftw") or {}).get(
        "extra_fields_groups") or []}


def is_session(entity):
    """True for an experiment made from the TEM Session template (links a Sample and a Microscope)."""
    f = fields_of(entity)
    return all(k in f for k in SESSION_FIELDS)


def _filled(v):
    return v not in (None, "", [], {}) and str(v).strip().lower() not in ("none", "['na']", "na")


def _ref_id(v):
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("id") or v.get("@id")
    return None if not _filled(v) else str(v).strip()


# ------------------------------------------------------------------------------------------------ times
def facility_time(value, zone):
    """UTC datetime of a facility-local time (ISO; with offset: used as is).  Refuses ambiguous / nonexistent."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(zone) if isinstance(zone, str) else zone
    local = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if local.tzinfo is not None:
        return local.astimezone(dt.timezone.utc)
    cands = set()
    for fold in (0, 1):
        utc = local.replace(tzinfo=tz, fold=fold).astimezone(dt.timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None) == local:
            cands.add(utc)
    if len(cands) != 1:
        raise ValueError(f"local time {value!r} is ambiguous or does not exist in {tz.key}")
    return cands.pop()


def utc_text(t):
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------------------------------------ sample
def owner_sample_id(specimen):
    for k in ("Sample_id", "sample_id", "identifier"):
        v = specimen.get(k)
        if v is not None and str(v).strip() and str(v).strip().casefold() != "none":
            return str(v).strip()
    return None


def material_components(sample):
    """[(name, formula, space group)] from the fields ``<name>_formula`` / ``<name>_space_group`` of the
    Materials group (tem-provenance convention)."""
    fields = fields_of(sample)
    mat = {gid for gid, name in groups_of(sample).items() if name == "Materials"}
    comps, sgs = {}, {}
    for name, f in fields.items():
        if name in ("chemical_formula", "atom_types") or str(f.get("group_id")) not in mat:
            continue
        val = str(f.get("value") or "").strip()
        key = "_".join(name.casefold().split())
        if key.endswith("_space_group"):
            if val:
                sgs[key[: -len("_space_group")]] = val
        else:
            key = key[: -len("_formula")] if key.endswith("_formula") else key
            comps[key] = (f.get("position") or 0, name[: -len("_formula")] if name.endswith("_formula") else name,
                          val)
    return [(n, formula, sgs.get(k)) for k, (_p, n, formula) in sorted(comps.items(), key=lambda kv: kv[1][0])
            if formula]


def _elements(formulas, extra=""):
    from ase.data import chemical_symbols
    found = {x.strip() for x in str(extra or "").split(",") if x.strip() in chemical_symbols[1:]}
    for f in formulas:
        if not f:
            continue
        try:
            from ase.formula import Formula
            found.update(Formula("".join(str(f).split())).count())
        except Exception:  # noqa: BLE001
            found.update(s for s in re.findall(r"[A-Z][a-z]?", str(f)) if s in chemical_symbols[1:])
    return sorted(found)


def _table(rows):
    rows = [(k, v) for k, v in rows if _filled(v)]
    if not rows:
        return ""
    return "<table>" + "".join(f"<tr><td><b>{html.escape(k)}</b></td><td>{html.escape(str(v))}</td></tr>"
                               for k, v in rows) + "</table>"


def _voltage_kV(v):
    m = re.search(r"([-+]?\d*\.?\d+)\s*(k?V)?", str(v or ""))
    if not m:
        return None
    x = float(m.group(1))
    return x / 1e3 if (m.group(2) or "").upper() == "V" else x


# ------------------------------------------------------------------------------------------------ session
def session_metadata(client, session_id, zone=DEFAULT_TZ, strict_times=True):
    """Metadata of a TEM session (see module docstring).

    ``client``: :class:`emingest.elabftw.ElabClient` or :class:`emingest.eln_archive.ElnArchiveClient`.
    Returns dict(meta, path, window=(start utc, end utc or None), notes, sample, microscope, session, zone)."""
    session = client.entity("experiments", session_id)
    f = values_of(session)
    missing = [k for k in ("Sample", "Microscope", "Path", "start_time") if not _filled(f.get(k))]
    if missing:
        raise ElabError(f"session {session_id} is missing {', '.join(missing)} (TEM Session template)")
    sample = client.entity("items", _ref_id(f["Sample"]))
    micro = client.entity("items", _ref_id(f["Microscope"]))
    spec = values_of(sample)
    mic = values_of(micro)
    notes = []
    parent = None
    if _filled(spec.get("identifier_parent")):
        pid = _ref_id(spec["identifier_parent"])
        if pid == str(sample.get("id")):
            raise ElabError("a specimen cannot be its own parent")
        try:
            parent = client.entity("items", pid)
        except ElabError:
            notes.append(f"Parent sample {pid} is not accessible.")
    start = facility_time(f["start_time"], zone)
    end = None
    if _filled(f.get("end_time")):
        end = facility_time(f["end_time"], zone)
        if end < start:
            raise ElabError("the session end precedes its start")
    # ----------------------------------------------------------------- entry
    m = {"entry": {}, "project": {}, "sample": {"is_simulation": False}, "instrument": {}, "users": [],
         "tags": _tags(session), "source": {}}
    e = m["entry"]
    e["experiment_alias"] = str(session.get("title") or f"session {session_id}")
    e["start_time"] = utc_text(start)
    if end:
        e["end_time"] = utc_text(end)
    e["identifier_experiment"] = session.get("elabid") or f"elabftw:experiments/{session_id}"
    url_of = (lambda t, i: client.entry_url(t, i)) if hasattr(client, "entry_url") else \
        (lambda t, i: f"{client.base.rsplit('/api/v2', 1)[0]}/{'experiments' if t == 'experiments' else 'database'}"
                      f".php?mode=view&id={i}")
    surl = url_of("experiments", session_id)
    desc = []
    goal = str(f.get("experiment_description") or "").strip()
    if goal:
        desc.append(f"<h3>Objective</h3>{richtext.from_text(goal)}")
    body = _body_html(session)
    if richtext.to_text(body).strip():
        desc.append(f"<h3>Session notes</h3>{body}")
    extra = _table([("Contamination", f.get("Contamination")), ("Plasma cleaning", f.get("Plasma Cleaning")),
                    ("Technical issues", f.get("Technical_issues")), ("Other notes", f.get("Other_notes"))])
    if extra:
        desc.append(f"<h3>Session record</h3>{extra}")
    prov = (f"eLabFTW session <a href=\"{html.escape(surl)}\">#{html.escape(str(session.get('id')))}</a> "
            f"({html.escape(str(session.get('elabid') or ''))}), sample resource "
            f"<a href=\"{html.escape(url_of('items', sample.get('id')))}\">#{html.escape(str(sample.get('id')))}</a> "
            f"({html.escape(str(sample.get('title')))}), microscope resource "
            f"<a href=\"{html.escape(url_of('items', micro.get('id')))}\">#{html.escape(str(micro.get('id')))}</a> "
            f"({html.escape(str(micro.get('title')))}); acquisition folder <code>{html.escape(str(f['Path']))}</code>.")
    desc.append(f"<p><i>Provenance: {prov}</i></p>")
    # ----------------------------------------------------------------- sample
    s = m["sample"]
    s["name"] = str(sample.get("title") or "")
    s["identifier_sample"] = sample.get("elabid") or f"elabftw:items/{sample.get('id')}"
    own = owner_sample_id(spec)
    if own:
        s["identifier_owner"] = own
    if parent is not None:
        s["identifier_parent"] = parent.get("elabid") or f"elabftw:items/{parent.get('id')}"
    comps = material_components(sample)
    if any(n.casefold() == "substrate" for n, _f, _s in comps):
        spec.pop("substrate_formula", None)
    elif _filled(spec.get("substrate_formula")):
        comps.insert(0, ("substrate", str(spec["substrate_formula"]).strip(), spec.get("substrate_space_group")))
    if _filled(spec.get("chemical_formula")):
        s["chemical_formula"] = str(spec["chemical_formula"]).strip()
    elems = _elements([spec.get("chemical_formula")] + [c[1] for c in comps], spec.get("atom_types"))
    if elems:
        s["atom_types"] = ", ".join(elems)
    if comps:
        s["components"] = [{"name": n, "chemical_formula": fo, **({"space_group": sg} if sg else {})}
                           for n, fo, sg in comps]
    if _filled(spec.get("thickness")):
        mm = re.match(r"^\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(\S+)?", str(spec["thickness"]))
        if mm:
            s["thickness"] = float(mm.group(1))
            u = (mm.group(2) or "nm").replace("µ", "u").replace("Å", "A")
            s["thickness_unit"] = u if u in ("nm", "um", "A", "m") else "nm"
    if _filled(spec.get("physical_form")):
        s["physical_form"] = str(spec["physical_form"])
    prep = str(spec.get("preparation_date") or "").strip()
    if _filled(prep):
        if len(prep) == 10:
            notes.append(f"Sample preparation: noon local time assumed for the date-only record {prep}.")
            prep += "T12:00"
        try:
            s["preparation_date"] = utc_text(facility_time(prep, zone))
        except ValueError as ex:
            notes.append(f"Preparation date not used: {ex}.")
    hist = []
    sb = _body_html(sample)
    if richtext.to_text(sb).strip():
        hist.append(sb)
    t = _table([("Preparation method", spec.get("Method")), ("Provider", spec.get("Sample_provider")),
                ("Received", spec.get("Reception_date")), ("Storage", spec.get("Sample_storage")),
                ("Owner's sample ID", own)] + [(f"Component {n}", f"{fo}" + (f" ({sg})" if sg else ""))
                                               for n, fo, sg in comps])
    if t:
        hist.append(f"<h3>Sample record</h3>{t}")
    s["history"] = "".join(hist)
    # ----------------------------------------------------------------- instrument
    ins = m["instrument"]
    ins["name"] = str(micro.get("title") or "")
    ins["type"] = "tem"
    for k in ("vendor", "model", "emitter_type"):
        if _filled(mic.get(k)):
            ins[k] = str(mic[k])
    if _filled(mic.get("identifier")):
        ins["identifier"] = str(mic["identifier"])
    if _filled(mic.get("capability")):
        cap = mic["capability"]
        ins["operation_mode"] = ", ".join(cap) if isinstance(cap, list) else str(cap)
    kv = _voltage_kV(f.get("voltage")) if _filled(f.get("voltage")) else _voltage_kV(mic.get("voltage"))
    if kv:
        ins["voltage_kV"] = kv
    # ----------------------------------------------------------------- user, source
    uid = session.get("userid")
    if session.get("fullname"):
        u = {"name": str(session["fullname"]).strip()}
        info = client.user(uid) if uid else {}
        if info.get("orcid"):
            from .metadata import norm_orcid
            u["orcid"] = norm_orcid(info["orcid"]) or info["orcid"]
        m["users"].append(u)
    m["source"] = {"elabftw_url": surl, "elabftw_type": "experiments", "elabftw_id": session.get("id"),
                   "elabftw_title": e["experiment_alias"], "session_path": str(f["Path"]),
                   "timezone": zone if isinstance(zone, str) else zone.key}
    if getattr(client, "archive", None) is not None:
        m["source"]["eln_file"] = client.archive.name
    if notes:
        desc.append("<p><i>" + html.escape(" ".join(notes)) + "</i></p>")
    e["experiment_description"] = "".join(desc)
    return {"meta": m, "path": str(f["Path"]).strip(), "window": (start, end), "notes": notes, "session": session,
            "sample": sample, "microscope": micro, "zone": zone}


def in_window(when, window, zone=DEFAULT_TZ):
    """True if an acquisition time (datetime or ISO text) lies in the session; without end time: the local date of
    the start (as in tem-provenance).  None if the time is not known."""
    if not when:
        return None
    if not isinstance(when, dt.datetime):
        try:
            when = dt.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        except ValueError:
            return None
    start, end = window
    if when.tzinfo is None:
        when = facility_time(when.isoformat(), zone)
    if end is not None:
        return start <= when <= end
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(zone) if isinstance(zone, str) else zone
    return when >= start and when.astimezone(tz).date() == start.astimezone(tz).date()


def map_path(path, rules):
    """Path of the session as seen here: ``rules`` = [(prefix in eLabFTW, replacement)] (first match wins), e.g.
    [("/mnt/storage/tem", "tem@storage.lab:/data/tem")]."""
    p = str(path or "").strip()
    for a, b in rules or []:
        a = a.rstrip("/\\")
        if a and (p == a or p.startswith(a + "/") or p.startswith(a + "\\")):
            return b.rstrip("/") + p[len(a):].replace("\\", "/")
    return p


def parse_rules(text):
    """'old=new' per line or separated by ';' -> [(old, new)]."""
    out = []
    for part in re.split(r"[;\n]", str(text or "")):
        if "=" in part:
            a, b = part.split("=", 1)
            if a.strip():
                out.append((a.strip(), b.strip()))
    return out


def list_sessions(archive_or_client, ids=None):
    """Sessions in an .eln archive (``ElnArchive``): [dict(id, title, date, path, sample, files)]."""
    arch = getattr(archive_or_client, "archive", archive_or_client)
    out = []
    for e in arch.entries.values():
        if e["type"] != "experiments" or not is_session(e):
            continue
        if ids and str(e["id"]) not in {str(i) for i in ids}:
            continue
        v = values_of(e)
        out.append({"id": e["id"], "title": e["title"], "date": v.get("start_time") or e["date"],
                    "path": v.get("Path") or "", "sample": _ref_id(v.get("Sample")), "elabid": e.get("elabid")})
    return sorted(out, key=lambda x: str(x["date"]))


def parse_ids(text):
    """'12, 15-18 21' -> ['12', '15', '16', '17', '18', '21']."""
    out = []
    for tok in re.split(r"[,\s;]+", str(text or "").strip()):
        if not tok:
            continue
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if b < a or b - a > 10000:
                raise ValueError(f"invalid range {tok}")
            out += [str(i) for i in range(a, b + 1)]
        elif tok.isdigit():
            out.append(tok)
        else:
            raise ValueError(f"not a session ID: {tok}")
    return list(dict.fromkeys(out))
