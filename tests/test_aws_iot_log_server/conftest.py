import pytest
from pathlib import Path


@pytest.fixture
def shared_datadir(tmp_path: Path):
    _shared_datadir = tmp_path / "_shared_datadir"
    _shared_datadir.symlink_to(Path(__file__).parent / "data")

    return _shared_datadir
