# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""The app (headless), the dashboard page and server, the NOMAD entry points and the conversion action on a real
Temporal server (Temporal CLI dev server; skipped if not available) with a local folder as the upload."""
import asyncio
import json
import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml
from conftest import full_meta
from mocks import ELAB_KEY, TOKEN
from test_core import KINDS, _elab_fixture

from emingest.app import ACTION_ID, App


def _fill(app, m):
    app.set_meta(m)
    assert "all required and recommended" in app.w_check.value, app.w_check.value


def test_app_local_conversion_and_upload(mock, tem_files, tmp_path):
    app = App(work_dir=str(tmp_path / "w"), server_mode=False, nomad_url=mock.url, token=TOKEN)
    assert app.add_local(tem_files) == len(KINDS)
    assert app.wait_inspected()
    assert {n: app.kind(f) for n, f in app.files.items()} == KINDS
    app.w_sel.value = ("notes.pdf",)
    app.w_kind.value = "other"
    app._set_kind()
    assert app.files["notes.pdf"]["kind_user"] == "other"
    assert "missing (required)" in app.w_check.value
    _fill(app, full_meta())
    assert app.get_meta()["sample"]["history"] == "<p>FIB lift-out, 2 kV cleaning</p>"
    assert app.connect() and app.w_banner.value == "" and "connected to" in app.w_conn_status.value
    assert app.project.box in app.tabs.children[0].children and app.project.box in app.tabs.children[3].children
    app.project.w_sel.value = "U1"
    app.w_conv.value = "local"
    app.push()
    assert "✔" in app.w_push_status.value, app.w_push_status.value
    up = mock.uploads["U1"]
    f = "tem/STO_session"
    assert {f"{f}/{n}" for n in KINDS} <= set(up)
    for n in ("STO_session.archive.yaml", "STO_session.eln_data.yaml", "STO_session.nxem.archive.yaml",
              "STO_session.workflow.archive.yaml", "nxem/scan4d.nxs", "nxem/eels.nxs", "nxem/focal.nxs"):
        assert f"{f}/{n}" in up, n
    eln = yaml.safe_load(up[f"{f}/STO_session.archive.yaml"])["data"]
    assert eln["m_def"].endswith("ELNMeasurement") and "FIB lift-out" in eln["description"]   # core schema
    assert "<svg" in app.w_result.value and "✔ ok" in app.w_result.value
    assert not [d for d in os.listdir(tmp_path / "w") if d.startswith("out_")]


def test_app_elabftw_import_and_action(mock, elab_server, tem_files, tmp_path):
    _elab_fixture(elab_server, tem_files)
    started = []

    def start(q, b):
        started.append(json.loads(b))
        return 200, {"action_instance_id": "inst-1"}
    mock.extra[("POST", f"actions/{ACTION_ID}/start")] = start
    mock.extra[("GET", "actions/inst-1/status")] = lambda q, b: (200, {"status": "COMPLETED"})
    mock.extra[("GET", "actions/inst-1/result")] = lambda q, b: (200, {"message": "2 of 2 file(s) converted"})
    mock.extra[("GET", "users/me")] = lambda q, b: (200, {"user_id": "u1", "name": "Ada Lovelace"})
    app = App(work_dir=str(tmp_path / "w"), server_mode=True, nomad_url=mock.url, token=TOKEN, in_nomad=True)
    assert app.connect() and app.w_banner.value == "" and "Ada Lovelace" in app.w_conn_status.value
    assert 'label="Upload file(s)"' in app.w_upload.template
    from emingest.nomad_project import NEW
    assert app.project.w_sel.value == NEW
    app.project.w_name.value = "STO EELS"
    assert app._user_rows[0]["w"]["name"].value == "Ada Lovelace"
    app.add_local(tem_files)                                    # server mode: not possible
    assert not app.files
    app.w_el_url.value = f"{elab_server.base}/experiments.php?mode=view&id=42"
    app.w_el_key.value = ELAB_KEY
    assert app._elab_fetch() and "STO EELS session" in app.w_el_preview.value
    assert "2 attachment(s)" in app.w_el_status.value
    app.rte[("entry", "experiment_description")].value = "<p>my own notes</p>"
    app._elab_apply()
    m = app.get_meta()
    assert m["entry"]["experiment_description"].startswith("<p>my own notes</p>") and "EELS" in \
        m["entry"]["experiment_description"]
    assert m["sample"]["chemical_formula"] == "SrTiO3" and m["instrument"]["voltage_kV"] == "300.0"
    assert [u["name"] for u in m["users"]] == ["Ada Lovelace"]
    assert m["source"]["elabftw_id"] == 42
    app._elab_download()
    assert app.wait_inspected() and set(app.files) == {"eels.msa", "notes.txt"}
    assert app.files["eels.msa"]["kind"] == "spectrum" and app.files["eels.msa"]["source"] == "elabFTW"
    app.mw[("instrument", "model")].value = "ARM200F"
    app.mw[("sample", "is_simulation")].value = False
    app.w_folder.value = "tem/elab42"
    assert app.w_conv.value == "action" and app.w_schema.value
    app.push()
    assert "✔" in app.w_push_status.value, app.w_push_status.value
    new = [u for u in mock.uploads if u != "U1"][0]
    assert mock.names[new] == "STO EELS" and app.project.w_sel.value == new
    assert {"tem/elab42/eels.msa", "tem/elab42/notes.txt", "tem/elab42/STO_EELS_session.archive.yaml"} <= \
        set(mock.uploads[new])
    eln = yaml.safe_load(mock.uploads[new]["tem/elab42/STO_EELS_session.archive.yaml"])["data"]
    assert eln["m_def"] == "emingest_nomad.schema_packages.tem.ELNTEMExperiment"
    assert eln["elabftw_entry"].endswith("experiments.php?mode=view&id=42") and eln["sample_info"]["name"] == \
        "STO lamella 12"
    assert started == [{"data": {"upload_id": new, "folder": "tem/elab42",
                                 "eln_data": "tem/elab42/STO_EELS_session.eln_data.yaml",
                                 "eln_entry": "tem/elab42/STO_EELS_session.archive.yaml", "name": "STO_EELS_session",
                                 "max_file_gb": 20.0}}]
    assert "completed" in app.w_result.value and "2 of 2" in app.w_result.value
    assert not any("nxem/" in p for p in mock.uploads[new])     # converted by the action


def test_required_metadata_block_the_upload(mock, tem_files, tmp_path):
    app = App(work_dir=str(tmp_path / "w"), nomad_url=mock.url, token=TOKEN)
    app.add_local(os.path.join(tem_files, "eels.msa"))
    app.push()
    assert "required metadata missing" in app.w_push_status.value and not mock.bodies
    app.w_force.value = True
    app.w_conv.value = "none"
    app.push()
    assert "✔" in app.w_push_status.value and mock.bodies


def test_dashboard_page_session(mock, tmp_path, monkeypatch):
    pytest.importorskip("solara")
    pytest.importorskip("nomad.config")
    from emingest_nomad.dashboards.page import api_candidates, make_app, nomad_token
    assert nomad_token({"Authorization": "Bearer%20abc"}) == "abc"
    assert "https://oasis.example.org/nomad-oasis/api/v1" in api_candidates(
        {"host": ["oasis.example.org"], "x-forwarded-proto": ["https"]}, "/nomad-oasis/dashboards/emingest")
    mock.extra[("GET", "users/me")] = lambda q, b: (200, {"user_id": "u1", "name": "Ada Lovelace"})
    monkeypatch.setenv("EMINGEST_NOMAD_API", mock.url)
    a = make_app({"Authorization": "Bearer " + TOKEN}, {}, "/nomad-oasis/dashboards/emingest",
                 work_dir=str(tmp_path / "w"))
    assert a.w_banner.value == "" and a.conn_box.layout.display == "none" and a.project.connected
    assert a.w_upload is not None and a.in_nomad and a.w_conv.value == "action"
    b = make_app({}, {}, "/x/dashboards/emingest", work_dir=str(tmp_path / "w2"))
    assert "Not signed in" in b.w_banner.value


def test_dashboard_server_mounted():
    pytest.importorskip("fastapi")
    pytest.importorskip("solara")
    pytest.importorskip("nomad.config")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from emingest_nomad.dashboards.server import create_app
    outer = FastAPI()
    outer.mount("/nomad-oasis/dashboards/emingest", create_app())
    cl = TestClient(outer)
    for _ in range(240):
        if cl.get("/nomad-oasis/dashboards/emingest/healthz").json()["status"] == "ok":
            break
        time.sleep(0.5)
    assert cl.get("/nomad-oasis/dashboards/emingest/healthz").json()["status"] == "ok"
    r = cl.get("/nomad-oasis/dashboards/emingest/")
    assert r.status_code == 200 and "/nomad-oasis/dashboards/emingest/static" in r.text


def test_entry_points():
    pytest.importorskip("nomad.config")
    from importlib.metadata import entry_points
    eps = {e.name: e for e in entry_points(group="nomad.plugin") if e.value.startswith("emingest")}
    assert set(eps) == {"emingest_dashboard", "emingest_schema", "emingest_nxem_conversion"}
    act = eps["emingest_nxem_conversion"].load()
    a = act.load()
    assert a.workflow.__name__ == "ConversionWorkflow" and len(a.activities) == 4
    assert eps["emingest_dashboard"].load().id_url_safe == "emingest"
    assert eps["emingest_schema"].load().load().name.startswith("emingest_nomad")
    assert "tem" in [e.name for e in entry_points(group="pynxtools.reader")]


# ------------------------------------------------------------------------------------------------ Temporal
def _cli():
    for c in (os.environ.get("TEMPORAL_CLI"), shutil.which("temporal"), "/home/claude/temporal/temporal"):
        if c and os.path.isfile(c):
            return c
    return None


@pytest.fixture(scope="module")
def temporal():
    pytest.importorskip("temporalio")
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment
    try:
        env = asyncio.new_event_loop().run_until_complete(
            WorkflowEnvironment.start_local(data_converter=pydantic_data_converter, dev_server_existing_path=_cli()))
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no Temporal dev server: {e}")
    yield env
    asyncio.new_event_loop().run_until_complete(env.shutdown())


def _run(temporal, root, inp):
    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.worker import Worker

    from emingest_nomad.workflows import backend
    from emingest_nomad.workflows.activities import (
        cleanup_conversion,
        convert_files,
        publish_conversion,
        stage_conversion,
    )
    from emingest_nomad.workflows.conversion_workflow import ConversionWorkflow
    backend.set_backend(backend.LocalBackend(str(root), writers={"U1": {"me"}}))

    async def go():
        client = await Client.connect(temporal.client.service_client.config.target_host,
                                      data_converter=pydantic_data_converter)
        async with Worker(client, task_queue="cpu-task-queue", workflows=[ConversionWorkflow],
                          activities=[stage_conversion, convert_files, publish_conversion, cleanup_conversion],
                          activity_executor=ThreadPoolExecutor(4)):
            return await client.execute_workflow(ConversionWorkflow.run, inp, id=f"emingest-{uuid.uuid4()}",
                                                 task_queue="cpu-task-queue")
    return asyncio.new_event_loop().run_until_complete(go())


@pytest.fixture
def upload(tem_files, tmp_path):
    """Upload U1 as the dashboard leaves it: data files + ELN + eln_data.yaml in tem/s1 (no conversion)."""
    from emingest import eln, formats
    root = tmp_path / "uploads"
    d = root / "U1" / "tem" / "s1"
    d.mkdir(parents=True)
    for n in KINDS:
        shutil.copy(os.path.join(tem_files, n), d / n)
    infos = [formats.inspect_file(str(d / n)) for n in sorted(KINDS)]
    fl = [dict(path="tem/s1/" + i["name"], kind=i["kind"], format=i["format"], shape=i["shape"], size=i["size"])
          for i in infos]
    eln.write_eln(str(d), "s1", full_meta(), fl, prefix="tem/s1")
    return root


def test_action_workflow(temporal, upload):
    from temporalio.client import WorkflowFailureError

    from emingest_nomad.workflows.models import ConversionInput
    res = _run(temporal, upload, ConversionInput(upload_id="U1", user_id="me", folder="tem/s1", max_file_gb=1.0))
    d = upload / "U1" / "tem" / "s1"
    assert res.converted == len(KINDS) - 1 and res.failed == 0, res.message
    assert sorted(os.listdir(d / "nxem")) == sorted(n.rsplit(".", 1)[0] + ".nxs" for n in KINDS if n != "notes.pdf")
    assert "tem/s1/s1.workflow.archive.yaml" in res.files and "tem/s1/nxem/scan4d.nxs" in res.files
    conv = yaml.safe_load((d / "s1.nxem.archive.yaml").read_text())["data"]
    assert conv["eln_entry"] == "../upload/archive/mainfile/tem/s1/s1.archive.yaml#/data"
    assert {c["status"] for c in conv["conversions"]} == {"ok"}
    wf = yaml.safe_load((d / "s1.workflow.archive.yaml").read_text())["workflow2"]
    assert wf["inputs"][0]["name"] == "metadata (ELN)" and any(
        i["section"].endswith("s1.archive.yaml#/data/data_files/0") for i in wf["inputs"])
    import h5py
    with h5py.File(d / "nxem" / "tilt.nxs") as f:
        assert f["entry1/sample/name"][()] == b"STO lamella"
    assert not (upload / "_work").exists() or not os.listdir(upload / "_work")
    # explicit files, other output folder; no access for other users
    res = _run(temporal, upload, ConversionInput(upload_id="U1", user_id="me", files=["tem/s1/eels.msa"],
                                                 output_folder="nx2", name="eels_only"))
    assert res.converted == 1 and (upload / "U1" / "nx2" / "eels.nxs").exists()
    assert (d / "eels_only.workflow.archive.yaml").exists()
    with pytest.raises(WorkflowFailureError):
        _run(temporal, upload, ConversionInput(upload_id="U1", user_id="intruder", folder="tem/s1"))
    with pytest.raises(WorkflowFailureError):
        _run(temporal, upload, ConversionInput(upload_id="U1", user_id="me", folder="nothing/here"))
