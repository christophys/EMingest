# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Detection, metadata, rich text, NXem conversion, ELN archives, elabFTW import and NOMAD client."""
import json
import os

import h5py
import numpy as np
import pytest
import yaml
from conftest import full_meta
from mocks import ELAB_KEY, TOKEN

from emingest import convert, elabftw, eln, formats, richtext
from emingest import metadata as md
from emingest.nomad import NomadClient

KINDS = {"eels.msa": "spectrum", "si.hspy": "spectrum image", "scan4d.hspy": "4D-STEM", "focal.hspy": "focal series",
         "tilt.hspy": "tilt series", "image.tif": "image", "notes.pdf": "other"}


def test_detection(tem_files):
    for n, k in KINDS.items():
        i = formats.inspect_file(os.path.join(tem_files, n))
        assert i["kind"] == k, (n, i)
    i = formats.inspect_file(os.path.join(tem_files, "eels.msa"))
    assert "pynxtools-em" in i["readers"] and "rosettasciio" in i["readers"] and i["format"] == "MSA"
    assert formats.inspect_file(os.path.join(tem_files, "scan4d.hspy"))["shape"] == [3, 4, 16, 16]
    assert "uploaded as it is" in formats.inspect_file(os.path.join(tem_files, "notes.pdf"))["note"]
    assert ".dm4" in formats.supported_extensions() and ".mib" in formats.supported_extensions()


def test_grouping_of_sidecars():
    g = formats.group_files(["a/t.mrc", "a/t.mrc.mdoc", "a/x.tif", "a/x.hdr", "a/y.dm4", "a/s.eln_data.yaml",
                             "a/orphan.rawtlt"])
    assert ["a/t.mrc", "a/t.mrc.mdoc"] in g and ["a/x.tif", "a/x.hdr"] in g and ["a/y.dm4"] in g
    assert ["a/orphan.rawtlt"] in g and not any("eln_data" in p for gg in g for p in gg)
    assert convert.output_names([["a/x.dm4"], ["b/x.dm4"]]) == ["x.nxs", "x_2.nxs"]


def test_metadata_check_and_eln_data(tmp_path):
    m = md.empty()
    req, rec = md.check(m)
    assert "Start" in req and "chemical formula or elements" in req and "Preparation date" in req
    assert "Vendor" in req and "user name" in rec
    m = full_meta()
    assert md.check(m) == ([], [])
    d = md.eln_data(m)
    assert d["entry"]["start_time"].startswith("2026-11-20T10:00:00") and d["sample"]["atom_types"] == "Sr, Ti, O"
    assert d["sample"]["thickness"] == {"value": 40.0, "unit": "nm"} and d["sample"]["description"].startswith("FIB")
    assert d["user"][0]["orcid"] == "https://orcid.org/0000-0002-1825-0097"
    assert d["instrument"]["voltage"] == {"value": 300.0, "unit": "kV"} and d["instrument"]["detectors"] == ["HAADF",
                                                                                                           "K3"]
    back = md.from_eln_data(d)
    assert back["sample"]["chemical_formula"] == "SrTiO3" and back["instrument"]["voltage_kV"] == 300.0
    assert md.to_iso("24.11.2026").startswith("2026-11-24T00:00:00") and md.to_iso("garbage") == ""
    mm = md.merge(md.empty(), {"sample": {"name": "A"}, "users": [{"name": "X"}], "tags": ["t"]})
    assert mm["sample"]["name"] == "A" and mm["users"] == [{"name": "X"}] and mm["tags"] == ["t"]
    assert md.merge(mm, {"sample": {"name": "B"}})["sample"]["name"] == "A"
    assert md.merge(mm, {"sample": {"name": "B"}}, overwrite=True)["sample"]["name"] == "B"


def test_richtext():
    dirty = ('<p onclick="x()">Hi <b>bold</b> <a href="javascript:alert(1)">x</a> <a href="https://a.org">a</a>'
             '<script>alert(1)</script><img src="data:image/png;base64,AAAA" onerror="y()"><iframe src="e"></iframe>'
             '<span style="color:red;position:fixed">c</span>')
    s = richtext.sanitize(dirty)
    assert "script" not in s and "onclick" not in s and "javascript" not in s and "iframe" not in s
    assert '<a href="https://a.org" target="_blank" rel="noopener">' in s and 'src="data:image/png' in s
    assert 'style="color:red"' in s and "position" not in s
    t = richtext.to_text("<h3>Sample</h3><p>grown by <b>PLD</b></p><ul><li>one</li><li>two</li></ul>"
                         "<ol><li>a</li></ol><p><a href='https://x.org'>link</a></p>")
    assert t == "### Sample\n\ngrown by PLD\n\n- one\n- two\n\n1. a\n\nlink <https://x.org>"
    assert richtext.from_text("# T\n- a\n- b\ntext") == "<h1>T</h1><ul><li>a</li><li>b</li></ul><p>text</p>"
    w = richtext.RichTextEditor(value="<p>x</p>")
    assert w.value == "<p>x</p>" and "contenteditable" in w.template


