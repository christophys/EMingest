# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""eLabFTW without a server (.eln export), TEM sessions of the tem-provenance templates, acquisition folders (local
and over SSH), batch ingestion (library, command line, dashboard)."""
import json
import os
import shutil
import socket
import subprocess
import time
import zipfile

import h5py
import pytest
import yaml
from conftest import make_files
from elab_fixtures import records, write_eln
from mocks import ELAB_KEY, TOKEN

from emingest import elabftw, ingest
from emingest import metadata as md
from emingest import tem_session as ts
from emingest.eln_archive import ElnArchive, ElnArchiveClient

STORAGE = "/mnt/storage/tem/2026-11-20_BTO"


@pytest.fixture(scope="module")
def acq(tmp_path_factory):
    """Acquisition folder of session 42 (with a sub folder and one spectrum acquired the day after)."""
    hs = pytest.importorskip("hyperspy.api")
    import numpy as np
    root = tmp_path_factory.mktemp("storage")
    d = root / "tem" / "2026-11-20_BTO"
    src = make_files(str(tmp_path_factory.mktemp("src")))
    (d / "area1").mkdir(parents=True)
    for n in ("eels.msa", "scan4d.hspy", "focal.hspy"):
        shutil.copy(os.path.join(src, n), d / n)
    shutil.copy(os.path.join(src, "image.tif"), d / "area1" / "image.tif")
    s = hs.signals.Signal1D(np.ones(64))
    s.axes_manager[0].units = "eV"
    s.metadata.General.date, s.metadata.General.time = "2026-11-21", "10:15:00"
    s.save(str(d / "late.msa"), overwrite=True)
    return root


@pytest.fixture
def eln(tmp_path):
    p = write_eln(str(tmp_path / "team.eln"), records(STORAGE),
                  {("experiments", 42): [("session_notes.txt", b"notes"), ("overview.png", b"\x89PNG fake")]})
    return p


def test_eln_archive(eln):
    a = ElnArchive(eln)
    assert a.version == "109" and a.software == "eLabFTW 5.4.0" and a.base == "https://eln.example.org/api/v2"
    lst = a.list(types=None)
    assert {(x["type"], x["id"]) for x in lst} == {("experiments", "42"), ("items", "4"), ("items", "5"),
                                                   ("items", "7")}
    c = ElnArchiveClient(a)
    e = c.entity("experiments", 42)
    assert e["elabid"] == "20261120-1f2e3d4c5b6a" and e["fullname"] == "Ada Lovelace" and e["tags"] == "STO|BTO"
    assert c.user(e["userid"])["orcid"] == "https://orcid.org/0000-0002-1825-0097"
    assert [u["real_name"] for u in c.uploads("experiments", 42)] == ["session_notes.txt", "overview.png"]
    assert c.entity("items", "20261117-a1b2c3d4e5f6")["title"] == "BTO on STO lamella 12"     # by elabid
    assert [x["entityid"] for x in e["items_links"]] == ["5", "7"]
    with pytest.raises(elabftw.ElabError, match="not in"):
        c.entity("items", 99)
    assert c.entry_url("items", 5) == "https://eln.example.org/database.php?mode=view&id=5"
    # generic import from the archive (the session is recognised as such)
    r = elabftw.import_entry(c, "experiments", 42)
    assert r["session"]["path"] == STORAGE and "TEM session #42" in r["summary"]
    r = elabftw.import_entry(c, "items", 7)
    assert r["meta"]["entry"]["experiment_alias"] == "Spectra 300 (TEM 2)"


def test_eln_attachments_and_damage(eln, tmp_path):
    c = ElnArchiveClient(ElnArchive(eln))
    ups = c.uploads("experiments", 42)
    p = c.download("experiments", 42, ups[0], str(tmp_path / "dl"))
    assert open(p, "rb").read() == b"notes"
    bad = tmp_path / "bad.eln"                       # same archive, one attachment changed
    with zipfile.ZipFile(eln) as zi, zipfile.ZipFile(bad, "w") as zo:
        for n in zi.namelist():
            data = zi.read(n)
            zo.writestr(n, b"tampered" if data == b"notes" else data)
    cb = ElnArchiveClient(ElnArchive(str(bad)))
    with pytest.raises(elabftw.ElabError, match="SHA-256"):
        cb.download("experiments", 42, cb.uploads("experiments", 42)[0], str(tmp_path / "dl2"))
    with pytest.raises(elabftw.ElabError, match="not an .eln"):
        ElnArchive(str(tmp_path / "dl" / "session_notes.txt"))


def test_session_mapping_server_and_archive(elab_server, eln):
    recs = records(STORAGE)
    for k, v in recs.items():
        elab_server.entities[k] = v
    elab_server.users[7] = {"fullname": "Ada Lovelace", "orcid": "0000-0002-1825-0097"}
    api = elabftw.ElabClient(f"{elab_server.base}/api/v2", ELAB_KEY)
    arch = ElnArchiveClient(ElnArchive(eln))
    a = ts.session_metadata(api, 42)
    b = ts.session_metadata(arch, 42)
    for s in (a, b):
        m = s["meta"]
        assert m["entry"]["start_time"] == "2026-11-20T08:30:00Z" and m["entry"]["end_time"] == "2026-11-20T16:00:00Z"
        assert m["entry"]["identifier_experiment"] == "20261120-1f2e3d4c5b6a"
        assert "Map the polarisation" in m["entry"]["experiment_description"] and "Drift corrected" in \
            m["entry"]["experiment_description"] and "Provenance" in m["entry"]["experiment_description"]
        smp = m["sample"]
        assert smp["identifier_sample"] == "20261117-a1b2c3d4e5f6" and smp["identifier_owner"] == "OWNER-STO-12"
        assert smp["identifier_parent"] == "20261101-0badc0ffee00" and smp["atom_types"] == "Ba, O, Sr, Ti"
        assert smp["components"] == [{"name": "substrate", "chemical_formula": "SrTiO3", "space_group": "Pm-3m"},
                                     {"name": "film", "chemical_formula": "BaTiO3"}]
        assert smp["thickness"] == 45.0 and smp["physical_form"] == "thin_film"
        assert smp["preparation_date"] == "2026-11-18T11:00:00Z" and "FIB Lamella" in smp["history"]
        assert m["instrument"] == {"name": "Spectra 300 (TEM 2)", "type": "tem", "vendor": "Thermo Fisher Scientific",
                                   "model": "Spectra 300", "emitter_type": "cold field emitter",
                                   "identifier": "INV-TEM-0007", "operation_mode": "STEM", "voltage_kV": 300.0}
        assert m["users"] == [{"name": "Ada Lovelace", "orcid": "https://orcid.org/0000-0002-1825-0097"}]
        assert s["path"] == STORAGE and "noon local time" in " ".join(s["notes"])
        assert md.check(md.merge(md.empty(), m, overwrite=True))[0] == []
    assert b["meta"]["source"]["eln_file"] == "team.eln" and "eln_file" not in a["meta"]["source"]
    # session without the template fields
    elab_server.entities[("experiments", 43)] = {"id": 43, "title": "x", "metadata": ""}
    with pytest.raises(elabftw.ElabError, match="missing Sample"):
        ts.session_metadata(api, 43)


def test_times_paths_ids():
    assert ts.utc_text(ts.facility_time("2026-07-01T12:00", "Europe/Rome")) == "2026-07-01T10:00:00Z"
    with pytest.raises(ValueError, match="ambiguous"):
        ts.facility_time("2026-10-25T02:30", "Europe/Rome")            # end of summer time
    with pytest.raises(ValueError, match="ambiguous"):
        ts.facility_time("2026-03-29T02:30", "Europe/Rome")            # does not exist
    w = (ts.facility_time("2026-11-20T09:30", "Europe/Rome"), None)
    assert ts.in_window("2026-11-20T18:00", w) is True and ts.in_window("2026-11-21T00:10", w) is False
    assert ts.in_window(None, w) is None
    rules = ts.parse_rules("/mnt/storage=/data/lab ; /mnt/tem = tem@nas:/tem")
    assert ts.map_path("/mnt/storage/a/b", rules) == "/data/lab/a/b"
    assert ts.map_path("/mnt/tem/x", rules) == "tem@nas:/tem/x" and ts.map_path("/other", rules) == "/other"
    assert ts.map_path("/mnt/storagex/a", rules) == "/mnt/storagex/a"
    assert ts.parse_ids("12, 15-17 21") == ["12", "15", "16", "17", "21"]
    with pytest.raises(ValueError):
        ts.parse_ids("12, abc")


def test_ingest_session_local(eln, acq, mock, tmp_path):
    c = ElnArchiveClient(ElnArchive(eln))
    rules = [("/mnt/storage", str(acq))]
    r = ingest.ingest_session(c, 42, rules=rules, dry_run=True)
    assert r["status"] == "checked" and r["files"] == 5 and r["outside_window"] == 1 and r["missing"] == []
    notes = {i["name"]: i["note"] for i in r["infos"]}
    assert "outside the session time" in notes["late.msa"] and "area1/image.tif" in notes
    r = ingest.ingest_session(c, 42, client=mock_client(mock), upload_id="U1", rules=rules, conversion="local")
    assert r["status"] == "ok", r["message"]
    f = r["folder"]
    assert f == "tem/session_42_BTO_STO_interface_session_1"
    up = mock.uploads["U1"]
    for n in ("eels.msa", "area1/image.tif", "late.msa", "nxem/scan4d.nxs", "nxem/image.nxs",
              "BTO_STO_interface_session_1.archive.yaml", "BTO_STO_interface_session_1.workflow.archive.yaml"):
        assert f"{f}/{n}" in up, n
    eln_arch = yaml.safe_load(up[f"{f}/BTO_STO_interface_session_1.archive.yaml"])["data"]
    assert eln_arch["sample_info"]["identifier_owner"] == "OWNER-STO-12"
    assert eln_arch["sample_info"]["components"] == "substrate: SrTiO3 (Pm-3m); film: BaTiO3"
    assert eln_arch["acquisition_folder"] == STORAGE and eln_arch["eln_file"] == "team.eln"
    assert eln_arch["instrument_info"]["identifier"] == "INV-TEM-0007"
    late = [d for d in eln_arch["data_files"] if d["name"] == "late.msa"][0]
    assert "unverified" in late["note"]
    conv = yaml.safe_load(up[f"{f}/BTO_STO_interface_session_1.nxem.archive.yaml"])["data"]["conversions"]
    assert {c["status"] for c in conv} == {"ok"} and all(c["validation_warnings"] == 0 for c in conv), conv
    nx = tmp_path / "s.nxs"
    nx.write_bytes(up[f"{f}/nxem/scan4d.nxs"])
    with h5py.File(nx) as h:
        smp = h["entry1/sample"]
        assert smp["identifier_owner"][()] == b"OWNER-STO-12"
        assert smp["identifier_sample"].attrs["type"] == "eLabFTW elabid"
        assert smp["component1/chemical_formula"][()] == b"SrTiO3" and smp["component2/name"][()] == b"film"
        assert h["entry1/identifier_experiment"][()] == b"20261120-1f2e3d4c5b6a"
    back = md.from_nxem(str(nx))                       # e.g. NXem written by tem-provenance
    assert back["sample"]["identifier_owner"] == "OWNER-STO-12" and back["sample"]["thickness"] == 45.0


def mock_client(mock):
    from emingest.nomad import NomadClient
    return NomadClient(mock.url, TOKEN)


def test_batch_and_failures(eln, acq, mock):
    c = ElnArchiveClient(ElnArchive(eln))
    res = ingest.ingest_sessions(c, ["42", "99"], client=mock_client(mock), upload_id="U1",
                                 rules=[("/mnt/storage", str(acq))], conversion="action")
    assert [r["status"] for r in res] == ["ok", "failed"] and "not in team.eln" in res[1]["message"]
    assert "could not be started" in res[0]["message"]        # (the mock has no actions)
    res = ingest.ingest_sessions(c, ["42"], client=mock_client(mock), upload_id="U1", conversion="none")
    assert res[0]["status"] == "failed" and "acquisition folder not found" in res[0]["message"]


# ------------------------------------------------------------------------------------------------ SSH
@pytest.fixture(scope="module")
def sshd(tmp_path_factory):
    exe = shutil.which("sshd") or "/usr/sbin/sshd"
    keygen = shutil.which("ssh-keygen")
    if not os.path.isfile(exe) or not keygen or os.geteuid() != 0:
        pytest.skip("no sshd to test with")
    pytest.importorskip("paramiko")
    d = tmp_path_factory.mktemp("sshd")
    for k in ("host_key", "user_key"):
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(d / k)], check=True)
    shutil.copy(d / "user_key.pub", d / "authorized_keys")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (d / "sshd_config").write_text(f"Port {port}\nListenAddress 127.0.0.1\nHostKey {d}/host_key\n"
                                   f"AuthorizedKeysFile {d}/authorized_keys\nPasswordAuthentication no\n"
                                   f"StrictModes no\nPidFile {d}/pid\nSubsystem sftp internal-sftp\nUsePAM no\n")
    os.makedirs("/run/sshd", exist_ok=True)
    p = subprocess.Popen([exe, "-D", "-f", str(d / "sshd_config"), "-E", str(d / "log")])
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield {"port": port, "key": (d / "user_key").read_text()}
    p.terminate()


def test_remote_eln_and_folder(sshd, eln, acq, mock, monkeypatch):
    from emingest.remote import Location, UnknownHostKey, open_eln
    monkeypatch.setenv("HOME", os.path.dirname(eln))        # no known_hosts of the test user
    spec = f"sftp://root@127.0.0.1:{sshd['port']}{os.path.dirname(eln)}"
    with pytest.raises(UnknownHostKey) as e:
        Location(spec, key_text=sshd["key"])
    fp = e.value.fingerprint
    kw = {"key_text": sshd["key"], "trust_fingerprint": fp}
    arch, loc = open_eln(spec, **kw)                       # folder: the newest .eln
    try:
        assert arch.name == "team.eln" and isinstance(arch.zip.fp, object)
        c = ElnArchiveClient(arch)
        rules = [("/mnt/storage", f"root@127.0.0.1:{acq}")]
        r = ingest.ingest_session(c, 42, rules=rules, location_kw=dict(kw, port=sshd["port"]),
                                  client=mock_client(mock), upload_id="U1", conversion="none")
    finally:
        loc.close()
    assert r["status"] == "ok", r["message"]
    assert f"{r['folder']}/area1/image.tif" in mock.uploads["U1"]
    with pytest.raises(Exception, match="refused|key"):
        Location(spec, key_text=sshd["key"].replace("A", "B", 3), trust_fingerprint=fp)


# ------------------------------------------------------------------------------------------------ CLI
def test_cli(eln, acq, mock, tmp_path, monkeypatch, capsys):
    from emingest import cli
    rc = cli.main(["--eln", eln, "--list"])
    assert rc == 0 and "BTO/STO interface, session 1" in capsys.readouterr().out
    rc = cli.main(["--eln", eln, "--all", "--map", f"/mnt/storage={acq}", "--check"])
    out = capsys.readouterr().out
    assert rc == 0 and '"status": "checked"' in out
    monkeypatch.setenv("NOMAD_API_KEY", TOKEN)
    rc = cli.main(["--eln", eln, "42", "--map", f"/mnt/storage={acq}", "--nomad-url", mock.url,
                   "--new-project", "TEM sessions", "--conversion", "none", "--folder", "sessions/{id}"])
    out = capsys.readouterr().out
    assert rc == 0, out
    new = [u for u in mock.uploads if u != "U1"][0]
    assert mock.names[new] == "TEM sessions" and "sessions/42/late.msa" in mock.uploads[new]
    # tem-provenance config.yml + its NXem output
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump({"elabftw": {"base_url": None}, "nomad": {"base_url": mock.url,
                                                                            "playground_upload_id": "U1"},
                                   "data": {"folder": None}}))
    nxs = tmp_path / "session-42.nxs"
    shutil.copy(_nxem(acq, eln, tmp_path), nxs)
    rc = cli.main(["--config", str(cfg), "--nxs", str(nxs)])
    out = capsys.readouterr().out
    assert rc == 0, out
    arch = [k for k in mock.uploads["U1"] if k.endswith(".archive.yaml") and "BTO_STO" in k and k.startswith("tem/")]
    assert any(k.endswith("session-42.nxs") for k in mock.uploads["U1"]) and arch
    d = yaml.safe_load(mock.uploads["U1"][arch[0]])["data"]
    assert d["sample_info"]["identifier_owner"] == "OWNER-STO-12"


def _nxem(acq, eln, tmp_path):
    from emingest import convert
    m = ts.session_metadata(ElnArchiveClient(ElnArchive(eln)), 42)["meta"]
    md.write_eln_data(md.merge(md.empty(), m, overwrite=True), str(tmp_path / "x.eln_data.yaml"))
    r = convert.convert_group([os.path.join(acq, "tem", "2026-11-20_BTO", "eels.msa")],
                              str(tmp_path / "x.eln_data.yaml"), str(tmp_path / "x.nxs"))
    assert r["status"] == "ok"
    return str(tmp_path / "x.nxs")


# ------------------------------------------------------------------------------------------------ dashboard
def test_app_eln_session_and_batch(eln, acq, mock, tmp_path, monkeypatch):
    from emingest.app import App
    from emingest.nomad_project import NEW
    app = App(work_dir=str(tmp_path / "w"), server_mode=True, nomad_url=mock.url, token=TOKEN, in_nomad=True)
    assert app.connect()
    dst = os.path.join(app.files_dir, "team.eln")            # as if transferred in tab 1
    shutil.copy(eln, dst)
    app._add_file(dst, "your computer", inspect_now=True)
    app.w_el_src.value = "eln"
    assert ("team.eln", "team.eln") in app.w_eln_tab1.options
    app.w_eln_tab1.value = "team.eln"
    app._eln_open()
    assert "1 TEM session" in app.w_eln_info.value and app.w_eln_entries.value == ("experiments", "42")
    assert app._elab_fetch() and "TEM session #42" in app.w_el_status.value
    assert "OWNER-STO-12" in app.w_el_preview.value and app.box_acq.layout.display == "flex"
    app._elab_apply()
    m = app.get_meta()
    assert m["sample"]["identifier_owner"] == "OWNER-STO-12" and len(m["sample"]["components"]) == 2
    assert "substrate: SrTiO3" in app.w_components.value and "all required" in app.w_check.value
    # server mode: folders of the server only if allowed
    app.w_map.value = f"/mnt/storage={acq}"
    app._session_files()
    assert "may not be read" in app.w_acq.value
    monkeypatch.setenv("EMINGEST_LOCAL_ROOTS", str(acq))
    app._session_files()
    assert "5 file(s)" in app.w_acq.value and "1 acquired outside" in app.w_acq.value, app.w_acq.value
    assert "unverified" in app.files["late.msa"]["note"] and "area1_image.tif" in app.files
    # the .eln file itself is not uploaded as data
    app.w_conv.value = "none"
    app.project.w_sel.value = "U1"
    app.push()
    assert "✔" in app.w_push_status.value, app.w_push_status.value
    assert not any(k.endswith("team.eln") for k in mock.uploads["U1"])
    # batch
    app.w_b_all.value = True
    app._batch(dry_run=True)
    assert "1 of 1 session(s) checked" in app.w_b_status.value and "#42" in app.w_b_table.value
    app.project.w_sel.value = NEW
    app.project.w_name.value = "sessions batch"
    app._batch()
    assert "1 of 1 session(s) ingested" in app.w_b_status.value, app.w_b_status.value + app.w_b_table.value
    new = [u for u, n in mock.names.items() if n == "sessions batch"][0]
    assert any(k.startswith("tem/session_42_") and k.endswith("late.msa") for k in mock.uploads[new])
    assert app.project.w_sel.value == new
    json.dumps(app.get_meta())
