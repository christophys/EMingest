# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Test records in the shape of the tem-provenance eLabFTW templates (Sample, TEM Session, TEM Microscope; only the
field names and types are used, written here from scratch) and an .eln export of them in the RO-Crate layout of
elabFTW (``bin/console export:eln``)."""
import hashlib
import json
import os
import uuid
import zipfile


def _meta(groups, fields):
    return json.dumps({"elabftw": {"extra_fields_groups": [{"id": i + 1, "name": g} for i, g in enumerate(groups)]},
                       "extra_fields": fields})


def records(path, start="2026-11-20T09:30", end="2026-11-20T17:00"):
    """{(type, id): API-like entity} for: session 42 (experiment), sample 5 with parent 4, microscope 7."""
    sample_fields = {
        "identifier": {"type": "text", "value": "OWNER-STO-12", "group_id": 4},
        "identifier_parent": {"type": "items", "value": 4, "group_id": 4},
        "chemical_formula": {"type": "text", "value": "", "group_id": 2},
        "atom_types": {"type": "text", "value": "", "group_id": 2},
        "substrate_formula": {"type": "text", "value": "SrTiO3", "group_id": 2, "position": 1},
        "substrate_space_group": {"type": "text", "value": "Pm-3m", "group_id": 2, "position": 2},
        "film_formula": {"type": "text", "value": "BaTiO3", "group_id": 2, "position": 3},
        "thickness": {"type": "text", "value": "45 nm", "group_id": 1},
        "physical_form": {"type": "select", "value": "thin_film", "group_id": 1},
        "preparation_date": {"type": "datetime-local", "value": "2026-11-18", "group_id": 3},
        "Method": {"type": "select", "value": "FIB Lamella", "group_id": 3},
        "Sample_provider": {"type": "text", "value": "Thin-film group", "group_id": 4},
        "Reception_date": {"type": "date", "value": "2026-11-17", "group_id": 5},
        "Sample_storage": {"type": "text", "value": "Box 3", "group_id": 5},
    }
    session_fields = {
        "Path": {"type": "text", "value": path, "group_id": 3},
        "Sample": {"type": "items", "value": 5, "group_id": 1},
        "Microscope": {"type": "items", "value": 7, "group_id": 1},
        "start_time": {"type": "datetime-local", "value": start, "group_id": 2},
        "end_time": {"type": "datetime-local", "value": end, "group_id": 2},
        "voltage": {"type": "number", "value": "300", "unit": "kV", "group_id": 2},
        "experiment_description": {"type": "text", "value": "Map the polarisation at the BTO/STO interface.",
                                   "group_id": 2},
        "Contamination": {"type": "radio", "value": "No", "group_id": 2},
        "Plasma Cleaning": {"type": "select", "value": "Sample", "group_id": 2},
        "Technical_issues": {"type": "text", "value": "", "group_id": 2},
        "Other_notes": {"type": "text", "value": "Drift corrected", "group_id": 2},
    }
    micro_fields = {
        "vendor": {"type": "text", "value": "Thermo Fisher Scientific", "group_id": 1},
        "model": {"type": "text", "value": "Spectra 300", "group_id": 1},
        "identifier": {"type": "text", "value": "INV-TEM-0007", "group_id": 1},
        "capability": {"type": "select", "value": "STEM", "group_id": 1},
        "emitter_type": {"type": "text", "value": "cold field emitter", "group_id": 2},
        "voltage": {"type": "select", "value": "200 kV", "group_id": 2},
    }
    return {
        ("experiments", 42): {"id": 42, "title": "BTO/STO interface, session 1", "date": "2026-11-20",
                              "elabid": "20261120-1f2e3d4c5b6a", "fullname": "Ada Lovelace", "userid": 7,
                              "tags": "STO|BTO", "category_title": "TEM Session",
                              "body": "<p>Free annotations of the operator.</p>",
                              "metadata": _meta(["Sample and microscope", "Session Info", "DATA INFO"],
                                                session_fields)},
        ("items", 5): {"id": 5, "title": "BTO on STO lamella 12", "elabid": "20261117-a1b2c3d4e5f6",
                       "category_title": "Sample", "date": "2026-11-17",
                       "body": "<h2>Material and origin</h2><p>PLD-grown film.</p>",
                       "metadata": _meta(["Specimen", "Materials", "Preparation", "Provenance",
                                          "Reception and storage"], sample_fields)},
        ("items", 4): {"id": 4, "title": "BTO/STO wafer W3", "elabid": "20261101-0badc0ffee00",
                       "category_title": "Sample", "date": "2026-11-01", "body": "", "metadata": ""},
        ("items", 7): {"id": 7, "title": "Spectra 300 (TEM 2)", "elabid": "20250101-123456abcdef",
                       "category_title": "TEM Microscope", "date": "2025-01-01", "body": "",
                       "metadata": _meta(["fabrication", "electron_source"], micro_fields)},
    }


def write_eln(dst, recs, attachments=None, base="https://eln.example.org", users=None):
    """An .eln file like elabFTW's export (RO-Crate 1.2, version 109).  ``attachments``: {(type, id): [(name,
    bytes)]}."""
    attachments = attachments or {}
    users = users or {7: ("Ada", "Lovelace", "0000-0002-1825-0097")}
    root = "2026-11-25-120000-export"
    graph = [{"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}},
             {"@id": "https://www.elabftw.net", "@type": "SoftwareApplication", "name": "eLabFTW",
              "version": "5.4.0"}]
    folders, files, parts = {}, [], []
    for (t, i) in recs:
        folders[(t, i)] = f"./{t}-{i}-{uuid.uuid4().hex[:8]}/"
    for (t, i), e in recs.items():
        fid = folders[(t, i)]
        pid = f"person://{hashlib.sha256(str(e.get('userid')).encode()).hexdigest()}?hash_algo=sha256"
        node = {"@id": fid, "@type": "Dataset", "name": e["title"], "text": e.get("body", ""),
                "temporal": e.get("date", "") + "T00:00:00+01:00", "dateCreated": e.get("date", "") + "T08:00:00+01:00",
                "url": f"{base}/{'experiments' if t == 'experiments' else 'database'}.php?mode=view&id={i}",
                "genre": "experiment" if t == "experiments" else "resource", "identifier": e.get("elabid", ""),
                "keywords": ",".join(x for x in str(e.get("tags", "")).split("|") if x)}
        if e.get("userid") in users:
            g, f, o = users[e["userid"]]
            node["author"] = {"@id": pid}
            if not any(n.get("@id") == pid for n in graph):
                graph.append({"@id": pid, "@type": "Person", "givenName": g, "familyName": f,
                              "identifier": f"https://orcid.org/{o}"})
        if e.get("category_title"):
            cid = f"#category-{e['category_title']}"
            node["about"] = {"@id": cid}
            if not any(n.get("@id") == cid for n in graph):
                graph.append({"@id": cid, "@type": "Thing", "name": e["category_title"]})
        pvs = []
        if e.get("metadata"):
            pv = {"@id": f"pv://{uuid.uuid4()}", "@type": "PropertyValue", "propertyID": "elabftw_metadata",
                  "value": e["metadata"]}
            graph.append(pv)
            pvs.append({"@id": pv["@id"]})
            for name, f in json.loads(e["metadata"]).get("extra_fields", {}).items():
                val = f.get("value")
                if f.get("type") == "items" and val and ("items", int(val)) in folders:
                    val = {"@id": folders[("items", int(val))]}
                p = {"@id": f"pv://{uuid.uuid4()}", "@type": "PropertyValue", "propertyID": name, "name": name,
                     "value": val if val not in (None, "") else "", "additionalType": f.get("type")}
                graph.append(p)
                pvs.append({"@id": p["@id"]})
        if pvs:
            node["variableMeasured"] = pvs
        ment = [{"@id": folders[("items", int(json.loads(e["metadata"])["extra_fields"][k]["value"]))]}
                for k in ("Sample", "Microscope") if e.get("metadata") and k in json.loads(e["metadata"]).get(
                    "extra_fields", {})]
        if ment:
            node["mentions"] = ment
        hp = []
        for name, data in attachments.get((t, i), []):
            aid = f"{fid}{uuid.uuid4()}"
            graph.append({"@id": aid, "@type": "File", "name": name, "contentSize": len(data),
                          "sha256": hashlib.sha256(data).hexdigest(), "encodingFormat": "application/octet-stream"})
            hp.append({"@id": aid})
            files.append((aid, data))
        if hp:
            node["hasPart"] = hp
        graph.append(node)
        parts.append({"@id": fid})
    graph.append({"@id": "./", "@type": "Dataset", "hasPart": parts, "name": "eLabFTW export", "version": "109"})
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{root}/ro-crate-metadata.json", json.dumps({"@context": "https://w3id.org/ro/crate/1.2/context",
                                                                "@graph": graph}, indent=1))
        for aid, data in files:
            z.writestr(f"{root}/{aid[2:]}", data)
    return dst
