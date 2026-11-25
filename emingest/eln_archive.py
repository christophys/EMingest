# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""elabFTW data without a running elabFTW: the ``.eln`` export (ELN file format of TheELNConsortium, an RO-Crate in
a zip file) as written by ``bin/console export:eln <team>`` or the export tab of elabFTW.

:class:`ElnArchive` reads the ``ro-crate-metadata.json`` of the archive and gives every entry (experiment,
resource, template) in the shape of the elabFTW API v2, so that the import code of :mod:`emingest.elabftw` and
:mod:`emingest.tem_session` works on an archive exactly as on a server:

* ``Dataset`` nodes -> entries: ``name`` -> title, ``text`` -> body, ``temporal`` / ``dateCreated`` -> date,
  ``identifier`` -> elabid, ``keywords`` -> tags, ``about`` -> category, ``creativeWorkStatus`` -> status,
  ``genre`` (experiment / resource / ... template) -> type, the numeric ID from ``url`` (``…?mode=view&id=42``),
  ``author`` -> fullname and ORCID, ``mentions`` -> links, ``hasPart`` (``File``) -> attachments,
  ``variableMeasured`` -> the extra fields (the ``elabftw_metadata`` JSON as it was in the database; for other ELN
  software the single ``PropertyValue`` nodes);
* extra fields that reference other entries (type ``items`` / ``experiments``) keep the numeric ID of the database;
  they are resolved through the IDs in the ``url`` of the exported entries, or through the ``@id`` that newer
  versions of elabFTW write into the ``PropertyValue``.

The archive can be a local file, a file object (e.g. an SFTP file of :mod:`emingest.remote`: only the parts that
are needed are transferred) or a file uploaded in the dashboard.  Attachments are read from the zip on demand and
checked against their SHA-256 sums.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import urllib.parse
import zipfile

from .elabftw import ElabError

GENRES = {"experiment": "experiments", "resource": "items", "item": "items", "database": "items",
          "experiment template": "experiments_templates", "resource template": "items_types",
          "resource category": "items_types", "experiments_templates": "experiments_templates"}


def _types(node):
    t = node.get("@type")
    return set(t) if isinstance(t, list) else {t}


def _ref(v):
    return v.get("@id") if isinstance(v, dict) else v


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


