```mermaid
stateDiagram-v2
    [*] --> INITIALIZING : wake up / session started / session finished
    INITIALIZING --> PROCESSING_METADATA : metadata processing is started
    PROCESSING_METADATA --> CALCULATING_DELTA : delta calculation is started
    CALCULATING_DELTA --> DOWNLOADING_OTA_FILES : delta resource downloading is started
    PROCESSING_METADATA --> DOWNLOADING_OTA_FILES : partition-based payload or overlay, no delta calculation
    DOWNLOADING_OTA_FILES --> APPLYING_UPDATE : update applying is started
    APPLYING_UPDATE --> PROCESSING_POSTUPDATE : post update processing is started
    PROCESSING_POSTUPDATE --> FINALIZING_UPDATE : finalizing update is started
    PROCESSING_POSTUPDATE --> [*] : overlay: SUCCESS in the same session, no reboot (or a reboot of the same slot after SUCCESS, when a changed slot's apply is reboot)
    PROCESSING_METADATA --> DOWNLOADING_OTA_CLIENT : client downloading is started
