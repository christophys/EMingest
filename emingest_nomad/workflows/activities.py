# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Activities of the NXem conversion action (all on the CPU task queue).

* ``stage_conversion``   checks access, finds the data files, the eln_data.yaml and the ELN entry in the upload; the
  files are used in place where the worker can read the upload's files, otherwise copied
* ``convert_files``      one pynxtools conversion per data file (+ sidecars) in a child process (time limit per file),
  conversion record + workflow archives (:mod:`emingest.eln`); heartbeats, stops when the action is cancelled
* ``publish_conversion`` adds the NXem files and the archives to the upload (NOMAD processes them)
"""
import contextvars
import os
import posixpath
import shutil
import tempfile
import threading
import time

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .backend import get_backend
from .models import ConversionInput, ConversionResult, StagedConversion


class Heartbeat:
    """Heartbeats from a helper thread + progress messages + cancellation."""

    def __init__(self, every_s=5.0):
        self.every = every_s
        self.msg = "started"
        self._stop = threading.Event()
        self._ctx = contextvars.copy_context()
        self._t = threading.Thread(target=lambda: self._ctx.run(self._loop), daemon=True)

    def _loop(self):
        while not self._stop.wait(self.every):
            try:
                activity.heartbeat(self.msg)
            except Exception:  # noqa: BLE001
                return

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()

    def stopped(self):
        try:
            return activity.is_cancelled()
        except RuntimeError:
            return False


def _safe(path):
    p = "/".join(s for s in str(path or "").replace("\\", "/").split("/") if s not in ("", "."))
    if any(s == ".." for s in p.split("/")):
        raise ApplicationError(f"invalid path {path!r}", non_retryable=True)
    return p


def _join(folder, name):
    return f"{folder}/{name}" if folder else name


@activity.defn
def stage_conversion(data: ConversionInput) -> StagedConversion:
    import yaml

    from emingest import convert, formats

    be = get_backend()
    be.check_access(data.upload_id, data.user_id)
    uid = data.upload_id
    folder = _safe(data.folder)
    out_folder = _safe(data.output_folder) if data.output_folder else _join(folder, "nxem")
    if data.files:
        files = [_safe(f) for f in data.files]
        missing = [f for f in files if not be.is_file(uid, f)]
        if missing:
            raise ApplicationError(f"not found in the upload: {', '.join(missing[:5])}", non_retryable=True)
    else:
        try:
            files = [_join(folder, n) for n, is_file in be.listdir(uid, folder) if is_file]
        except Exception as e:  # noqa: BLE001
            raise ApplicationError(f"folder {folder!r} not found in the upload: {e}", non_retryable=True) from e
    eln_data = _safe(data.eln_data) if data.eln_data else ""
    if not eln_data:
        cands = [f for f in files if f.lower().endswith(("eln_data.yaml", "eln_data.yml"))]
        if not cands and data.files:
            base = posixpath.dirname(files[0])
            cands = [_join(base, n) for n, f in be.listdir(uid, base) if f and n.lower().endswith("eln_data.yaml")]
        eln_data = sorted(cands)[0] if cands else ""
    elif not be.is_file(uid, eln_data):
        raise ApplicationError(f"{eln_data!r} not found in the upload", non_retryable=True)
    data_paths = [f for f in files if convert.convertible(f)]
    groups = [g for g in formats.group_files(data_paths) if convert.convertible(g[0])]
    if not groups:
        raise ApplicationError("no data files that rosettasciio or pynxtools-em can read" +
                               (f" in folder {folder!r}" if not data.files else ""), non_retryable=True)
    work = tempfile.mkdtemp(prefix="emingest_action_", dir=be.tmp_root())
    try:
        local = {}
        for g in groups:
            for p in g:
                local[p] = be.local_path(uid, p, os.path.join(work, "in"))
        eln_local = be.local_path(uid, eln_data, os.path.join(work, "meta")) if eln_data else ""
        name = data.name or (os.path.basename(eln_data)[: -len("eln_data.yaml")].rstrip("._-") if eln_data else "") \
            or (posixpath.basename(folder) if folder else "tem")
        eln_entry = _safe(data.eln_entry) if data.eln_entry else ""
        if not eln_entry and eln_data:
            cand = eln_data[: -len("eln_data.yaml")].rstrip(".") + ".archive.yaml"
            if cand != ".archive.yaml" and be.is_file(uid, cand):
                eln_entry = cand
        data_files = []
        if eln_entry:
            try:
                with open(be.local_path(uid, eln_entry, os.path.join(work, "meta")), encoding="utf-8") as f:
                    d = (yaml.safe_load(f) or {}).get("data") or {}
                data_files = [x.get("file") for x in d.get("data_files") or [] if x.get("file")]
            except Exception:  # noqa: BLE001
                data_files = []
        return StagedConversion(work_dir=work, groups=groups, local=local, eln_data=eln_data,
                                eln_data_local=eln_local, eln_entry=eln_entry, data_files=data_files,
                                output_folder=out_folder, name=name,
                                prefix=posixpath.dirname(eln_entry) if eln_entry else folder)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


@activity.defn
def convert_files(args: list) -> dict:
    """args: [ConversionInput, StagedConversion] (as dicts).  Returns dict(nxem=[local], archives=[local],
    results=[...] with paths in the upload)."""
    from emingest import convert, eln

    data, st = ConversionInput.model_validate(args[0]), StagedConversion.model_validate(args[1])
    out_dir = os.path.join(st.work_dir, "out")
    os.makedirs(out_dir, exist_ok=True)
    names = convert.output_names(st.groups)
    results, nxem = [], []
    t_end = time.time() + data.timeout_hours * 3600
    with Heartbeat() as hb:
        for i, (g, n) in enumerate(zip(st.groups, names)):
            if hb.stopped():
                raise InterruptedError("cancelled")
            if time.time() > t_end:
                results.append({"inputs": g, "output": _join(st.output_folder, n), "status": "skipped",
                                "message": "time limit of the action reached", "warnings": 0, "runtime_s": 0})
                continue
            hb.msg = f"converting {i + 1} of {len(st.groups)}: {os.path.basename(g[0])}"
            r = convert.convert_group([st.local[p] for p in g], st.eln_data_local or None, os.path.join(out_dir, n),
                                      isolate=True, timeout=data.timeout_minutes_per_file * 60,
                                      max_gb=data.max_file_gb)
            if r["status"] == "ok":
                nxem.append(r["output"])
            r["inputs"], r["output"] = list(g), _join(st.output_folder, n)
            r.pop("log", None)
            results.append(r)
    arch = eln.write_conversion(os.path.join(st.work_dir, "archives"), st.name, results,
                                st.data_files or [p for g in st.groups for p in g], prefix=st.prefix,
                                eln_entry=st.eln_entry)
    return {"nxem": nxem, "archives": list(arch.values()), "results": results}


@activity.defn
def publish_conversion(args: list) -> ConversionResult:
    """args: [ConversionInput, StagedConversion, convert_files result]."""
    data, st, out = ConversionInput.model_validate(args[0]), StagedConversion.model_validate(args[1]), dict(args[2])
    be = get_backend()
    try:
        if out["nxem"]:
            be.add_files(data.upload_id, out["nxem"], st.output_folder)
        be.add_files(data.upload_id, out["archives"], st.prefix)
    finally:
        shutil.rmtree(st.work_dir, ignore_errors=True)
    res = out["results"]
    written = [_join(st.output_folder, os.path.basename(f)) for f in out["nxem"]] + \
              [_join(st.prefix, os.path.basename(f)) for f in out["archives"]]
    n = {s: sum(1 for r in res if r["status"] == s) for s in ("ok", "failed", "skipped")}
    msg = f"{n['ok']} of {len(res)} file(s) converted to NXem into {st.output_folder or 'the upload root'}"
    if n["failed"]:
        msg += "; failed: " + ", ".join(f"{os.path.basename(r['inputs'][0])} ({r['message']})"
                                         for r in res if r["status"] == "failed")
    if n["skipped"]:
        msg += f"; skipped: {n['skipped']}"
    return ConversionResult(upload_id=data.upload_id, files=written, converted=n["ok"], failed=n["failed"],
                            skipped=n["skipped"], details=res, message=msg)


@activity.defn
def cleanup_conversion(work_dir: str) -> None:
    if work_dir and os.path.basename(work_dir).startswith("emingest_action_"):
        shutil.rmtree(work_dir, ignore_errors=True)