def _tree(path):
    out = {}
    with h5py.File(path, "r") as f:
        f.visititems(lambda n, o: out.__setitem__(n, o))
        return {n: (o[()] if isinstance(o, h5py.Dataset) and o.size < 50 else getattr(o, "shape", None))
                for n, o in out.items()}, {n: dict(o.attrs) for n, o in out.items()}


def test_conversion_to_nxem(tem_files, tmp_path):
    m = full_meta()
    ed = md.write_eln_data(m, str(tmp_path / "s.eln_data.yaml"))
    files = [os.path.join(tem_files, n) for n in sorted(KINDS)]
    res = convert.convert_all(files, ed, str(tmp_path / "nx"))
    st = {os.path.basename(r["inputs"][0]): r for r in res}
    assert "notes.pdf" not in st and set(st) == set(KINDS) - {"notes.pdf"}
    assert all(r["status"] == "ok" for r in res), [(r["inputs"], r["message"], r["log"][-800:]) for r in res]
    assert all(r["warnings"] == 0 for r in res), [(r["inputs"], [ln for ln in r["log"].splitlines()
                                                                if "WARN" in ln]) for r in res]
    v, a = _tree(str(tmp_path / "nx" / "scan4d.nxs"))
    assert v["entry1/definition"] == b"NXem" and v["entry1/sample/name"] == b"STO lamella"
    assert v["entry1/measurement/event1/image1/image_4d/real"] == (3, 4, 16, 16)
    assert list(a["entry1/measurement/event1/image1/image_4d"]["axes"]) == ["axis_m", "axis_k", "axis_j", "axis_i"]
    assert a["entry1/measurement/event1/image1/image_4d/axis_i"]["units"] == "mrad"
    assert v["entry1/measurement/instrument/fabrication/model"] == b"Titan Themis"
    assert v["entry1/user1/identifier_orcid"] == b"https://orcid.org/0000-0002-1825-0097"
    assert v["entry1/sample/thickness"] == pytest.approx(4e-8)
    v, a = _tree(str(tmp_path / "nx" / "focal.nxs"))
    assert v["entry1/measurement/event1/image1/image_3d/real"] == (5, 24, 24)
    assert a["entry1/measurement/event1/image1/image_3d/axis_k"]["long_name"] == "defocus (nm)"
    v, a = _tree(str(tmp_path / "nx" / "si.nxs"))
    assert v["entry1/measurement/event1/spectrum1/spectrum_2d/intensity"] == (4, 5, 32)
    v, a = _tree(str(tmp_path / "nx" / "eels.nxs"))      # pynxtools-em (EMSA parser)
    assert any(k.endswith("spectrum_0d/intensity") for k in v)
    # in-process conversion and the size limit
    r = convert.convert_group([os.path.join(tem_files, "image.tif")], ed, str(tmp_path / "i.nxs"), isolate=False)
    assert r["status"] == "ok"
    r = convert.convert_group([os.path.join(tem_files, "scan4d.hspy")], ed, str(tmp_path / "big.nxs"), max_gb=1e-6)
    assert r["status"] == "skipped" and not os.path.exists(tmp_path / "big.nxs")
    r = convert.convert_group([os.path.join(tem_files, "notes.pdf")], ed, str(tmp_path / "bad.nxs"))
    assert r["status"] == "failed" and not os.path.exists(tmp_path / "bad.nxs")


