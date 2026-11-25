# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Conversion of TEM data files to NXem with pynxtools (reader ``tem`` of :mod:`emingest.nx_reader`).

Every conversion group (:func:`emingest.formats.group_files`: a data file + its sidecar files) gives one NXem file
``<stem>.nxs``.  By default each conversion runs in a child process (memory is given back, a crash or time-out does
not stop the others)::

    python -m emingest.convert --eln eln_data.yaml --out x.nxs data.dm4 [sidecar ...]
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

from . import formats


def _patch_reader():
    import pynxtools.dataconverter.convert as pc
    if getattr(pc.get_reader, "_emingest", False):
        return pc
    orig = pc.get_reader

    def get_reader(name):
        if name == "tem":
            from .nx_reader import TEMReader
            return TEMReader
        return orig(name)
    get_reader._emingest = True
    pc.get_reader = get_reader
    return pc


def convert_in_process(files, eln_yaml, output):
    pc = _patch_reader()
    inputs = tuple(list(files) + ([eln_yaml] if eln_yaml else []))
    pc.convert(input_file=inputs, reader="tem", nxdl="NXem", output=output)
    if not os.path.isfile(output):
        raise RuntimeError("pynxtools wrote no file")
    return output


def _summary(log):
    # (pynxtools-em writes undocumented profiling fields itself: not counted)
    warn = [ln for ln in log.splitlines() if "WARNING" in ln and "/profiling/" not in ln]
    err = [ln for ln in log.splitlines() if re.search(r"\bERROR\b|Traceback|Error:", ln)]
    return warn, err


def convert_group(files, eln_yaml, output, isolate=True, timeout=None, max_gb=None):
    """Convert one group.  Returns dict(inputs, output, status ok|failed|skipped, message, warnings, log)."""
    files = [str(f) for f in files]
    res = {"inputs": files, "output": output, "status": "failed", "message": "", "warnings": 0, "log": "",
           "runtime_s": 0.0}
    size = sum(os.path.getsize(f) for f in files if os.path.isfile(f))
    if max_gb and size > max_gb * 1e9:
        res.update(status="skipped", message=f"{size / 1e9:.1f} GB > limit of {max_gb} GB: not converted")
        return res
    os.makedirs(os.path.dirname(os.path.abspath(output)) or ".", exist_ok=True)
    t0 = time.time()
    if isolate:
        cmd = [sys.executable, "-m", "emingest.convert", "--out", output] + \
              (["--eln", eln_yaml] if eln_yaml else []) + files
        env = dict(os.environ)
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
            log = (p.stdout or "") + (p.stderr or "")
            ok = p.returncode == 0 and os.path.isfile(output)
        except subprocess.TimeoutExpired as e:
            log = f"time-out after {timeout} s\n" + str(e.stdout or "")
            ok = False
    else:
        import io
        import logging
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setLevel(logging.INFO)
        loggers = [logging.getLogger(n) for n in ("pynxtools", "pynxtools_em", "")]
        for lg in loggers:
            lg.addHandler(h)
        try:
            convert_in_process(files, eln_yaml, output)
            ok = True
        except Exception as e:  # noqa: BLE001
            buf.write(f"ERROR {type(e).__name__}: {e}\n")
            ok = False
        finally:
            for lg in loggers:
                lg.removeHandler(h)
        log = buf.getvalue()
    warn, err = _summary(log)
    res.update(log=log[-20000:], warnings=len(warn), runtime_s=round(time.time() - t0, 1))
    if ok:
        res.update(status="ok", message=f"{len(warn)} validation warning(s)" if warn else "valid")
    else:
        res.update(message=(err[-1] if err else log.strip().splitlines()[-1] if log.strip() else "failed")[:300])
        if os.path.isfile(output):
            os.remove(output)
    return res


def output_names(groups, suffix=".nxs"):
    """Unique NXem names <stem>.nxs for the groups."""
    used, out = set(), []
    for g in groups:
        st = formats._stem(g[0])
        n, k = st + suffix, 2
        while n.lower() in used:
            n, k = f"{st}_{k}{suffix}", k + 1
        used.add(n.lower())
        out.append(n)
    return out


def convertible(path):
    n = path.lower()
    return not n.endswith(formats.SKIP_NAMES + formats.NEXUS_SUFFIXES) and (
        formats.pynxtools_em_accepts(n) or bool(formats.rsciio_plugins_for(n)))


def convert_all(paths, eln_yaml, out_dir, callback=None, stop=None, isolate=True, timeout=None, max_gb=None,
                only=None):
    """Convert all convertible files (grouped with their sidecars) into out_dir.  ``callback(i, n, name)``, ``stop()``
    -> True to stop.  ``only``: base names of the data files to convert.  Returns the list of results."""
    groups = [g for g in formats.group_files(paths) if convertible(g[0])]
    if only:
        sel = {os.path.basename(x) for x in only}
        groups = [g for g in groups if os.path.basename(g[0]) in sel]
    names = output_names(groups)
    results = []
    for i, (g, n) in enumerate(zip(groups, names)):
        if stop and stop():
            break
        if callback:
            callback(i, len(groups), os.path.basename(g[0]))
        results.append(convert_group(g, eln_yaml, os.path.join(out_dir, n), isolate=isolate, timeout=timeout,
                                     max_gb=max_gb))
    if callback:
        callback(len(groups), len(groups), "")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="TEM data -> NXem (pynxtools, reader 'tem')")
    ap.add_argument("--eln", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("files", nargs="+")
    a = ap.parse_args(argv)
    convert_in_process(a.files, a.eln or None, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
