# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Import of experiments and items (resources) of elabFTW (REST API v2) into the metadata form and the file list.

API (elabFTW >= 4.3, ``{base}/api/v2``; the API key -- elabFTW user panel -> API keys, read access suffices -- is sent
as ``Authorization: <key>``; it is kept only in memory for the session):

* ``GET /experiments/{id}``, ``GET /items/{id}``                            entry (title, body, date, tags, metadata
  with the extra fields, links to items)
* ``GET /{experiments|items}/{id}/uploads``                                 attachments
* ``GET /{experiments|items}/{id}/uploads/{upload_id}?format=binary``       file content
* ``GET /users/{id}``                                                       author (name, ORCID)

:func:`parse_ref` accepts the address of an entry as shown in the browser (``.../experiments.php?mode=view&id=42``,
``.../database.php?mode=view&id=7``), an API address, or the server address plus type and ID.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import metadata as md
from . import richtext

TYPES = ("experiments", "items")


class ElabError(RuntimeError):
    pass


def parse_ref(url, entity_id=None, entity_type=None):
    """(api_base, entity_type, id) from an elabFTW address (+ optional ID and type)."""
    u = str(url or "").strip()
    if not u:
        raise ElabError("give the address of the elabFTW server or of the entry")
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urllib.parse.urlparse(u)
    q = urllib.parse.parse_qs(p.query)
    path = p.path or "/"
    etype, eid = entity_type, entity_id
    m = re.search(r"/api/v2/(experiments|items)/(\d+)", path)
    if m:
        etype, eid = etype or m.group(1), eid or m.group(2)
        root = path[: path.index("/api/v2")]
    else:
        m = re.search(r"/(experiments|database|resources|items)\.php$", path)
        root = path[: m.start()] if m else re.sub(r"/api(/v2)?/?$", "", path.rstrip("/"))
        if m:
            etype = etype or ("experiments" if m.group(1) == "experiments" else "items")
            if not eid and q.get("id"):
                eid = q["id"][0]
    if etype in ("resources", "database", "resource", "item"):
        etype = "items"
    if etype in ("experiment",):
        etype = "experiments"
    if etype not in TYPES:
        raise ElabError("choose whether the entry is an experiment or a resource (item)")
    try:
        eid = int(str(eid).strip())
    except (TypeError, ValueError):
        raise ElabError("give the ID of the entry (the number after id= in its address)") from None
    base = f"{p.scheme}://{p.netloc}{root.rstrip('/')}/api/v2"
    return base, etype, eid


class ElabClient:
    def __init__(self, api_base, api_key, verify=True, timeout=60, opener=None):
        self.base = api_base.rstrip("/")
        self.key = str(api_key or "").strip()
        if not self.key:
            raise ElabError("an elabFTW API key is needed (elabFTW: user panel -> API keys; read access)")
        self.timeout = timeout
        ctx = None
        if not verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.ctx = ctx
        self.opener = opener

    def _open(self, path, params=None):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        req = urllib.request.Request(self.base + path + q, headers={"Authorization": self.key,
                                                                    "Accept": "application/json, */*"})
        try:
            if self.opener:
                return self.opener(req, timeout=self.timeout)
            return urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ElabError(f"elabFTW refused the request ({e.code}): check the API key and your access to the "
                                f"entry") from None
            if e.code == 404:
                raise ElabError(f"not found on elabFTW (404): {path}") from None
            raise ElabError(f"elabFTW request failed: {e}") from None
        except urllib.error.URLError as e:
            raise ElabError(f"elabFTW is not reachable ({e.reason}): {self.base}") from None

    def get(self, path, params=None):
        with self._open(path, params) as r:
            return json.loads(r.read().decode() or "null")

    def entity(self, etype, eid):
        return self.get(f"/{etype}/{int(eid)}")

    def uploads(self, etype, eid):
        """[dict(id, real_name, filesize, comment, hash)] of the attachments."""
        try:
            ups = self.get(f"/{etype}/{int(eid)}/uploads")
        except ElabError:
            ups = (self.entity(etype, eid) or {}).get("uploads") or []
        return [u for u in ups or [] if isinstance(u, dict) and u.get("id") is not None and int(u.get("state", 1)) == 1]

    def download(self, etype, eid, upload, dest_dir, progress=None, chunk=4 << 20):
        """Stream one attachment into dest_dir (name: its real name).  Returns the local path."""
        name = os.path.basename(str(upload.get("real_name") or f"upload_{upload['id']}")) or f"upload_{upload['id']}"
        os.makedirs(dest_dir, exist_ok=True)
        dst = os.path.join(dest_dir, name)
        total = int(upload.get("filesize") or 0)
        got = 0
        with self._open(f"/{etype}/{int(eid)}/uploads/{int(upload['id'])}", {"format": "binary"}) as r, \
                open(dst, "wb") as fo:
            while True:
                b = r.read(chunk)
                if not b:
                    break
                fo.write(b)
                got += len(b)
                if progress:
                    progress(name, got, total)
        return dst

    def user(self, uid):
        try:
            return self.get(f"/users/{int(uid)}")
        except Exception:  # noqa: BLE001
            return {}


