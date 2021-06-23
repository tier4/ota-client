#!/usr/bin/env python3

import pytest


def test_gen_ota_status_file(tmpdir):
    import ota_boot
    ota_status_path = tmpdir.join("ota_status")
    ota_boot._gen_ota_status_file(ota_status_path)
    assert ota_status_path.read() == "NORMAL"
