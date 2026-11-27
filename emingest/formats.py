# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Format and kind of TEM data files.

* the **format** comes from the file name suffix: the readers of rosettasciio (``rsciio.IO_PLUGINS``) and the
  suffixes the ``em`` reader of pynxtools-em accepts;
* the **kind** (image, diffraction pattern, spectrum, focal series, tilt series, 4D-STEM, spectrum image ...) comes
  from the signals that rosettasciio reads lazily (dimensions, axis names and units, signal type), with hints from
  the name and format where the file does not say (e.g. ``.mib`` / ``.blo`` -> 4D-STEM).  The user can change it.

:func:`inspect_file` never raises: a file that cannot be read gets ``kind="other"`` and the error in ``note``.
:func:`group_files` puts sidecar files (``x.mrc`` + ``x.mrc.mdoc``, ``x.tif`` + ``x.hdr`` / ``x.txt``) with their
data file: pynxtools-em converts at most two data files at a time.  Nion Swift exports (``x.npy`` + ``x.json``, or
both in ``x.zip``) are read by :mod:`emingest.nion`; the ``.json`` is the sidecar of the ``.npy``.
"""
from __future__ import annotations

import importlib
import os
import re

from . import nion

KINDS = ["image", "diffraction pattern", "spectrum", "image series", "focal series", "tilt series",
         "diffraction series", "4D-STEM", "spectrum image", "line scan", "hologram", "other"]

# suffixes of the em reader of pynxtools-em 0.4 (utils/io_case_logic.py)
PYNXTOOLS_EM_SUFFIXES = (".emd", ".msa", ".dm2", ".dm3", ".dm4", ".dm5", ".tiff", ".tif", ".txt", ".zip", ".nsproj",
                         ".edaxh5", ".h5oina", ".oh5", ".dream3d", ".mtex.h5", ".h5", ".hdf5", ".hdr", ".mrc",
                         ".mrc.mdoc", ".rawtlt", ".shifts", ".xml")
SIDECAR_SUFFIXES = (".mdoc", ".rawtlt", ".shifts", ".hdr", ".txt", ".xml", ".emi", ".json")
NION_SUFFIXES = (".npy", ".json")
NEXUS_SUFFIXES = (".nxs", ".nx", ".nexus")
SKIP_NAMES = ("eln_data.yaml", ".archive.yaml", ".archive.json")
# formats that are (almost) always 4D-STEM / diffraction stacks
FOURD_SUFFIXES = (".mib", ".blo", ".tvips", ".prz", ".de5")


def _plugins():
    try:
        from rsciio import IO_PLUGINS
        return IO_PLUGINS
    except Exception:  # noqa: BLE001
        return []


def rsciio_extensions():
    return sorted({"." + e.lower() for p in _plugins() for e in p["file_extensions"]})


def supported_extensions():
    """All suffixes the dashboard accepts (rosettasciio + pynxtools-em + Nion exports + NeXus)."""
    return sorted(set(rsciio_extensions()) | set(PYNXTOOLS_EM_SUFFIXES) | set(NEXUS_SUFFIXES) | {".mdoc"}
                  | set(NION_SUFFIXES))


def rsciio_plugins_for(path):
    """rosettasciio plugins whose extensions match the file (several for .h5 / .xml / .csv ...)."""
    ext = os.path.splitext(path)[1][1:].lower()
    return [p for p in _plugins() if ext in [e.lower() for e in p["file_extensions"]]]


def pynxtools_em_accepts(path):
    n = path.lower()
    return n.endswith(PYNXTOOLS_EM_SUFFIXES) and not n.endswith(SKIP_NAMES)


def is_sidecar(path):
    n = path.lower()
    return n.endswith((".mrc.mdoc", ".rawtlt", ".shifts", ".mdoc")) or (n.endswith(".json") and not n.endswith(
        SKIP_NAMES))


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _unit_class(u):
    u = (u or "").strip().replace("µ", "u").replace("μ", "u").replace("Å", "A")
    if not u or u in ("<undefined>", "t.u.", "a.u."):
        return ""
    ul = u.lower()
    if ul in ("ev", "kev", "mev"):
        return "energy"
    if ul.startswith("1/") or ul.endswith("^-1") or ul.endswith("-1") or "1 /" in ul:
        return "reciprocal"
    if ul in ("mrad", "rad", "urad"):
        return "angle_small"
    if ul in ("deg", "degree", "degrees", "°"):
        return "angle"
    if ul in ("nm", "um", "pm", "a", "m", "mm", "angstrom"):
        return "length"
    if ul in ("s", "ms", "us", "ns", "min"):
        return "time"
    if ul in ("px", "pixel", "pixels"):
        return "pixel"
    return "other"


def _axis_name(a):
    return str(a.get("name") or "").lower()


def classify(signal, ext="", name=""):
    """Kind of a rosettasciio signal dict (keys data, axes, metadata)."""
    axes = list(signal.get("axes") or [])
    md = signal.get("metadata") or {}
    stype = str((md.get("Signal") or {}).get("signal_type") or "").lower()
    nav = [a for a in axes if a.get("navigate")]
    sig = [a for a in axes if not a.get("navigate")]
    if not axes:
        shape = tuple(getattr(signal.get("data"), "shape", ()) or ())
        sig = [{} for _ in shape[-2:]]
        nav = [{} for _ in shape[:-2]]
    sclass = [_unit_class(a.get("units")) for a in sig]
    nclass = [_unit_class(a.get("units")) for a in nav]
    nnames = " ".join(_axis_name(a) for a in nav) + " " + name.lower()
    diffr = ("diffraction" in stype or "dp" == stype or any(c in ("reciprocal", "angle_small") for c in sclass))
    spectral = (len(sig) == 1 and (stype in ("eels", "eds_tem", "eds_sem", "eds", "cl", "edx")
                                   or "eels" in stype or "eds" in stype or sclass == ["energy"]))
    if ext in FOURD_SUFFIXES and len(nav) + len(sig) >= 3:
        return "4D-STEM" if len(nav) >= 2 else "diffraction series"
    if len(sig) == 1:
        if spectral or not nav:
            return {0: "spectrum", 1: "line scan"}.get(len(nav), "spectrum image")
        return "spectrum image" if len(nav) >= 2 else "line scan"
    if len(sig) >= 2:
        if "hologram" in stype or "holo" in name.lower():
            if not nav:
                return "hologram"
        if not nav:
            return "diffraction pattern" if diffr else "image"
        if len(nav) >= 2:
            return "4D-STEM" if diffr or ext in FOURD_SUFFIXES or len(nav) == 2 else "image series"
        # one navigation axis
        if re.search(r"focus|defocus|\bdf\b|through.?focal|focal", nnames) or (nclass == ["length"] and not diffr
                                                                                and "z" not in nnames):
            return "focal series"
        if re.search(r"tilt|angle|alpha|beta|tomo", nnames) or nclass[0] == "angle" or ext in (".mrc", ".ali", ".st"):
            return "tilt series"
        return "diffraction series" if diffr else "image series"
    return "other"


def _shape(signal):
    try:
        return tuple(int(x) for x in signal["data"].shape)
    except Exception:  # noqa: BLE001
        return tuple(int(a.get("size", 0)) for a in signal.get("axes") or [])


def _axes_info(signal):
    out = []
    for a in signal.get("axes") or []:
        out.append({"name": str(a.get("name") or ""), "size": int(a.get("size") or 0),
                    "scale": _f(a.get("scale"), 1.0), "offset": _f(a.get("offset"), 0.0),
                    "units": str(a.get("units") or ""), "navigate": bool(a.get("navigate"))})
    return out


def read_signals(path, lazy=True):
    """rosettasciio signal dicts of the file (first plugin that reads it).  Returns (plugin_name, signals)."""
    if nion.is_nion(path):
        return nion.FORMAT, [nion.signal(path, load=None if lazy else True)]
    errs = []
    for p in rsciio_plugins_for(path):
        try:
            mod = importlib.import_module(p["api"])
            try:
                sigs = mod.file_reader(path, lazy=lazy)
            except TypeError:
                sigs = mod.file_reader(path)
            sigs = [s for s in (sigs or []) if isinstance(s, dict) and "data" in s]
            if sigs:
                return p["name"], sigs
        except Exception as e:  # noqa: BLE001
            errs.append(f"{p['name']}: {e}")
    raise ValueError("; ".join(errs) or "no rosettasciio reader for this file")


def inspect_file(path, max_eager_mb=200):
    """dict(name, path, size, format, readers, kind, kinds (per signal), shape, axes, title, signal_type, note)."""
    name = os.path.basename(path)
    n = name.lower()
    ext = os.path.splitext(n)[1]
    info = {"name": name, "path": path, "size": os.path.getsize(path) if os.path.isfile(path) else 0,
            "format": "", "readers": [], "kind": "other", "kinds": [], "shape": [], "axes": [], "title": "",
            "signal_type": "", "note": "", "metadata": {}}
    if n.endswith(SKIP_NAMES):
        info.update(format="metadata", note="metadata file (not converted)")
        return info
    if n.endswith(".eln"):
        info.update(format="elabFTW export (.eln)", note="eLabFTW export: open it in tab 3 (not uploaded as data)")
        return info
    if nion.is_nion(path):
        return _inspect_nion(path, info)
    if pynxtools_em_accepts(n):
        info["readers"].append("pynxtools-em")
    if is_sidecar(n):
        note = "metadata of the Nion data of the same name" if n.endswith(".json") else \
            "sidecar file (converted with its data file)"
        info.update(format="sidecar", kind="other", note=note)
        return info
    if n.endswith(NEXUS_SUFFIXES):
        info.update(format="NeXus", readers=["NeXus"], note="NeXus file: parsed by NOMAD as it is")
        try:
            import h5py
            with h5py.File(path, "r") as f:
                for k in f:
                    d = f[k].get("definition")
                    if d is not None:
                        info["note"] = f"NeXus {d[()].decode() if hasattr(d[()], 'decode') else d[()]}"
                        break
        except Exception:  # noqa: BLE001
            pass
        return info
    plugins = rsciio_plugins_for(n)
    if plugins:
        info["readers"].append("rosettasciio")
    if not plugins:
        info.update(format=ext.lstrip(".").upper() or "?",
                    note="" if info["readers"] else "format not supported by rosettasciio / pynxtools-em: "
                                                    "uploaded as it is")
        return info
    try:
        lazy = True
        try:
            fmt, sigs = read_signals(path, lazy=True)
        except Exception:  # noqa: BLE001
            if info["size"] > max_eager_mb * 1e6:
                raise
            lazy = False
            fmt, sigs = read_signals(path, lazy=False)
        info["format"] = fmt
        kinds = [classify(s, ext, name) for s in sigs]
        main = max(range(len(sigs)), key=lambda i: _nbytes(sigs[i]))
        s = sigs[main]
        md = s.get("metadata") or {}
        info.update(kind=kinds[main], kinds=kinds, shape=list(_shape(s)), axes=_axes_info(s),
                    title=str((md.get("General") or {}).get("title") or ""),
                    signal_type=str((md.get("Signal") or {}).get("signal_type") or ""),
                    metadata=_instrument_hints(md))
        if len(sigs) > 1:
            info["note"] = f"{len(sigs)} signals: " + ", ".join(f"{k} {tuple(_shape(x))}" for k, x in zip(kinds, sigs))
        if not lazy and info["size"] > 50e6:
            info["note"] = (info["note"] + "; " if info["note"] else "") + "read completely (no lazy reading)"
        for x in sigs:                                         # close lazily opened files
            _close(x)
    except Exception as e:  # noqa: BLE001
        info.update(format=plugins[0]["name"], note=f"not readable with rosettasciio: {str(e)[:200]}")
    return info


def _inspect_nion(path, info):
    try:
        s = nion.signal(path, load=False)
    except Exception as e:  # noqa: BLE001
        info.update(format=nion.FORMAT, note=f"not readable as Nion export: {str(e)[:200]}")
        return info
    has_json = bool(s["original_metadata"])
    kind = s["kind"] or classify(s, os.path.splitext(path)[1].lower(), os.path.basename(path))
    info.update(format=nion.FORMAT, readers=["EMingest (Nion)"], kind=kind, kinds=[kind], shape=list(s["shape"]),
                axes=_axes_info(s), title=str(s["metadata"]["General"].get("title") or ""),
                signal_type=s["metadata"]["Signal"]["signal_type"], metadata=_instrument_hints(s["metadata"]))
    if not has_json:
        info["note"] = "no .json with the metadata found" + (" in the zip" if path.lower().endswith(".zip") else
                                                              " (expected: same name, .json)")
    return info


def _nbytes(s):
    try:
        import numpy as np
        return int(np.prod(s["data"].shape))
    except Exception:  # noqa: BLE001
        return 0


def _close(s):
    d = s.get("data")
    try:
        for f in getattr(d, "_file_handles", []) or []:
            f.close()
    except Exception:  # noqa: BLE001
        pass


def _instrument_hints(md):
    """Acquisition metadata that rosettasciio maps to HyperSpy keys (for prefilling the form)."""
    tem = (md.get("Acquisition_instrument") or {}).get("TEM") or {}
    out = {}
    if _f(tem.get("beam_energy")):
        out["voltage_kV"] = _f(tem.get("beam_energy"))
    for k in ("microscope", "magnification", "camera_length", "convergence_angle", "probe_area", "beam_current",
              "acquisition_mode", "detector", "vendor"):
        if tem.get(k) not in (None, ""):
            out[k] = tem.get(k)
    gen = md.get("General") or {}
    if gen.get("date"):
        out["date"] = f"{gen.get('date')}T{gen.get('time', '00:00:00')}"
    return out


def group_files(paths):
    """Conversion groups: [[data file, optional sidecars ...]].  Files that are neither data nor sidecar (ELN yaml,
    archives) are left out."""
    paths = [p for p in paths if not os.path.basename(p).lower().endswith(SKIP_NAMES)]
    data = [p for p in paths if not is_sidecar(p)]
    side = [p for p in paths if is_sidecar(p)]
    groups = {p: [p] for p in data}
    stems = {}
    for p in data:
        stems.setdefault(_stem(p), []).append(p)
    # .hdr / .txt next to a .tif of the same stem are sidecars (Hitachi, Tescan, JEOL)
    for st, ps in stems.items():
        tif = [p for p in ps if p.lower().endswith((".tif", ".tiff"))]
        if len(tif) == 1:
            for p in ps:
                if p.lower().endswith((".hdr", ".txt")) and p in groups:
                    groups[tif[0]].append(p)
                    del groups[p]
    for s in side:
        n = os.path.basename(s).lower()
        target = None
        for p in groups:
            b = os.path.basename(p).lower()
            if n.startswith(b) or _stem(s) == _stem(p):
                target = p
                break
        if target:
            groups[target].append(s)
        else:
            groups[s] = [s]
    return list(groups.values())


def _stem(p):
    n = os.path.basename(p)
    for suf in (".mrc.mdoc", ".mtex.h5"):
        if n.lower().endswith(suf):
            return n[: -len(suf)]
    return os.path.splitext(n)[0]
