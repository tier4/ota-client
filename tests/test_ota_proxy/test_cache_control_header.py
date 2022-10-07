import pytest
from otaclient.ota_proxy.cache_control import OTAFileCacheControl


@pytest.mark.parametrize(
    "raw_str, expected",
    (
        ("use_cache", {OTAFileCacheControl.use_cache}),
        (
            "use_cache,retry_caching",
            {OTAFileCacheControl.use_cache, OTAFileCacheControl.retry_caching},
        ),
        ("no_cache", {OTAFileCacheControl.no_cache}),
    ),
)
def test_cache_control_header(raw_str, expected):
    assert OTAFileCacheControl.parse_to_enum_set(raw_str) == expected
