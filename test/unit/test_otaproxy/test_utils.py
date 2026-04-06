from __future__ import annotations

import pytest

from ota_proxy.utils import process_raw_url


@pytest.mark.parametrize(
    "_input, _enable_https, _expected",
    (
        # Basic ASCII path
        ("http://example.com/usr/local/bin", False, "http://example.com/usr/local/bin"),
        ("http://example.com/usr/local/bin", True, "https://example.com/usr/local/bin"),
        # Path with space
        (
            "http://example.com/home/user/My Documents",
            False,
            "http://example.com/home/user/My%20Documents",
        ),
        (
            "http://example.com/home/user/My Documents",
            True,
            "https://example.com/home/user/My%20Documents",
        ),
        # Unicode path
        (
            "http://example.com/home/user/Café Bébé",
            False,
            "http://example.com/home/user/Caf%C3%A9%20B%C3%A9b%C3%A9",
        ),
        (
            "http://example.com/home/user/Café Bébé",
            True,
            "https://example.com/home/user/Caf%C3%A9%20B%C3%A9b%C3%A9",
        ),
        # Special shell characters
        (
            "http://example.com/tmp/a&b|c>output",
            False,
            "http://example.com/tmp/a%26b%7Cc%3Eoutput",
        ),
        (
            "http://example.com/tmp/a&b|c>output",
            True,
            "https://example.com/tmp/a%26b%7Cc%3Eoutput",
        ),
        # Question mark and fragment as literal path part (not query/fragment)
        (
            "http://example.com/data/some?file#name",
            False,
            "http://example.com/data/some%3Ffile%23name",
        ),
        (
            "http://example.com/data/some?file#name",
            True,
            "https://example.com/data/some%3Ffile%23name",
        ),
        # File name with plus and percent signs
        (
            "http://example.com/home/user/file+name%.txt",
            False,
            "http://example.com/home/user/file%2Bname%25.txt",
        ),
        (
            "http://example.com/home/user/file+name%.txt",
            True,
            "https://example.com/home/user/file%2Bname%25.txt",
        ),
        # Dotfiles and hidden folders
        (
            "http://example.com/home/user/.cache/.myconfig",
            False,
            "http://example.com/home/user/.cache/.myconfig",
        ),
        (
            "http://example.com/home/user/.cache/.myconfig",
            True,
            "https://example.com/home/user/.cache/.myconfig",
        ),
        # Emoji in filename
        (
            "http://example.com/home/user/📁.txt",
            False,
            "http://example.com/home/user/%F0%9F%93%81.txt",
        ),
        (
            "http://example.com/home/user/📁.txt",
            True,
            "https://example.com/home/user/%F0%9F%93%81.txt",
        ),
        # Trailing space and newline
        (
            "http://example.com/home/user/file \n",
            False,
            "http://example.com/home/user/file%20%0A",
        ),
        (
            "http://example.com/home/user/file \n",
            True,
            "https://example.com/home/user/file%20%0A",
        ),
        # No path component
        ("http://example.com", False, "http://example.com"),
        ("http://example.com", True, "https://example.com"),
        # Root path only
        ("http://example.com/", False, "http://example.com/"),
        ("http://example.com/", True, "https://example.com/"),
        # Scheme downgrade (https input, enable_https=False)
        (
            "https://example.com/usr/local/bin",
            False,
            "http://example.com/usr/local/bin",
        ),
        (
            "https://example.com/usr/local/bin",
            True,
            "https://example.com/usr/local/bin",
        ),
        # Netloc with port
        (
            "http://example.com:8080/data/file.bin",
            False,
            "http://example.com:8080/data/file.bin",
        ),
        (
            "http://example.com:8080/data/file.bin",
            True,
            "https://example.com:8080/data/file.bin",
        ),
    ),
)
def test_process_raw_url(_input: str, _enable_https: bool, _expected: str):
    assert process_raw_url(_input, _enable_https) == _expected