def test_eln_archives_parse_in_nomad(tem_files, tmp_path):
    pytest.importorskip("nomad.client")
    from nomad.client import normalize_all, parse
    m = full_meta()
    m["source"] = {"elabftw_url": "https://eln.example.org/experiments.php?mode=view&id=4"}
    m["tags"] = ["STO"]
    infos = [formats.inspect_file(os.path.join(tem_files, n)) for n in sorted(KINDS)]
    fl = [dict(path="tem/s1/" + i["name"], kind=i["kind"], format=i["format"], shape=i["shape"], size=i["size"],
               note=i["note"]) for i in infos]
    p = eln.write_eln(str(tmp_path), "s1", m, fl, prefix="tem/s1")
    a = parse(p["eln"])[0]
    normalize_all(a)
    d = a.data
    assert d.m_def.name == "ELNTEMExperiment" and d.name == "STO session" and d.method
    assert d.description == "<p>EELS and 4D-STEM of <b>SrTiO3</b></p>" and d.sample_info.history.startswith("<p>FIB")
    assert d.instrument_info.acceleration_voltage.to("kV").magnitude == pytest.approx(300)
    assert [f.kind for f in d.data_files] == [KINDS[n] for n in sorted(KINDS)] and d.data_files[0].file.startswith(
        "tem/s1/")
    assert d.users[0].orcid == "https://orcid.org/0000-0002-1825-0097" and d.eln_data_file == "tem/s1/s1.eln_data.yaml"
    assert d.elabftw_entry.endswith("id=4")
    # core schema (plugin not installed on the target NOMAD)
    pc = eln.write_eln(str(tmp_path / "core"), "s1", m, fl, prefix="tem/s1", schema="core")
    a = parse(pc["eln"])[0]
    assert a.data.m_def.name == "ELNMeasurement" and "FIB lift-out" in a.data.description
    # conversion record and workflow
    res = [{"inputs": ["tem/s1/si.hspy"], "output": "tem/s1/nxem/si.nxs", "status": "ok", "message": "valid",
            "warnings": 0, "runtime_s": 1.0},
           {"inputs": ["tem/s1/x.ser", "tem/s1/x.emi"], "output": "tem/s1/nxem/x.nxs", "status": "failed",
            "message": "boom", "warnings": 0, "runtime_s": 0.5}]
    q = eln.write_conversion(str(tmp_path), "s1", res, [f["path"] for f in fl], prefix="tem/s1",
                             eln_entry="tem/s1/s1.archive.yaml")
    conv = yaml.safe_load(open(q["conversion"]))["data"]
    assert conv["eln_entry"] == "../upload/archive/mainfile/tem/s1/s1.archive.yaml#/data"
    del conv["eln_entry"]
    with open(tmp_path / "c.archive.yaml", "w") as f:
        yaml.safe_dump({"data": conv}, f)
    a = parse(str(tmp_path / "c.archive.yaml"))[0]
    assert [(c.name, c.status) for c in a.data.conversions] == [("si.hspy", "ok"), ("x.ser", "failed")]
    wf = yaml.safe_load(open(q["workflow"]))["workflow2"]
    si = sorted(KINDS).index("si.hspy")
    assert wf["tasks"][0]["inputs"][0]["section"] == f"../upload/archive/mainfile/tem/s1/s1.archive.yaml#/data/" \
                                                      f"data_files/{si}"
    assert wf["outputs"][0]["section"] == "../upload/archive/mainfile/tem/s1/nxem/si.nxs#/data"
    assert wf["tasks"][1]["outputs"][0]["section"].endswith("s1.nxem.archive.yaml#/data/conversions/1")
    assert len(wf["tasks"]) == 2 and wf["inputs"][0]["name"] == "metadata (ELN)"   # (references need an upload)
    assert "<svg" in eln.preview_svg(q["workflow"])
    back = eln.meta_from_archive(yaml.safe_load(open(p["eln"])))
    assert back["sample"]["history"].startswith("<p>FIB") and back["instrument"]["voltage_kV"] == 300


def _elab_fixture(el, tem_files):
    el.entities[("experiments", 42)] = {
        "id": 42, "title": "STO EELS session", "date": "2026-11-20", "body": "<p>Measured <b>EELS</b></p>",
        "fullname": "Ada Lovelace", "userid": 7, "tags": "STO|EELS", "elabid": "20261120-abc",
        "category_title": "TEM session",
        "metadata": json.dumps({"extra_fields": {
            "Microscope": {"type": "text", "value": "TEM 2"}, "High tension": {"type": "number", "value": "300",
                                                                              "unit": "kV"},
            "Vendor": {"type": "text", "value": "JEOL"}, "Mode": {"type": "select", "value": "STEM"},
            "Sample link": {"type": "items", "value": 5}}}),
        "items_links": [{"entityid": 5, "title": "STO lamella 12", "category_title": "Sample"}]}
    el.entities[("items", 5)] = {
        "id": 5, "title": "STO lamella 12", "body": "<p>FIB lift-out on 2026-11-01</p>", "category_title": "Sample",
        "metadata": json.dumps({"extra_fields": {"Chemical formula": {"type": "text", "value": "SrTiO3"},
                                                 "Preparation date": {"type": "date", "value": "2026-11-01"},
                                                 "Thickness": {"type": "number", "value": "35", "unit": "nm"}}})}
    el.users[7] = {"fullname": "Ada Lovelace", "orcid": "0000-0002-1825-0097", "email": "ada@example.org"}
    el.files[("experiments", 42)] = [
        ({"id": 1, "real_name": "eels.msa", "filesize": os.path.getsize(os.path.join(tem_files, "eels.msa")),
          "state": 1}, open(os.path.join(tem_files, "eels.msa"), "rb").read()),
        ({"id": 2, "real_name": "notes.txt", "filesize": 5, "state": 1}, b"hello")]


