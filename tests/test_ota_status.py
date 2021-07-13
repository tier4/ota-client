#!/usr/bin/env python3

import pytest


def test_get_ota_status(tmpdir):
    from ota_status import OtaStatus

    p = tmpdir.join("ots_status")
    p.write("NORMAL")
    p1 = tmpdir.join("rollback_count")
    p1.write("0")
    ota_st = OtaStatus(ota_status_file=p, ota_rollback_file=p1)
    ota_st.set_ota_status("UPDATE")
    assert p.read() == "UPDATE"
    assert ota_st.get_ota_status() == "UPDATE"
    assert p1.read() == "0"
    assert ota_st.is_rollback_available() == False
    ota_st.inc_rollback_count()
    assert p1.read() == "1"
    assert ota_st.is_rollback_available() == True
