# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Nion Swift exports: ``<name>.npy`` (data) + ``<name>.json`` (metadata), or both zipped in ``<name>.zip``.

Reading follows ``nion2nxs`` of fit4DSTEM (``fit4dstem/convert.py``): the .npy is memory mapped, a .npy inside a zip
is decompressed on the fly, and large data are streamed along the first axis (scan rows for 4D-STEM), so they never
need to fit into memory.  Integer counts stored as floats are written losslessly as uint16 / uint32.

* :func:`is_nion` - is the file a Nion export (``.npy``; ``.zip`` with exactly one ``.npy``)?
* :func:`signal` - rosettasciio-like signal dict (axes with calibration and ``navigate``, HyperSpy-style
  metadata for the form, the original JSON), lazily: ``data`` is only loaded when it is small;
* :func:`nx_metadata` - template entries of the NXem file (high tension, convergence angle, detector, times);
* :func:`fill_pending` - writes the data of large exports into the NXem file after pynxtools has written it.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import zipfile

import numpy as np

FORMAT = "Nion Swift export"
# data larger than this are not loaded by the pynxtools reader but streamed into the file afterwards
MAX_MEMORY_MB = float(os.environ.get("EMINGEST_NION_MAX_MEMORY_MB", "256"))
# (template key of the signal field, source path): filled by the reader, written by fill_pending
PENDING: list[tuple[str, str]] = []


def read_npy_header(fh):
    """(shape, fortran_order, dtype) of a .npy stream positioned at its start (public numpy API only)."""
    version = np.lib.format.read_magic(fh)
    if version == (1, 0):
        return np.lib.format.read_array_header_1_0(fh)
    return np.lib.format.read_array_header_2_0(fh)


def _zip_members(path):
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n]
    except (zipfile.BadZipFile, OSError):
        return [], []
    return ([n for n in names if n.lower().endswith(".npy")], [n for n in names if n.lower().endswith(".json")])


def is_nion(path):
    """``.npy`` file, or ``.zip`` with exactly one ``.npy`` (and a ``.json``) inside."""
    n = str(path).lower()
    if n.endswith(".npy"):
        return True
    if n.endswith(".zip") and os.path.isfile(path):
        npy, _js = _zip_members(path)
        return len(npy) == 1
    return False


def json_for(npy_path):
    """The ``.json`` of a ``.npy`` (same base name), or None."""
    base = os.path.splitext(npy_path)[0]
    for ext in (".json", ".JSON"):
        if os.path.isfile(base + ext):
            return base + ext
    return None


class NpySource:
    """Reader of a .npy file (memory map) or of a .npy inside a zip (streamed decompression), with its JSON."""

    def __init__(self, path):
        self.path = str(path)
        self.zip = None
        self.arr = None
        self.meta = {}
        if self.path.lower().endswith(".zip"):
            npy, js = _zip_members(self.path)
            if len(npy) != 1:
                raise ValueError(f"{os.path.basename(self.path)}: expected one .npy in the zip, found {npy}")
            self.zip = zipfile.ZipFile(self.path)
            self.member = npy[0]
            stem = os.path.splitext(self.member)[0]
            js = sorted(js, key=lambda n: os.path.splitext(n)[0] != stem)    # the .json of the same name first
            if js:
                self.meta = json.loads(self.zip.read(js[0]))
            with self.zip.open(self.member) as fh:
                self.shape, self.fortran, self.dtype = read_npy_header(fh)
        else:
            js = json_for(self.path)
            if js:
                with open(js, encoding="utf-8") as f:
                    self.meta = json.load(f)
            self.arr = np.load(self.path, mmap_mode="r")
            self.shape, self.fortran, self.dtype = self.arr.shape, not self.arr.flags["C_CONTIGUOUS"], self.arr.dtype
        self.shape = tuple(int(x) for x in self.shape)
        self.dtype = np.dtype(self.dtype)
        if self.fortran and self.arr is None:
            raise ValueError(f"{os.path.basename(self.path)}: Fortran-ordered arrays in zips are not supported")

    @property
    def nbytes(self):
        return int(np.prod(self.shape, dtype=np.int64)) * self.dtype.itemsize

    def load(self):
        if self.arr is not None:
            return np.array(self.arr)
        rows = [r for _i, r in self.rows()]
        return np.stack(rows) if rows else np.zeros(self.shape, self.dtype)

    def rows(self, start=0):
        """Yield (i, array of shape[1:]) along the first axis from ``start`` on."""
        if not self.shape:
            yield 0, self.load()
            return
        n0 = self.shape[0]
        if self.arr is not None:
            for i in range(start, n0):
                yield i, np.asarray(self.arr[i])
            return
        items = int(np.prod(self.shape[1:], dtype=np.int64))
        nbytes = items * self.dtype.itemsize
        with self.zip.open(self.member) as fh:
            read_npy_header(fh)
            for i in range(n0):
                buf = bytearray(nbytes)
                mv = memoryview(buf)
                got = 0
                while got < nbytes:
                    k = fh.readinto(mv[got:])
                    if not k:
                        raise OSError(f"{os.path.basename(self.path)}: unexpected end of data in row {i}")
                    got += k
                if i >= start:
                    yield i, np.frombuffer(buf, self.dtype).reshape(self.shape[1:])

    def close(self):
        if self.zip is not None:
            self.zip.close()
            self.zip = None
        self.arr = None


