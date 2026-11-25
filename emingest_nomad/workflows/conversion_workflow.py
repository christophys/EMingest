# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Temporal workflow of the emingest NXem conversion action: stage -> convert -> publish (CPU workers)."""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .activities import cleanup_conversion, convert_files, publish_conversion, stage_conversion
    from .models import ConversionInput, ConversionResult

HEARTBEAT = timedelta(seconds=60)


@workflow.defn
class ConversionWorkflow:
    def __init__(self):
        self.stage = "starting"

    @workflow.query
    def progress(self) -> str:
        return self.stage

    @workflow.run
    async def run(self, data: ConversionInput) -> ConversionResult:
        self.stage = "finding the data files"
        st = await workflow.execute_activity(stage_conversion, data, start_to_close_timeout=timedelta(hours=2),
                                             retry_policy=RetryPolicy(maximum_attempts=2))
        try:
            self.stage = f"converting {len(st.groups)} file(s) to NXem"
            out = await workflow.execute_activity(
                convert_files, [data.model_dump(), st.model_dump()], heartbeat_timeout=HEARTBEAT,
                start_to_close_timeout=timedelta(hours=data.timeout_hours) + timedelta(minutes=30),
                retry_policy=RetryPolicy(maximum_attempts=1))
            self.stage = "adding the NXem files to the upload"
            res = await workflow.execute_activity(
                publish_conversion, [data.model_dump(), st.model_dump(), out],
                start_to_close_timeout=timedelta(hours=2), retry_policy=RetryPolicy(maximum_attempts=3))
            self.stage = "done"
            return res
        except (asyncio.CancelledError, Exception):
            self.stage = "cleaning up"
            await workflow.execute_activity(cleanup_conversion, st.work_dir,
                                            start_to_close_timeout=timedelta(minutes=10))
            raise
