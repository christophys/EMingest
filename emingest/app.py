# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""GUI of emingest (Jupyter, Voila, Solara, NOMAD dashboard).

Tabs
----
1. **Files**      add any number of files (from your computer: chunked transfer with progress; in Jupyter also from
                  local folders); format and kind (image, diffraction, spectrum, focal / tilt series, 4D-STEM,
                  spectrum image ...) are detected and can be changed
2. **Metadata**   the fields of NXem / pynxtools (required ``*``, recommended ``°``), users with ORCID, rich-text
                  description of the experiment and of the sample's history
3. **elabFTW**    import an experiment or a resource (item) of elabFTW: metadata, main text, linked sample,
                  attachments
4. **NOMAD**      upload the files with ELN entry and ``eln_data.yaml`` into a new or an existing upload (folder);
                  NXem conversion by the NOMAD action (on a worker) or here before uploading
"""
from __future__ import annotations

import html
import os
import re
import shutil
import tempfile
import threading
import time
import traceback

import ipywidgets as W
import yaml

from . import __version__, formats, richtext
from . import convert as cv
from . import elabftw as elab
from . import eln as elnm
from . import metadata as md
from .nomad import NomadClient, NomadError
from .nomad_project import NEW, ProjectError, ProjectPicker, create_project, upload_button

ACTION_ID = "emingest_nomad.actions:emingest_nxem_conversion"
MARK = {md.REQUIRED: "<span style='color:#c62828' title='required by NXem'>*</span>",
        md.RECOMMENDED: "<span style='color:#ef6c00' title='recommended by NXem'>°</span>",
        md.OPTIONAL: "<span style='color:#bbb'>&nbsp;</span>"}
SECTIONS = [("entry", "Experiment"), ("project", "Project"), ("sample", "Sample"), ("instrument", "Instrument")]
OPEN_CHOICES = {"physical_form", "emitter_type", "operation_mode"}


def _L(**kw):
    return W.Layout(**kw)


def _ok(msg, ok=True):
    c = "green" if ok else "crimson"
    return f"<span style='color:{c}'>{html.escape(str(msg))}</span>"


def _mb(n):
    n = float(n or 0)
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB" if n >= 1e5 else f"{n / 1e3:.0f} kB"


def safe_name(s, default="tem"):
    s = re.sub(r"[^\w.-]+", "_", str(s or "")).strip("._")
    return s[:60] or default


class App:
    def __init__(self, work_dir=None, server_mode=False, nomad_url="production", token="", elab_url="",
                 in_nomad=False):
        self.server_mode = server_mode
        self.in_nomad = in_nomad
        self.work_dir = os.path.abspath(work_dir or tempfile.mkdtemp(prefix="emingest_"))
        self.files_dir = os.path.join(self.work_dir, "files")
        self.out_root = os.path.join(self.work_dir, "out")
        os.makedirs(self.files_dir, exist_ok=True)
        self.files = {}                       # name -> info (formats.inspect_file + kind_user, source, ready)
        self.lock = threading.RLock()
        self._busy = False
        self.client = None
        self.elab_result = None
        self.elab_client = None
        self.last_instance = None
        self._elab_url = elab_url or os.environ.get("EMINGEST_ELABFTW_URL", "")
        self._build(nomad_url, token)

    # ================================================================== UI
    def _build(self, nomad_url, token):
        hdr = W.HTML(f"<h3 style='margin:4px 0'>TEM data &rarr; NOMAD <small style='color:#777'>EMingest "
                     f"{__version__}</small></h3><small>Add files (tab 1), describe them (tab 2, or import from "
                     f"elabFTW in tab 3) and upload them to NOMAD (tab 4).</small>")
        self.w_log = W.Output(layout=_L(max_height="160px", overflow="auto", border="1px solid #eee"))
        self.tabs = W.Tab(children=[self._files_tab(), self._meta_tab(), self._elab_tab(),
                                    self._nomad_tab(nomad_url, token)])
        for i, t in enumerate(["1 · Files", "2 · Metadata", "3 · elabFTW import", "4 · To NOMAD"]):
            self.tabs.set_title(i, t)
        self.ui = W.VBox([hdr, self.w_banner, self.tabs, W.HTML("<small>log</small>"), self.w_log])
        self._refresh_check()

    def log(self, msg):
        with self.w_log:
            print(time.strftime("%H:%M:%S"), msg)

    # ------------------------------------------------------------------ tab 1: files
    def _files_tab(self):
        self.w_up_progress = W.HTML()
        # NOMAD project (upload) the files go to in tab 4: chosen here already (same widgets as in tab 4)
        self.project = ProjectPicker(folder="", allow_none=False)
        self.project.w_folder.layout.display = "none"          # the folder is set in tab 4
        rows = [self.project.box]
        self.w_upload = None
        if self.server_mode:
            self.w_upload = upload_button(label="Upload file(s)")
            if self.w_upload is not None:
                self.w_upload.observe(lambda ch: ch["new"] and threading.Thread(
                    target=self._stream_job, args=(list(ch["new"]),), daemon=True).start(), names="file_info")
                rows.append(W.HBox([W.HTML("<b>From your computer:</b>", layout=_L(width="160px")),
                                    W.Box([self.w_upload], layout=_L(width="400px"))]))
            else:
                rows.append(W.HTML("<i>file transfer not available (ipyvuetify missing)</i>"))
        if not self.server_mode:
            self.w_path = W.Text(placeholder="file or folder on this computer", layout=_L(width="480px"))
            b = W.Button(description="add", icon="plus", layout=_L(width="80px"))
            b.on_click(lambda _: self.add_local(self.w_path.value))
            self.w_recursive = W.Checkbox(value=False, description="with subfolders", indent=False,
                                          layout=_L(width="140px"))
            rows.append(W.HBox([W.HTML("<b>Local file / folder:</b>", layout=_L(width="160px")), self.w_path,
                                self.w_recursive, b]))
        exts = ", ".join(formats.supported_extensions())
        rows.append(W.HTML(f"<small>Any number of files, of any size (transferred in chunks). Other files (notes, "
                           f"PDFs …) are uploaded as they are.</small><details style='font-size:12px;color:#666'>"
                           f"<summary>formats read by rosettasciio / pynxtools-em</summary>{html.escape(exts)}"
                           f"</details>"))
        rows.append(self.w_up_progress)
        self.w_table = W.HTML()
        self.w_sel = W.SelectMultiple(options=[], rows=6, layout=_L(width="420px"))
        self.w_kind = W.Dropdown(options=formats.KINDS, layout=_L(width="170px"))
        b_kind = W.Button(description="set kind", layout=_L(width="90px"))
        b_kind.on_click(lambda _: self._set_kind())
        b_rm = W.Button(description="remove", icon="trash", layout=_L(width="100px"))
        b_rm.on_click(lambda _: self._remove())
        b_hint = W.Button(description="metadata from the files", icon="magic", layout=_L(width="200px"),
                          tooltip="high tension, microscope and acquisition time found in the files -> tab 2")
        b_hint.on_click(lambda _: self._apply_file_hints())
        rows += [self.w_table,
                 W.HBox([self.w_sel, W.VBox([W.HTML("<small>selected files:</small>"),
                                             W.HBox([self.w_kind, b_kind]), b_rm, b_hint])])]
        self._render_table()
        return W.VBox(rows)

    def _stream_job(self, info, chunk=8 << 20):
        files = self.w_upload.get_files(timeout=300)
        total = sum(int(f["size"]) for f in info) or 1
        done, t0, added = 0, time.time(), []
        try:
            for k, f in enumerate(files):
                name = self._unique(os.path.basename(f["name"]) or f"upload_{k}")
                size, got = int(f["size"]), 0
                path = os.path.join(self.files_dir, name)
                with open(path, "wb") as fo:
                    while got < size:
                        b = f["file_obj"].read(min(chunk, size - got))
                        if not b:
                            break
                        fo.write(b)
                        got += len(b)
                        done += len(b)
                        rate = done / max(time.time() - t0, 1e-3) / 1e6
                        self.w_up_progress.value = (
                            f"<span style='color:#1565c0'>⬆ <b>{html.escape(name)}</b>"
                            f"{f' ({k + 1} of {len(files)})' if len(files) > 1 else ''}: "
                            f"{100 * got / max(size, 1):.0f} % ({_mb(got)} of {_mb(size)}, {rate:.1f} MB/s)</span>"
                            f"<progress value='{done}' max='{total}' style='width:300px;margin-left:8px'></progress>")
                if got != size:
                    raise IOError(f"transfer of {name} incomplete ({got} of {size} bytes)")
                added.append(path)
                self._add_file(path, "your computer")
        except Exception as e:  # noqa: BLE001
            self.w_up_progress.value = _ok(f"transfer failed: {e!r}", False)
            return
        self.w_up_progress.value = _ok(f"✔ {len(added)} file(s), {_mb(total)} in {time.time() - t0:.0f} s")

    def _unique(self, name):
        base, ext = os.path.splitext(name)
        if name.lower().endswith(".mrc.mdoc"):
            base, ext = name[:-9], name[-9:]
        n, k = name, 2
        with self.lock:
            while n in self.files or os.path.exists(os.path.join(self.files_dir, n)):
                n, k = f"{base}_{k}{ext}", k + 1
        return n

    def add_local(self, path):
        """Add a local file or the files of a folder (Jupyter; not in server mode)."""
        if self.server_mode:
            self.log("adding files of the server is not possible here")
            return 0
        path = os.path.expanduser(str(path or "").strip())
        paths = []
        if os.path.isfile(path):
            paths = [path]
        elif os.path.isdir(path):
            if getattr(self, "w_recursive", None) is not None and self.w_recursive.value:
                for r, _d, fs in os.walk(path):
                    paths += [os.path.join(r, f) for f in sorted(fs) if not f.startswith(".")]
            else:
                paths = [os.path.join(path, f) for f in sorted(os.listdir(path))
                         if os.path.isfile(os.path.join(path, f)) and not f.startswith(".")]
        else:
            self.log(f"not found: {path}")
            return 0
        for p in paths:
            self._add_file(p, "local", copy=False)
        self.log(f"added {len(paths)} file(s)")
        return len(paths)

    def _add_file(self, path, source, copy=False, inspect_now=False, name=None):
        name = name or os.path.basename(path)
        with self.lock:
            if name in self.files and self.files[name]["path"] != path:
                name = self._unique(name)
            self.files[name] = {"name": name, "path": path, "size": os.path.getsize(path), "source": source,
                                "kind": "", "format": "…", "readers": [], "shape": [], "note": "inspecting …",
                                "ready": False, "kind_user": ""}
        self._render_table()
        if inspect_now:
            self._inspect(name)
        else:
            threading.Thread(target=self._inspect, args=(name,), daemon=True).start()
        return name

    def _inspect(self, name):
        with self.lock:
            f = self.files.get(name)
        if not f:
            return
        try:
            info = formats.inspect_file(f["path"])
        except Exception as e:  # noqa: BLE001
            info = {"note": f"inspection failed: {e}", "kind": "other", "format": "?"}
        info.pop("name", None)
        info.pop("path", None)
        with self.lock:
            if name in self.files:
                self.files[name].update(info, ready=True)
        self._render_table()
        self._refresh_meta_files()
        if name.lower().endswith(".eln"):
            self._refresh_eln_choices()

    def wait_inspected(self, timeout=600):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.lock:
                if all(f["ready"] for f in self.files.values()):
                    return True
            time.sleep(0.2)
        return False

    def kind(self, f):
        return f.get("kind_user") or f.get("kind") or "other"

    def _render_table(self):
        with self.lock:
            fl = list(self.files.values())
        if not fl:
            self.w_table.value = "<i style='color:#777'>no files yet</i>"
            self.w_sel.options = []
            return
        th = "".join(f"<th style='text-align:left;padding:2px 8px;border-bottom:1px solid #ccc'>{h}</th>"
                     for h in ("#", "file", "size", "format", "read by", "kind", "shape", "note"))
        rows = []
        for i, f in enumerate(fl):
            kind = self.kind(f)
            if f.get("kind_user"):
                kind = f"<b>{html.escape(kind)}</b> <small>(set)</small>"
            else:
                kind = html.escape(kind) if f["ready"] else "…"
            rd = ", ".join(f.get("readers") or []) or ("<span style='color:#999'>as is</span>" if f["ready"] else "")
            rows.append("<tr>" + "".join(f"<td style='padding:1px 8px'>{c}</td>" for c in (
                i + 1, html.escape(f["name"]), _mb(f["size"]), html.escape(str(f.get("format") or "")), rd, kind,
                "×".join(str(x) for x in f.get("shape") or []), f"<small>{html.escape(str(f.get('note') or ''))}"
                                                                 f"</small>")) + "</tr>")
        tot = sum(f["size"] for f in fl)
        conv = sum(1 for f in fl if f["ready"] and cv.convertible(f["name"]) and not formats.is_sidecar(f["name"]))
        self.w_table.value = (f"<div style='max-height:340px;overflow:auto'><table style='border-collapse:collapse;"
                              f"font-size:13px'><tr>{th}</tr>{''.join(rows)}</table></div><small>{len(fl)} file(s), "
                              f"{_mb(tot)}; {conv} can be converted to NXem</small>")
        sel = [s for s in self.w_sel.value if s in self.files]
        self.w_sel.options = [f["name"] for f in fl]
        self.w_sel.value = tuple(sel)

    def _set_kind(self):
        with self.lock:
            for n in self.w_sel.value:
                if n in self.files:
                    self.files[n]["kind_user"] = self.w_kind.value
        self._render_table()

    def _remove(self):
        with self.lock:
            for n in self.w_sel.value:
                f = self.files.pop(n, None)
                if f and f["path"].startswith(self.files_dir):
                    try:
                        os.remove(f["path"])
                    except OSError:
                        pass
        self.w_sel.value = ()
        self._render_table()
        self._refresh_meta_files()

    def _apply_file_hints(self):
        with self.lock:
            infos = [f for f in self.files.values() if f["ready"]]
        hints = md.from_file_hints(infos)
        n = sum(len(v) for v in hints.values())
        cur = self.get_meta()
        for f in infos:                         # NXem files (e.g. written by tem-provenance): their metadata
            if f.get("format") == "NeXus" and "NXem" in str(f.get("note")):
                try:
                    nx = md.from_nxem(f["path"])
                except Exception as e:  # noqa: BLE001
                    self.log(f"{f['name']}: {e}")
                    continue
                cur = md.merge(cur, nx)
                n += sum(1 for sec in ("entry", "sample", "instrument") for v in nx[sec].values() if v)
        self.set_meta(md.merge(cur, hints))
        self.log(f"{n} value(s) from the files put into empty fields of tab 2" if n else
                 "no acquisition metadata found in the files")

    # ------------------------------------------------------------------ tab 2: metadata
    def _field_widget(self, sec, key, typ):
        lay = _L(width="330px")
        if typ == "bool":
            return W.Checkbox(value=False, indent=False, layout=_L(width="330px"))
        if typ.startswith("choice:"):
            opts = typ[7:].split("|")
            if key in OPEN_CHOICES:
                return W.Combobox(options=[o for o in opts if o], ensure_option=False, layout=lay,
                                  placeholder="choose or type")
            return W.Dropdown(options=opts, value=opts[0], layout=lay)
        ph = {"datetime": "YYYY-MM-DD HH:MM (local time)", "float": "number"}.get(typ, "")
        return W.Text(placeholder=ph, layout=lay)

    def _meta_tab(self):
        self.mw = {}
        blocks = []
        for sec, title in SECTIONS:
            rows = [W.HTML(f"<b style='font-size:14px'>{title}</b>")]
            for s, key, label, level, typ, helptext in md.FIELDS:
                if s != sec:
                    continue
                w = self._field_widget(s, key, typ)
                self.mw[(s, key)] = w
                w.observe(lambda _ch: self._refresh_check(), names="value")
                rows.append(W.HBox([W.HTML(f"{MARK[level]} {html.escape(label)}", layout=_L(width="170px")), w,
                                    W.HTML(f"<small style='color:#777'>{html.escape(helptext)}</small>",
                                           layout=_L(width="330px"))]))
            blocks.append(W.VBox(rows, layout=_L(margin="0 0 8px 0")))
        self.w_tags = W.Text(placeholder="comma-separated", layout=_L(width="330px"))
        blocks.insert(2, W.HBox([W.HTML(f"{MARK[md.OPTIONAL]} Tags", layout=_L(width="170px")), self.w_tags]))
        # users
        self.w_users = W.VBox([])
        b_add = W.Button(description="add user", icon="user-plus", layout=_L(width="120px"))
        b_add.on_click(lambda _: self._add_user_row({}))
        self._user_rows = []
        self._add_user_row({})
        users = W.VBox([W.HTML("<b style='font-size:14px'>Users</b> <small>(NXem /ENTRY/userID: name, "
                               "affiliation, ORCID ...)</small>"), self.w_users, b_add])
        # rich text
        self.rte = {}
        rich = []
        for sec, key, label in md.HTML_FIELDS:
            ph = ("What was measured and why; conditions, procedure, observations, problems ..." if key ==
                  "experiment_description" else "Origin, synthesis / growth, preparation (FIB, ion milling, "
                                                "crushing ...), storage, earlier measurements ...")
            w = richtext.RichTextEditor(placeholder=ph, height="150px", width="1000px")
            w.observe(lambda _ch: self._refresh_check(), names="value")
            self.rte[(sec, key)] = w
            rich += [W.HTML(f"<b style='font-size:14px'>{html.escape(label)}</b>{MARK[md.RECOMMENDED]}"
                            if key == "experiment_description" else f"<b style='font-size:14px'>"
                                                                       f"{html.escape(label)}</b>"), w]
        # reuse metadata of an earlier data set
        self.w_meta_src = W.Dropdown(options=[("(a *eln_data.yaml / *.archive.yaml in tab 1)", "")],
                                     layout=_L(width="420px"))
        b_load = W.Button(description="fill from file", icon="upload", layout=_L(width="130px"))
        b_load.on_click(lambda _: self._load_meta_file())
        self.w_meta_overwrite = W.Checkbox(value=False, description="overwrite filled fields", indent=False,
                                     layout=_L(width="auto"))
        self.w_check = W.HTML()
        legend = W.HTML(f"<small>{MARK[md.REQUIRED]} required and {MARK[md.RECOMMENDED]} recommended by the NXem "
                        f"application definition (pynxtools); the description and the sample history (rich text) "
                        f"go to the NOMAD ELN entry, as plain text also into NXem.</small>")
        exp, proj, tags, sample, instr = blocks
        self._components = []
        self.w_components = W.HTML()
        sample.children = tuple(sample.children) + (self.w_components,)
        return W.VBox([legend, self.w_check, W.HBox([W.HTML("Reuse metadata:", layout=_L(width="120px")),
                                                     self.w_meta_src, self.w_meta_overwrite, b_load])]
                      + rich[:2] + [exp, proj, tags, users, sample] + rich[2:] + [instr])

    def _add_user_row(self, u):
        ws = {k: W.Text(value=str(u.get(k) or ""), placeholder=lbl, layout=_L(width=wd))
              for (k, lbl, _lv), wd in zip(md.USER_FIELDS[:5], ("220px", "220px", "200px", "190px", "120px"))}
        for w in ws.values():
            w.observe(lambda _ch: self._refresh_check(), names="value")
        rm = W.Button(icon="times", tooltip="remove", layout=_L(width="36px"))
        row = W.HBox(list(ws.values()) + [rm])
        entry = {"row": row, "w": ws, "extra": {k: u.get(k) for k, _l, _v in md.USER_FIELDS[5:] if u.get(k)}}
        self._user_rows.append(entry)

        def remove(_):
            if entry in self._user_rows:
                self._user_rows.remove(entry)
            self.w_users.children = [e["row"] for e in self._user_rows]
            self._refresh_check()
        rm.on_click(remove)
        self.w_users.children = [e["row"] for e in self._user_rows]

    def get_meta(self):
        m = md.empty()
        for (sec, key), w in self.mw.items():
            v = w.value
            if isinstance(v, str):
                v = v.strip()
            m.setdefault(sec, {})[key] = v
        for (sec, key), w in self.rte.items():
            v = w.value or ""
            m[sec][key] = richtext.sanitize(v if "<" in v else richtext.from_text(v))
        m["users"] = []
        for e in self._user_rows:
            u = {k: w.value.strip() for k, w in e["w"].items() if w.value.strip()}
            u.update(e["extra"])
            if u:
                m["users"].append(u)
        m["users"] = m["users"] or [{}]
        m["tags"] = [t.strip() for t in self.w_tags.value.split(",") if t.strip()]
        m["source"] = dict(getattr(self, "_source", {}) or {})
        if getattr(self, "_components", None):
            m["sample"]["components"] = [dict(c) for c in self._components]
        return m

    def set_meta(self, m):
        for (sec, key), w in self.mw.items():
            v = (m.get(sec) or {}).get(key)
            if v is None:
                continue
            if isinstance(w, W.Checkbox):
                w.value = bool(v)
            elif isinstance(w, W.Dropdown):
                if str(v) in w.options:
                    w.value = str(v)
            else:
                w.value = "" if v is None else str(v)
        for (sec, key), w in self.rte.items():
            v = (m.get(sec) or {}).get(key)
            if v is not None:
                w.value = v if not isinstance(w, W.Textarea) else richtext.to_text(v)
        self._user_rows = []
        for u in m.get("users") or [{}]:
            self._add_user_row(u)
        self.w_tags.value = ", ".join(m.get("tags") or [])
        self._source = dict(m.get("source") or {})
        if "components" in (m.get("sample") or {}):
            self._components = [dict(c) for c in m["sample"]["components"] or [] if isinstance(c, dict)]
            self.w_components.value = ("<small style='margin-left:174px'>components (from eLabFTW / the files): " +
                                       html.escape("; ".join(f"{c.get('name')}: {c.get('chemical_formula', '')}"
                                                             + (f" ({c['space_group']})" if c.get("space_group")
                                                                else "") for c in self._components)) + "</small>"
                                       if self._components else "")
        self._refresh_check()

    def _refresh_check(self):
        if not hasattr(self, "w_check") or not hasattr(self, "_user_rows"):
            return
        try:
            req, rec = md.check(self.get_meta())
        except Exception as e:  # noqa: BLE001
            self.w_check.value = _ok(f"check failed: {e}", False)
            return
        parts = []
        if req:
            parts.append(f"<span style='color:#c62828'>missing (required): {html.escape(', '.join(req))}</span>")
        if rec:
            parts.append(f"<span style='color:#ef6c00'>recommended: {html.escape(', '.join(rec))}</span>")
        self.w_check.value = ("<div style='padding:3px 6px;background:#fafafa;border:1px solid #eee'>" +
                              ("<br>".join(parts) if parts else "<span style='color:green'>✔ all required and "
                                                                "recommended fields are filled</span>") + "</div>")

    def _refresh_meta_files(self):
        with self.lock:
            opts = [(n, n) for n in self.files if n.lower().endswith(("eln_data.yaml", ".archive.yaml"))]
        self.w_meta_src.options = [("(a *eln_data.yaml / *.archive.yaml in tab 1)", "")] + opts

    def _load_meta_file(self):
        n = self.w_meta_src.value
        if not n:
            self.log("add an eln_data.yaml or an ELN archive (.archive.yaml) of an earlier data set in tab 1")
            return
        try:
            with open(self.files[n]["path"], encoding="utf-8") as f:
                d = yaml.safe_load(f) or {}
            new = elnm.meta_from_archive(d) if "data" in d else md.from_eln_data(d)
        except Exception as e:  # noqa: BLE001
            self.log(f"cannot read {n}: {e}")
            return
        self.set_meta(md.merge(self.get_meta(), new, overwrite=self.w_meta_overwrite.value))
        self.log(f"metadata taken from {n}")

    # ------------------------------------------------------------------ tab 3: elabFTW
    def _elab_tab(self):
        lab = lambda t: W.HTML(t, layout=_L(width="130px"))  # noqa: E731
        self.w_el_src = W.ToggleButtons(options=[("elabFTW server (API)", "api"), (".eln export file", "eln")],
                                        value="api", style={"button_width": "170px"})
        # --- server
        self.w_el_url = W.Text(value=self._elab_url, placeholder="https://eln.example.org  or the address of the "
                                                                     "entry (…/experiments.php?mode=view&id=42)",
                               layout=_L(width="560px"))
        self.w_el_type = W.ToggleButtons(options=[("experiment", "experiments"), ("resource / item", "items")],
                                         value="experiments", style={"button_width": "130px"})
        self.w_el_id = W.Text(placeholder="ID", layout=_L(width="120px"))
        self.w_el_key = W.Password(placeholder="API key (elabFTW: user panel → API keys; read access is enough)",
                                   layout=_L(width="560px"))
        self.w_el_verify = W.Checkbox(value=True, description="verify the TLS certificate", indent=False,
                                      layout=_L(width="auto"))
        box_api = W.VBox([W.HBox([lab("elabFTW address"), self.w_el_url]),
                          W.HBox([lab("entry"), self.w_el_type, W.HTML("&nbsp;ID&nbsp;"), self.w_el_id]),
                          W.HBox([lab("API key"), self.w_el_key]), W.HBox([lab(""), self.w_el_verify])])
        # --- .eln file
        self.w_eln_loc = W.Text(placeholder="/path/export.eln  ·  user@host:/var/elabftw/exports/  (folder: the "
                                            "newest .eln)  ·  or choose a file of tab 1 →", layout=_L(width="560px"))
        self.w_eln_tab1 = W.Dropdown(options=[("(.eln files of tab 1)", "")], layout=_L(width="250px"))
        b_open = W.Button(description="open", icon="folder-open", layout=_L(width="90px"))
        b_open.on_click(lambda _: threading.Thread(target=self._eln_open, daemon=True).start())
        self.w_eln_filter = W.ToggleButtons(options=[("TEM sessions", "sessions"), ("experiments", "experiments"),
                                                     ("resources", "items"), ("all", "all")], value="sessions",
                                            style={"button_width": "110px"})
        self.w_eln_filter.observe(lambda _ch: self._eln_list(), names="value")
        self.w_eln_entries = W.Select(options=[], rows=7, layout=_L(width="760px"))
        self.w_eln_info = W.HTML()
        box_eln = W.VBox([W.HBox([lab(".eln file"), self.w_eln_loc, self.w_eln_tab1, b_open]), self.w_eln_info,
                          W.HBox([lab("show"), self.w_eln_filter]), W.HBox([lab("entry"), self.w_eln_entries])])
        # --- SSH (for an .eln file or acquisition folders on another computer)
        self.w_ssh_pw = W.Password(placeholder="SSH password (or a key below)", layout=_L(width="270px"))
        self.w_ssh_key = W.Textarea(placeholder="private key (-----BEGIN OPENSSH PRIVATE KEY----- …), optional",
                                    layout=_L(width="420px", height="58px"))
        self.w_ssh_pass = W.Password(placeholder="key passphrase", layout=_L(width="160px"))
        self.w_ssh_trust = W.Text(placeholder="accepted host key (SHA256:…)", layout=_L(width="420px"))
        self.w_ssh_info = W.HTML("<small>For locations <code>user@host:/path</code>. Host keys are checked against "
                                 "known_hosts; an unknown host shows its fingerprint to compare and accept. "
                                 "Credentials stay in this session's memory.</small>")
        self.box_ssh = W.VBox([W.HBox([lab("SSH"), self.w_ssh_pw, self.w_ssh_pass]),
                               W.HBox([lab(""), self.w_ssh_key]), W.HBox([lab("host key"), self.w_ssh_trust]),
                               W.HBox([lab(""), self.w_ssh_info])])
        acc_ssh = W.Accordion(children=[self.box_ssh])
        acc_ssh.set_title(0, "SSH access (remote .eln file or acquisition folders)")
        acc_ssh.selected_index = None
        self.w_el_src.observe(lambda _ch: self._el_src_vis(), names="value")
        self.box_api, self.box_eln = box_api, box_eln
        # --- common
        self.w_el_links = W.Checkbox(value=True, description="also read linked resources (e.g. the sample)",
                                     indent=False, layout=_L(width="auto"))
        self.w_el_tz = W.Text(value="Europe/Rome", description="facility time zone",
                              style={"description_width": "initial"}, layout=_L(width="300px"))
        b_fetch = W.Button(description="read entry", icon="search", button_style="primary",
                           layout=_L(width="140px"))
        b_fetch.on_click(lambda _: self._elab_fetch())
        self.w_el_status = W.HTML()
        self.w_el_preview = W.HTML()
        self.w_el_files = W.SelectMultiple(options=[], rows=5, layout=_L(width="560px"))
        self.w_el_overwrite = W.Checkbox(value=False, description="overwrite fields that are already filled",
                                         indent=False, layout=_L(width="auto"))
        b_meta = W.Button(description="use metadata", icon="check", layout=_L(width="150px"))
        b_meta.on_click(lambda _: self._elab_apply())
        b_dl = W.Button(description="add selected attachments", icon="download", layout=_L(width="220px"))
        b_dl.on_click(lambda _: threading.Thread(target=self._elab_download, daemon=True).start())
        self.w_el_dl = W.HTML()
        # --- acquisition folder of a TEM session
        self.w_map = W.Text(value=os.environ.get("EMINGEST_PATH_MAP", ""),
                            placeholder="path mapping: /mnt/storage=/data/lab ; /mnt/tem=user@storage:/tem",
                            layout=_L(width="560px"))
        self.w_acq = W.HTML()
        b_acq = W.Button(description="add the files of the acquisition folder", icon="download",
                         layout=_L(width="300px"))
        b_acq.on_click(lambda _: threading.Thread(target=self._session_files, daemon=True).start())
        self.box_acq = W.VBox([W.HTML("<b>Acquisition folder of the session</b> <small>(the <code>Path</code> "
                                      "of the session; with a mapping when it is mounted elsewhere here or lies "
                                      "on another computer)</small>"),
                               W.HBox([lab("path mapping"), self.w_map]), W.HBox([lab(""), b_acq]), self.w_acq],
                              layout=_L(display="none"))
        # --- batch
        self.w_b_ids = W.Text(placeholder="session IDs: 12, 15-18", layout=_L(width="300px"))
        self.w_b_all = W.Checkbox(value=False, description="all TEM sessions of the .eln file", indent=False,
                                  layout=_L(width="auto"))
        self.w_b_folder = W.Text(value="tem/session_{id}_{title}", description="folder",
                                 style={"description_width": "50px"}, layout=_L(width="360px"))
        b_check = W.Button(description="check", icon="list-check", layout=_L(width="100px"))
        b_check.on_click(lambda _: threading.Thread(target=self._batch, kwargs=dict(dry_run=True),
                                                    daemon=True).start())
        self.w_b_run = W.Button(description="ingest into NOMAD", icon="cloud-upload", button_style="success",
                                layout=_L(width="190px"))
        self.w_b_run.on_click(lambda _: threading.Thread(target=self._batch, daemon=True).start())
        self.w_b_status = W.HTML()
        self.w_b_table = W.HTML()
        box_batch = W.VBox([
            W.HTML("<small>Each TEM session becomes its own folder in the NOMAD project of tab 4 (ELN entry with "
                   "the metadata of session, sample and microscope; all files of its acquisition folder; NXem "
                   "conversion as chosen in tab 4). Uses the source, SSH access, time zone and path mapping "
                   "above.</small>"),
            W.HBox([lab("sessions"), self.w_b_ids, self.w_b_all]), W.HBox([lab(""), self.w_b_folder]),
            W.HBox([lab(""), b_check, self.w_b_run]), self.w_b_status, self.w_b_table])
        acc_batch = W.Accordion(children=[box_batch])
        acc_batch.set_title(0, "Batch: several TEM sessions → NOMAD")
        acc_batch.selected_index = None
        self._el_src_vis()
        return W.VBox([
            W.HTML("<small>Reads experiments and resources of <b>elabFTW</b> — from a running server (API v2) or "
                   "from an <b>.eln export</b> (<code>bin/console export:eln</code>, here or on another computer) "
                   "without a running elabFTW. <b>TEM sessions</b> made with the tem-provenance templates are "
                   "followed to their sample and microscope, and the files of their acquisition folder can be "
                   "added. API key and SSH credentials stay in this session's memory.</small>"),
            W.HBox([lab("source"), self.w_el_src]), box_api, box_eln, acc_ssh,
            W.HBox([lab(""), self.w_el_links, self.w_el_tz]),
            W.HBox([lab(""), b_fetch]), self.w_el_status, self.w_el_preview,
            W.HBox([lab("metadata"), b_meta, self.w_el_overwrite]),
            W.HBox([lab("attachments"), self.w_el_files]), W.HBox([lab(""), b_dl]), self.w_el_dl,
            self.box_acq, acc_batch])

    def _el_src_vis(self):
        api = self.w_el_src.value == "api"
        self.box_api.layout.display = "flex" if api else "none"
        self.box_eln.layout.display = "none" if api else "flex"
        if not api:
            self._refresh_eln_choices()

    def _refresh_eln_choices(self):
        with self.lock:
            opts = [(n, n) for n in self.files if n.lower().endswith(".eln")]
        self.w_eln_tab1.options = [("(.eln files of tab 1)", "")] + opts

    def _ssh_kw(self):
        kw = {}
        if self.w_ssh_pw.value:
            kw["password"] = self.w_ssh_pw.value
        if self.w_ssh_key.value.strip():
            kw["key_text"] = self.w_ssh_key.value.strip() + "\n"
            kw["key_passphrase"] = self.w_ssh_pass.value or None
        if self.w_ssh_trust.value.strip():
            kw["trust_fingerprint"] = self.w_ssh_trust.value.strip()
        return kw

    def _local_roots(self):
        """Folders of the server the dashboard may read (server mode: EMINGEST_LOCAL_ROOTS); None = any."""
        if not self.server_mode:
            return None
        return [p for p in os.environ.get("EMINGEST_LOCAL_ROOTS", "").split(os.pathsep) if p]

    def _remote_error(self, e, w):
        from .remote import UnknownHostKey
        if isinstance(e, UnknownHostKey):
            self.w_ssh_trust.value = e.fingerprint
            w.value = (f"<span style='color:#b26a00'>The host key of <b>{html.escape(e.host)}</b> is not known: "
                       f"{html.escape(e.key_type)} <code>{html.escape(e.fingerprint)}</code>. Compare it with the "
                       f"key of the server (<code>ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub</code> there); if it "
                       f"matches, it is now entered under <i>SSH access → host key</i>: try again.</span>")
        else:
            w.value = _ok(str(e), False)

    def _eln_open(self):
        from .eln_archive import ElnArchive, ElnArchiveClient
        from .remote import RemoteError, open_eln
        self.w_eln_info.value = "<i>opening …</i>"
        try:
            if getattr(self, "_eln_loc", None):
                self._eln_loc.close()
                self._eln_loc = None
            if self.w_eln_tab1.value:
                arch = ElnArchive(self.files[self.w_eln_tab1.value]["path"], name=self.w_eln_tab1.value)
            else:
                arch, self._eln_loc = open_eln(self.w_eln_loc.value, dest_dir=self.work_dir,
                                               allowed_local_roots=self._local_roots(), **self._ssh_kw())
        except (RemoteError, elab.ElabError) as e:
            self._remote_error(e, self.w_eln_info)
            return
        except Exception as e:  # noqa: BLE001
            self.w_eln_info.value = _ok(f"cannot open: {e!r}", False)
            self.log(traceback.format_exc())
            return
        self.eln_archive = arch
        self.elab_client = ElnArchiveClient(arch)
        n = {t: sum(1 for e in arch.entries.values() if e["type"] == t) for t in ("experiments", "items")}
        from . import tem_session as ts
        ns = len(ts.list_sessions(arch))
        self.w_eln_info.value = _ok(f"✔ {arch.name}: {n['experiments']} experiment(s) ({ns} TEM session(s)), "
                                    f"{n['items']} resource(s)" + (f"; {arch.software}" if arch.software else ""))
        self._eln_list()

    def _eln_list(self):
        arch = getattr(self, "eln_archive", None)
        if arch is None:
            return
        from . import tem_session as ts
        f = self.w_eln_filter.value
        if f == "sessions":
            rows = [(f"session #{s['id']} · {str(s['date'])[:16]} · {s['title']} · {s['path']}", ("experiments",
                                                                                               s["id"]))
                    for s in ts.list_sessions(arch)]
        else:
            rows = [(f"{'experiment' if e['type'] == 'experiments' else 'resource'} #{e['id']} · "
                     f"{str(e['date'])[:10]} · {e['title']}" + (f" · {e['category']}" if e["category"] else "")
                     + (f" · {e['files']} file(s)" if e["files"] else ""), (e["type"], e["id"]))
                    for e in arch.list(types=None if f == "all" else (f,))]
        self.w_eln_entries.options = rows
        if rows:
            self.w_eln_entries.value = rows[0][1]

    def _elab_source(self):
        """(client, type, id) of the entry chosen in the API or .eln part."""
        if self.w_el_src.value == "eln":
            if getattr(self, "eln_archive", None) is None:
                raise elab.ElabError("open an .eln file first")
            if not self.w_eln_entries.value:
                raise elab.ElabError("choose an entry of the .eln file")
            etype, eid = self.w_eln_entries.value
            return self.elab_client, etype, eid
        base, etype, eid = elab.parse_ref(self.w_el_url.value, self.w_el_id.value.strip() or None,
                                          self.w_el_type.value if not re.search(
                                              r"(experiments|database|resources)\.php|/api/v2/",
                                              self.w_el_url.value) else None)
        self.w_el_type.value = etype
        self.w_el_id.value = str(eid)
        self.elab_client = elab.ElabClient(base, self.w_el_key.value, verify=self.w_el_verify.value)
        return self.elab_client, etype, eid

    def _elab_fetch(self):
        self.w_el_status.value = "<i>reading …</i>"
        try:
            client, etype, eid = self._elab_source()
            r = elab.import_entry(client, etype, eid, follow_links=self.w_el_links.value,
                                  zone=self.w_el_tz.value.strip() or None)
        except (elab.ElabError, ValueError) as e:
            self.w_el_status.value = _ok(str(e), False)
            return None
        except Exception as e:  # noqa: BLE001
            self.w_el_status.value = _ok(f"failed: {e!r}", False)
            self.log(traceback.format_exc())
            return None
        self.elab_result = r
        self.w_el_status.value = _ok("✔ " + r["summary"])
        m = r["meta"]
        items = [("title", m["entry"].get("experiment_alias")), ("start", m["entry"].get("start_time")),
                 ("end", m["entry"].get("end_time")), ("sample", m["sample"].get("name")),
                 ("sample ID (eLabFTW)", m["sample"].get("identifier_sample")),
                 ("owner's sample ID", m["sample"].get("identifier_owner")),
                 ("formula", m["sample"].get("chemical_formula")), ("elements", m["sample"].get("atom_types")),
                 ("components", "; ".join(f"{c.get('name')}: {c.get('chemical_formula')}"
                                          for c in m["sample"].get("components") or [])),
                 ("microscope", m["instrument"].get("name") or m["instrument"].get("model")),
                 ("high tension (kV)", m["instrument"].get("voltage_kV")),
                 ("acquisition folder", (m.get("source") or {}).get("session_path")),
                 ("users", ", ".join(u.get("name", "") for u in m["users"])), ("tags", ", ".join(m["tags"]))]
        rows = "".join(f"<tr><td style='padding:0 10px;color:#555'>{k}</td><td>{html.escape(str(v))}</td></tr>"
                       for k, v in items if v not in (None, ""))
        body = m["entry"].get("experiment_description") or ""
        self.w_el_preview.value = (f"<table style='font-size:13px'>{rows}</table><div style='max-height:180px;"
                                   f"overflow:auto;border:1px solid #eee;padding:4px;margin-top:4px;font-size:13px'>"
                                   f"{body}</div>")
        self.w_el_files.options = [(f"{u.get('real_name')}  ({_mb(u.get('filesize'))})", i)
                                   for i, u in enumerate(r["uploads"])]
        self.w_el_files.value = tuple(range(len(r["uploads"])))
        self.box_acq.layout.display = "flex" if r.get("session") else "none"
        self.w_acq.value = ""
        return r

    def _session_files(self):
        """Files of the acquisition folder of the session read last -> tab 1 (time-window check)."""
        from . import ingest
        from . import tem_session as ts
        s = (self.elab_result or {}).get("session")
        if not s:
            self.w_acq.value = _ok("read a TEM session first", False)
            return
        dest = tempfile.mkdtemp(prefix="acq_", dir=self.work_dir)
        try:
            pairs, label = ingest.collect_session_files(
                s["path"], dest, location_kw=self._ssh_kw(), rules=ts.parse_rules(self.w_map.value),
                allowed_local_roots=self._local_roots(),
                progress=lambda rel, d, t: setattr(self.w_acq, "value", f"<span style='color:#1565c0'>⬇ "
                                                   f"{html.escape(rel)}: {100 * d / max(t, 1):.0f} %</span>"))
        except Exception as e:  # noqa: BLE001
            self._remote_error(e, self.w_acq)
            return
        names = []
        for p, rel in pairs:
            names.append(self._add_file(p, "acquisition folder", name=rel.replace("/", "_") if "/" in rel else None))
        self.w_acq.value = f"<i>{len(names)} file(s) from {html.escape(label)}: inspecting …</i>"
        self.wait_inspected()
        with self.lock:
            infos = [self.files[n] for n in names if n in self.files]
        n_out = ingest.flag_window(infos, s["window"], s["zone"])
        self._render_table()
        self.w_acq.value = _ok(f"✔ {len(names)} file(s) of {label} added to tab 1" +
                               (f"; {n_out} acquired outside the session time (marked)" if n_out else ""))

    def _batch(self, dry_run=False):
        from . import ingest
        from . import tem_session as ts
        if self._busy:
            return
        self._busy = True
        self.w_b_run.disabled = True
        rows = []
        try:
            if self.w_el_src.value == "eln":
                if getattr(self, "eln_archive", None) is None:
                    raise ValueError("open an .eln file first")
                source = self.elab_client
            else:
                base = elab.parse_ref(self.w_el_url.value, 1, "experiments")[0]
                source = elab.ElabClient(base, self.w_el_key.value, verify=self.w_el_verify.value)
            if self.w_b_all.value:
                if self.w_el_src.value != "eln":
                    raise ValueError("'all TEM sessions' needs an .eln file; give the IDs for a server")
                ids = [s["id"] for s in ts.list_sessions(source)]
            else:
                ids = ts.parse_ids(self.w_b_ids.value)
            if not ids:
                raise ValueError("give session IDs (e.g. 12, 15-18)")
            client, uid = None, None
            if not dry_run:
                if not self.client and not self.connect():
                    raise ValueError("not connected to NOMAD (tab 4)")
                client = self.client
                uid = self.project.w_sel.value
                if uid in (None, NEW):
                    if uid == NEW and not self.project.can_create():
                        raise ValueError("the maximum number of open NOMAD projects is reached: choose an existing "
                                         "project")
                    uid = create_project(client.url, client.token, self.project.w_name.value.strip()
                                         or f"TEM sessions {time.strftime('%Y-%m-%d')}")
                    self.project.refresh(select=uid)
            for k, sid in enumerate(ids):
                def cb(stage, msg, k=k):
                    self.w_b_status.value = f"<i>{k + 1}/{len(ids)} · {html.escape(msg)}</i>"
                r = ingest.ingest_session(
                    source, sid, client=client, upload_id=uid, zone=self.w_el_tz.value.strip() or ts.DEFAULT_TZ,
                    rules=ts.parse_rules(self.w_map.value), location_kw=self._ssh_kw(),
                    folder_template=self.w_b_folder.value.strip() or "tem/session_{id}_{title}",
                    schema="plugin" if self.w_schema.value else "core", conversion=self.w_conv.value,
                    max_gb=self.w_max_gb.value, work_dir=self.work_dir, callback=cb, dry_run=dry_run,
                    allowed_local_roots=self._local_roots())
                rows.append(r)
                self.w_b_table.value = self._batch_html(rows)
            ok = sum(1 for r in rows if r["status"] in ("ok", "checked"))
            self.w_b_status.value = _ok(f"✔ {ok} of {len(rows)} session(s) " + ("checked" if dry_run else
                                                                               "ingested"), ok == len(rows))
            if not dry_run:
                self._list_uploads()
        except Exception as e:  # noqa: BLE001
            self._remote_error(e, self.w_b_status)
            self.log(traceback.format_exc())
        finally:
            self._busy = False
            self.w_b_run.disabled = False

    def _batch_html(self, rows):
        def st(r):
            return {"ok": "✔", "checked": "✔", "failed": "✖"}.get(r["status"], "–") + " " + r["status"]
        tr = "".join(
            f"<tr><td style='padding:0 8px'>#{html.escape(str(r['session']))}</td>"
            f"<td style='padding:0 8px'>{html.escape(str(r.get('title') or ''))}</td>"
            f"<td style='padding:0 8px'>{st(r)}</td><td style='padding:0 8px'>{html.escape(r.get('folder') or '')}"
            f"</td><td style='padding:0 8px'><small>{html.escape(r.get('message') or '')}"
            + (f" · missing: {html.escape(', '.join(r['missing']))}" if r.get("missing") else "")
            + "</small></td></tr>" for r in rows)
        return ("<table style='font-size:13px'><tr><th>session</th><th>title</th><th>status</th><th>folder</th>"
                f"<th></th></tr>{tr}</table>")

    def _elab_apply(self):
        if not self.elab_result:
            self.w_el_status.value = _ok("read an entry first", False)
            return
        new = self.elab_result["meta"]
        cur = self.get_meta()
        if not self.w_el_overwrite.value:      # append the elabFTW text to a description that is already there
            for sec, key, _l in md.HTML_FIELDS:
                a, b = cur[sec].get(key) or "", new.get(sec, {}).get(key) or ""
                if a and b and b not in a:
                    new = dict(new)
                    new[sec] = dict(new[sec])
                    new[sec][key] = a + b
                    cur[sec][key] = ""
        self.set_meta(md.merge(cur, new, overwrite=self.w_el_overwrite.value))
        self.log("metadata of elabFTW put into tab 2 (" + ("overwriting" if self.w_el_overwrite.value
                                                            else "only empty fields") + ")")
        self.tabs.selected_index = 1

    def _elab_download(self):
        if not self.elab_result:
            return
        r, ups = self.elab_result, self.elab_result["uploads"]
        sel = [ups[i] for i in self.w_el_files.value]
        etype, eid = r["meta"]["source"]["elabftw_type"], r["meta"]["source"]["elabftw_id"]
        tot = sum(int(u.get("filesize") or 0) for u in sel) or 1
        done = [0]
        for u in sel:
            d0 = done[0]

            def prog(name, got, total, d0=d0):
                done[0] = d0 + got
                self.w_el_dl.value = (f"<span style='color:#1565c0'>⬇ {html.escape(name)}: {_mb(got)} of "
                                      f"{_mb(total)}</span><progress value='{done[0]}' max='{tot}' "
                                      f"style='width:300px;margin-left:8px'></progress>")
            try:
                tmp = tempfile.mkdtemp(dir=self.work_dir)
                p = self.elab_client.download(etype, eid, u, tmp, progress=prog)
                dst = os.path.join(self.files_dir, self._unique(os.path.basename(p)))
                shutil.move(p, dst)
                shutil.rmtree(tmp, ignore_errors=True)
                self._add_file(dst, "elabFTW")
            except Exception as e:  # noqa: BLE001
                self.w_el_dl.value = _ok(f"download of {u.get('real_name')} failed: {e}", False)
                return
        self.w_el_dl.value = _ok(f"✔ {len(sel)} attachment(s) added to tab 1")

    # ------------------------------------------------------------------ tab 4: NOMAD
    def _nomad_tab(self, nomad_url, token):
        self.w_banner = W.HTML()
        self.w_nm_url = W.Dropdown(options=[("NOMAD (central)", "production"), ("NOMAD test", "test"),
                                            ("other / Oasis", "custom")],
                                   value=nomad_url if nomad_url in ("production", "test") else "custom",
                                   layout=_L(width="200px"))
        self.w_nm_custom = W.Text(value="" if nomad_url in ("production", "test") else nomad_url,
                                  placeholder="https://…/api/v1", layout=_L(width="360px"))
        self.w_nm_token = W.Password(value=token or "", placeholder="API token (NOMAD: user menu → API tokens)",
                                     layout=_L(width="360px"))
        b_conn = W.Button(description="connect", icon="plug", layout=_L(width="110px"))
        b_conn.on_click(lambda _: self.connect())
        self.conn_box = W.VBox([W.HBox([self.w_nm_url, self.w_nm_custom]), W.HBox([self.w_nm_token, b_conn])])
        self.w_conn_status = W.HTML()
        self.conn_box.children = tuple(self.conn_box.children) + (self.w_conn_status,)
        self.w_folder = W.Text(placeholder="folder in the upload (default: tem/<title>)", layout=_L(width="420px"))
        self.w_name = W.Text(placeholder="base name of the ELN files (default: from the title)",
                             layout=_L(width="420px"))
        self.w_schema = W.Checkbox(value=self.in_nomad, indent=False, style={"description_width": "initial"},
                                   description="ELN with the emingest schema (needs the plugin on that NOMAD; "
                                               "otherwise NOMAD's basic ELNMeasurement)", layout=_L(width="auto"))
        opts = [("with the NOMAD action, on a worker (large data)", "action"),
                ("here, before uploading", "local"), ("no conversion", "none")]
        self.w_conv = W.RadioButtons(options=opts, value="action" if self.in_nomad else "local",
                                     layout=_L(width="420px"))
        self.w_max_gb = W.FloatText(value=20.0, layout=_L(width="90px"))
        self.w_force = W.Checkbox(value=False, indent=False, description="upload although required metadata are "
                                                                         "missing", layout=_L(width="auto"))
        self.w_push = W.Button(description="Upload to NOMAD", icon="cloud-upload", button_style="success",
                               layout=_L(width="200px"))
        self.w_push.on_click(lambda _: threading.Thread(target=self.push, daemon=True).start())
        self.w_push_status = W.HTML()
        self.w_result = W.HTML()
        lab = lambda t: W.HTML(t, layout=_L(width="150px"))  # noqa: E731
        ui = W.VBox([self.conn_box, self.project.box,
                     W.HBox([lab("folder"), self.w_folder]), W.HBox([lab("name"), self.w_name]),
                     W.HBox([lab("ELN"), self.w_schema]),
                     W.HBox([lab("<b>NXem conversion</b>"), self.w_conv,
                             W.VBox([W.HTML("<small>files up to (GB)</small>"), self.w_max_gb])]),
                     W.HBox([lab(""), self.w_force]), W.HBox([lab(""), self.w_push]), self.w_push_status,
                     self.w_result])
        if not self.server_mode:
            b_local = W.Button(description="write here only", icon="folder", layout=_L(width="160px"),
                               tooltip="ELN, eln_data.yaml and (if chosen) NXem files into the session folder")
            b_local.on_click(lambda _: threading.Thread(target=self.prepare, kwargs=dict(report=True),
                                                        daemon=True).start())
            ui.children = ui.children[:-3] + (W.HBox([lab(""), self.w_push, b_local]),) + ui.children[-2:]
        return ui

    def connect(self, url=None, token=None):
        if url is None:
            url = self.w_nm_custom.value.strip() if self.w_nm_url.value == "custom" else self.w_nm_url.value
        token = self.w_nm_token.value if token is None else token
        try:
            c = NomadClient(url, token, timeout=30)
            me = c.me()
        except NomadError as e:
            self.w_banner.value = _ok(f"not connected: {e}", False)
            return False
        self.client = c
        name = me.get("name") or me.get("username") or me.get("user_id", "")
        self.w_banner.value = ""                     # no line above the title once signed in
        self.w_conn_status.value = f"<small>connected to {html.escape(c.url)} as {html.escape(str(name))}</small>"
        rows = self._user_rows
        if len(rows) == 1 and not any(w.value for w in rows[0]["w"].values()) and name:
            rows[0]["w"]["name"].value = str(name)
            if me.get("affiliation"):
                rows[0]["w"]["affiliation"].value = str(me["affiliation"])
        self.project.connect(c.url, c.token)
        return True

    def _list_uploads(self):
        self.project.refresh()

    # ------------------------------------------------------------------ preparing and pushing
    def _names(self, meta):
        alias = meta["entry"].get("experiment_alias") or meta["sample"].get("name") or time.strftime("%Y%m%d_%H%M")
        name = safe_name(self.w_name.value or alias)
        folder = self.w_folder.value.strip().strip("/") or f"tem/{safe_name(alias)}"
        if ".." in folder.split("/"):
            raise ValueError("invalid folder")
        return name, folder

    def prepare(self, report=False, conversion=None):
        """ELN + eln_data.yaml (+ NXem files and conversion record if converted here) into a new output folder.
        Returns dict(name, folder, items=[(local, path in folder)], results, meta, out)."""
        meta = self.get_meta()
        name, folder = self._names(meta)
        self.wait_inspected()
        with self.lock:
            fl = list(self.files.values())
        if not fl:
            raise ValueError("no files: add files in tab 1")
        out = tempfile.mkdtemp(prefix="out_", dir=self.work_dir) if self.server_mode or not report else \
            os.path.join(self.out_root, f"{name}_{time.strftime('%Y%m%d_%H%M%S')}")
        from . import ingest

        def cb(i, n, nm):
            self.w_push_status.value = (f"<span style='color:#1565c0'>NXem conversion {i + 1 if nm else n} of "
                                        f"{n}: {html.escape(nm)}</span>" if nm else
                                        "<span style='color:#1565c0'>NXem conversion done</span>")
        files = [dict(f, kind=self.kind(f)) for f in fl if not f["name"].lower().endswith(".eln")]
        ds = ingest.build_dataset(files, meta, name, folder, out, schema="plugin" if self.w_schema.value else "core",
                                  conversion=conversion or self.w_conv.value, max_gb=self.w_max_gb.value,
                                  callback=cb)
        items, results = ds["items"], ds["results"]
        if ds["workflow"]:
            self.w_result.value = self._results_html(results) + elnm.preview_svg(ds["workflow"])
        if report:
            self.w_push_status.value = _ok(f"✔ written into {out}")
        return {"name": name, "folder": folder, "items": items, "results": results, "meta": meta, "out": out}

    def _results_html(self, rec):
        rows = "".join(f"<tr><td style='padding:0 8px'>{html.escape(os.path.basename(r['inputs'][0]))}</td>"
                       f"<td style='padding:0 8px'>{'✔' if r['status'] == 'ok' else '✖' if r['status'] == 'failed' else '–'}"
                       f" {html.escape(r['status'])}</td><td style='padding:0 8px'><small>"
                       f"{html.escape(str(r.get('message') or ''))}</small></td></tr>" for r in rec)
        return f"<table style='font-size:13px'>{rows}</table>" if rows else ""

    def push(self):
        if self._busy:
            return
        self._busy = True
        self.w_push.disabled = True
        self.w_result.value = ""
        try:
            self._push()
        except (ValueError, NomadError) as e:
            self.w_push_status.value = _ok(str(e), False)
        except Exception as e:  # noqa: BLE001
            self.w_push_status.value = _ok(f"failed: {e!r}", False)
            self.log(traceback.format_exc())
        finally:
            self._busy = False
            self.w_push.disabled = False

    def _push(self):
        if not self.client and not self.connect():
            raise ValueError("not connected to NOMAD")
        req, _rec = md.check(self.get_meta())
        if req and not self.w_force.value:
            raise ValueError("required metadata missing (tab 2): " + ", ".join(req) +
                             " — fill them in or tick 'upload although required metadata are missing'")
        if self.project.w_sel.value is None:
            raise ValueError("choose the NOMAD project (tab 1 or 4)")
        if self.project.w_sel.value == NEW and not self.project.can_create():
            raise ValueError("the maximum number of open NOMAD projects is reached: choose an existing project")
        conv = self.w_conv.value
        self.w_push_status.value = "<i>preparing …</i>"
        p = self.prepare(conversion=conv)
        t0 = time.time()

        def prog(sent, size):
            rate = sent / max(time.time() - t0, 1e-3) / 1e6
            self.w_push_status.value = (f"<span style='color:#1565c0'>⬆ to NOMAD: {100 * sent / max(size, 1):.0f} % "
                                        f"({_mb(sent)} of {_mb(size)}, {rate:.1f} MB/s)</span><progress "
                                        f"value='{sent}' max='{size}' style='width:300px;margin-left:8px'></progress>")
        uid = self.project.w_sel.value
        if uid == NEW:
            try:
                uid = create_project(self.client.url, self.client.token, self.project.w_name.value.strip()
                                     or p["meta"]["entry"].get("experiment_alias") or p["name"])
            except ProjectError as e:
                raise ValueError(str(e)) from None
            self.project.refresh(select=uid)
        r = self.client.upload(p["items"], upload_id=uid, folder=p["folder"], progress=prog, tmp_dir=self.work_dir)
        uid = r["upload_id"]
        shutil.rmtree(p["out"], ignore_errors=True)
        self.w_push_status.value = "<i>NOMAD is processing the upload …</i>"
        st = self.client.wait_processed(uid, timeout=1800)
        link = (f"<a href='{html.escape(r['gui_url'])}' target='_blank'>open the upload in NOMAD</a>"
                if r.get("gui_url") else "")
        msg = (f"✔ {r['files']} file(s) in folder <b>{html.escape(p['folder'])}</b> of upload "
               f"<code>{html.escape(uid)}</code> ({html.escape(str(st.get('process_status')))}) {link}")
        if st.get("errors"):
            msg += f"<br><span style='color:#b26a00'>NOMAD: {html.escape('; '.join(map(str, st['errors'][:3])))}</span>"
        self.w_push_status.value = msg
        self._list_uploads()
        if conv == "action":
            data = {"upload_id": uid, "folder": p["folder"], "eln_data": f"{p['folder']}/{p['name']}.eln_data.yaml",
                    "eln_entry": f"{p['folder']}/{p['name']}.archive.yaml", "name": p["name"],
                    "max_file_gb": float(self.w_max_gb.value)}
            try:
                iid = self.client.start_action(ACTION_ID, data)
            except NomadError as e:
                self.w_result.value = _ok(f"the conversion action could not be started ({e}); the files are in "
                                          f"NOMAD — convert them later or choose 'here, before uploading'", False)
                return
            self.last_instance = iid
            self.w_result.value = f"<i>NXem conversion action started ({html.escape(str(iid))}) …</i>"
            self._poll_action(iid)

    def _poll_action(self, iid, every=5, timeout=24 * 3600):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                s = self.client.action_status(iid)
            except NomadError as e:
                self.w_result.value = _ok(f"status of the action: {e}", False)
                return
            if s in ("COMPLETED", "FAILED", "TERMINATED", "CANCELED", "CANCELLED", "TIMED_OUT"):
                txt = ""
                try:
                    res = self.client.action_result(iid) or {}
                    res = res.get("data", res) if isinstance(res, dict) else res
                    txt = (res or {}).get("message", "") if isinstance(res, dict) else str(res)
                except NomadError as e:
                    txt = str(e)
                self.w_result.value = (_ok(f"NXem conversion {s.lower()}: {txt}", s == "COMPLETED"))
                return s
            self.w_result.value = (f"<i>NXem conversion on a NOMAD worker: {html.escape(s)} "
                                   f"({time.time() - t0:.0f} s)</i>")
            time.sleep(every)


def show(**kw):
    """Display the app in Jupyter."""
    from IPython.display import display
    a = App(**kw)
    display(a.ui)
    return a
