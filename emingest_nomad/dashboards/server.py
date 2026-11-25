# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""FastAPI app of the dashboard that NOMAD mounts at ``{api_base_path}/dashboards/emingest``.

The Solara server of the app (:mod:`.page`) runs as a child process of the NOMAD app (:mod:`.solara_proxy`); this
app forwards pages, assets and websockets to it, so that several Solara dashboards (emingest, fit4DSTEM,
inlineHolo ...) can be installed in the same NOMAD.
"""
PAGE = "emingest_nomad.dashboards.page:Page"


def create_app():
    from fastapi.responses import JSONResponse

    from .solara_proxy import SolaraProcess, proxy_app

    child = SolaraProcess(PAGE, name="emingest").start()

    def healthz():
        import emingest

        return JSONResponse({"status": "ok" if child.ready(0) else "starting", "emingest": emingest.__version__})

    return proxy_app(child, title="emingest dashboard", extra_routes={"/healthz": healthz})
