# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""NOMAD action ``emingest_nomad.actions:emingest_nxem_conversion``: converts the TEM data files of a folder of an
upload (or a list of files) to NeXus NXem with pynxtools, using the metadata of the ``*eln_data.yaml`` written by the
dashboard (or given), on a NOMAD CPU worker.  NXem files, a conversion record and a workflow are added to the upload.

Started from NOMAD's Actions page (form generated from :class:`~emingest_nomad.workflows.models.ConversionInput`),
from the emingest dashboard (tab 4), or via the API::

    POST {api}/v1/actions/emingest_nomad.actions:emingest_nxem_conversion/start
    {"data": {"upload_id": "...", "folder": "tem/session_01"}}
"""
from nomad.actions import TaskQueue
from nomad.config.models.plugins import ActionEntryPoint
from pydantic import Field

ACTION_ID = "emingest_nomad.actions:emingest_nxem_conversion"


class EMIngestActionEntryPoint(ActionEntryPoint):
    task_queue: str = Field(default=TaskQueue.CPU, description="task queue of the worker")

    def load(self):
        from nomad.actions import Action

        from ..workflows.activities import cleanup_conversion, convert_files, publish_conversion, stage_conversion
        from ..workflows.conversion_workflow import ConversionWorkflow

        return Action(task_queue=self.task_queue, workflow=ConversionWorkflow,
                      activities=[stage_conversion, convert_files, publish_conversion, cleanup_conversion])


emingest_nxem_conversion = EMIngestActionEntryPoint(
    name="emingest: convert TEM data to NXem",
    description="Converts the TEM data files (any format of rosettasciio / pynxtools-em) of a folder of an upload "
                "to NeXus NXem with pynxtools, with the metadata of its eln_data.yaml; NXem files, a conversion "
                "record and a workflow are added to the upload.",
)
