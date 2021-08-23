# boot status


class OtaBootStatusString:
    NORMAL_BOOT = "NORMAL_BOOT"
    SWITCH_BOOT = "SWITCH_BOOT"
    SWITCH_BOOT_FAIL = "SWITCH_BOOT_FAIL"
    ROLLBACK_BOOT = "ROLLBACK_BOOT"
    ROLLBACK_BOOT_FAIL = "ROLLBACK_BOOT_FAIL"
    UPDATE_INCOMPLETE = "UPDATE_INCOMPLETE"
    ROLLBACK_INCOMPLETE = "ROLLBACK_INCOMPLETE"


class OtaStatusString:
    NORMAL_STATE = "NORMAL"
    UPDATE_STATE = "UPDATE"
    PREPARED_STATE = "PREPARED"
    SWITCHA_STATE = "SWITCHA"
    SWITCHB_STATE = "SWITCHB"
    UPDATE_FAIL_STATE = "UPDATE_FAIL"
    ROLLBACK_STATE = "ROLLBACK"
    ROLLBACKA_STATE = "ROLLBACKA"
    ROLLBACKB_STATE = "ROLLBACKB"
    ROLLBACK_FAIL_STATE = "ROLLBACK_FAIL"
