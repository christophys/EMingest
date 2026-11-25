# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""NOMAD (production, test or an Oasis) API v1 client of the dashboard (urllib only; adapted from fit4DSTEM).

An API token is sent as ``Authorization: Bearer <token>`` (in the NOMAD dashboard: the user's login); it is never
written to disk.  Files are sent as one zip built on disk and streamed (no size limit in memory)::

    PUT  /uploads/{id}/raw/{folder}?file_name=x.zip      add files (the zip is extracted into the folder)
    POST /uploads?file_name=x.zip&upload_name=...         new upload
    GET  /uploads/{id}                                    processing state
    POST /actions/{action_id}/start  {"data": {...}}      start an action; status / result of the instance
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

NOMAD_URLS = {"production": "https://nomad-lab.eu/prod/v1/api/v1",
              "test": "https://nomad-lab.eu/prod/v1/test/api/v1"}


class NomadError(RuntimeError):
    pass


class NomadClient:
    """Minimal NOMAD API v1 client (urllib only).  ``opener`` can replace urllib.request.urlopen (tests)."""

    def __init__(self, url="production", token="", opener=None, timeout=300):
        url = str(url or "production").strip()
        self.url = NOMAD_URLS.get(url, url).rstrip("/")
        self.token = str(token or "").strip()
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    # ------------------------------------------------------------------ http
    def _req(self, method, path, params=None, data=None, ctype=None, raw=False):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        hdr = {"Accept": "application/json" if not raw else "*/*"}
        if self.token:
            hdr["Authorization"] = "Bearer " + self.token
        if ctype:
            hdr["Content-Type"] = ctype
        req = urllib.request.Request(self.url + path + q, data=data, headers=hdr, method=method)
        try:
            with self.opener(req, timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as ex:
            detail = ""
            try:
                detail = ex.read().decode(errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            if ex.code in (401, 403):
                raise NomadError(f"NOMAD refused the request ({ex.code}): check the API token and its permissions. "
                                 f"{detail}")
            if ex.code == 404:
                raise NomadError(f"not found on NOMAD (404): check the upload ID and the path. {detail}")
            raise NomadError(f"NOMAD request failed: {ex} {detail}")
        except urllib.error.URLError as ex:
            raise NomadError(f"NOMAD is not reachable ({ex.reason}); internet access to {self.url} is needed")
        if raw:
            return body
        return json.loads(body.decode() or "{}")

    @staticmethod
    def _qp(path):
        return urllib.parse.quote(str(path or "").strip("/"))

    # ------------------------------------------------------------------ reading
    def me(self):
        return self._req("GET", "/users/me")

    def uploads(self, page_size=100):
        """[(upload_id, upload_name, published)] of the user (needs a token)."""
        if not self.token:
            raise NomadError("an API token is needed to list your uploads")
        r = self._req("GET", "/uploads", {"page_size": page_size, "order_by": "upload_create_time", "order": "desc"})
        return [(d.get("upload_id"), d.get("upload_name") or "", bool(d.get("published"))) for d in r.get("data", [])]

    def listdir(self, upload_id, path=""):
        """[(name, is_file, size)] of a folder of the upload (all pages)."""
        out, page = [], 1
        while True:
            r = self._req("GET", f"/uploads/{self._qp(upload_id)}/rawdir/{self._qp(path)}",
                          {"page_size": 500, "page": page})
            dm = r.get("directory_metadata")
            if dm is None:
                if r.get("file_metadata") is not None:
                    raise NomadError(f"{path} is a file, not a folder")
                break
            items = dm.get("content", [])
            out += [(e.get("name"), bool(e.get("is_file")), e.get("size")) for e in items]
            pag = r.get("pagination") or {}
            if len(items) < 500 or not pag or page * 500 >= int(pag.get("total", 0) or 0):
                break
            page += 1
        return out

    def file_size(self, upload_id, path):
        r = self._req("GET", f"/uploads/{self._qp(upload_id)}/rawdir/{self._qp(path)}")
        fm = r.get("file_metadata") or {}
        if "size" not in fm:
            raise NomadError(f"{path} is not a file of upload {upload_id}")
        return int(fm["size"])

    def read_range(self, upload_id, path, offset, length):
        """Bytes [offset, offset + length) of a raw file of the upload."""
        return self._req("GET", f"/uploads/{self._qp(upload_id)}/raw/{self._qp(path)}",
                         {"offset": int(offset), "length": int(length), "decompress": "false",
                          "ignore_mime_type": "true"}, raw=True)

    def download(self, upload_id, path, dest_dir):
        """Download one raw file of the upload into dest_dir (keeps its relative path); returns the local path."""
        rel = str(path).strip("/")
        if ".." in rel.split("/"):
            raise NomadError("invalid path")
        body = self._req("GET", f"/uploads/{self._qp(upload_id)}/raw/{self._qp(rel)}", raw=True)
        local = os.path.join(dest_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
        with open(local, "wb") as f:
            f.write(body)
        return local

    # ------------------------------------------------------------------ writing
    def create_upload(self, upload_name=""):
        r = self._req("POST", "/uploads", {"upload_name": upload_name} if upload_name else None)
        return r.get("upload_id") or (r.get("data") or {}).get("upload_id")

    def _stream(self, method, path, params, fobj, size, progress=None, chunk=4 << 20):
        """Send a file object as request body (Content-Length, read in chunks with progress)."""
        class _Body:
            def __init__(self):
                self.sent = 0

            def read(self, n=-1):
                b = fobj.read(chunk if n is None or n < 0 else min(n, chunk))
                self.sent += len(b)
                if progress:
                    progress(self.sent, size)
                return b

            def __len__(self):
                return size
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        hdr = {"Accept": "application/json", "Content-Type": "application/octet-stream",
               "Content-Length": str(size)}
        if self.token:
            hdr["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.url + path + q, data=_Body(), headers=hdr, method=method)
        try:
            with self.opener(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as ex:
            detail = ""
            try:
                detail = ex.read().decode(errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise NomadError(f"NOMAD refused the files ({ex.code}): {detail}") from None
        except urllib.error.URLError as ex:
            raise NomadError(f"NOMAD is not reachable ({ex.reason})") from None

    def upload(self, files, upload_id="", folder="", upload_name="", progress=None, tmp_dir=None):
        """Put files into a folder of an upload (new upload without ``upload_id``).  ``files``: [(local path,
        path in the folder)] or local paths.  One zip on disk (stored, not compressed), streamed.  Returns
        dict(upload_id, folder, files, gui_url)."""
        if not self.token:
            raise NomadError("you are not signed in to NOMAD (or no API token)")
        items = [(f, os.path.basename(f)) if isinstance(f, str) else (f[0], f[1]) for f in files]
        items = [(p, a) for p, a in items if os.path.isfile(p)]
        if not items:
            raise NomadError("no files to upload")
        folder = str(folder or "").strip().strip("/")
        if ".." in folder.split("/"):
            raise NomadError("invalid folder")
        upload_id = str(upload_id or "").strip()
        fd, zpath = tempfile.mkstemp(suffix=".zip", dir=tmp_dir)
        os.close(fd)
        try:
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for p, arc in items:
                    arc = arc.replace(os.sep, "/").strip("/")
                    if ".." in arc.split("/"):
                        raise NomadError(f"invalid name {arc}")
                    z.write(p, (folder + "/" + arc) if (folder and not upload_id) else arc)
            size = os.path.getsize(zpath)
            zipname = "emingest_%s.zip" % dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            with open(zpath, "rb") as fo:
                if upload_id:
                    r = self._stream("PUT", f"/uploads/{self._qp(upload_id)}/raw/{self._qp(folder)}",
                                     {"file_name": zipname}, fo, size, progress)
                else:
                    params = {"file_name": zipname}
                    if upload_name:
                        params["upload_name"] = str(upload_name)
                    r = self._stream("POST", "/uploads", params, fo, size, progress)
        finally:
            os.remove(zpath)
        uid = r.get("upload_id") or (r.get("data") or {}).get("upload_id") or upload_id
        return {"upload_id": uid, "folder": folder, "files": len(items), "gui_url": self.gui_url(uid)}

    def upload_state(self, upload_id):
        d = self._req("GET", f"/uploads/{self._qp(upload_id)}").get("data") or {}
        return {"process_running": bool(d.get("process_running")), "process_status": d.get("process_status"),
                "errors": d.get("errors") or [], "upload_name": d.get("upload_name")}

    def wait_processed(self, upload_id, timeout=600, poll=3, callback=None):
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = self.upload_state(upload_id)
            if callback:
                callback(s)
            if not s["process_running"]:
                return s
            time.sleep(poll)
        return s

    # ------------------------------------------------------------------ actions
    def start_action(self, action_id, data):
        r = self._req("POST", f"/actions/{action_id}/start", data=json.dumps({"data": data}).encode(),
                      ctype="application/json")
        return r.get("action_instance_id") or r.get("id") or (r.get("data") or {}).get("action_instance_id")

    def action_status(self, instance_id):
        return self._req("GET", f"/actions/{instance_id}/status").get("status") or "?"

    def action_result(self, instance_id):
        return self._req("GET", f"/actions/{instance_id}/result")

    def gui_url(self, upload_id):
        if not upload_id:
            return None
        return self.url.replace("/api/v1", "/gui/user/uploads/upload/id/" + upload_id)
