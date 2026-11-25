# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""NOMAD archives of a TEM data set (written into the NOMAD upload next to the data):

* ``<name>.archive.yaml``           ELN entry (``ELNTEMExperiment`` of this plugin, a ``Measurement`` of NOMAD; or the
  core ``ELNMeasurement`` if the plugin is not installed): description (rich text), experiment, project, sample with
  its history (rich text), instrument, users with ORCID, the data files with format and kind, link to elabFTW
* ``<name>.eln_data.yaml``          the same metadata for pynxtools (NXem), see :mod:`emingest.metadata`
* ``<name>.nxem.archive.yaml``      result of the NXem conversion (``TEMConversion``): input files -> NXem files, status
* ``<name>.workflow.archive.yaml``  workflow: data files + metadata -> one conversion task per file -> NXem files

All paths in the archives are relative to the upload root; ``prefix`` is the folder of the upload with the archives.
"""
from __future__ import annotations

import html
import os

import yaml

from . import metadata as md
from . import richtext

PLUGIN_SECTION = "emingest_nomad.schema_packages.tem.ELNTEMExperiment"
CONVERSION_SECTION = "emingest_nomad.schema_packages.tem.TEMConversion"
CORE_SECTION = "nomad.datamodel.metainfo.eln.ELNMeasurement"
UP = "../upload/archive/mainfile/"


def _pre(prefix):
    p = str(prefix or "").strip().strip("/")
    return p + "/" if p else ""


def _plain(o):
    if isinstance(o, dict):
        return {str(k): _plain(v) for k, v in o.items() if v not in (None, "", [], {})}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if hasattr(o, "item"):
        return o.item()
    return o


def _dump(path, arch):
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(_plain(arch), f, sort_keys=False, allow_unicode=True, width=120)
    return path


def method_of(kinds):
    ks = [k for k in kinds if k and k != "other"]
    if not ks:
        return "TEM"
    names = {"4D-STEM": "4D-STEM", "spectrum image": "STEM spectrum imaging", "spectrum": "EELS / EDS",
             "line scan": "STEM spectroscopy", "focal series": "focal series", "tilt series": "tomography",
             "diffraction pattern": "electron diffraction", "diffraction series": "electron diffraction",
             "hologram": "electron holography", "image": "imaging", "image series": "imaging"}
    distinct = sorted({names.get(k, k) for k in ks})
    if len(distinct) > 1:
        return "TEM: " + ", ".join(distinct)
    main = ks[0]
    return {"4D-STEM": "4D-STEM", "spectrum image": "STEM spectrum imaging", "spectrum": "EELS / EDS spectroscopy",
            "line scan": "STEM spectroscopy", "focal series": "TEM focal series", "tilt series": "electron tomography",
            "diffraction pattern": "electron diffraction", "diffraction series": "electron diffraction",
            "hologram": "electron holography", "image": "TEM / STEM imaging",
            "image series": "TEM / STEM imaging"}.get(main, "TEM")


def description_html(meta):
    """Description of the ELN entry: the rich text + a short summary of what the form says."""
    e = meta.get("entry") or {}
    return richtext.sanitize(e.get("experiment_description") or "")


def eln_archive(name, meta, files, schema="plugin", eln_data_file="", nxem_files=()):
    """dict of the ELN archive (see module docstring).  ``files``: [dict(path, kind, format, shape, size, note)]."""
    e, p, s, ins = (meta.get(k) or {} for k in ("entry", "project", "sample", "instrument"))
    src = meta.get("source") or {}
    now = md.now_iso()
    start = md.to_iso(e.get("start_time")) or now
    title = e.get("experiment_alias") or name
    smp = {"name": s.get("name") or "specimen"}
    if s.get("identifier_sample"):
        smp["lab_id"] = str(s["identifier_sample"])
    inst = {"name": ins.get("name") or " ".join(x for x in (ins.get("vendor"), ins.get("model")) if x) or "TEM"}
    if ins.get("serial_number"):
        inst["lab_id"] = str(ins["serial_number"])
    data = {"m_def": PLUGIN_SECTION if schema == "plugin" else CORE_SECTION, "name": str(title),
            "datetime": start, "description": description_html(meta),
            "method": method_of([f.get("kind") for f in files]), "samples": [smp], "instruments": [inst]}
    if e.get("identifier_experiment"):
        data["lab_id"] = str(e["identifier_experiment"])
    if schema == "plugin":
        data["experiment_alias"] = e.get("experiment_alias")
        if md.to_iso(e.get("end_time")):
            data["end_time"] = md.to_iso(e.get("end_time"))
        data["project_name"] = p.get("name")
        data["tags"] = list(meta.get("tags") or [])
        sample = {k: s.get(k) for k in ("name", "identifier_sample", "identifier_owner", "identifier_parent",
                                         "chemical_formula", "physical_form")}
        comps = [c for c in s.get("components") or [] if isinstance(c, dict) and c.get("name")]
        if comps:
            sample["components"] = "; ".join(f"{c['name']}: {c.get('chemical_formula', '')}"
                                             + (f" ({c['space_group']})" if c.get("space_group") else "")
                                             for c in comps)
        sample["atom_types"] = ", ".join(md.atom_types_of(meta)) or None
        sample["is_simulation"] = bool(s.get("is_simulation"))
        sample["preparation_date"] = md.to_iso(s.get("preparation_date")) or None
        try:
            if s.get("thickness") not in (None, ""):
                sample["thickness"] = float(s["thickness"])
                sample["thickness_unit"] = s.get("thickness_unit") or "nm"
        except (TypeError, ValueError):
            pass
        sample["history"] = richtext.sanitize(s.get("history") or "")
        data["sample_info"] = sample
        ii = {k: ins.get(k) for k in ("name", "location", "type", "vendor", "model", "serial_number", "identifier",
                                       "operation_mode", "emitter_type", "detectors")}
        try:
            if ins.get("voltage_kV") not in (None, ""):
                ii["acceleration_voltage"] = float(ins["voltage_kV"]) * 1e3
        except (TypeError, ValueError):
            pass
        data["instrument_info"] = ii
        users = []
        for u in meta.get("users") or []:
            d = {k: u.get(k) for k, _l, _v in md.USER_FIELDS}
            if d.get("orcid"):
                d["orcid"] = md.norm_orcid(d["orcid"]) or d["orcid"]
            if any(v for v in d.values()):
                users.append(d)
        data["users"] = users
        data["data_files"] = [{"name": os.path.basename(f["path"]), "file": f["path"], "kind": f.get("kind"),
                               "format": f.get("format"), "shape": f.get("shape") or None,
                               "size_mb": round(float(f.get("size") or 0) / 1e6, 3), "note": f.get("note")}
                              for f in files]
        data["eln_data_file"] = eln_data_file
        data["nxem_files"] = list(nxem_files) or None
        if src.get("elabftw_url"):
            data["elabftw_entry"] = src.get("elabftw_url")
        if src.get("eln_file"):
            data["eln_file"] = src["eln_file"]
        if src.get("session_path"):
            data["acquisition_folder"] = src["session_path"]
    else:
        extra = []
        hist = richtext.sanitize(s.get("history") or "")
        if hist:
            extra.append(f"<h3>Sample: history and preparation</h3>{hist}")
        lis = "".join(f"<li>{html.escape(os.path.basename(f['path']))}: {html.escape(str(f.get('kind') or ''))} "
                      f"({html.escape(str(f.get('format') or ''))})</li>" for f in files)
        if lis:
            extra.append(f"<h3>Data files</h3><ul>{lis}</ul>")
        if src.get("elabftw_url"):
            extra.append(f'<p>elabFTW: <a href="{html.escape(src["elabftw_url"])}">'
                         f'{html.escape(src["elabftw_url"])}</a></p>')
        data["description"] = (data["description"] or "") + "".join(extra)
        data["tags"] = list(meta.get("tags") or []) + ["TEM"]
    return {"data": data}


def write_eln(folder, name, meta, files, prefix="", schema="plugin"):
    """Write ``<name>.archive.yaml`` + ``<name>.eln_data.yaml`` into folder.  Returns {kind: local path}."""
    os.makedirs(folder, exist_ok=True)
    pre = _pre(prefix)
    eln_yaml = f"{name}.eln_data.yaml"
    out = {"eln_data": md.write_eln_data(meta, os.path.join(folder, eln_yaml))}
    out["eln"] = _dump(os.path.join(folder, f"{name}.archive.yaml"),
                       eln_archive(name, meta, files, schema=schema, eln_data_file=pre + eln_yaml))
    return out


def write_conversion(folder, name, results, data_files, prefix="", eln_entry="", schema="plugin", software=""):
    """Write ``<name>.nxem.archive.yaml`` and ``<name>.workflow.archive.yaml`` for the conversion results
    (:func:`emingest.convert.convert_all`, with ``inputs`` / ``output`` given as paths in the upload).

    ``data_files``: paths (in the upload) of the data files in the order of the ELN entry's ``data_files``;
    ``eln_entry``: path of the ELN archive in the upload.  Returns {kind: local path}."""
    os.makedirs(folder, exist_ok=True)
    pre = _pre(prefix)
    now = md.now_iso()
    conv_name = f"{name}.nxem.archive.yaml"
    ok = [r for r in results if r.get("status") == "ok"]
    from . import __version__
    conv = {"m_def": CONVERSION_SECTION if schema == "plugin" else "nomad.datamodel.metainfo.eln.ELNAnalysis",
            "name": f"{name}: NXem conversion", "datetime": now,
            "description": (f"<p>Conversion of {len(results)} data file(s) to NeXus NXem with pynxtools "
                            f"(reader <code>tem</code> of emingest {__version__}: pynxtools-em with a rosettasciio "
                            f"fallback); {len(ok)} converted.</p>")}
    if schema == "plugin":
        if eln_entry:
            conv["eln_entry"] = f"{UP}{eln_entry}#/data"
        conv["software"] = software or f"emingest {__version__}"
        conv["conversions"] = [{"name": os.path.basename(r["inputs"][0]), "input_files": list(r["inputs"]),
                                "nxem_file": r["output"] if r.get("status") == "ok" else None,
                                "status": r.get("status"), "message": r.get("message"),
                                "validation_warnings": int(r.get("warnings") or 0),
                                "runtime": float(r.get("runtime_s") or 0)} for r in results]
    else:
        lis = "".join(f"<li>{html.escape(', '.join(r['inputs']))} &rarr; {html.escape(r['output'])}: "
                      f"{html.escape(r.get('status', ''))} {html.escape(r.get('message', ''))}</li>" for r in results)
        conv["description"] += f"<ul>{lis}</ul>"
    paths = {"conversion": _dump(os.path.join(folder, conv_name), {"data": conv})}
    # ---------------------------------------------------------------- workflow
    idx = {p: i for i, p in enumerate(data_files)}

    def file_ref(p):
        if eln_entry and p in idx and schema == "plugin":
            return f"{UP}{eln_entry}#/data/data_files/{idx[p]}"
        return f"{UP}{eln_entry}#/data" if eln_entry else f"{UP}{pre}{conv_name}#/data"

    meta_ref = {"name": "metadata (ELN)", "section": f"{UP}{eln_entry}#/data"} if eln_entry else None
    inputs, tasks, outputs = [], [], []
    for k, r in enumerate(results):
        ins = [{"name": os.path.basename(p), "section": file_ref(p)} for p in r["inputs"]]
        inputs += ins
        t = {"m_def": "nomad.datamodel.metainfo.workflow.Task",
             "name": f"NXem: {os.path.basename(r['inputs'][0])}",
             "inputs": [dict(x) for x in ins] + ([dict(meta_ref)] if meta_ref else [])}
        if r.get("status") == "ok":
            o = {"name": os.path.basename(r["output"]), "section": f"{UP}{r['output']}#/data"}
            outputs.append(o)
            t["outputs"] = [dict(o)]
        else:
            t["outputs"] = [{"name": f"{r.get('status')}: {os.path.basename(r['inputs'][0])}",
                             "section": f"{UP}{pre}{conv_name}#/data/conversions/{k}" if schema == "plugin"
                             else f"{UP}{pre}{conv_name}#/data"}]
        tasks.append(t)
    if meta_ref:
        inputs.insert(0, meta_ref)
    seen, uniq = set(), []
    for x in inputs:
        if x["section"] not in seen:
            seen.add(x["section"])
            uniq.append(x)
    outputs.append({"name": "conversion record", "section": f"{UP}{pre}{conv_name}#/data"})
    wf = {"workflow2": {"name": f"{name}: TEM data -> NXem", "inputs": uniq, "outputs": outputs, "tasks": tasks}}
    paths["workflow"] = _dump(os.path.join(folder, f"{name}.workflow.archive.yaml"), wf)
    return paths


# ====================================================================== workflow graph (from fit4dstem.eln)
def _label(link):
    return link.get("name") or link.get("section", "").split("#")[-1]


def workflow_svg(wf, width=980):
    """SVG of the workflow graph: inputs (left) -> tasks (middle, in order) -> outputs (right), with the edges
    NOMAD derives from shared section references."""
    inputs, outputs, tasks = wf.get("inputs", []), wf.get("outputs", []), wf.get("tasks", [])
    col_x = [20, 330, width - 250]
    box_w = [230, 260, 230]
    row_h = 58
    n_rows = max(len(inputs), len(tasks), len(outputs), 1)
    H = 70 + n_rows * row_h + 20
    pos = {}

    def ypos(i, n):
        top = 70 + (n_rows - n) * row_h / 2
        return top + i * row_h

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{H}" font-family="Helvetica, Arial, '
             f'sans-serif" font-size="12">',
             '<defs><marker id="arr" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">'
             '<path d="M0,0 L10,4 L0,8 z" fill="#5a6b7b"/></marker></defs>',
             f'<text x="{col_x[0]}" y="30" font-size="14" font-weight="bold" fill="#2a3a4a">'
             f'{html.escape(wf.get("name", "workflow"))}</text>',
             f'<text x="{col_x[0]}" y="55" fill="#5a6b7b">inputs</text>',
             f'<text x="{col_x[1]}" y="55" fill="#5a6b7b">tasks</text>',
             f'<text x="{col_x[2]}" y="55" fill="#5a6b7b">outputs</text>']
    edges = []
    # sections -> node
    sec_owner = {}
    for i, l in enumerate(inputs):
        y = ypos(i, len(inputs))
        pos[("in", i)] = (col_x[0], y)
        sec_owner.setdefault(l.get("section"), []).append(("in", i))
        parts.append(f'<rect x="{col_x[0]}" y="{y}" rx="18" width="{box_w[0]}" height="40" fill="#e3f0fb" '
                     f'stroke="#2f80c3"/><text x="{col_x[0] + 14}" y="{y + 25}" fill="#123">'
                     f'{html.escape(_label(l))}</text>')
    for i, t in enumerate(tasks):
        y = ypos(i, len(tasks))
        pos[("task", i)] = (col_x[1], y)
        parts.append(f'<rect x="{col_x[1]}" y="{y}" rx="4" width="{box_w[1]}" height="40" fill="#fff1df" '
                     f'stroke="#e08a1e"/><text x="{col_x[1] + 14}" y="{y + 25}" fill="#321">'
                     f'{i + 1}. {html.escape(t.get("name", "task"))}</text>')
    for i, l in enumerate(outputs):
        y = ypos(i, len(outputs))
        pos[("out", i)] = (col_x[2], y)
        parts.append(f'<rect x="{col_x[2]}" y="{y}" rx="18" width="{box_w[2]}" height="40" fill="#e5f6e8" '
                     f'stroke="#2e9e4f"/><text x="{col_x[2] + 14}" y="{y + 25}" fill="#132">'
                     f'{html.escape(_label(l))}</text>')
    # edges: input -> task when a task input shares the section of a workflow input; task -> task when an output
    # section of one task is an input section of a later task; task -> output likewise.
    task_out = {}
    for i, t in enumerate(tasks):
        for l in t.get("outputs", []):
            task_out.setdefault(l.get("section"), []).append(i)
    for i, t in enumerate(tasks):
        for l in t.get("inputs", []):
            s = l.get("section")
            for j, w in enumerate(inputs):
                if w.get("section") == s:
                    edges.append((("in", j), ("task", i)))
            for k in task_out.get(s, []):
                if k != i:
                    edges.append((("task", k), ("task", i)))
    for j, w in enumerate(outputs):
        for k in task_out.get(w.get("section"), []):
            edges.append((("task", k), ("out", j)))
    for a, b in dict.fromkeys(edges):
        xa, ya = pos[a]
        xb, yb = pos[b]
        wa = box_w[{"in": 0, "task": 1, "out": 2}[a[0]]]
        if a[0] == "task" and b[0] == "task":
            # vertical edge on the right side of the task column
            x = xa + wa
            parts.append(f'<path d="M{x},{ya + 20} C{x + 30},{ya + 20} {x + 30},{yb + 20} {x},{yb + 20}" '
                         f'fill="none" stroke="#5a6b7b" marker-end="url(#arr)"/>')
        else:
            parts.append(f'<path d="M{xa + wa},{ya + 20} C{(xa + wa + xb) / 2},{ya + 20} {(xa + wa + xb) / 2},'
                         f'{yb + 20} {xb},{yb + 20}" fill="none" stroke="#5a6b7b" marker-end="url(#arr)"/>')
    parts.append("</svg>")
    return "\n".join(parts)


def preview_svg(path_or_dict):
    if isinstance(path_or_dict, str):
        with open(path_or_dict) as f:
            path_or_dict = yaml.safe_load(f)["workflow2"]
    return workflow_svg(path_or_dict)


def meta_from_archive(arch):
    """Metadata from an ELN archive written by :func:`eln_archive` (to reuse the metadata of an earlier data set)."""
    d = (arch or {}).get("data") or {}
    m = md.empty()
    m["entry"].update({k: v for k, v in {
        "experiment_alias": d.get("experiment_alias") or d.get("name"), "start_time": d.get("datetime"),
        "end_time": d.get("end_time"), "identifier_experiment": d.get("lab_id"),
        "experiment_description": d.get("description") or ""}.items() if v is not None})
    if d.get("project_name"):
        m["project"]["name"] = d["project_name"]
    si = d.get("sample_info") or {}
    for k in ("name", "identifier_sample", "identifier_owner", "identifier_parent", "chemical_formula", "atom_types",
              "physical_form", "preparation_date", "thickness", "thickness_unit", "history"):
        if si.get(k) not in (None, ""):
            m["sample"][k] = si[k]
    m["sample"]["is_simulation"] = bool(si.get("is_simulation"))
    if not si and d.get("samples"):
        m["sample"]["name"] = (d["samples"][0] or {}).get("name", "")
    ii = d.get("instrument_info") or {}
    for k in ("name", "location", "type", "vendor", "model", "serial_number", "operation_mode", "emitter_type",
              "detectors"):
        if ii.get(k):
            m["instrument"][k] = ii[k]
    if ii.get("acceleration_voltage"):
        m["instrument"]["voltage_kV"] = float(ii["acceleration_voltage"]) / 1e3
    m["users"] = [{k: v for k, v in u.items() if v} for u in d.get("users") or []] or [{}]
    m["tags"] = list(d.get("tags") or [])
    if d.get("elabftw_entry"):
        m["source"] = {"elabftw_url": d["elabftw_entry"]}
    return m