# ------------------------------------------------------------------ mapping elabFTW -> metadata
EXTRA_MAP = [  # (regex on the extra field's name, (section, key))
    (r"^(sample|specimen)([ _-]?name)?$", ("sample", "name")),
    (r"^(sample|specimen)[ _-]?(id|identifier|number|no\.?)$", ("sample", "identifier_sample")),
    (r"^(parent|wafer|batch|material)[ _-]?(id|identifier)?$", ("sample", "identifier_parent")),
    (r"(chemical[ _-]?)?formula|composition", ("sample", "chemical_formula")),
    (r"^elements?$|atom[ _-]?types", ("sample", "atom_types")),
    (r"prep(aration)?[ _-]?date|date[ _-]?of[ _-]?prep", ("sample", "preparation_date")),
    (r"thickness", ("sample", "thickness")),
    (r"physical[ _-]?form|sample[ _-]?type|geometry", ("sample", "physical_form")),
    (r"simulat", ("sample", "is_simulation")),
    (r"^(microscope|instrument|tem|equipment)([ _-]?name)?$", ("instrument", "name")),
    (r"vendor|manufacturer", ("instrument", "vendor")),
    (r"^model$|microscope[ _-]?model", ("instrument", "model")),
    (r"serial", ("instrument", "serial_number")),
    (r"high[ _-]?tension|acceleration|voltage|^ht$|^kv$|beam[ _-]?energy", ("instrument", "voltage_kV")),
    (r"^mode$|operation[ _-]?mode|technique|method", ("instrument", "operation_mode")),
    (r"detector", ("instrument", "detectors")),
    (r"location|^lab$|room|facility", ("instrument", "location")),
    (r"^project", ("project", "name")),
    (r"start|session[ _-]?date|^date$|measurement[ _-]?date", ("entry", "start_time")),
    (r"^end", ("entry", "end_time")),
]


def _num(s):
    m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(s))
    return float(m.group(0)) if m else None


def extra_fields(entity):
    meta = entity.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except ValueError:
            meta = {}
    ef = (meta or {}).get("extra_fields") or {}
    out = {}
    for k, v in ef.items():
        val = v.get("value") if isinstance(v, dict) else v
        if isinstance(val, list):
            val = ", ".join(str(x) for x in val)
        if val in (None, ""):
            continue
        unit = v.get("unit") if isinstance(v, dict) else None
        out[str(k)] = (val, unit, (v.get("type") if isinstance(v, dict) else None))
    return out


def map_extra(fields, meta, only_sections=None):
    """Put extra fields of elabFTW into the metadata (by name); returns the names that were used."""
    used = []
    for name, (val, unit, typ) in fields.items():
        if typ in ("items", "experiments", "users"):
            continue
        n = name.strip().lower()
        for rx, (sec, key) in EXTRA_MAP:
            if only_sections and sec not in only_sections:
                continue
            if re.search(rx, n):
                if key == "thickness":
                    x = _num(val)
                    if x is None:
                        break
                    meta[sec][key] = x
                    u = (unit or re.sub(r"[-+\d.eE\s]", "", str(val)) or "nm").replace("µ", "u")
                    meta[sec]["thickness_unit"] = u if u in ("nm", "um", "A", "m") else "nm"
                elif key == "voltage_kV":
                    x = _num(val)
                    if x is None:
                        break
                    u = str(unit or val).lower()
                    meta[sec][key] = x / 1e3 if ("ev" in u and "kev" not in u and x > 1e3) or "volt" == u.strip() \
                        or (u.strip() == "v") else x
                elif key == "is_simulation":
                    meta[sec][key] = str(val).strip().lower() in ("1", "yes", "true", "on", "y", "simulated")
                elif key in ("start_time", "end_time", "preparation_date"):
                    meta[sec][key] = md.to_iso(val) or str(val)
                else:
                    meta[sec][key] = str(val)
                used.append(name)
                break
    return used


def _body_html(entity):
    body = entity.get("body_html") or entity.get("body") or ""
    if int(entity.get("content_type") or 1) == 2 and not entity.get("body_html"):     # markdown
        return richtext.from_text(body)
    return richtext.sanitize(body)


def _tags(entity):
    t = entity.get("tags") or ""
    if isinstance(t, list):
        return [str(x.get("tag") if isinstance(x, dict) else x) for x in t]
    return [x.strip() for x in str(t).split("|") if x.strip()]


def entry_url(api_base, etype, eid):
    root = api_base.rsplit("/api/v2", 1)[0]
    page = "experiments.php" if etype == "experiments" else "database.php"
    return f"{root}/{page}?mode=view&id={eid}"


