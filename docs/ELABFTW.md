# eLabFTW in EMingest: server, `.eln` exports, TEM sessions, batches

## Sources

| source | how | what is needed |
|---|---|---|
| eLabFTW server | API v2 (`/api/v2/experiments/{id}`, `/items/{id}`, `/…/uploads`) | address + API key (read access) |
| `.eln` file here | file added in tab 1, or a path on the machine running EMingest | — (in the NOMAD dashboard only folders listed in `EMINGEST_LOCAL_ROOTS`) |
| `.eln` file elsewhere | `user@host:/path/file.eln`, `sftp://user@host:port/folder/` (newest `.eln` of the folder) | SSH access: agent / keys of the machine running EMingest, a private key or a password entered in the dashboard |

The `.eln` export is written by `bin/console export:eln <team ID>` (all experiments, resources and templates of the
team, not deleted ones) or from the export tab of the profile page. EMingest reads its `ro-crate-metadata.json`:

| RO-Crate (`Dataset`) | eLabFTW / EMingest |
|---|---|
| `name`, `text`, `temporal` / `dateCreated` | title, main text, date |
| `identifier` | elabid (persistent ID) |
| `url` (`…?mode=view&id=42`), `genre` | numeric ID and type (experiment / resource) — extra fields that link entries use these IDs |
| `keywords`, `about`, `creativeWorkStatus` | tags, category, status |
| `author` (`Person`, `identifier` = ORCID) | user |
| `mentions` | linked entries |
| `variableMeasured` (`elabftw_metadata`) | the extra fields as in the database; newer exports also give linked entries as `@id` |
| `hasPart` (`File`, `sha256`) | attachments (checked against their SHA-256 sums) |

Over SFTP the archive is opened for random access: only the index, the metadata and the chosen attachments are
transferred. Host keys are checked against `known_hosts`; for an unknown host the fingerprint is shown and must be
accepted explicitly. API keys, passwords and private keys are kept in the memory of the session only.

## TEM sessions

Experiments made from the **TEM Session** template of
[tem-provenance](https://github.com/erotunno/tem-provenance) (extra fields `Sample` and `Microscope` linking
resources, `Path`, `start_time`, …) are recognised automatically and mapped as described in
[`emingest/tem_session.py`](../emingest/tem_session.py). The field names follow the three templates of
tem-provenance (Sample, TEM Session, TEM Microscope); create the templates in your eLabFTW as described there.

| eLabFTW | NOMAD ELN (`ELNTEMExperiment`) | NXem |
|---|---|---|
| session elabid | `lab_id` | `identifier_experiment` (type eLabFTW elabid) |
| `start_time` / `end_time` (facility time, `--timezone`, default Europe/Rome) | `datetime`, `end_time` (UTC) | `start_time`, `end_time` |
| `experiment_description`, body, session record | `description` (rich text) | `experiment_description` |
| sample elabid / owner's `identifier` | `sample_info.identifier_sample` / `identifier_owner` | `sample/identifier_sample`, `sample/identifier_owner` |
| parent sample (resource link) | `sample_info.identifier_parent` (its elabid) | `sample/identifier_parent` |
| Materials `<name>_formula` / `<name>_space_group` | `sample_info.components` | `sample/component<n>/{name, chemical_formula, space_group}` |
| microscope `vendor`, `model`, `identifier`, `voltage` … | `instrument_info` | `measurement/instrument/…` |
| `Path` | `acquisition_folder`; its files are the data files | one NXem file per data file |

**Acquisition folders.** The `Path` is the folder as recorded in the lab. A *path mapping* (dashboard: field
*path mapping*; command line: `--map`; environment: `EMINGEST_PATH_MAP`) translates it to where it can be read:
`/mnt/storage=/data/lab` (mounted elsewhere) or `/mnt/storage=tem@nas.lab.org:/data` (over SSH). All files of the
folder (sub folders included, hidden files left out) become the data of the session. Files with an acquisition
time outside the session window are marked *specimen attribution unverified* (as tem-provenance does).

**NXem files of tem-provenance.** `session-<ID>.nxs` written by its `export_tem_image.py` can be added like any
file (NOMAD parses them); *metadata from the files* (tab 1) and `emingest-ingest --nxs` read their entry, sample
(with identifiers and components) and users.

## Batches

Dashboard: tab 3 → *Batch*. Command line: `emingest-ingest` (installed with the package):

```text
emingest-ingest [session IDs …] (--elabftw-url URL | --eln LOCATION [--all | --list]) [--config config.yml]
                [--timezone Europe/Rome] [--map OLD=NEW …] [--ssh-key FILE] [--ssh-password] [--trust-host-key SHA256:…]
                [--nomad-url URL (--upload-id ID | --new-project NAME)] [--folder tem/session_{id}_{title}]
                [--conversion action|local|none] [--core-schema] [--max-gb 20] [--check] [--out DIR]
emingest-ingest --nxs FILE … [--upload-id ID]
```

Each session becomes the folder `--folder` (placeholders `{id}`, `{title}`) of the upload with the ELN entry, the
`eln_data.yaml`, all acquisition files and — with `--conversion local` — the NXem files, the conversion record and
the workflow; `action` starts the NXem conversion on a NOMAD worker. A session that fails (missing template fields,
folder not found …) is reported and the others continue. `--check` runs everything except writing / uploading.
`--config` reads tem-provenance's `config.yml` (`elabftw.base_url`, `nomad.base_url`, `nomad.playground_upload_id`);
keys come from `ELABFTW_API_KEY` and `NOMAD_API_KEY` or a prompt.
