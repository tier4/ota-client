#!/usr/bin/env python3

import pytest


def test__initial_read_ota_status_no_file(tmp_path):
    from ota_status import OtaStatus
    import os

    ota_status_path = tmp_path / "ots_status"
    rollback_count_path = tmp_path / "rollback_count"

    otastatus = OtaStatus(
        ota_status_file=str(ota_status_path), ota_rollback_file=str(rollback_count_path)
    )
    assert otastatus._initial_read_ota_status() == "NORMAL"
    assert os.path.isfile(str(ota_status_path))


@pytest.mark.parametrize(
    "ota_state, result_state",
    [
        ("NORMAL", "NORMAL"),
        ("SWITCHA", "SWITCHA"),
        ("SWITCHB", "SWITCHB"),
        ("ROLLBACKA", "ROLLBACKA"),
        ("ROLLBACKB", "ROLLBACKB"),
        ("ROLLBACK", "ROLLBACK"),
        ("UPDATE", "UPDATE"),
    ],
)
def test__initial_read_ota_status(tmp_path, ota_state, result_state):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text(ota_state)
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text("0")

    otastatus = OtaStatus(
        ota_status_file=str(ota_status_path), ota_rollback_file=str(rollback_count_path)
    )
    assert otastatus._initial_read_ota_status() == result_state


def test__initial_read_rollback_count_no_file(tmp_path):
    from ota_status import OtaStatus
    import os

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"

    otastatus = OtaStatus(
        ota_status_file=str(ota_status_path), ota_rollback_file=str(rollback_count_path)
    )
    assert otastatus._initial_read_rollback_count() == 0
    assert os.path.isfile(str(rollback_count_path))


@pytest.mark.parametrize(
    "rollback_count, result",
    [
        ("0", 0),
        ("1", 1),
    ],
)
def test__initial_read_rollback_count(tmp_path, rollback_count, result):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"

    rollback_count_path.write_text(rollback_count)

    otastatus = OtaStatus(
        ota_status_file=str(ota_status_path), ota_rollback_file=str(rollback_count_path)
    )
    assert otastatus._initial_read_rollback_count() == result


@pytest.mark.parametrize(
    "rollback_count, result",
    [
        ("0", 0),
        ("1", 1),
    ],
)
def test_get_rollback_count(tmp_path, rollback_count, result):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text(rollback_count)

    otastatus = OtaStatus(
        ota_status_file=str(ota_status_path), ota_rollback_file=str(rollback_count_path)
    )
    assert otastatus.get_rollback_count() == result


@pytest.mark.parametrize(
    "rollback_count, result",
    [
        ("0", False),
        ("1", True),
    ],
)
def test_is_rollback_available(tmp_path, rollback_count, result):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text(rollback_count)

    otastatus = OtaStatus(
        ota_status_file=str(ota_status_path), ota_rollback_file=str(rollback_count_path)
    )
    assert otastatus.is_rollback_available() == result


@pytest.mark.parametrize(
    "rollback_count, result",
    [
        ("0", 1),
        ("1", 1),
    ],
)
def test_inc_rollback_count(tmp_path, rollback_count, result):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text(rollback_count)

    otastatus = OtaStatus(
        ota_status_file=str(ota_status_path), ota_rollback_file=str(rollback_count_path)
    )
    otastatus.inc_rollback_count()
    assert otastatus.get_rollback_count() == result
    assert otastatus._initial_read_rollback_count() == result


@pytest.mark.parametrize(
    "rollback_count, result",
    [
        ("0", 0),
        ("1", 0),
    ],
)
def test_dec_rollback_count(tmp_path, rollback_count, result):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text(rollback_count)

    otastatus = OtaStatus(
        ota_status_file=str(ota_status_path), ota_rollback_file=str(rollback_count_path)
    )
    otastatus.dec_rollback_count()
    assert otastatus.get_rollback_count() == result
    assert otastatus._initial_read_rollback_count() == result


@pytest.mark.parametrize(
    "ota_state, result_state",
    [
        ("NORMAL", "NORMAL"),
        ("SWITCHA", "SWITCHA"),
        ("SWITCHB", "SWITCHB"),
        ("ROLLBACKA", "ROLLBACKA"),
        ("ROLLBACKB", "ROLLBACKB"),
        ("ROLLBACK", "ROLLBACK"),
        ("UPDATE", "UPDATE"),
    ],
)
def test_set_ota_status(tmp_path, ota_state, result_state):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text("0")

    otastatus = OtaStatus(
        ota_status_file=str(ota_status_path), ota_rollback_file=str(rollback_count_path)
    )
    otastatus.set_ota_status(ota_state)
    assert otastatus.get_ota_status() == result_state
    assert ota_status_path.read_text() == result_state


@pytest.mark.parametrize(
    "ota_state, result_state",
    [
        ("NORMAL", "NORMAL"),
        ("SWITCHA", "SWITCHA"),
        ("SWITCHB", "SWITCHB"),
        ("ROLLBACKA", "ROLLBACKA"),
        ("ROLLBACKB", "ROLLBACKB"),
        ("ROLLBACK", "ROLLBACK"),
        ("UPDATE", "UPDATE"),
    ],
)
def test_get_ota_status(tmp_path, ota_state, result_state):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text(ota_state)
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text("0")

    otastatus = OtaStatus(
        ota_status_file=str(ota_status_path), ota_rollback_file=str(rollback_count_path)
    )
    assert otastatus.get_ota_status() == result_state
