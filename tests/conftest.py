import pytest
import sys

from pathlib import Path


@pytest.fixture(autouse=True, scope="session")
def pythonpath():
    _base_dir = Path(__file__).absolute().parent.parent
    sys.path.extend([str(_base_dir), str(_base_dir / "app")])


# not enable proxy when doing test
DEFUALT_PROXY_INFO = """
enable_local_ota_proxy: false
"""


@pytest.fixture(scope="session")
def proxy_cfg():
    import tempfile
    import proxy_info

    with tempfile.NamedTemporaryFile() as f:
        Path(f.name).write_text(DEFUALT_PROXY_INFO)
        return proxy_info.parse_proxy_info(proxy_info_file=f.name)