def test_elabftw_import(elab_server, tem_files, tmp_path):
    el = elab_server
    _elab_fixture(el, tem_files)
    assert elabftw.parse_ref(f"{el.base}/experiments.php?mode=view&id=42") == (f"{el.base}/api/v2", "experiments",
                                                                                42)
    assert elabftw.parse_ref("eln.example.org/sub/database.php?mode=view&id=7")[1:] == ("items", 7)
    assert elabftw.parse_ref("https://eln.example.org/api/v2/items/3")[1:] == ("items", 3)
    assert elabftw.parse_ref("https://eln.example.org", 9, "experiments")[0] == "https://eln.example.org/api/v2"
    with pytest.raises(elabftw.ElabError, match="ID"):
        elabftw.parse_ref("https://eln.example.org", None, "items")
    with pytest.raises(elabftw.ElabError, match="API key"):
        elabftw.ElabClient(f"{el.base}/api/v2", "")
    with pytest.raises(elabftw.ElabError, match="refused"):
        elabftw.import_entry(elabftw.ElabClient(f"{el.base}/api/v2", "wrong"), "experiments", 42)
    c = elabftw.ElabClient(f"{el.base}/api/v2", ELAB_KEY)
    r = elabftw.import_entry(c, "experiments", 42)
    m = r["meta"]
    assert m["entry"]["experiment_alias"] == "STO EELS session" and m["entry"]["start_time"].startswith("2026-11-20")
    assert "<b>EELS</b>" in m["entry"]["experiment_description"] and "Linked resources" in \
        m["entry"]["experiment_description"]
    assert m["instrument"] == {"name": "TEM 2", "voltage_kV": 300.0, "vendor": "JEOL", "operation_mode": "STEM"}
    assert m["sample"]["name"] == "STO lamella 12" and m["sample"]["chemical_formula"] == "SrTiO3"
    assert m["sample"]["thickness"] == 35.0 and m["sample"]["preparation_date"].startswith("2026-11-01")
    assert "FIB lift-out" in m["sample"]["history"] and m["sample"]["identifier_sample"] == "elabftw:items/5"
    assert m["users"] == [{"name": "Ada Lovelace", "orcid": "https://orcid.org/0000-0002-1825-0097"}]
    assert m["tags"] == ["STO", "EELS"] and m["source"]["elabftw_id"] == 42
    assert [u["real_name"] for u in r["uploads"]] == ["eels.msa", "notes.txt"]
    seen = []
    p = c.download("experiments", 42, r["uploads"][0], str(tmp_path), progress=lambda n, g, t: seen.append(g))
    assert open(p, "rb").read() == open(os.path.join(tem_files, "eels.msa"), "rb").read() and seen
    assert all(a == ELAB_KEY for _p, _q, a in el.requests[-3:])


def test_nomad_client_streamed_upload(mock, tem_files):
    c = NomadClient(mock.url, TOKEN)
    files = [os.path.join(tem_files, n) for n in ("eels.msa", "scan4d.hspy")]
    seen = []
    r = c.upload([(files[0], "eels.msa"), (files[1], "sub/scan4d.hspy")],
                 upload_id="U1", folder="tem/s1", progress=lambda s, t: seen.append((s, t)))
    assert r["upload_id"] == "U1" and set(mock.uploads["U1"]) == {"tem/s1/eels.msa", "tem/s1/sub/scan4d.hspy"}
    assert seen[-1][0] == seen[-1][1] == mock.bodies[-1]
    r = c.upload(files, folder="tem/new", upload_name="my data")
    assert set(mock.uploads[r["upload_id"]]) == {"tem/new/eels.msa", "tem/new/scan4d.hspy"}
    assert mock.names[r["upload_id"]] == "my data"
    assert c.upload_state("U1")["process_running"] is False
    with pytest.raises(Exception, match="invalid"):
        c.upload(files, upload_id="U1", folder="../x")
    np.testing.assert_equal(mock.uploads["U1"]["tem/s1/eels.msa"], open(files[0], "rb").read())
