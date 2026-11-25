# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Command line: batch ingestion of TEM sessions (tem-provenance eLabFTW templates) into NOMAD.

Examples::

    # sessions 12 and 15-18 from an eLabFTW server; files from the session folders; conversion on a NOMAD worker
    emingest-ingest --elabftw-url https://eln.lab.org --nomad-url https://oasis.lab.org/nomad-oasis/api/v1 \\
                    --new-project "TEM sessions 2026" 12 15-18

    # all sessions of an elabFTW export (bin/console export:eln) lying on another computer, acquisition folders
    # on the lab storage (mapped), conversion here
    emingest-ingest --eln elab@eln.lab.org:/var/elabftw/exports/team1.eln --all \\
                    --map /mnt/storage=tem@storage.lab.org:/data --conversion local --upload-id AbC123

    # settings of tem-provenance (config.yml: elabftw.base_url, nomad.base_url, nomad.playground_upload_id)
    emingest-ingest --config ../tem-provenance/config.yml 12

    # NXem files written by tem-provenance's export_tem_image.py: one data set each, metadata from the file
    emingest-ingest --nxs session-12.nxs session-15.nxs --upload-id AbC123

Credentials come from the environment (``ELABFTW_API_KEY``, ``NOMAD_API_KEY``, as in tem-provenance), from the
config file or from a prompt -- never from the command line.  Without ``--nomad-url`` / ``--upload-id`` /
``--new-project`` the data sets are only written into ``--out`` (dry runs: ``--check``).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from . import ingest, tem_session as ts


def _config(path):
    import yaml
    with open(path, encoding="utf-8") as f:
        c = yaml.safe_load(f) or {}
    return c if isinstance(c, dict) else {}