class ElnArchive:
    """An ``.eln`` file (path or seekable binary file object)."""

    def __init__(self, src, name=None):
        self.name = name or (os.path.basename(src) if isinstance(src, str) else getattr(src, "name", "archive.eln"))
        try:
            self.zip = zipfile.ZipFile(src)
        except zipfile.BadZipFile:
            raise ElabError(f"{self.name} is not an .eln file (not a zip archive)") from None
        metas = [n for n in self.zip.namelist() if n.endswith("ro-crate-metadata.json")]
        if not metas:
            raise ElabError(f"{self.name}: no ro-crate-metadata.json (not an .eln export)")
        meta = min(metas, key=len)
        self.root = posixpath.dirname(meta)
        with self.zip.open(meta) as f:
            crate = json.load(f)
        self.graph = {n.get("@id"): n for n in crate.get("@graph") or [] if isinstance(n, dict)}
        root_node = self.graph.get("./") or {}
        self.version = str(root_node.get("version") or "")
        self.software = ""
        for n in self.graph.values():
            if "SoftwareApplication" in _types(n):
                self.software = f"{n.get('name', '')} {n.get('version', '')}".strip()
        self.entries = {}                      # @id -> api-like dict
        self.keys = {}                         # (type, numeric id) / elabid / @id -> @id
        self.base = ""
        for nid, n in self.graph.items():
            if nid == "./" or "Dataset" not in _types(n):
                continue
            e = self._entity(nid, n)
            self.entries[nid] = e
            self.keys[(e["type"], str(e["id"]))] = nid
            if e.get("elabid"):
                self.keys[("elabid", e["elabid"])] = nid
            self.keys[("@id", nid)] = nid
            if not self.base and e.get("url"):
                m = re.match(r"^(.*?)/(?:experiments|database|resources|templates)\.php", e["url"])
                if m:
                    self.base = m.group(1) + "/api/v2"
        self.base = self.base or "eln-archive://" + self.name + "/api/v2"

    # ------------------------------------------------------------------ entries
    def _entity(self, nid, n):
        url = str(n.get("url") or (nid if str(nid).startswith("http") else ""))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        genre = str(n.get("genre") or "").strip().lower()
        etype = GENRES.get(genre)
        if not etype:
            etype = "experiments" if "experiments.php" in url else "items" if (
                "database.php" in url or "resources.php" in url) else "experiments"
        num = q.get("id", [None])[0]
        author = self.graph.get(_ref(n.get("author"))) or {}
        cat = self.graph.get(_ref(n.get("about"))) or {}
        files = []
        for i, part in enumerate(_as_list(n.get("hasPart"))):
            fid = _ref(part)
            f = self.graph.get(fid) or {}
            if "File" not in _types(f) or str(fid).startswith(("http://", "https://")):
                continue                       # (files referenced by URL are not in the archive)
            files.append({"id": i + 1, "real_name": f.get("name") or posixpath.basename(fid.rstrip("/")),
                          "filesize": int(f.get("contentSize") or 0), "sha256": f.get("sha256") or "",
                          "comment": f.get("description") or "", "state": 1, "_member": self._member(fid)})
        links = {"items": [], "experiments": []}
        for m in _as_list(n.get("mentions")):
            t = self.graph.get(_ref(m)) or {}
            if "Dataset" not in _types(t):
                continue
            tt = GENRES.get(str(t.get("genre") or "").lower()) or ("items" if "database.php" in str(t.get("url"))
                                                                     else "experiments")
            if tt in links:
                links[tt].append({"entityid": self._num(t) or _ref(m), "title": t.get("name"),
                                  "category_title": (self.graph.get(_ref(t.get("about"))) or {}).get("name")})
        kw = n.get("keywords") or ""
        tags = kw if isinstance(kw, list) else [k.strip() for k in str(kw).split(",") if k.strip()]
        return {"@id": nid, "type": etype, "id": num if num is not None else nid, "title": n.get("name") or "",
                "body": n.get("text") or "", "date": n.get("temporal") or n.get("dateCreated") or "",
                "created_at": n.get("dateCreated"), "modified_at": n.get("dateModified"),
                "elabid": n.get("identifier") if isinstance(n.get("identifier"), str) else "",
                "tags": "|".join(tags), "category_title": cat.get("name") or "",
                "status_title": n.get("creativeWorkStatus") or "", "url": url,
                "fullname": " ".join(x for x in (author.get("givenName"), author.get("familyName")) if x)
                or author.get("name") or "", "userid": _ref(n.get("author")),
                "metadata": self._metadata(n), "items_links": links["items"],
                "experiments_links": links["experiments"], "uploads": files, "content_type": 1}

    def _num(self, node):
        u = node.get("url") or (node.get("@id") if str(node.get("@id")).startswith("http") else "")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(str(u or "")).query)
        return q.get("id", [None])[0]

    def _member(self, fid):
        p = urllib.parse.unquote(str(fid))
        p = p[2:] if p.startswith("./") else p
        cand = posixpath.join(self.root, p) if self.root else p
        names = set(self.zip.namelist())
        if cand in names:
            return cand
        alt = cand.replace("//", "/")
        for n in names:                        # older exports: "folder//file" in the zip
            if n.replace("//", "/") == alt:
                return n
        return cand

    def _metadata(self, n):
        """The extra fields as elabFTW stores them (JSON string)."""
        pvs = [self.graph.get(_ref(v)) or {} for v in _as_list(n.get("variableMeasured"))]
        for pv in pvs:
            if pv.get("propertyID") == "elabftw_metadata" and pv.get("value"):
                raw = pv["value"]
                try:
                    meta = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    break
                # newer exports also have the references as @id in the single PropertyValues
                for p in pvs:
                    name, val = p.get("name") or p.get("propertyID"), p.get("value")
                    ef = (meta.get("extra_fields") or {}).get(name) if name else None
                    if isinstance(ef, dict) and isinstance(val, dict) and val.get("@id"):
                        ef["_ref"] = val["@id"]
                return json.dumps(meta)
        ef = {}
        for pv in pvs:                          # other ELN software: one PropertyValue per field
            name = pv.get("name") or pv.get("propertyID")
            if not name or name == "elabftw_metadata":
                continue
            v = pv.get("value")
            d = {"value": _ref(v) if isinstance(v, dict) else v, "type": pv.get("additionalType") or "text"}
            if pv.get("unitText"):
                d["unit"] = pv["unitText"]
            if isinstance(v, dict) and v.get("@id"):
                d["_ref"] = v["@id"]
            ef[str(name)] = d
        return json.dumps({"extra_fields": ef}) if ef else ""

    def list(self, types=("experiments", "items")):
        """[dict(type, id, title, date, category, status, elabid, files, @id)] of the entries."""
        out = []
        for e in self.entries.values():
            if types and e["type"] not in types:
                continue
            out.append({k: e[k] for k in ("type", "id", "title", "date", "elabid", "@id")} |
                       {"category": e["category_title"], "status": e["status_title"], "files": len(e["uploads"]),
                        "author": e["fullname"]})
        return sorted(out, key=lambda x: (x["type"], str(x["date"]), str(x["title"])), reverse=False)

    def find(self, etype, eid):
        """@id of an entry by (type, numeric ID), elabid or @id."""
        s = str(eid)
        for k in ((etype, s), ("elabid", s), ("@id", s)):
            if k in self.keys:
                return self.keys[k]
        if etype == "items":                       # a reference written as @id of a template / other type
            for t in ("items_types", "experiments"):
                if (t, s) in self.keys:
                    return self.keys[(t, s)]
        raise ElabError(f"{etype} {eid} is not in {self.name}")

    def close(self):
        self.zip.close()


