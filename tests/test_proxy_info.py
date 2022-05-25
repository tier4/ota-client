import logging
import pytest
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

MAINECU_PROXY_INFO: str = """
enable_ota_proxy: true
gateway: true
"""
PERCEPTION_ECU_PROXY_INFO: str = """
gateway: false
enable_ota_proxy: true
upper_ota_proxy: "http://10.0.0.1:8082"
enable_ota_proxy_cache: false
"""
EMPTY_PROXY_INFO: str = ""

# define all options as opposite to the default value
FULL_PROXY_INFO: str = """
enable_ota_proxy: false
gateway: false
upper_ota_proxy: "http://10.0.0.1:8082"
enable_ota_proxy_cache: false
ota_proxy_listen_addr: "10.0.0.2"
ota_proxy_listen_port: 2808
"""

# check ProxyInfo for detail
_DEFAULT: Dict[str, Any] = {
    "enable_ota_proxy": True,
    "gateway": True,
    "enable_ota_proxy_cache": True,
    "upper_ota_proxy": None,
    "ota_proxy_listen_addr": "0.0.0.0",
    "ota_proxy_listen_port": 8082,
}


@pytest.mark.parametrize(
    "_input_yaml, _expected",
    (
        (
            MAINECU_PROXY_INFO,
            {
                **_DEFAULT,
                **{
                    "enable_ota_proxy": True,
                    "gateway": True,
                },
            },
        ),
        (
            PERCEPTION_ECU_PROXY_INFO,
            {
                **_DEFAULT,
                **{
                    "enable_ota_proxy": True,
                    "gateway": False,
                    "upper_ota_proxy": "http://10.0.0.1:8082",
                    "enable_ota_proxy_cache": False,
                },
            },
        ),
        (EMPTY_PROXY_INFO, _DEFAULT),
        (
            FULL_PROXY_INFO,
            {
                "enable_ota_proxy": False,
                "gateway": False,
                "enable_ota_proxy_cache": False,
                "upper_ota_proxy": "http://10.0.0.1:8082",
                "ota_proxy_listen_addr": "10.0.0.2",
                "ota_proxy_listen_port": 2808,
            },
        ),
    ),
)
def test_proxy_info(tmp_path: Path, _input_yaml: str, _expected: Dict[str, Any]):
    from proxy_info import parse_proxy_info

    proxy_info_file = tmp_path / "proxy_info.yml"
    proxy_info_file.write_text(_input_yaml)
    _proxy_info = parse_proxy_info(proxy_info_file)

    assert asdict(_proxy_info) == _expected
