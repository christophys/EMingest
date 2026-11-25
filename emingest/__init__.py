# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""emingest: bring TEM data of any kind (images, diffraction patterns, spectra, focal and tilt series, 4D-STEM data
sets, spectrum images ...) in the formats of rosettasciio and pynxtools-em into NOMAD, together with the metadata
pynxtools needs for NXem, a rich-text description and, optionally, an experiment or item of elabFTW.

Modules
-------
formats    detection of format and kind of a file (rosettasciio, pynxtools-em), grouping of sidecar files
metadata   the metadata (NXem / NOMAD Oasis ELN keys) with required / recommended fields, ``eln_data.yaml``
richtext   HTML sanitising, HTML -> text, the rich-text editor widget
elabftw    elabFTW API v2 client (experiments, items, attachments)
eln_archive  elabFTW data without elabFTW: the .eln export (RO-Crate), same interface as the API client
remote     files and folders on other computers over SSH / SFTP (scp-like locations)
tem_session  TEM sessions of the tem-provenance eLabFTW templates (session -> sample, microscope, folder)
ingest     headless pipeline: data set -> ELN (+ NXem) -> NOMAD; sessions and batches
cli        command line ``emingest-ingest`` (batch ingestion)
convert    conversion to NXem with pynxtools (reader ``em`` of pynxtools-em, generic rosettasciio fallback)
nx_reader  the pynxtools reader ``tem`` (pynxtools-em + rosettasciio fallback + instrument / sample metadata)
eln        NOMAD ELN, conversion and workflow archives
nomad      NOMAD API client (uploads, streamed file transfer, actions)
app        the GUI (Jupyter / Voila / Solara / NOMAD dashboard)
"""
__version__ = "0.2.0"
