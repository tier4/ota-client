```mermaid
stateDiagram-v2
    [*] --> INITIALIZING : update or client update API called
    INITIALIZING --> PROCESSING_METADATA : metadata processing is started
    PROCESSING_METADATA --> CALCULATING_DELTA : delta calculation is started
    CALCULATING_DELTA --> DOWNLOADING_OTA_FILES : delta resource downloading is started
    DOWNLOADING_OTA_FILES --> APPLYING_UPDATE : update applying is started
    APPLYING_UPDATE --> PROCESSING_POSTUPDATE : post update processing is started
    PROCESSING_POSTUPDATE --> FINALIZING_UPDATE : finalizing update is started
    PROCESSING_METADATA --> DOWNLOADING_OTA_CLIENT : client downloading is started
