"""Nion Swift exports (.npy + .json, or both in a .zip): inspection, grouping and conversion to NXem."""
import io
import json
import zipfile

import h5py
import numpy as np
import pytest

from emingest import convert as cv
from emingest import formats, nion

pytest.importorskip("pynxtools_em")

EV = "entry1/measurement/event1"


def _meta(cal, **kw):
    m = {"title": kw.pop("title", "scan"), "spatial_calibrations": cal,
         "datetime_original": {"local_datetime": "2026-11-20T14:03:11", "tz": "+0100"},
         "metadata": {"hardware_source": {"hardware_source_name": "Dectris ELA"},
                      "instrument": {"high_tension": 60000.0, "probe_ha": 0.032},
                      "scan": {"fov_nm": 12.8, "rotation_deg": 0.0}}}
    m.update(kw)
    return m


@pytest.fixture()
def exports(tmp_path):
    rng = np.random.default_rng(0)
    d4 = rng.poisson(3, (6, 5, 16, 16)).astype(np.float32)
    m4 = _meta([{"offset": 0, "scale": 0.1, "units": "nm"}] * 2 + [{"offset": 0, "scale": 1, "units": "eV"}] * 2,
               collection_dimension_count=2, datum_dimension_count=2, is_sequence=False)
    np.save(tmp_path / "A.npy", d4)
    (tmp_path / "A.json").write_text(json.dumps(m4))
    buf = io.BytesIO()
    np.save(buf, d4)
    with zipfile.ZipFile(tmp_path / "B.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("B.npy", buf.getvalue())
        z.writestr("B.json", json.dumps(m4))
    si = (rng.random((4, 5, 64)) * 100).astype(np.float32)
    np.save(tmp_path / "C.npy", si)
    (tmp_path / "C.json").write_text(json.dumps(_meta([{"scale": 0.2, "units": "nm"}] * 2 +
                                                      [{"offset": 280, "scale": 0.5, "units": "eV"}])))
    im = rng.integers(0, 70000, (32, 32)).astype(np.float64)
    np.save(tmp_path / "D.npy", im)
    (tmp_path / "other.json").write_text("{}")
    eln = tmp_path / "eln_data.yaml"
    eln.write_text("entry: {experiment_alias: nion}\nsample: {name: S, atom_types: 'Sr, Ti, O'}\n"
                   "instrument: {vendor: Nion, model: HERMES, voltage: {value: 60, unit: kV}}\n")
    return tmp_path, d4, si, im


def test_inspect_and_group(exports):
    d = exports[0]
    a = formats.inspect_file(str(d / "A.npy"))
    assert (a["format"], a["kind"], a["shape"]) == (nion.FORMAT, "4D-STEM", [6, 5, 16, 16])
    assert a["metadata"]["voltage_kV"] == 60 and a["metadata"]["vendor"] == "Nion"
    assert [x["units"] for x in a["axes"]] == ["nm", "nm", "", ""]          # camera axes: pixels, not eV
    assert formats.inspect_file(str(d / "B.zip"))["kind"] == "4D-STEM"
    assert formats.inspect_file(str(d / "C.npy"))["kind"] == "spectrum image"
    dd = formats.inspect_file(str(d / "D.npy"))
    assert dd["kind"] == "image" and "no .json" in dd["note"]
    assert formats.inspect_file(str(d / "A.json"))["format"] == "sidecar"
    paths = sorted(str(p) for p in d.iterdir())
    groups = {tuple(p.rsplit("/", 1)[1] for p in g) for g in formats.group_files(paths) if cv.convertible(g[0])}
    assert groups == {("A.npy", "A.json"), ("B.zip",), ("C.npy", "C.json"), ("D.npy",)}
    assert not cv.convertible(str(d / "other.json"))


@pytest.mark.parametrize("stream", [False, True])
def test_convert(exports, monkeypatch, stream):
    d, d4, si, im = exports
    if stream:
        monkeypatch.setattr(nion, "MAX_MEMORY_MB", 0.0)
    paths = [str(d / n) for n in ("A.npy", "A.json", "B.zip", "C.npy", "C.json", "D.npy")]
    res = cv.convert_all(paths, str(d / "eln_data.yaml"), str(d / "out"), isolate=False)
    assert [r["status"] for r in res] == ["ok"] * 4, [r["message"] for r in res]
    for r in res:
        assert "Length of axis" not in r["log"] and "Inconsistent dimensions" not in r["log"]
    for name in ("A", "B"):
        with h5py.File(d / "out" / f"{name}.nxs") as h:
            ds = h[f"{EV}/image1/image_4d/real"]
            assert ds.dtype == np.uint16 and np.array_equal(ds[()], d4)           # lossless, smaller type
            assert h[f"{EV}/instrument/optics/semi_convergence_angle"][()] == pytest.approx(0.032)
            assert h[f"{EV}/start_time"][()].decode().startswith("2026-11-20T14:03:11+01:00")
            ax = h[f"{EV}/image1/image_4d/axis_m"]
            assert ax.attrs["units"] == "nm" and ax[1] == pytest.approx(0.1)
            assert h["entry1/measurement/instrument/fabrication/vendor"][()] == b"Nion"
    with h5py.File(d / "out" / "C.nxs") as h:
        ds = h[f"{EV}/spectrum1/spectrum_2d/intensity"]
        assert ds.dtype == np.float32 and np.array_equal(ds[()], si)
        assert h[f"{EV}/spectrum1/spectrum_2d/axis_energy"][0] == pytest.approx(280)
    with h5py.File(d / "out" / "D.nxs") as h:
        ds = h[f"{EV}/image1/image_2d/real"]
        assert ds.dtype == np.uint32 and np.array_equal(ds[()], im)


def test_lossless_dtype():
    assert nion.lossless_dtype(np.array([0, 1.0, 65535])) == np.uint16
    assert nion.lossless_dtype(np.array([0, 1.0, 65536])) == np.uint32
    assert nion.lossless_dtype(np.array([0, 1.5])) == np.float64
    assert nion.lossless_dtype(np.array([-1, 2], dtype=np.int16)) == np.int16
    assert nion.lossless_dtype(np.array([1, 2], dtype=np.uint8)) == np.uint8


def test_app_nion_upload(exports, tmp_path):
    from mocks import TOKEN

    from emingest.app import App
    d = exports[0]
    app = App(work_dir=str(tmp_path / "w"), server_mode=False, nomad_url="http://127.0.0.1:9", token=TOKEN)
    assert app.add_local(str(d / "A.npy")) == 1 and app.add_local(str(d / "A.json")) == 1
    assert app.add_local(str(d / "B.zip")) == 1
    assert app.wait_inspected()
    assert {n: app.kind(f) for n, f in app.files.items()} == {"A.npy": "4D-STEM", "A.json": "other", "B.zip": "4D-STEM"}
    assert "2 can be converted to NXem" in app.w_table.value
    app._apply_file_hints()
    ins = app.get_meta()["instrument"]
    assert float(ins["voltage_kV"]) == 60 and ins["vendor"] == "Nion" and ins["detectors"] == "Dectris ELA"
