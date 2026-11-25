# EMingest as a NOMAD plugin (dashboard + action + schema)

| entry point | type | what it does |
|---|---|---|
| `emingest_nomad.dashboards:emingest_dashboard` | dashboard | the app inside NOMAD at `{api_base_path}/dashboards/emingest/`, signed in with the user's NOMAD login |
| `emingest_nomad.actions:emingest_nxem_conversion` | action | converts the TEM data files of a folder of an upload to NXem on a NOMAD CPU worker |
| `emingest_nomad.schema_packages:tem_schema` | schema | `ELNTEMExperiment` (ELN of the data set, a `Measurement`) and `TEMConversion` (conversion record) |
| `tem` in `pynxtools.reader` | pynxtools reader | `dataconverter --reader tem --nxdl NXem …` |

NOMAD ≥ 2.0 with the new GUI (dashboards and actions), pynxtools and pynxtools-em (part of the NOMAD distribution).

## Installation in nomad-distro-dev

```bash
cd nomad-distro-dev
git submodule add https://github.com/christophys/EMingest.git packages/EMingest
uv add packages/EMingest --extra nomad
uv sync
docker compose up -d          # infrastructure (MongoDB, Elasticsearch, Temporal, …)
uv run poe start              # NOMAD API + GUI
uv run poe cpuworker          # second terminal: the worker that runs the conversion action
```

Several Solara dashboards (EMingest, fit4DSTEM, inlineHolo) can be installed together: each runs its Solara
server in a child process of the NOMAD app behind a small proxy.

Environment variables of the NOMAD app (all optional): `EMINGEST_ELABFTW_URL` (elabFTW server preselected in
tab 3), `EMINGEST_NOMAD_API` (API address the dashboard uses, default: this NOMAD), `EMINGEST_DASHBOARD_URL`
(serve the app elsewhere and only link it from NOMAD).

## Using the dashboard

New GUI → *Dashboards* → *TEM data upload* (or `http://localhost:8000/nomad-oasis/dashboards/emingest/`).

1. **Files:** *From your computer* — choose any number of files; they are transferred in chunks (progress bar)
   into a folder of the session. Format, reader (pynxtools-em / rosettasciio / "as is"), kind and shape are
   shown; the kind can be changed for selected files. *metadata from the files* puts the high tension,
   microscope and acquisition time found in the files into empty fields of tab 2.
2. **Metadata:** `*` required, `°` recommended by NXem; the line at the top lists what is missing. Two rich-text
   editors: description of the experiment, history and preparation of the sample. *Reuse metadata* fills the form
   from an `*.eln_data.yaml` or `*.archive.yaml` of an earlier data set (add it in tab 1).
3. **elabFTW:** paste the address of the experiment or resource as shown in the browser
   (`…/experiments.php?mode=view&id=42`, `…/database.php?mode=view&id=7`) or the server address plus type and ID,
   and an API key (elabFTW: *User panel → API keys*, read access). *read entry* shows what was found; *use
   metadata* puts it into tab 2 (only empty fields, unless *overwrite* is ticked; the elabFTW text is appended to a
   description that is already there); *add selected attachments* downloads the attachments into tab 1. The key is
   kept only in memory for the session and is never passed to the action or written to NOMAD.

   Mapping: title → title, date → start, main text → description, tags → tags, author → user (with ORCID), extra
   fields by name (*Sample*, *Sample ID*, *Chemical formula* / *Composition*, *Elements*, *Preparation date*,
   *Thickness*, *Microscope* / *Instrument*, *Vendor*, *Model*, *High tension* / *Voltage*, *Mode*, *Detector*,
   *Location*, *Project* …), linked resource of category *Sample* (or the only linked resource) → sample name, ID
   (`elabftw:items/<id>`), its extra fields and its main text → sample history. A link to the elabFTW entry is kept
   in the ELN entry.
4. **To NOMAD:** new upload (name) or one of your unpublished uploads, folder (default `tem/<title>`), base name.
   NXem conversion *with the NOMAD action* (on a worker, for large data; the dashboard shows its progress),
   *here, before uploading*, or none. Files larger than the limit are not converted. The upload is refused while
   required fields are missing unless the box is ticked.

What ends up in the folder of the upload:

```
tem/<title>/
  <data files …>                     as uploaded
  <name>.archive.yaml                ELN entry (ELNTEMExperiment): description, sample, instrument, users,
                                     data files with kind / format / shape, link to elabFTW
  <name>.eln_data.yaml               metadata for pynxtools (NXem)
  nxem/<file>.nxs                    one NXem file per data file (+ sidecars), processed by NOMAD as NXem entries
  <name>.nxem.archive.yaml           conversion record (TEMConversion): input files → NXem file, status, warnings
  <name>.workflow.archive.yaml       workflow: metadata + data files → one conversion task per file → NXem files
```

## The action

New GUI → *Actions* → *emingest: convert TEM data to NXem*, started by the dashboard, or via the API:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     http://localhost:8000/nomad-oasis/api/v1/actions/emingest_nomad.actions:emingest_nxem_conversion/start \
     -d '{"data": {"upload_id": "<upload id>", "folder": "tem/session_01"}}'
```

Input: `upload_id`, `folder` (default: upload root) or a list of `files`, `eln_data` (default: the
`*eln_data.yaml` of the folder), `eln_entry` (default: the `.archive.yaml` next to it), `output_folder`
(default `<folder>/nxem`), `name`, `max_file_gb`, `timeout_minutes_per_file`, `timeout_hours`.

Steps (Temporal workflow `ConversionWorkflow`, CPU queue): `stage_conversion` (access check: only authors and
co-authors of an unpublished upload; data files of the folder that rosettasciio or pynxtools-em can read, grouped
with sidecar files such as `.mdoc`; read in place where the worker sees the upload's files) → `convert_files`
(one pynxtools conversion per file in a child process with a time limit; heartbeats; cancellation within seconds;
a file that fails is reported, the others continue) → `publish_conversion` (NXem files, conversion record and
workflow added to the upload). The result lists converted, failed and skipped files.

## The NXem conversion (reader `tem`)

1. pynxtools-em (`em` reader) for the ELN file and every data file it knows — vendor metadata (Velox, Gatan,
   Nion, Bruker, EDAX, Oxford, JEOL, Hitachi, Tescan, Zeiss, Point Electronic, MRC + mdoc …);
2. if it wrote no data (formats it does not know: TIA, .bcf, .mib, blockfile, .hspy, DENS, TVIPS, generic TIFF /
   PNG …) every rosettasciio signal of the file becomes an NXem event: `image_2d`, `image_3d` (focal / tilt / time
   series), `image_4d` (4D-STEM), `spectrum_0d` … `spectrum_3d` (spectra, line scans, spectrum images), other
   dimensionalities as `NXdata`, with axes, units and the checksum of the source file;
3. instrument (`/ENTRY/measurement/instrument`: name, location, type, fabrication, high tension, electron source,
   operation mode), sample identifiers, physical form and description, ORCIDs of the users as
   `identifier_orcid` (type ORCID).

With complete metadata in tab 2 the files validate against NXem without warnings.
