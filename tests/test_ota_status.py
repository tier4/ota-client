#!/usr/bin/env python3

from pathlib import Path
import pytest


def test__gen_ota_status_file(tmp_path: Path):
    from ota_status import OtaStatus
    import os

    ota_status_path = tmp_path / "ots_status"
    assert OtaStatus._gen_ota_status_file(ota_status_path) == "NORMAL"
    assert ota_status_path.is_file()


def test__initial_read_ota_status_no_file(mocker, tmp_path: Path):
    from ota_status import OtaStatus
    import os

    ota_status_path = tmp_path / "ots_status"
    rollback_count_path = tmp_path / "rollback_count"

    mocker.patch.object(OtaStatus, "_status_file", ota_status_path)
    mocker.patch.object(OtaStatus, "_rollback_file", rollback_count_path)
    otastatus = OtaStatus()
    assert otastatus._initial_read_ota_status() == "NORMAL"
    assert ota_status_path.is_file()


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
def test__initial_read_ota_status(mocker, tmp_path: Path, ota_state, result_state):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text(ota_state)
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text("0")

    mocker.patch.object(OtaStatus, "_status_file", ota_status_path)
    mocker.patch.object(OtaStatus, "_rollback_file", rollback_count_path)

    otastatus = OtaStatus()
    assert otastatus._initial_read_ota_status() == result_state


def test__initial_read_rollback_count_no_file(mocker, tmp_path: Path):
    from ota_status import OtaStatus
    import os

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"

    mocker.patch.object(OtaStatus, "_status_file", ota_status_path)
    mocker.patch.object(OtaStatus, "_rollback_file", rollback_count_path)

    otastatus = OtaStatus()
    assert otastatus._initial_read_rollback_count() == 0
    assert rollback_count_path.is_file()


@pytest.mark.parametrize(
    "rollback_count, result",
    [
        ("0", 0),
        ("1", 1),
    ],
)
def test__initial_read_rollback_count(mocker, tmp_path: Path, rollback_count, result):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"

    rollback_count_path.write_text(rollback_count)

    mocker.patch.object(OtaStatus, "_status_file", ota_status_path)
    mocker.patch.object(OtaStatus, "_rollback_file", rollback_count_path)

    otastatus = OtaStatus()
    assert otastatus._initial_read_rollback_count() == result


@pytest.mark.parametrize(
    "rollback_count, result",
    [
        ("0", 0),
        ("1", 1),
    ],
)
def test_get_rollback_count(mocker, tmp_path: Path, rollback_count, result):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text(rollback_count)

    mocker.patch.object(OtaStatus, "_status_file", ota_status_path)
    mocker.patch.object(OtaStatus, "_rollback_file", rollback_count_path)

    otastatus = OtaStatus()
    assert otastatus.get_rollback_count() == result


@pytest.mark.parametrize(
    "rollback_count, result",
    [
        ("0", False),
        ("1", True),
    ],
)
def test_is_rollback_available(mocker, tmp_path: Path, rollback_count, result):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text(rollback_count)

    mocker.patch.object(OtaStatus, "_status_file", ota_status_path)
    mocker.patch.object(OtaStatus, "_rollback_file", rollback_count_path)

    otastatus = OtaStatus()
    assert otastatus.is_rollback_available() == result


@pytest.mark.parametrize(
    "rollback_count, result",
    [
        ("0", 1),
        ("1", 1),
    ],
)
def test_inc_rollback_count(mocker, tmp_path: Path, rollback_count, result):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text(rollback_count)

    mocker.patch.object(OtaStatus, "_status_file", ota_status_path)
    mocker.patch.object(OtaStatus, "_rollback_file", rollback_count_path)

    otastatus = OtaStatus()
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
def test_dec_rollback_count(mocker, tmp_path: Path, rollback_count, result):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text(rollback_count)

    mocker.patch.object(OtaStatus, "_status_file", ota_status_path)
    mocker.patch.object(OtaStatus, "_rollback_file", rollback_count_path)

    otastatus = OtaStatus()
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
def test_set_ota_status(mocker, tmp_path: Path, ota_state, result_state):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text("NORMAL")
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text("0")

    mocker.patch.object(OtaStatus, "_status_file", ota_status_path)
    mocker.patch.object(OtaStatus, "_rollback_file", rollback_count_path)

    otastatus = OtaStatus()
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
def test_get_ota_status(mocker, tmp_path: Path, ota_state, result_state):
    from ota_status import OtaStatus

    ota_status_path = tmp_path / "ots_status"
    ota_status_path.write_text(ota_state)
    rollback_count_path = tmp_path / "rollback_count"
    rollback_count_path.write_text("0")

    mocker.patch.object(OtaStatus, "_status_file", ota_status_path)
    mocker.patch.object(OtaStatus, "_rollback_file", rollback_count_path)

    otastatus = OtaStatus()
    assert otastatus.get_ota_status() == result_state
