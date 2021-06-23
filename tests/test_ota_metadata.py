#!/usr/bin/env python3

import pytest


def test_OtaMetaData_get_directory_info():
    from ota_metadata import OtaMetaData
    metadata_jwt = """\
eyJhbGciOiAiRVMyNTYifQ==.W3sidmVyc2lvbiI6IDF9LCB7ImRpcmVjdG9yeSI6ICJkaXJzLnR4dCIsICJoYXNoIjogIjQzYWZiZDE5ZWFiN2M5ZTI3ZjQwMmEzMzMyYzM4ZDA3MmE2OWM3OTMyZmIzNWMzMmMxZmM3MDY5Njk1MjM1ZjEifSwgeyJzeW1ib2xpY2xpbmsiOiAic3ltbGlua3MudHh0IiwgImhhc2giOiAiNjY0M2JmODk2ZDNhYzNiZDQwMzRkNzQyZmFlMGQ3ZWI4MmJkMzg0MDYyNDkyMjM1NDA0MTE0YWViMzRlZmQ3ZCJ9LCB7InJlZ3VsYXIiOiAicmVndWxhcnMudHh0IiwgImhhc2giOiAiYTM5MGY5MmZlNDliODQwMmEyYmI5ZjBiNTk0ZTlkZTcwZjcwZGM2YmY0MjkwMzFhYzRkMGIyMTM2NTI1MTYwMCJ9LCB7InBlcnNpc3RlbnQiOiAicGVyc2lzdGVudHMudHh0IiwgImhhc2giOiAiMzE5NWRlZDczMDQ3NGQwMTgxMjU3MjA0YmEwZmQ3OTc2NjcyMWFiNjJhY2UzOTVmMjBkZWNkNDQ5ODNjYjJkMyJ9LCB7InJvb3Rmc19kaXJlY3RvcnkiOiAiZGF0YSJ9LCB7ImNlcnRpZmljYXRlIjogIm90YS1pbnRlcm1lZGlhdGUucGVtIiwgImhhc2giOiAiMjRjMGM5ZWEyOTI0NTgzOThmMDViOWIyYTMxYjQ4M2M0NWQyN2UyODQ3NDNhM2YwYzc5NjNlMmFjMGM2MmVkMiJ9XQ==.MEYCIQC2iI5Tu5FxG_Int-rlsPDw37rZKR6QnOC51droeNN1uwIhAKBKhbvgFx_Ja8-cpIZIncykwlzGI6_bFKVbY2VmV1qt
"""
    otametadata = OtaMetaData(metadata_jwt)
    dir_file, dir_hash = otametadata.get_directories_info()
    assert dir_file == 'dirs.txt'
    symboliclink, symboliclink_hash =  otametadata.get_symboliclinks_info()
    assert symboliclink == 'symlinks.txt'
    regular, regular_hash =  otametadata.get_regulars_info()
    assert regular == 'regulars.txt'
    persistent, persistent_hash =  otametadata.get_persistent_info()
    assert persistent == 'persistents.txt'
    rootfs_dir =  otametadata.get_rootfsdir_info()
    assert rootfs_dir == 'data'
    certificate, certificate_hash = otametadata.get_certificate_info()
    assert certificate == 'ota-intermediate.pem'

