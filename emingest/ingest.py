# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Headless ingestion of data sets into NOMAD (used by the dashboard, the batch tab and the command line).

A *data set* is a list of files plus the metadata (:mod:`emingest.metadata`).  :func:`build_dataset` writes the ELN
entry and ``eln_data.yaml`` (and, if wanted, converts to NXem with the conversion record and workflow);
:func:`upload_dataset` puts everything into a folder of a NOMAD upload; :func:`start_action` starts the NXem
conversion on a NOMAD worker instead.

:func:`ingest_session` does all of that for one TEM session of eLabFTW (server or ``.eln`` file, see
:mod:`emingest.tem_session`): metadata of session, sample and microscope, all files of the session's acquisition
folder (local or over SSH), the time-window check of the acquisitions, upload into ``tem/session_<id>_<title>``.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time

from . import convert as cv
from . import eln as elnm
from . import formats
from . import metadata as md
from . import tem_session as ts

ACTION_ID = "emingest_nomad.actions:emingest_nxem_conversion"
META_SUFFIXES = ("eln_data.yaml", ".archive.yaml", ".archive.json")


def safe_name(s, default="tem"):
    import re
    s = re.sub(r"[^\w.-]+", "_", str(s or "")).strip("._")
    return s[:60] or default


def inspect_files(paths, names=None):
    """[info] (formats.inspect_file + name = path inside the data set folder)."""
    out = []
    for i, p in enumerate(paths):
        info = formats.inspect_file(p)
        info["name"] = (names[i] if names else os.path.basename(p)).replace(os.sep, "/")
        info["path"] = p
        out.append(info)
    return out


def flag_window(infos, window, zone=ts.DEFAULT_TZ):
    """Note files whose acquisition time lies outside the session (specimen attribution unverified)."""
    n = 0
    for f in infos:
        when = (f.get("metadata") or {}).get("date")
        ok = ts.in_window(when, window, zone) if when else None
        if ok is False:
            f["note"] = ((f.get("note") or "") + "; " if f.get("note") else "") + \
                "acquired outside the session time: specimen attribution unverified"
            n += 1
    return n


def build_dataset(files, meta, name, folder, out, schema="plugin", conversion="none", max_gb=None, callback=None):
    """ELN entry + eln_data.yaml (+ NXem files, conversion record, workflow for conversion="local") in ``out``.

    ``files``: [dict(path (local), name (inside the folder), kind, format, shape, size, note)].
    Returns dict(name, folder, items=[(local, path inside folder)], results, workflow, meta, out)."""
    os.makedirs(out, exist_ok=True)
    data = [f for f in files if not f["name"].lower().endswith(META_SUFFIXES)]
    flist = [dict(path=f"{folder}/{f['name']}", kind=f.get("kind_user") or f.get("kind"), format=f.get("format"),
                  shape=f.get("shape"), size=f.get("size"), note=f.get("note")) for f in data]
    paths = elnm.write_eln(out, name, meta, flist, prefix=folder, schema=schema)
    items = [(f["path"], f["name"]) for f in data] + [(p, os.path.basename(p)) for p in paths.values()]
    rec, workflow = [], None
    if conversion == "local":
        nxdir = os.path.join(out, "nxem")
        results = cv.convert_all([f["path"] for f in data], paths["eln_data"], nxdir, callback=callback,
                                 max_gb=max_gb)
        by_local = {f["path"]: f"{folder}/{f['name']}" for f in data}
        items += [(r["output"], "nxem/" + os.path.basename(r["output"])) for r in results if r["status"] == "ok"]
        rec = [dict(r, inputs=[by_local.get(p, p) for p in r["inputs"]],
                    output=f"{folder}/nxem/{os.path.basename(r['output'])}") for r in results]
        for r in rec:
            r.pop("log", None)
        arch = elnm.write_conversion(out, name, rec, [x["path"] for x in flist], prefix=folder,
                                     eln_entry=f"{folder}/{name}.archive.yaml", schema=schema)
        items += [(p, os.path.basename(p)) for p in arch.values()]
        workflow = arch["workflow"]
    return {"name": name, "folder": folder, "items": items, "results": rec, "workflow": workflow, "meta": meta,
            "out": out}


def upload_dataset(client, ds, upload_id, progress=None, tmp_dir=None, wait=True, timeout=1800):
    r = client.upload(ds["items"], upload_id=upload_id, folder=ds["folder"], progress=progress, tmp_dir=tmp_dir)
    st = client.wait_processed(r["upload_id"], timeout=timeout) if wait else {}
    return {"upload": r, "state": st}


def start_action(client, ds, upload_id, max_gb=20.0):
    return client.start_action(ACTION_ID, {
        "upload_id": upload_id, "folder": ds["folder"], "eln_data": f"{ds['folder']}/{ds['name']}.eln_data.yaml",
        "eln_entry": f"{ds['folder']}/{ds['name']}.archive.yaml", "name": ds["name"], "max_file_gb": float(max_gb)})


