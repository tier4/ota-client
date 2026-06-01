from __future__ import annotations

import pytest
import yarl

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
        # @ in path (should not be interpreted as userinfo separator)
        (
            "http://example.com/user@host/file",
            False,
            "http://example.com/user%40host/file",
        ),
        (
            "http://example.com/user@host/file",
            True,
            "https://example.com/user%40host/file",
        ),
        # Consecutive slashes in path (should be preserved as-is)
        (
            "http://example.com/data//file.bin",
            False,
            "http://example.com/data//file.bin",
        ),
        (
            "http://example.com/data//file.bin",
            True,
            "https://example.com/data//file.bin",
        ),
        # Colon in path (e.g. versioned filenames)
        (
            "http://example.com/data/file:2",
            False,
            "http://example.com/data/file%3A2",
        ),
        (
            "http://example.com/data/file:2",
            True,
            "https://example.com/data/file%3A2",
        ),
        # Semicolon in path (should not be interpreted as path parameter)
        (
            "http://example.com/data/part1;part2",
            False,
            "http://example.com/data/part1%3Bpart2",
        ),
        (
            "http://example.com/data/part1;part2",
            True,
            "https://example.com/data/part1%3Bpart2",
        ),
        # Already percent-encoded input (uvicorn sends unquoted, so % gets double-encoded)
        (
            "http://example.com/home/user/My%20Documents/file.bin",
            False,
            "http://example.com/home/user/My%2520Documents/file.bin",
        ),
        (
            "http://example.com/home/user/My%20Documents/file.bin",
            True,
            "https://example.com/home/user/My%2520Documents/file.bin",
        ),
    ),
)
def test_process_raw_url(_input: str, _enable_https: bool, _expected: str):
    """Test that process_raw_url correctly quotes the path portion of the URL.

    In our use case, everything after the netloc is a filesystem path (not a
    standard HTTP URL with query strings or fragments). Characters like ?, #,
    &, +, @, ;, : that have special meaning in URLs are actually part of
    filenames and must be percent-encoded as literal path characters.

    The raw URL comes from uvicorn already unquoted, so we re-quote the path
    and return a yarl.URL with encoded=True to prevent aiohttp from
    double-encoding it.
    """
    assert process_raw_url(_input, _enable_https) == yarl.URL(_expected, encoded=True)
