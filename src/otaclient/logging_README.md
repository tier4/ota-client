# Protocol Documentation

## Table of Contents

- [Protocol Documentation](#protocol-documentation)
  - [Table of Contents](#table-of-contents)
  - [proto/otaclient\_iot\_logging\_server\_v1.proto](#protootaclient_iot_logging_server_v1proto)
    - [PutLogRequest](#putlogrequest)
    - [PutLogResponse](#putlogresponse)
    - [ErrorCode](#errorcode)
    - [LogType](#logtype)
    - [OtaClientIoTLoggingService](#otaclientiotloggingservice)
  - [Scalar Value Types](#scalar-value-types)

## proto/otaclient_iot_logging_server_v1.proto

### PutLogRequest

| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| ecu_id | string |  | target ECU ID |
| log_type | [LogType](#logtype) |  | log type |
| message | string |  | log message |

### PutLogResponse

| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| code | [ErrorCode](#errorcode) |  | error code |
| message | string |  | error message |

### ErrorCode

| Name | Number | Description |
| ---- | ------ | ----------- |
| NO_FAILURE | 0 | Success |
| SERVER_QUEUE_FULL | 1 | Error: Server queue is full |
| NOT_ALLOWED_ECU_ID | 2 | Error: Specified ECU ID is not allowed |
| NO_MESSAGE | 3 | Error: No message in the request |

### LogType

| Name | Number | Description |
| ---- | ------ | ----------- |
| LOG | 0 |  |
| METRICS | 1 |  |

### OtaClientIoTLoggingService

| Method Name | Request Type | Response Type | Description |
| ----------- | ------------ | ------------- | ------------|
| PutLog | [.PutLogRequest](#putlogrequest) | [.PutLogResponse](#putlogresponse) | `PutLog` service requests OTA Client logging service to put log. |

## Scalar Value Types

| .proto Type | Notes | C++ | Java | Python | Go | C# | PHP | Ruby |
| ----------- | ----- | --- | ---- | ------ | -- | -- | --- | ---- |
| double |  | double | double | float | float64 | double | float | Float |
| float |  | float | float | float | float32 | float | float | Float |
| int32 | Uses variable-length encoding. Inefficient for encoding negative numbers – if your field is likely to have negative values, use sint32 instead. | int32 | int | int | int32 | int | integer | Bignum or Fixnum (as required) |
| int64 | Uses variable-length encoding. Inefficient for encoding negative numbers – if your field is likely to have negative values, use sint64 instead. | int64 | long | int/long | int64 | long | integer/string | Bignum |
| uint32 | Uses variable-length encoding. | uint32 | int | int/long | uint32 | uint | integer | Bignum or Fixnum (as required) |
| uint64 | Uses variable-length encoding. | uint64 | long | int/long | uint64 | ulong | integer/string | Bignum or Fixnum (as required) |
| sint32 | Uses variable-length encoding. Signed int value. These more efficiently encode negative numbers than regular int32s. | int32 | int | int | int32 | int | integer | Bignum or Fixnum (as required) |
| sint64 | Uses variable-length encoding. Signed int value. These more efficiently encode negative numbers than regular int64s. | int64 | long | int/long | int64 | long | integer/string | Bignum |
| fixed32 | Always four bytes. More efficient than uint32 if values are often greater than 2^28. | uint32 | int | int | uint32 | uint | integer | Bignum or Fixnum (as required) |
| fixed64 | Always eight bytes. More efficient than uint64 if values are often greater than 2^56. | uint64 | long | int/long | uint64 | ulong | integer/string | Bignum |
| sfixed32 | Always four bytes. | int32 | int | int | int32 | int | integer | Bignum or Fixnum (as required) |
| sfixed64 | Always eight bytes. | int64 | long | int/long | int64 | long | integer/string | Bignum |
| bool |  | bool | boolean | boolean | bool | bool | boolean | TrueClass/FalseClass |
| string | A string must always contain UTF-8 encoded or 7-bit ASCII text. | string | String | str/unicode | string | string | string | String (UTF-8) |
| bytes | May contain any arbitrary sequence of bytes. | string | ByteString | str | []byte | ByteString | string | String (ASCII-8BIT) |