class ElnArchiveClient:
    """The interface of :class:`emingest.elabftw.ElabClient` on an :class:`ElnArchive`."""

    def __init__(self, archive):
        self.archive = archive
        self.base = archive.base

    def entity(self, etype, eid):
        e = dict(self.archive.entries[self.archive.find(etype, eid)])
        meta = e.get("metadata")
        if meta:                                    # references given as @id -> numeric IDs of this archive
            try:
                m = json.loads(meta)
                for f in (m.get("extra_fields") or {}).values():
                    ref = f.get("_ref") if isinstance(f, dict) else None
                    if ref and ref in self.archive.entries:
                        f["value"] = self.archive.entries[ref]["id"]
                e["metadata"] = json.dumps(m)
            except ValueError:
                pass
        return e

    def uploads(self, etype, eid):
        return list(self.entity(etype, eid)["uploads"])

    def user(self, uid):
        p = self.archive.graph.get(uid) or {}
        ident = p.get("identifier")
        orcid = ident if isinstance(ident, str) and "orcid" in ident else ""
        return {"orcid": orcid, "fullname": " ".join(x for x in (p.get("givenName"), p.get("familyName")) if x)}

    def entry_url(self, etype, eid):
        try:
            e = self.archive.entries[self.archive.find(etype, eid)]
            if e.get("url"):
                return e["url"]
        except ElabError:
            pass
        return f"{self.archive.name}#{etype}/{eid}"

    def download(self, etype, eid, upload, dest_dir, progress=None, chunk=4 << 20):
        name = os.path.basename(str(upload.get("real_name") or f"upload_{upload['id']}")) or f"upload_{upload['id']}"
        os.makedirs(dest_dir, exist_ok=True)
        dst = os.path.join(dest_dir, name)
        h, got = hashlib.sha256(), 0
        try:
            src = self.archive.zip.open(upload["_member"])
        except KeyError:
            raise ElabError(f"{name} is listed but not contained in {self.archive.name}") from None
        with src, open(dst, "wb") as fo:
            while True:
                b = src.read(chunk)
                if not b:
                    break
                fo.write(b)
                h.update(b)
                got += len(b)
                if progress:
                    progress(name, got, int(upload.get("filesize") or 0))
        if upload.get("sha256") and h.hexdigest() != upload["sha256"]:
            os.remove(dst)
            raise ElabError(f"{name}: SHA-256 sum does not match the .eln file (damaged archive?)")
        return dst
