# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""NOMAD schema of the TEM data sets brought in with emingest (dashboard / action).

* ``ELNTEMExperiment`` -- a ``Measurement`` (NOMAD base section; with the NOMADMeasurementsCategory of
  nomad-measurements if installed) with the metadata of the NXem application definition (experiment, project,
  sample with its history as rich text, instrument, users with ORCID), the data files (format, kind, shape) and the
  NXem files made from them; the description is a rich-text field.
* ``TEMConversion``    -- an ``Analysis``: which data file gave which NXem file (status, validation warnings), with
  a reference to the ELN entry.
"""
from nomad.datamodel.data import ArchiveSection, EntryData
from nomad.datamodel.metainfo.annotations import ELNAnnotation, ELNComponentEnum
from nomad.datamodel.metainfo.basesections import Analysis, Measurement
from nomad.metainfo import Quantity, SchemaPackage, Section, SubSection

try:  # nomad-measurements (FAIRmat-NFDI/nomad-measurements)
    from nomad_measurements.general import NOMADMeasurementsCategory
    _CATEGORIES = [NOMADMeasurementsCategory]
except Exception:  # noqa: BLE001
    _CATEGORIES = []

m_package = SchemaPackage(name="emingest_nomad")

S = ELNComponentEnum.StringEditQuantity
R = ELNComponentEnum.RichTextEditQuantity
N = ELNComponentEnum.NumberEditQuantity
B = ELNComponentEnum.BoolEditQuantity
D = ELNComponentEnum.DateTimeEditQuantity
F = ELNComponentEnum.FileEditQuantity
U = ELNComponentEnum.URLEditQuantity


class TEMSampleInfo(ArchiveSection):
    """The specimen (NXem /ENTRY/sample)."""
    m_def = Section(label="Sample")
    name = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    identifier_sample = Quantity(type=str, description="ID of the sample", a_eln=ELNAnnotation(component=S))
    identifier_parent = Quantity(type=str, description="ID of the material / wafer the sample comes from",
                                 a_eln=ELNAnnotation(component=S))
    chemical_formula = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    atom_types = Quantity(type=str, description="elements, comma-separated", a_eln=ELNAnnotation(component=S))
    is_simulation = Quantity(type=bool, default=False, a_eln=ELNAnnotation(component=B))
    preparation_date = Quantity(type=str, description="ISO 8601", a_eln=ELNAnnotation(component=S))
    physical_form = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    thickness = Quantity(type=float, a_eln=ELNAnnotation(component=N))
    thickness_unit = Quantity(type=str, default="nm", a_eln=ELNAnnotation(component=S))
    history = Quantity(type=str, description="history and preparation of the sample",
                       a_eln=ELNAnnotation(component=R))


class TEMInstrumentInfo(ArchiveSection):
    """The microscope (NXem /ENTRY/measurement/instrument)."""
    m_def = Section(label="Instrument")
    name = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    location = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    type = Quantity(type=str, description="tem | sem | fib", a_eln=ELNAnnotation(component=S))
    vendor = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    model = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    serial_number = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    acceleration_voltage = Quantity(type=float, unit="V", a_eln=ELNAnnotation(component=N, defaultDisplayUnit="kV"))
    emitter_type = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    operation_mode = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    detectors = Quantity(type=str, a_eln=ELNAnnotation(component=S))


class TEMUser(ArchiveSection):
    m_def = Section(label_quantity="name")
    name = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    affiliation = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    email = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    orcid = Quantity(type=str, a_eln=ELNAnnotation(component=U))
    role = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    address = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    telephone_number = Quantity(type=str, a_eln=ELNAnnotation(component=S))


class TEMDataFile(ArchiveSection):
    """A data file of the data set."""
    m_def = Section(label_quantity="name")
    name = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    file = Quantity(type=str, a_browser=dict(adaptor="RawFileAdaptor"), a_eln=ELNAnnotation(component=F))
    kind = Quantity(type=str, description="image, diffraction pattern, spectrum, focal series, tilt series, 4D-STEM, "
                                          "spectrum image ...", a_eln=ELNAnnotation(component=S))
    format = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    shape = Quantity(type=int, shape=["*"])
    size_mb = Quantity(type=float, a_eln=ELNAnnotation(component=N))
    note = Quantity(type=str, a_eln=ELNAnnotation(component=S))


class ELNTEMExperiment(Measurement, EntryData):
    """TEM data (images, diffraction, spectra, series, 4D-STEM, spectrum images ...) with their metadata."""
    m_def = Section(categories=_CATEGORIES, label="TEM data set (emingest)",
                    a_eln=ELNAnnotation(lane_width="700px"))
    experiment_alias = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    end_time = Quantity(type=str, description="ISO 8601", a_eln=ELNAnnotation(component=S))
    project_name = Quantity(type=str, a_eln=ELNAnnotation(component=S))
    elabftw_entry = Quantity(type=str, description="the experiment / resource in elabFTW",
                             a_eln=ELNAnnotation(component=U))
    eln_data_file = Quantity(type=str, description="metadata for pynxtools (eln_data.yaml)",
                             a_browser=dict(adaptor="RawFileAdaptor"), a_eln=ELNAnnotation(component=F))
    nxem_files = Quantity(type=str, shape=["*"], a_browser=dict(adaptor="RawFileAdaptor"))
    sample_info = SubSection(section_def=TEMSampleInfo)
    instrument_info = SubSection(section_def=TEMInstrumentInfo)
    users = SubSection(section_def=TEMUser, repeats=True)
    data_files = SubSection(section_def=TEMDataFile, repeats=True)

    def normalize(self, archive, logger):
        super().normalize(archive, logger)
        if not self.method:
            self.method = "TEM"
        try:
            if self.tags:
                archive.metadata.comment = archive.metadata.comment or ", ".join(self.tags)
        except Exception:  # noqa: BLE001
            pass


class TEMConversionRecord(ArchiveSection):
    m_def = Section(label_quantity="name")
    name = Quantity(type=str)
    input_files = Quantity(type=str, shape=["*"], a_browser=dict(adaptor="RawFileAdaptor"))
    nxem_file = Quantity(type=str, a_browser=dict(adaptor="RawFileAdaptor"), a_eln=ELNAnnotation(component=F))
    status = Quantity(type=str)
    message = Quantity(type=str)
    validation_warnings = Quantity(type=int)
    runtime = Quantity(type=float, unit="s")


class TEMConversion(Analysis, EntryData):
    """Conversion of TEM data files to NeXus NXem (pynxtools)."""
    m_def = Section(label="TEM data -> NXem (emingest)")
    eln_entry = Quantity(type=ELNTEMExperiment, a_eln=ELNAnnotation(component=ELNComponentEnum.ReferenceEditQuantity))
    software = Quantity(type=str)
    conversions = SubSection(section_def=TEMConversionRecord, repeats=True)


m_package.__init_metainfo__()