# ------------------------------------------------------------------ metadata --
def _find(d, keys):
    """First scalar value of one of ``keys`` anywhere in the nested dict / list ``d``."""
    if isinstance(d, dict):
        for k, v in d.items():
            if k in keys and not isinstance(v, (dict, list)):
                return v
        for v in d.values():
            r = _find(v, keys)
            if r is not None:
                return r
    elif isinstance(d, list):
        for v in d:
            r = _find(v, keys)
            if r is not None:
                return r
    return None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _iso(v):
    """ISO time with time zone of a Nion time (string, or dict local_datetime + tz like "+0100")."""
    if isinstance(v, dict):
        s, tz = v.get("local_datetime"), str(v.get("tz") or "")
        if s and re.match(r"^[+-]\d{4}$", tz):
            s = f"{s}{tz[:3]}:{tz[3:]}"
        v = s
    if not v:
        return None
    try:
        t = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.isoformat()


def summary(meta):
    """Acquisition parameters of the Nion JSON (None where missing)."""
    md = meta.get("metadata") or {}
    s = {"voltage_V": _f(_find(md, ("high_tension", "EHT"))),
         "semi_convergence_angle_rad": _f(_find(md, ("probe_ha",))),
         "detector": _find(md, ("hardware_source_name",)) or _find(md, ("description",)),
         "start_time": _iso(meta.get("datetime_original") or meta.get("created")
                            or _find(md, ("data_collection_date",))),
         "exposure_s": _f(_find(md.get("hardware_source") or {}, ("exposure",))),
         "defocus_m": _f(_find(md.get("instrument") or {}, ("defocus",))),
         "fov_nm": _f(_find(md.get("scan") or {}, ("fov_nm",))),
         "scan_rotation_deg": _f(_find(md.get("scan") or {}, ("rotation_deg",))),
         "title": meta.get("title") or ""}
    return s


def _dims(meta, shape):
    """(sequence, collection, datum) dimension counts: from the JSON, else from the shape and calibrations."""
    nd = len(shape)
    cdc, ddc = meta.get("collection_dimension_count"), meta.get("datum_dimension_count")
    seq = 1 if meta.get("is_sequence") else 0
    if isinstance(cdc, int) and isinstance(ddc, int) and seq + cdc + ddc == nd:
        return seq, cdc, ddc
    cal = _calibrations(meta)
    last = str((cal[-1] if cal else {}).get("units") or "").strip().lower()
    if nd == 4:
        return 0, 2, 2
    if nd == 3:
        return (0, 2, 1) if last in ("ev", "kev") else (1, 0, 2)
    if nd == 2:
        return (0, 1, 1) if last in ("ev", "kev") else (0, 0, 2)
    return 0, 0, nd


def _calibrations(meta):
    return meta.get("spatial_calibrations") or meta.get("dimensional_calibrations") or []


