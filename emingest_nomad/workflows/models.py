# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Input (= the form NOMAD shows for the action) and result of the emingest NXem conversion action."""
from pydantic import BaseModel, Field


class ConversionInput(BaseModel):
    """Convert TEM data files of a NOMAD upload to NeXus NXem (pynxtools, reader 'tem': pynxtools-em with a
    rosettasciio fallback), with the metadata of an eln_data.yaml."""

    upload_id: str = Field(..., description="ID of the NOMAD upload (not published); the results are written into it")
    user_id: str = Field("", description="set by NOMAD to the user who starts the action")
    folder: str = Field("", description="folder of the upload with the data files (e.g. tem/session_01); "
                                        "default: the upload root")
    files: list[str] = Field(default_factory=list,
                             description="data files to convert (paths in the upload); default: all files of the "
                                         "folder that rosettasciio or pynxtools-em can read")
    eln_data: str = Field("", description="metadata for NXem (path of a *eln_data.yaml in the upload); default: "
                                          "the one in the folder")
    eln_entry: str = Field("", description="ELN entry of the data set (*.archive.yaml written by the emingest "
                                           "dashboard); default: the one next to the eln_data.yaml")
    output_folder: str = Field("", description="folder for the NXem files; default: <folder>/nxem")
    name: str = Field("", description="base name of the conversion record and workflow; default: from the "
                                      "eln_data.yaml / folder")
    max_file_gb: float = Field(20.0, gt=0, description="files (with sidecars) larger than this are not converted")
    timeout_minutes_per_file: float = Field(120.0, gt=0, description="time limit of one conversion")
    timeout_hours: float = Field(24.0, gt=0, le=168, description="maximum run time of all conversions")


class StagedConversion(BaseModel):
    work_dir: str
    groups: list[list[str]] = Field(default_factory=list, description="paths in the upload, data file first")
    local: dict = Field(default_factory=dict, description="path in the upload -> local path on the worker")
    eln_data: str = ""
    eln_data_local: str = ""
    eln_entry: str = ""
    data_files: list[str] = Field(default_factory=list, description="data files as listed in the ELN entry")
    output_folder: str = ""
    name: str = ""
    prefix: str = ""


class ConversionResult(BaseModel):
    upload_id: str
    files: list[str] = Field(default_factory=list, description="paths of the written files in the upload")
    converted: int = 0
    failed: int = 0
    skipped: int = 0
    details: list[dict] = Field(default_factory=list)
    message: str = ""
