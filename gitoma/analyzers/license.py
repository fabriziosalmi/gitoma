"""License file analyzer."""

from __future__ import annotations


from gitoma.analyzers.base import BaseAnalyzer, MetricResult

SPDX_IDS = [
    "MIT", "Apache-2.0", "GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0",
    "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0", "AGPL-3.0",
    "CC0-1.0", "Unlicense", "WTFPL",
]


class LicenseAnalyzer(BaseAnalyzer):
    name = "license"
    display_name = "License"
    weight = 0.8

    def analyze(self) -> MetricResult:
        license_path = self.file_exists(
            "LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "licence.md"
        )

        if not license_path:
            return MetricResult.from_score(
                self.name, self.display_name, 0.0,
                "No LICENSE file found",
                ["Add a LICENSE file. Recommended: MIT or Apache-2.0 for open-source projects"],
                self.weight,
            )

        content = (self.read(license_path) or "").upper()
        suggestions: list[str] = []

        # Detect known SPDX license
        detected = [spdx for spdx in SPDX_IDS if spdx.upper() in content]
        if detected:
            details = f"{license_path} — {detected[0]} license detected"
            score = 1.0
        else:
            details = f"{license_path} found but license type not recognized"
            score = 0.6
            suggestions.append(
                "License file found but SPDX type not recognized. "
                "Make sure it contains standard license text"
            )

        # Check if mentioned in README
        readme_content = (
            self.read("README.md") or self.read("README.rst") or ""
        ).lower()
        if "license" in readme_content:
            score = min(1.0, score + 0.0)  # already good, no bonus needed
        else:
            suggestions.append("Mention the license in your README (e.g., a License section)")

        return MetricResult.from_score(
            self.name, self.display_name, score, details, suggestions, self.weight
        )
