# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""pynxtools reader ``tem`` (``dataconverter --reader tem --nxdl NXem``): NXem from TEM data of any format that
rosettasciio reads, with the metadata of ``eln_data.yaml``.

1. the ``em`` reader of pynxtools-em is used for the ELN file and for all data files it knows (vendor metadata of
   Thermo Fisher / Velox, Gatan, Nion, Bruker, EDAX, Oxford, JEOL, Hitachi, Tescan, Zeiss, MRC + mdoc ...);
2. if it wrote no data (format it does not know, e.g. TIA .ser/.emi, Bruker .bcf, Merlin .mib, blockfile, .hspy,
   DENS, TVIPS, generic TIFF / PNG ...), every signal that rosettasciio reads from the file is written as NXimage /
   NXspectrum of an event (image_2d, image_3d for focal / tilt / time series, image_4d for 4D-STEM, spectrum_0d .. 3d
   for spectra, line scans and spectrum images; other dimensionalities as NXdata);
3. the instrument (``/ENTRY/measurement/instrument``: name, location, fabrication, high tension) and the sample
   fields that pynxtools-em does not read (identifier, parent, physical form, description) are added.
"""
from __future__ import annotations

import hashlib
import os
import re

import numpy as np
import yaml
from pynxtools.dataconverter.readers.base.reader import BaseReader

from . import formats

ENTRY = 1


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def _unit(u):
    """pint-compatible unit string or None."""
    u = str(u or "").strip()
    if not u or u in ("<undefined>", "t.u.", "a.u.", "px", "pixel", "pixels", "counts", "Counts"):
        return None
    u = u.replace("µ", "u").replace("μ", "u").replace("Å", "angstrom").replace("°", "degree")
    try:
        from pynxtools_em.utils.pint_custom_unit_registry import ureg
        ureg.Unit(u)
        return u
    except Exception:  # noqa: BLE001
        return None


def _compressed(a):
    a = np.asarray(a)
    if a.ndim == 0 or a.size < 1024:
        return a
    try:
        from pynxtools.dataconverter.chunk import prioritized_axes_heuristic
        chunks = prioritized_axes_heuristic(a, np.arange(a.ndim))
    except Exception:  # noqa: BLE001
        chunks = True
    return {"compress": a, "chunks": chunks, "strength": 1}


def has_data(template):
    keys = [k for k in template.keys() if "/measurement/eventID[" in k]
    return any(k.endswith(("/real", "/intensity", "/complex", "/data")) or "/DATA[" in k for k in keys)


def _layout(nav, sig, energy):
    """(group, signal field, axis names in array order) or None for generic NXdata."""
    if sig == 1 and energy:
        names = {0: [], 1: ["axis_i"], 2: ["axis_j", "axis_i"], 3: ["axis_k", "axis_j", "axis_i"]}.get(nav)
        if names is None:
            return None
        return ("spectrumID[spectrum1]", f"spectrum_{nav}d", "intensity", names + ["axis_energy"])
    if sig == 1 and nav == 0:
        return ("imageID[image1]", "image_1d", "real", ["axis_i"])
    if sig == 2:
        names = {0: ["axis_j", "axis_i"], 1: ["axis_k", "axis_j", "axis_i"],
                 2: ["axis_m", "axis_k", "axis_j", "axis_i"]}.get(nav)
        if names is None:
            return None
        return ("imageID[image1]", f"image_{nav + 2}d", "real", names)
    return None


def write_signal(template, signal, event, source, checksum, kind="", plugin="", detector=""):
    """One rosettasciio signal -> NXem event (see module docstring).  Returns the target group."""
    data = signal["data"]
    if hasattr(data, "compute"):
        data = data.compute()
    data = np.asarray(data)
    axes = list(signal.get("axes") or [])
    if len(axes) != data.ndim or any(int(a.get("size", -1)) != n for a, n in zip(axes, data.shape)):
        axes = [{"name": f"dim_{i}", "size": n, "scale": 1.0, "offset": 0.0, "units": "", "navigate": i < data.ndim - 2}
                for i, n in enumerate(data.shape)]
    nav = sum(1 for a in axes if a.get("navigate"))
    sig = data.ndim - nav
    energy = sig == 1 and formats._unit_class(axes[-1].get("units")) in ("energy",) or (
        sig == 1 and "eels" in str(((signal.get("metadata") or {}).get("Signal") or {}).get("signal_type", "")).lower())
    # navigation axes first (rosettasciio gives them in array order; make sure the signal axes are last)
    order = [i for i, a in enumerate(axes) if a.get("navigate")] + [i for i, a in enumerate(axes)
                                                                    if not a.get("navigate")]
    if order != list(range(data.ndim)):
        data = np.transpose(data, order)
        axes = [axes[i] for i in order]
    md = signal.get("metadata") or {}
    title = str((md.get("General") or {}).get("title") or os.path.basename(source))
    prfx = f"/ENTRY[entry{ENTRY}]/measurement/eventID[event{event}]"
    lay = _layout(nav, sig, energy)
    if lay is None:
        grp = f"{prfx}/DATA[data1]"
        names = [f"axis_{c}" for c in "ijkmnopq"[: data.ndim]][::-1]
        sigf = "data"
        src_grp = grp
    else:
        sub, nxname, sigf, names = lay
        if np.iscomplexobj(data):
            sigf = "complex" if sigf == "real" else sigf
        src_grp = f"{prfx}/{sub}"
        grp = f"{src_grp}/{nxname}"
    template[f"{src_grp}/PROCESS[process]/input/file_name"] = os.path.basename(source)
    template[f"{src_grp}/PROCESS[process]/input/checksum"] = checksum
    template[f"{src_grp}/PROCESS[process]/input/algorithm"] = "sha256"
    template[f"{src_grp}/PROCESS[process]/input/type"] = "file"
    template[f"{src_grp}/PROCESS[process]/input/context"] = f"read with rosettasciio ({plugin})" if plugin else \
        "read with rosettasciio"
    if detector:
        template[f"{src_grp}/PROCESS[process]/detector_identifier"] = detector
    template[f"{grp}/title"] = f"{title}" + (f" ({kind})" if kind else "")
    template[f"{grp}/@signal"] = sigf
    template[f"{grp}/{sigf}"] = _compressed(data)
    template[f"{grp}/{sigf}/@long_name"] = "Intensity" if sigf != "complex" else "Complex amplitude"
    template[f"{grp}/@axes"] = list(names)
    for idx, (nm, a) in enumerate(zip(names, axes)):
        n = data.shape[idx]
        vals = np.asarray(float(a.get("offset") or 0.0) + np.arange(n) * float(a.get("scale") or 1.0),
                          dtype=np.float64)
        key = f"{grp}/AXISNAME[{nm}]"
        template[f"{grp}/@AXISNAME_indices[{nm}_indices]"] = np.uint32(idx)
        template[key] = vals
        u = _unit(a.get("units"))
        if u:
            template[f"{key}/@units"] = u
        label = str(a.get("name") or "").strip() or nm.replace("axis_", "")
        template[f"{key}/@long_name"] = f"{label}" + (f" ({u})" if u else "")
    return grp


def _id_type(v):
    lv = v.lower()
    if lv.startswith(("10.", "doi:", "https://doi.org/")):
        return "DOI"
    if lv.startswith(("http://", "https://")):
        return "URL"
    if lv.startswith("elabftw:"):
        return "elabFTW"
    return "local"


def add_extra_metadata(template, eln):
    """Instrument and sample keys of eln_data.yaml that pynxtools-em does not read."""
    ins = eln.get("instrument") or {}
    base = f"/ENTRY[entry{ENTRY}]/measurement/instrument"
    if ins.get("name"):
        template[f"{base}/name"] = str(ins["name"])
    for k in ("location", "type"):
        if ins.get(k):
            template[f"{base}/{k}"] = str(ins[k])
    if ins.get("emitter_type"):
        template[f"{base}/ebeam_column/electron_source/emitter_type"] = str(ins["emitter_type"])
        template[f"{base}/ebeam_column/electron_source/probe"] = "electron"
    for k in ("vendor", "model", "serial_number"):
        if ins.get(k):
            template[f"{base}/fabrication/{k}"] = str(ins[k])
    v = ins.get("voltage")
    if isinstance(v, dict) and v.get("value") is not None:
        template[f"{base}/ebeam_column/electron_source/voltage"] = float(v["value"])
        template[f"{base}/ebeam_column/electron_source/voltage/@units"] = str(v.get("unit") or "kV")
    if ins.get("operation_mode"):
        template[f"{base}/ebeam_column/operation_mode"] = str(ins["operation_mode"])
    s = eln.get("sample") or {}
    sb = f"/ENTRY[entry{ENTRY}]/sampleID[sample]"
    for k in ("identifier_sample", "identifier_parent", "physical_form", "description"):
        if s.get(k):
            template[f"{sb}/{k}"] = str(s[k])
            if k.startswith("identifier"):
                template[f"{sb}/{k}/@type"] = _id_type(str(s[k]))
    if s.get("atom_types") and not template.get(f"{sb}/atom_types"):
        template[f"{sb}/atom_types"] = str(s["atom_types"])
    for k in list(template.keys()):                     # pynxtools-em: ORCID as undocumented "identifier" (DOI)
        if "/userID[" in k and k.endswith("/identifier") and template.get(k):
            ident = str(template[k])
            m = re.search(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", ident)
            del template[k]
            if f"{k}/@type" in template:
                del template[f"{k}/@type"]
            if m:
                template[f"{k}NAME[identifier_orcid]"] = f"https://orcid.org/{m.group(0)}"
                template[f"{k}NAME[identifier_orcid]/@type"] = "ORCID"
            else:
                template[f"{k}NAME[identifier_other]"] = ident
                template[f"{k}NAME[identifier_other]/@type"] = _id_type(ident)
    if ins and not template.get(f"{base}/name"):
        template[f"{base}/name"] = str(ins.get("model") or ins.get("vendor") or "electron microscope")


class TEMReader(BaseReader):
    """See module docstring."""

    supported_nxdls = ["NXem"]

    def read(self, template=None, file_paths=(), objects=None):
        from pynxtools_em.reader import EMReader

        files = tuple(str(f) for f in file_paths or ())
        elns = [f for f in files if f.lower().endswith(("eln_data.yaml", "eln_data.yml"))]
        data = [f for f in files if f not in elns and not f.lower().endswith((".yaml", ".yml"))]
        em_data = [f for f in data if formats.pynxtools_em_accepts(f)]
        out = None
        if em_data and len(em_data) == len(data):
            try:
                out = EMReader().read(template=template, file_paths=tuple(em_data + elns[:1]))
            except Exception as e:  # noqa: BLE001  -- vendor parser failed: generic path below
                print(f"tem reader: pynxtools-em failed on {em_data}: {e}")
                out = None
        if not out or not has_data(out):
            template.clear()
            out = EMReader().read(template=template, file_paths=tuple(elns[:1])) if elns else template
            if out is None or out == {}:
                out = template
            event = 1
            detector = ""
            if elns:
                with open(elns[0], encoding="utf-8") as fh:
                    dets = ((yaml.safe_load(fh) or {}).get("instrument") or {}).get("detectors") or []
                detector = str(dets[0]) if dets else ""
            for f in data:
                if formats.is_sidecar(f):
                    continue
                try:
                    _fmt, sigs = formats.read_signals(f, lazy=False)
                except Exception as e:  # noqa: BLE001
                    print(f"tem reader: rosettasciio cannot read {f}: {e}")
                    continue
                chk = _sha256(f)
                for s in sigs:
                    write_signal(out, s, event, f, chk, kind=formats.classify(s, os.path.splitext(f)[1].lower(),
                                                                              os.path.basename(f)),
                                 plugin=_fmt, detector=detector)
                    event += 1
            if data and event == 1:
                raise ValueError("no data could be read from " + ", ".join(os.path.basename(f) for f in data))
            try:
                from pynxtools_em.utils.nx_default_plots import NxEmDefaultPlotResolver
                NxEmDefaultPlotResolver().priority_select(out, ENTRY)
            except Exception as e:  # noqa: BLE001
                print(f"tem reader: no default plot: {e}")
        if elns:
            with open(elns[0], encoding="utf-8") as f:
                add_extra_metadata(out, yaml.safe_load(f) or {})
        return out


READER = TEMReader
