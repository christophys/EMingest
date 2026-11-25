# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""NOMAD dashboard: upload TEM data of any kind and format with their metadata into a NOMAD upload.

Served at ``{api_base_path}/dashboards/emingest/``.  The user's NOMAD login is passed on (no API token needed).

Environment variables of the NOMAD app process:

* ``EMINGEST_DASHBOARD_URL`` -- serve the app elsewhere and only link it from NOMAD (``external_url``)
* ``EMINGEST_NOMAD_API``     -- API URL the app uses for NOMAD (default: this NOMAD)
* ``EMINGEST_ELABFTW_URL``   -- elabFTW server preselected in the import tab
"""
import os

from nomad.config.models.plugins import DashboardEntryPoint


class EMIngestDashboardEntryPoint(DashboardEntryPoint):
    def load(self):
        from .server import create_app

        return create_app()


emingest_dashboard = EMIngestDashboardEntryPoint(
    name="TEM data upload",
    description="Upload TEM data of any kind (images, diffraction, spectra, focal / tilt series, 4D-STEM, spectrum "
                "images) in the formats of rosettasciio and pynxtools-em, with the NXem metadata, a rich-text "
                "description and the sample history; import from elabFTW; conversion to NXem.",
    id_url_safe="emingest",
    launch_modes=["embedded", "tab"],
    external_url=os.environ.get("EMINGEST_DASHBOARD_URL") or None,
)
