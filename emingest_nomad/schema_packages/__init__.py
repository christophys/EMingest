# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
from nomad.config.models.plugins import SchemaPackageEntryPoint


class TEMSchemaEntryPoint(SchemaPackageEntryPoint):
    def load(self):
        from .tem import m_package

        return m_package


tem_schema = TEMSchemaEntryPoint(
    name="emingest TEM data set",
    description="ELN of TEM data sets (images, diffraction, spectra, focal / tilt series, 4D-STEM, spectrum images) "
                "with NXem metadata, rich-text description and sample history, and their conversion to NXem.",
)