def signal(path, load=None):
    """rosettasciio-like dict(data, shape, dtype, axes, metadata, original_metadata, kind, source).

    ``load``: True / False, or None = load when smaller than ``MAX_MEMORY_MB``.  Without loading, ``data`` is None
    and the data are streamed into the NXem file by :func:`fill_pending`."""
    src = NpySource(path)
    try:
        meta, shape = src.meta, src.shape
        seq, col, dat = _dims(meta, shape)
        cal = _calibrations(meta)
        names = (["sequence"] * seq + {2: ["scan y", "scan x"], 1: ["scan"]}.get(col, [f"c{i}" for i in range(col)])
                 + {2: ["detector y", "detector x"] if col == 2 else ["y", "x"], 1: ["energy"]}.get(
                     dat, [f"d{i}" for i in range(dat)]))
        axes = []
        for i, n in enumerate(shape):
            c = cal[i] if i < len(cal) and isinstance(cal[i], dict) else {}
            units = str(c.get("units") or "")
            # 4D-STEM exports calibrate the camera axes like an EELS spectrometer (eV): use pixels
            if col == 2 and dat == 2 and i >= seq + col and units.strip().lower() in ("ev", "kev"):
                c, units = {}, ""
            axes.append({"name": names[i], "size": int(n), "scale": _f(c.get("scale")) or 1.0,
                         "offset": _f(c.get("offset")) or 0.0, "units": units, "navigate": i < seq + col})
        s = summary(meta)
        tem = {}
        if s["voltage_V"]:
            tem["beam_energy"] = s["voltage_V"] / 1000.0
        if s["semi_convergence_angle_rad"]:
            tem["convergence_angle"] = s["semi_convergence_angle_rad"] * 1000.0
        if s["detector"]:
            tem["detector"] = str(s["detector"])
        tem["vendor"] = "Nion"
        gen = {"title": s["title"] or os.path.splitext(os.path.basename(path))[0], "original_filename":
               os.path.basename(path)}
        if s["start_time"]:
            gen["date"], gen["time"] = s["start_time"][:10], s["start_time"][11:19]
        if col == 2 and dat == 2:
            kind = "4D-STEM"
        elif dat == 1:
            kind = {0: "spectrum", 1: "line scan"}.get(col, "spectrum image")
        elif dat == 2 and col == 0:
            kind = "image series" if seq else "image"
        else:
            kind = ""
        stype = {"4D-STEM": "diffraction", "spectrum": "EELS", "line scan": "EELS", "spectrum image": "EELS"}.get(kind, "")
        if load is None:
            load = src.nbytes <= MAX_MEMORY_MB * 1e6
        data = src.load() if load else None
        return {"data": data, "shape": shape, "dtype": str(src.dtype), "axes": axes, "kind": kind, "source": str(path),
                "metadata": {"General": gen, "Signal": {"signal_type": stype},
                             "Acquisition_instrument": {"TEM": tem}},
                "original_metadata": meta, "summary": s}
    finally:
        src.close()


def nx_metadata(template, s, entry=1, event=1):
    """NXem entries of the Nion summary ``s``: high tension, semi-convergence angle, detector, event times.
    (The ELN form, written afterwards, overrides the high tension.)"""
    base = f"/ENTRY[entry{entry}]/measurement/instrument"
    if s.get("voltage_V"):
        template[f"{base}/ebeam_column/electron_source/voltage"] = float(s["voltage_V"])
        template[f"{base}/ebeam_column/electron_source/voltage/@units"] = "V"
        template.setdefault(f"{base}/ebeam_column/electron_source/probe", "electron")
    ev = f"/ENTRY[entry{entry}]/measurement/eventID[event{event}]"
    if s.get("voltage_V"):                           # state of the instrument during the event (as pynxtools-em)
        template[f"{ev}/instrument/ebeam_column/electron_source/voltage"] = float(s["voltage_V"])
        template[f"{ev}/instrument/ebeam_column/electron_source/voltage/@units"] = "V"
    opt = f"{ev}/instrument/optics"
    for key, field, unit in (("semi_convergence_angle_rad", "semi_convergence_angle", "rad"),
                             ("fov_nm", "field_of_view", "nm"), ("scan_rotation_deg", "rotation", "degree")):
        if s.get(key) is not None:
            template[f"{opt}/{field}"] = float(s[key])
            template[f"{opt}/{field}/@units"] = unit
    if s.get("start_time"):
        template[f"{ev}/start_time"] = s["start_time"]
        template[f"{ev}/end_time"] = s["start_time"]