# ------------------------------------------------------------------------------------------------ sessions
def collect_session_files(path, dest, location_kw=None, rules=None, progress=None, max_files=20000,
                          allowed_local_roots=None):
    """Files of a session's acquisition folder (mapped with ``rules``; local or ``user@host:/path``).  Local files
    are used in place, remote files are copied into ``dest``.  Returns ([(local path, relative name)], label)."""
    from .remote import Location, RemoteError
    spec = ts.map_path(path, rules)
    if not spec:
        raise RemoteError("the session has no acquisition folder (Path)")
    with Location(spec, allowed_local_roots=allowed_local_roots, **(location_kw or {})) as loc:
        if not loc.is_dir():
            raise RemoteError(f"acquisition folder not found: {loc.label()} (from {path!r}; set a path mapping if "
                              f"the folder is mounted elsewhere here)")
        listing = loc.walk(max_files=max_files)
        total = sum(s for _r, s, _t in listing) or 1
        out, done = [], 0
        for rel, size, _mt in listing:
            if not loc.remote:
                out.append((loc.child(rel), rel))
                continue
            dst = os.path.join(dest, *rel.split("/"))

            def prog(d, t, d0=done, rel=rel):
                if progress:
                    progress(rel, d0 + d, total)
            loc.fetch(loc.child(rel), dst, progress=prog)
            done += size
            out.append((dst, rel))
        return out, loc.label()


def ingest_session(source, session_id, *, client=None, upload_id=None, zone=ts.DEFAULT_TZ, rules=None,
                   location_kw=None, folder_template="tem/session_{id}_{title}", schema="plugin",
                   conversion="action", max_gb=20.0, work_dir=None, callback=None, allowed_local_roots=None,
                   dry_run=False):
    """One TEM session -> one folder of a NOMAD upload.  ``source``: ElabClient / ElnArchiveClient.
    ``callback(stage, message)``.  Returns dict(session, folder, files, status, message, upload_id, instance)."""
    cb = callback or (lambda *a: None)
    res = {"session": str(session_id), "status": "failed", "message": "", "files": 0, "folder": "",
           "upload_id": upload_id, "instance": None}
    work = tempfile.mkdtemp(prefix=f"session_{session_id}_", dir=work_dir)
    try:
        cb("metadata", f"session {session_id}: reading the records")
        s = ts.session_metadata(source, session_id, zone=zone)
        meta = md.merge(md.empty(), s["meta"], overwrite=True)
        title = meta["entry"].get("experiment_alias") or f"session {session_id}"
        res["title"] = title
        folder = folder_template.format(id=session_id, title=safe_name(title)).strip("/")
        res["folder"] = folder
        cb("files", f"session {session_id}: collecting {s['path']}")
        pairs, label = collect_session_files(
            s["path"], os.path.join(work, "data"), location_kw=location_kw, rules=rules,
            allowed_local_roots=allowed_local_roots,
            progress=lambda rel, d, t: cb("files", f"session {session_id}: {rel} ({100 * d / max(t, 1):.0f} %)"))
        if not pairs:
            raise ValueError(f"no files in the acquisition folder {label}")
        infos = inspect_files([p for p, _r in pairs], [r for _p, r in pairs])
        hints = md.from_file_hints(infos)
        hints["entry"] = {}                     # the session record decides start and end
        meta = md.merge(meta, hints)
        n_out = flag_window(infos, s["window"], zone)
        res["files"] = len(infos)
        res["outside_window"] = n_out
        res["missing"] = md.check(meta)[0]
        if dry_run:
            res.update(status="checked", message=f"{len(infos)} file(s) in {label}" +
                       (f"; {n_out} outside the session time" if n_out else ""), meta=meta, infos=infos)
            return res
        name = safe_name(title)
        cb("prepare", f"session {session_id}: ELN entry" + (" and NXem conversion" if conversion == "local" else ""))
        ds = build_dataset(infos, meta, name, folder, os.path.join(work, "out"), schema=schema,
                           conversion=conversion, max_gb=max_gb,
                           callback=lambda i, n, nm: cb("convert", f"session {session_id}: NXem {i + 1}/{n} {nm}"
                                                         if nm else f"session {session_id}: NXem done"))
        if client is None:
            res.update(status="prepared", message=f"written into {ds['out']}", dataset=ds)
            work = None                         # keep the output
            return res
        cb("upload", f"session {session_id}: uploading {len(ds['items'])} file(s)")
        up = upload_dataset(client, ds, upload_id, tmp_dir=work,
                            progress=lambda d, t: cb("upload", f"session {session_id}: "
                                                               f"{100 * d / max(t, 1):.0f} % uploaded"))
        res["upload_id"] = up["upload"]["upload_id"]
        res["gui_url"] = up["upload"].get("gui_url")
        msg = f"{len(infos)} file(s) in {folder}"
        if n_out:
            msg += f"; {n_out} acquired outside the session time"
        if conversion == "local":
            ok = sum(1 for r in ds["results"] if r["status"] == "ok")
            msg += f"; {ok} of {len(ds['results'])} converted to NXem"
        elif conversion == "action":
            try:
                res["instance"] = start_action(client, ds, res["upload_id"], max_gb)
                msg += "; NXem conversion started on a NOMAD worker"
            except Exception as e:  # noqa: BLE001
                msg += f"; the conversion action could not be started ({e})"
        res.update(status="ok", message=msg)
        return res
    except Exception as e:  # noqa: BLE001
        res["message"] = str(e)
        return res
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)


def ingest_sessions(source, ids, **kw):
    """Batch: :func:`ingest_session` for every ID (one folder each); a failing session does not stop the others."""
    out = []
    for sid in ids:
        t0 = time.time()
        r = ingest_session(source, sid, **kw)
        r["runtime_s"] = round(time.time() - t0, 1)
        out.append(r)
    return out