def import_entry(client, etype, eid, follow_links=True, zone=None):
    """Metadata (as :func:`emingest.metadata.empty`, only the fields found) + attachments of the entry.

    * title -> experiment alias, date -> start, main text -> description, tags, author (with ORCID) -> user
    * extra fields -> form fields by their names (sample, formula, microscope, high tension ...)
    * a linked item (e.g. the sample in the elabFTW database) -> sample name, ID, extra fields; its main text ->
      sample history
    Returns dict(meta, uploads, summary, links)."""
    ent = client.entity(etype, eid)
    if not isinstance(ent, dict):
        raise ElabError("unexpected answer of elabFTW")
    from . import tem_session as ts
    if etype == "experiments" and ts.is_session(ent):        # TEM Session template (tem-provenance)
        s = ts.session_metadata(client, eid, zone=zone or ts.DEFAULT_TZ)
        ups = client.uploads(etype, eid)
        summary = (f"TEM session #{eid} “{ent.get('title')}”: sample “{s['sample'].get('title')}”, "
                   f"microscope “{s['microscope'].get('title')}”, acquisition folder {s['path']}; "
                   f"{len(ups)} attachment(s)")
        return {"meta": s["meta"], "uploads": ups, "summary": summary, "links": [], "entity": ent, "session": s}
    meta = {"entry": {}, "project": {}, "sample": {}, "instrument": {}, "users": [], "tags": _tags(ent),
            "source": {}}
    title = str(ent.get("title") or "")
    meta["entry"]["experiment_alias"] = title
    meta["entry"]["identifier_experiment"] = f"elabftw:{etype}/{eid}" + (f" ({ent['elabid']})" if ent.get("elabid")
                                                                        else "")
    if ent.get("date"):
        meta["entry"]["start_time"] = md.to_iso(ent["date"])
    url = client.entry_url(etype, eid) if hasattr(client, "entry_url") else entry_url(client.base, etype, eid)
    try:
        num = int(eid)
    except (TypeError, ValueError):
        num = str(eid)                         # entries of other ELN software in an .eln file
    meta["source"] = {"elabftw_url": url, "elabftw_type": etype, "elabftw_id": num, "elabftw_title": title}
    if getattr(client, "archive", None) is not None:
        meta["source"]["eln_file"] = client.archive.name
    body = _body_html(ent)
    if etype == "items":                       # an item is typically the sample itself
        meta["sample"]["name"] = title
        meta["sample"]["history"] = body
    head = (f"<p><i>Imported from elabFTW: <a href=\"{url}\">{richtext._html.escape(title)}</a>"
            f"{' (' + richtext._html.escape(str(ent.get('category_title'))) + ')' if ent.get('category_title') else ''}"
            f"</i></p>")
    meta["entry"]["experiment_description"] = head + (body if etype == "experiments" else "")
    used = map_extra(extra_fields(ent), meta)
    uid = ent.get("userid")
    if ent.get("fullname") or uid:
        u = {"name": str(ent.get("fullname") or "").strip()}
        info = client.user(uid) if uid else {}
        if info.get("orcid"):
            u["orcid"] = md.norm_orcid(info["orcid"]) or info["orcid"]
        # the e-mail address is not copied (personal data): the user can type it in the form
        if u["name"]:
            meta["users"].append(u)
    links = []
    if follow_links:
        for ln in (ent.get("items_links") or []):
            lid = ln.get("entityid") or ln.get("itemid") or ln.get("id")
            if lid is None:
                continue
            try:
                it = client.entity("items", lid)
            except ElabError:
                continue
            cat = str(it.get("category_title") or ln.get("category_title") or "")
            links.append({"id": lid, "title": it.get("title"), "category": cat})
            is_sample = "sample" in cat.lower() or "specimen" in cat.lower()
            if is_sample or not meta["sample"].get("name"):
                if is_sample or len(ent.get("items_links") or []) == 1:
                    meta["sample"].setdefault("name", str(it.get("title") or ""))
                    meta["sample"].setdefault("identifier_sample", f"elabftw:items/{lid}")
                    hb = _body_html(it)
                    if hb:
                        meta["sample"]["history"] = (meta["sample"].get("history") or "") + hb
                    map_extra(extra_fields(it), meta, only_sections=("sample",))
        if links:
            link = client.entry_url if hasattr(client, "entry_url") else (
                lambda t, i: entry_url(client.base, t, i))
            lis = "".join(f"<li><a href=\"{link('items', x['id'])}\">"
                          f"{richtext._html.escape(str(x['title']))}</a>"
                          f"{' (' + richtext._html.escape(x['category']) + ')' if x['category'] else ''}</li>"
                          for x in links)
            meta["entry"]["experiment_description"] += f"<p><b>Linked resources in elabFTW:</b></p><ul>{lis}</ul>"
    ups = client.uploads(etype, eid)
    summary = (f"{'experiment' if etype == 'experiments' else 'resource'} #{eid} “{title}”: "
               f"{len(ups)} attachment(s), {len(used)} extra field(s) used"
               + (f" ({', '.join(used)})" if used else "") + (f", {len(links)} linked resource(s)" if links else ""))
    return {"meta": meta, "uploads": ups, "summary": summary, "links": links, "entity": ent}
