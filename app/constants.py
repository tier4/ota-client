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

class OtaBootConst:
    status_checker_key = "status_checker",
    success_key = "success",
    failure_key = "failure",
    state_key = "state",
    boot_key = "boot",
    finalize_key = "finalize"

    # define status checker methods and status
    state_table = {
        OtaStatusString.NORMAL_STATE: {
            success_key: {
                state_key: OtaStatusString.NORMAL_STATE,
                boot_key: OtaBootStatusString.NORMAL_BOOT,
            },
        },
        OtaStatusString.SWITCHA_STATE: {
            status_checker_key: "_confirm_banka",
            success_key: {
                state_key: OtaStatusString.NORMAL_STATE,
                boot_key: OtaBootStatusString.SWITCH_BOOT,
                finalize_key: "_finalize_update",
            },
            failure_key: {
                state_key: OtaStatusString.UPDATE_FAIL_STATE,
                boot_key: OtaBootStatusString.SWITCH_BOOT_FAIL,
            },
        },
        OtaStatusString.SWITCHB_STATE: {
            status_checker_key: "_confirm_bankb",
            success_key: {
                state_key: OtaStatusString.NORMAL_STATE,
                boot_key: OtaBootStatusString.SWITCH_BOOT,
                finalize_key: "_finalize_update",
            },
            failure_key: {
                state_key: OtaStatusString.UPDATE_FAIL_STATE,
                boot_key: OtaBootStatusString.SWITCH_BOOT_FAIL,
            },
        },
        OtaStatusString.ROLLBACKA_STATE: {
            status_checker_key: "_confirm_banka",
            success_key: {
                state_key: OtaStatusString.NORMAL_STATE,
                boot_key: OtaBootStatusString.ROLLBACK_BOOT,
                finalize_key: "_finalize_rollback",
            },
            failure_key: {
                state_key: OtaStatusString.ROLLBACK_FAIL_STATE,
                boot_key: OtaBootStatusString.ROLLBACK_BOOT_FAIL,
            },
        },
        OtaStatusString.ROLLBACKB_STATE: {
            status_checker_key: "_confirm_bankb",
            success_key: {
                state_key: OtaStatusString.NORMAL_STATE,
                boot_key: OtaBootStatusString.ROLLBACK_BOOT,
                finalize_key: "_finalize_rollback",
            },
            failure_key: {
                state_key: OtaStatusString.ROLLBACK_FAIL_STATE,
                boot_key: OtaBootStatusString.ROLLBACK_BOOT_FAIL,
            },
        },
        OtaStatusString.UPDATE_STATE: {
            success_key: {
                state_key: OtaStatusString.UPDATE_FAIL_STATE,
                boot_key: OtaBootStatusString.UPDATE_INCOMPLETE,
            },
        },
        OtaStatusString.PREPARED_STATE: {
            success_key: {
                state_key: OtaStatusString.UPDATE_FAIL_STATE,
                boot_key: OtaBootStatusString.UPDATE_INCOMPLETE,
            },
        },
        OtaStatusString.ROLLBACK_STATE: {
            success_key: {
                state_key: OtaStatusString.ROLLBACK_FAIL_STATE,
                boot_key: OtaBootStatusString.ROLLBACK_INCOMPLETE,
            },
        },
        OtaStatusString.UPDATE_FAIL_STATE: {
            success_key: {
                state_key: OtaStatusString.NORMAL_STATE,
                boot_key: OtaBootStatusString.NORMAL_BOOT,
            },
        },
    }