# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""emingest as a stand-alone web application (no Jupyter / Voila):

    solara run webapp/solara_app.py --host 0.0.0.0 --port 8766

Every browser session gets its own App in server mode (files are transferred from the user's computer into a
session folder that is removed at the end of the session).  Environment: EMINGEST_NOMAD_URL (NOMAD API, default
the central NOMAD), EMINGEST_ELABFTW_URL (elabFTW server preselected in tab 3).
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import solara  # noqa: E402

from emingest.app import App  # noqa: E402


@solara.component
def Page():
    def build():
        wd = tempfile.mkdtemp(prefix="emingest_")
        return App(work_dir=wd, server_mode=True,
                   nomad_url=os.environ.get("EMINGEST_NOMAD_URL", "production")), wd
    app, wd = solara.use_memo(build, [])
    solara.use_effect(lambda: (lambda: shutil.rmtree(wd, ignore_errors=True)), [])
    solara.Title("TEM data -> NOMAD")
    solara.display(app.ui)