# --------------------------------------------------------------- storage type --
def lossless_dtype(data_or_dtype, vmax=None, vmin=None, integral=None):
    """Smallest lossless type: non-negative integer values (also of floats) -> uint16 / uint32, else the source type."""
    if not isinstance(data_or_dtype, np.dtype):
        a = np.asarray(data_or_dtype)
        if a.size == 0:
            return a.dtype
        dt = a.dtype
        if dt.kind not in "fiu":
            return dt
        vmin, vmax = float(a.min()), float(a.max())
        integral = dt.kind in "iu" or bool(np.array_equal(a, np.floor(a)))
    else:
        dt = data_or_dtype
    if dt.kind not in "fiu" or not integral or vmin is None or vmin < 0:
        return dt
    for t in (np.uint16, np.uint32):
        if vmax <= np.iinfo(t).max and (dt.kind == "f" or np.dtype(t).itemsize <= dt.itemsize):
            return np.dtype(t)
    return dt


def _h5_path(key):
    """HDF5 path of a pynxtools template key (``/ENTRY[entry1]/a/eventID[event1]/b`` -> ``/entry1/a/event1/b``)."""
    return re.sub(r"[A-Za-z_]+\[([^\]]+)\]", r"\1", key)


def _chunks(shape, itemsize, target=4 << 20):
    ch = [1] + list(shape[1:])
    i = 1
    while i < len(ch) and int(np.prod(ch)) * itemsize > target:
        ch[i] = max(1, ch[i] // 2) if ch[i] > 1 else 1
        if ch[i] == 1:
            i += 1
    return tuple(int(c) for c in ch)


class _Wider(Exception):
    pass


def stream_into(h5, path, source, log=None):
    """Replace the dataset ``path`` of the open h5py file by the data of ``source`` (streamed, lossless type,
    gzip + shuffle).  The attributes of the placeholder dataset are kept."""
    old = h5[path]
    attrs = dict(old.attrs)
    parent, name = old.parent, old.name.rsplit("/", 1)[1]
    del parent[name]
    src = NpySource(source)
    try:
        cand = [t for t in (np.uint16, np.uint32) if src.dtype.kind == "f" or
                (src.dtype.kind in "iu" and np.dtype(t).itemsize <= src.dtype.itemsize)]
        types = [np.dtype(t) for t in cand] + [src.dtype]
        for k, out_t in enumerate(types):
            if name in parent:
                del parent[name]
            ds = parent.create_dataset(name, shape=src.shape, dtype=out_t, chunks=_chunks(src.shape, out_t.itemsize)
                                       if len(src.shape) > 1 else None, compression="gzip", compression_opts=4,
                                       shuffle=True)
            try:
                for i, row in src.rows():
                    if out_t.kind == "u" and src.dtype.kind in "fi":
                        if row.size and (row.min() < 0 or (src.dtype.kind == "f" and not np.array_equal(
                                row, np.floor(row))) or row.max() > np.iinfo(out_t).max):
                            raise _Wider()
                    elif out_t.kind == "u" and row.size and row.max() > np.iinfo(out_t).max:
                        raise _Wider()
                    ds[i] = row.astype(out_t, copy=False)
                    if log and (i % 50 == 0 or i == src.shape[0] - 1):
                        log(f"{os.path.basename(source)}: row {i + 1}/{src.shape[0]} ({out_t.name})")
                break
            except _Wider:
                src.close()
                src = NpySource(source)
                continue
        for k_, v in attrs.items():
            ds.attrs[k_] = v
    finally:
        src.close()
    return ds


def fill_pending(output, log=None):
    """Write the data registered by the reader (``PENDING``) into the NXem file ``output``."""
    if not PENDING:
        return 0
    import h5py
    n = 0
    with h5py.File(output, "r+") as h5:
        for key, source in PENDING:
            stream_into(h5, _h5_path(key), source, log=log)
            n += 1
    PENDING.clear()
    return n
