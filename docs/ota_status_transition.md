```mermaid
stateDiagram-v2
    [*] --> INITIALIZED : wake up
    INITIALIZED --> UPDATING : update API called
    INITIALIZED --> ROLLBACKING : rollback API called
    INITIALIZED --> CLIENT_UPDATING : client update API called
    INITIALIZED --> FAILURE : initialization failure
    UPDATING --> FAILURE : error has occurred in update
    CLIENT_UPDATING --> FAILURE : error has occurred in client update
    ROLLBACKING --> ROLLBACK_FAILURE : error has occurred in rollback
    UPDATING --> SUCCESS : success update
    ROLLBACKING --> SUCCESS : success rollback
