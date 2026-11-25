# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mocks import MockElab, MockNomad  # noqa: E402


def make_files(d):
    """Synthetic TEM files of several kinds and formats (hyperspy / rosettasciio writers)."""
    hs = pytest.importorskip("hyperspy.api")
    os.makedirs(d, exist_ok=True)
    rng = np.random.default_rng(0)
    s = hs.signals.Signal1D(rng.poisson(100, 256).astype(float))
    a = s.axes_manager[0]
    a.scale, a.offset, a.units, a.name = 0.5, 400.0, "eV", "Energy"
    s.metadata.General.title = "EELS"
    s.save(os.path.join(d, "eels.msa"), overwrite=True)
    si = hs.signals.Signal1D(rng.random((4, 5, 32)))
    si.axes_manager[2].units, si.axes_manager[2].name = "eV", "Energy"
    for i in (0, 1):
        si.axes_manager[i].units, si.axes_manager[i].scale = "nm", 0.2
    si.save(os.path.join(d, "si.hspy"), overwrite=True)
    q = hs.signals.Signal2D(rng.random((3, 4, 16, 16)).astype("float32"))
    for i in (0, 1):
        q.axes_manager[i].units = "nm"
    for i in (2, 3):
        q.axes_manager[i].units, q.axes_manager[i].scale = "mrad", 0.5
    q.save(os.path.join(d, "scan4d.hspy"), overwrite=True)
    f = hs.signals.Signal2D(rng.random((5, 24, 24)).astype("float32"))
    f.axes_manager[0].name, f.axes_manager[0].units, f.axes_manager[0].scale = "defocus", "nm", 50
    for i in (1, 2):
        f.axes_manager[i].units, f.axes_manager[i].scale = "nm", 0.1
    f.save(os.path.join(d, "focal.hspy"), overwrite=True)
    t = hs.signals.Signal2D(rng.random((7, 20, 20)).astype("float32"))
    t.axes_manager[0].name, t.axes_manager[0].units, t.axes_manager[0].scale = "tilt", "deg", 10
    t.save(os.path.join(d, "tilt.hspy"), overwrite=True)
    im = hs.signals.Signal2D((rng.random((40, 32)) * 1000).astype("uint16"))
    for i in (0, 1):
        im.axes_manager[i].units, im.axes_manager[i].scale = "nm", 0.05
    im.save(os.path.join(d, "image.tif"), overwrite=True)
    with open(os.path.join(d, "notes.pdf"), "wb") as fo:
        fo.write(b"%PDF-1.4 not a real pdf")
    return d


@pytest.fixture(scope="session")
def tem_files(tmp_path_factory):
    return make_files(str(tmp_path_factory.mktemp("tem")))


@pytest.fixture
def no_proxy(monkeypatch):
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def mock(no_proxy):
    m = MockNomad()
    yield m
    m.server.shutdown()


@pytest.fixture
def elab_server(no_proxy):
    m = MockElab()
    yield m
    m.server.shutdown()


def full_meta():
    from emingest import metadata as md
    m = md.empty()
    m["entry"].update(experiment_alias="STO session", start_time="2026-11-20 10:00", end_time="2026-11-20 16:30",
                      experiment_description="<p>EELS and 4D-STEM of <b>SrTiO3</b></p>")
    m["sample"].update(name="STO lamella", identifier_sample="S-12", chemical_formula="SrTiO3",
                       preparation_date="2026-11-01", physical_form="lamella", thickness="40",
                       history="<p>FIB lift-out, 2 kV cleaning</p>")
    m["instrument"].update(name="TEM 2", vendor="Thermo Fisher", model="Titan Themis", voltage_kV="300",
                           type="tem", emitter_type="Schottky field emitter", detectors="HAADF, K3")
    m["users"] = [{"name": "Ada Lovelace", "orcid": "0000-0002-1825-0097", "affiliation": "Lab"}]
    return m
