"""Keep the suite hermetic.

`discover` ingests whatever external scanner is installed (see
`deptool.sources`), which would make every discovery test depend on whether
Trivy happens to be on the machine running it — and would shell out once per
call, taking the suite from under a second to over twenty. Ingest is therefore
off by default here and switched on explicitly, against a fake binary, by the
tests that are about ingesting.
"""

from __future__ import annotations

import pytest

from deptool import sources


@pytest.fixture(autouse=True)
def no_ingest(monkeypatch):
    monkeypatch.setattr(sources, "detect", lambda root: [])