def build_parser():
    ap = argparse.ArgumentParser(prog="emingest-ingest", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    src = ap.add_argument_group("source")
    src.add_argument("sessions", nargs="*", help="session IDs or ranges (12 15-18)")
    src.add_argument("--elabftw-url", help="eLabFTW server (or its /api/v2 address)")
    src.add_argument("--eln", help=".eln export: path, folder (newest .eln) or user@host:/path")
    src.add_argument("--all", action="store_true", help="all TEM sessions of the .eln file")
    src.add_argument("--list", action="store_true", help="list the TEM sessions and stop")
    src.add_argument("--nxs", nargs="+", help="NXem files (e.g. of tem-provenance): one data set each")
    src.add_argument("--config", help="config.yml of tem-provenance")
    src.add_argument("--timezone", default=ts.DEFAULT_TZ, help="facility time zone of the session times")
    src.add_argument("--no-verify", action="store_true", help="do not verify the TLS certificate of eLabFTW")
    acc = ap.add_argument_group("acquisition folders and SSH")
    acc.add_argument("--map", action="append", default=[], metavar="OLD=NEW",
                     help="path mapping, e.g. /mnt/storage=user@host:/data (repeatable)")
    acc.add_argument("--ssh-key", help="private key file for SSH")
    acc.add_argument("--ssh-password", action="store_true", help="ask for an SSH password")
    acc.add_argument("--trust-host-key", help="accept this host key fingerprint (SHA256:...) for unknown hosts")
    dst = ap.add_argument_group("NOMAD")
    dst.add_argument("--nomad-url", help="NOMAD API, e.g. http://localhost:8000/nomad-oasis/api/v1")
    dst.add_argument("--upload-id", help="existing unpublished upload (project)")
    dst.add_argument("--new-project", metavar="NAME", help="create a new upload for all sessions")
    dst.add_argument("--folder", default="tem/session_{id}_{title}", help="folder of each session in the upload")
    dst.add_argument("--conversion", choices=("action", "local", "none"), default="action")
    dst.add_argument("--core-schema", action="store_true", help="NOMAD's ELNMeasurement instead of the plugin's")
    dst.add_argument("--max-gb", type=float, default=20.0)
    dst.add_argument("--out", default="emingest_out", help="output folder without NOMAD")
    dst.add_argument("--check", action="store_true", help="only check: metadata and files, no output")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    cfg = _config(a.config) if a.config else {}
    el, nm = cfg.get("elabftw") or {}, cfg.get("nomad") or {}
    elab_url = a.elabftw_url or el.get("base_url")
    nomad_url = a.nomad_url or nm.get("base_url")
    upload_id = a.upload_id or nm.get("playground_upload_id")
    loc_kw = {}
    if a.ssh_key:
        with open(os.path.expanduser(a.ssh_key), encoding="utf-8") as f:
            loc_kw["key_text"] = f.read()
    if a.ssh_password:
        loc_kw["password"] = getpass.getpass("SSH password: ")
    if a.trust_host_key:
        loc_kw["trust_fingerprint"] = a.trust_host_key
    client = None
    if nomad_url and (upload_id or a.new_project) and not a.check:
        from .nomad import NomadClient
        token = os.environ.get("NOMAD_API_KEY") or nm.get("api_key") or getpass.getpass("NOMAD API token: ")
        client = NomadClient(nomad_url, token)
        client.me()
        if a.new_project and not upload_id:
            from .nomad_project import create_project
            upload_id = create_project(client.url, client.token, a.new_project)
            print(f"new NOMAD project {upload_id}: {client.gui_url(upload_id)}")
    common = dict(client=client, upload_id=upload_id, schema="core" if a.core_schema else "plugin",
                  conversion=a.conversion, max_gb=a.max_gb, work_dir=None)
    results = []
    if a.nxs:
        results += ingest_nxs(a.nxs, out=a.out, check=a.check, **common)
    if a.sessions or a.all or a.list:
        loc = None
        if a.eln:
            from .eln_archive import ElnArchiveClient
            from .remote import open_eln
            arch, loc = open_eln(a.eln, **loc_kw)
            source = ElnArchiveClient(arch)
        elif elab_url:
            from .elabftw import ElabClient, parse_ref
            key = os.environ.get("ELABFTW_API_KEY") or el.get("api_key") or getpass.getpass("eLabFTW API key: ")
            base = parse_ref(elab_url, 1, "experiments")[0]
            source = ElabClient(base, key, verify=not a.no_verify)
        else:
            print("give --elabftw-url, --eln or --config", file=sys.stderr)
            return 2
        try:
            if a.list or a.all:
                if not a.eln:
                    print("--list / --all need an .eln file (the API has no search for templates here)",
                          file=sys.stderr)
                    return 2
                sess = ts.list_sessions(source)
                if a.list:
                    for s in sess:
                        print(f"{s['id']:>6}  {str(s['date'])[:16]:16}  {s['title']}  [{s['path']}]")
                    return 0
                ids = [s["id"] for s in sess]
            else:
                ids = []
                for tok in a.sessions:
                    ids += ts.parse_ids(tok)
            results += ingest.ingest_sessions(
                source, ids, zone=a.timezone, rules=ts.parse_rules(";".join(a.map)), location_kw=loc_kw,
                folder_template=a.folder, dry_run=a.check, callback=lambda st, msg: print(f"  {msg}", flush=True),
                **common)
        finally:
            if loc:
                loc.close()
    for r in results:
        print(json.dumps({k: r.get(k) for k in ("session", "nxs", "status", "folder", "upload_id", "message",
                                                  "instance") if r.get(k) not in (None, "")}))
    return 0 if results and all(r["status"] in ("ok", "checked", "prepared") for r in results) else 1


def ingest_nxs(paths, client=None, upload_id=None, out="emingest_out", check=False, folder="tem/{name}", **kw):
    """NXem files (e.g. of tem-provenance) as data sets: metadata from the file; the file itself is the data."""
    from . import metadata as md
    res = []
    for p in paths:
        r = {"nxs": p, "status": "failed", "message": ""}
        try:
            meta = md.from_nxem(p)
            name = ingest.safe_name(meta["entry"].get("experiment_alias") or os.path.splitext(os.path.basename(p))[0])
            infos = ingest.inspect_files([p])
            infos[0]["kind"] = "other"
            fold = folder.format(name=name)
            if check:
                r.update(status="checked", message=f"missing: {', '.join(md.check(meta)[0]) or 'nothing'}")
            else:
                import tempfile
                outdir = tempfile.mkdtemp(prefix="nxs_") if client else os.path.join(out, name)
                ds = ingest.build_dataset(infos, meta, name, fold, outdir, schema=kw.get("schema", "plugin"))
                if client:
                    up = ingest.upload_dataset(client, ds, upload_id)
                    r.update(status="ok", folder=fold, upload_id=up["upload"]["upload_id"],
                             message=f"{os.path.basename(p)} with ELN entry in {fold}")
                else:
                    r.update(status="prepared", folder=fold, message=f"written into {outdir}")
        except Exception as e:  # noqa: BLE001
            r["message"] = str(e)
        res.append(r)
    return res


if __name__ == "__main__":
    sys.exit(main())
