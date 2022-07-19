import pytest
from pathlib import Path


# not enable proxy when doing test
DEFUALT_PROXY_INFO = """
enable_local_ota_proxy: false
"""


@pytest.fixture(scope="session")
def proxy_cfg():
    import tempfile
    from app import proxy_info

    with tempfile.NamedTemporaryFile() as f:
        Path(f.name).write_text(DEFUALT_PROXY_INFO)
        return proxy_info.parse_proxy_info(proxy_info_file=f.name)
