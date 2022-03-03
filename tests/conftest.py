import pytest
import sys

from pathlib import Path

@pytest.fixture(autouse=True, scope="session")
def pythonpath():
    _base_dir = Path(__file__).absolute().parent.parent
    sys.path.extend(
        [str(_base_dir), str(_base_dir / "app")]
    )
