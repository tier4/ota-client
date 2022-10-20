# Protocol Documentation
<a name="top"></a>

## Table of Contents

- [otaclient_v2.proto](#otaclient_v2-proto)
    - [RollbackRequest](#OtaClientV2-RollbackRequest)
    - [RollbackRequestEcu](#OtaClientV2-RollbackRequestEcu)
    - [RollbackResponse](#OtaClientV2-RollbackResponse)
    - [RollbackResponseEcu](#OtaClientV2-RollbackResponseEcu)
    - [Status](#OtaClientV2-Status)
    - [StatusProgress](#OtaClientV2-StatusProgress)
    - [StatusRequest](#OtaClientV2-StatusRequest)
    - [StatusResponse](#OtaClientV2-StatusResponse)
    - [StatusResponseEcu](#OtaClientV2-StatusResponseEcu)
    - [UpdateRequest](#OtaClientV2-UpdateRequest)
    - [UpdateRequestEcu](#OtaClientV2-UpdateRequestEcu)
    - [UpdateResponse](#OtaClientV2-UpdateResponse)
    - [UpdateResponseEcu](#OtaClientV2-UpdateResponseEcu)
  
    - [FailureType](#OtaClientV2-FailureType)
    - [StatusOta](#OtaClientV2-StatusOta)
    - [StatusProgressPhase](#OtaClientV2-StatusProgressPhase)
  
    - [OtaClientService](#OtaClientV2-OtaClientService)
  
- [Scalar Value Types](#scalar-value-types)



<a name="otaclient_v2-proto"></a>
<p align="right"><a href="#top">Top</a></p>

## otaclient_v2.proto



<a name="OtaClientV2-RollbackRequest"></a>

### RollbackRequest



| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| ecu | [RollbackRequestEcu](#OtaClientV2-RollbackRequestEcu) | repeated |  |






<a name="OtaClientV2-RollbackRequestEcu"></a>

### RollbackRequestEcu
Request


| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| ecu_id | [string](#string) |  |  |






<a name="OtaClientV2-RollbackResponse"></a>

### RollbackResponse



| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| ecu | [RollbackResponseEcu](#OtaClientV2-RollbackResponseEcu) | repeated |  |






<a name="OtaClientV2-RollbackResponseEcu"></a>

### RollbackResponseEcu
Response


| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| ecu_id | [string](#string) |  |  |
| result | [FailureType](#OtaClientV2-FailureType) |  |  |






<a name="OtaClientV2-Status"></a>

### Status



| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| status | [StatusOta](#OtaClientV2-StatusOta) |  |  |
| failure | [FailureType](#OtaClientV2-FailureType) |  |  |
| failure_reason | [string](#string) |  | failure reason string |
| version | [string](#string) |  | current version string |
| progress | [StatusProgress](#OtaClientV2-StatusProgress) |  | status is UPDATING, this field is valid. |






<a name="OtaClientV2-StatusProgress"></a>

### StatusProgress



| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| phase | [StatusProgressPhase](#OtaClientV2-StatusProgressPhase) |  |  |
| total_regular_files | [uint64](#uint64) |  | number of total regular file |
| regular_files_processed | [uint64](#uint64) |  | number of the regular file processed |
| files_processed_copy | [uint64](#uint64) |  | number of the regular file processed by copy |
| files_processed_link | [uint64](#uint64) |  | number of the regular file processed by hard-link |
| files_processed_download | [uint64](#uint64) |  | number of the regular file processed by download |
| file_size_processed_copy | [uint64](#uint64) |  | total file size of the regular file processed by copy |
| file_size_processed_link | [uint64](#uint64) |  | total file size of the regular file processed by hard-link |
| file_size_processed_download | [uint64](#uint64) |  | total file size of the regular file processed by download |
| elapsed_time_copy | [google.protobuf.Duration](#google-protobuf-Duration) |  | total elapsed time by copy |
| elapsed_time_link | [google.protobuf.Duration](#google-protobuf-Duration) |  | total elapsed time by hard-link |
| elapsed_time_download | [google.protobuf.Duration](#google-protobuf-Duration) |  | total elapsed time by download |
| errors_download | [uint64](#uint64) |  | total number of download error |
| total_regular_file_size | [uint64](#uint64) |  | total regular file size |
| total_elapsed_time | [google.protobuf.Duration](#google-protobuf-Duration) |  | total elapsed time |
| download_bytes | [uint64](#uint64) |  | data transfer volume during the whole OTA update process |






<a name="OtaClientV2-StatusRequest"></a>

### StatusRequest
Status

Request






<a name="OtaClientV2-StatusResponse"></a>

### StatusResponse



| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| ecu | [StatusResponseEcu](#OtaClientV2-StatusResponseEcu) | repeated |  |
| available_ecu_ids | [string](#string) | repeated | list of available ECU (see ecu_info.yml) |






<a name="OtaClientV2-StatusResponseEcu"></a>

### StatusResponseEcu



| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| ecu_id | [string](#string) |  | ECU id respond |
| result | [FailureType](#OtaClientV2-FailureType) |  |  |
| status | [Status](#OtaClientV2-Status) |  |  |






<a name="OtaClientV2-UpdateRequest"></a>

### UpdateRequest



| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| ecu | [UpdateRequestEcu](#OtaClientV2-UpdateRequestEcu) | repeated |  |






<a name="OtaClientV2-UpdateRequestEcu"></a>

### UpdateRequestEcu
Request


| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| ecu_id | [string](#string) |  | ECU id to update. OTA client pickups the entry that matches their own ECU id and this ECU id. |
| version | [string](#string) |  | version to update. Any version string can be used. When the update is done successfully, the OTA client saves this version as the current version. |
| url | [string](#string) |  | OTA server URL. |
| cookies | [string](#string) |  | cookie entries with JSON notation. |






<a name="OtaClientV2-UpdateResponse"></a>

### UpdateResponse



| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| ecu | [UpdateResponseEcu](#OtaClientV2-UpdateResponseEcu) | repeated |  |






<a name="OtaClientV2-UpdateResponseEcu"></a>

### UpdateResponseEcu
Response


| Field | Type | Label | Description |
| ----- | ---- | ----- | ----------- |
| ecu_id | [string](#string) |  | EUC id responded |
| result | [FailureType](#OtaClientV2-FailureType) |  | result |





 


<a name="OtaClientV2-FailureType"></a>

### FailureType
Common

| Name | Number | Description |
| ---- | ------ | ----------- |
| NO_FAILURE | 0 |  |
| RECOVERABLE | 1 |  |
| UNRECOVERABLE | 2 |  |



<a name="OtaClientV2-StatusOta"></a>

### StatusOta
Response

| Name | Number | Description |
| ---- | ------ | ----------- |
| INITIALIZED | 0 |  |
| SUCCESS | 1 |  |
| FAILURE | 2 |  |
| UPDATING | 3 |  |
| ROLLBACKING | 4 |  |
| ROLLBACK_FAILURE | 5 |  |



<a name="OtaClientV2-StatusProgressPhase"></a>

### StatusProgressPhase


| Name | Number | Description |
| ---- | ------ | ----------- |
| INITIAL | 0 |  |
| METADATA | 1 |  |
| DIRECTORY | 2 |  |
| SYMLINK | 3 |  |
| REGULAR | 4 |  |
| PERSISTENT | 5 |  |
| POST_PROCESSING | 6 |  |


 

 


<a name="OtaClientV2-OtaClientService"></a>

### OtaClientService
The OTA Client service definition.
Style Guide: https://developers.google.com/protocol-buffers/docs/style#message_and_field_names

| Method Name | Request Type | Response Type | Description |
| ----------- | ------------ | ------------- | ------------|
| Update | [UpdateRequest](#OtaClientV2-UpdateRequest) | [UpdateResponse](#OtaClientV2-UpdateResponse) | `Update` service requests OTA client to start updating. The OTA client of each ECU retrieves the request that matches its own ECU id and starts it. Requests to each ECU included in the `UpdateRequest` are handled by that respective ECU and returns the response to the parent ECU. Main ECU merges the responses as UpdateResponse. After requesting `Update` and if the OTA status is `UPDATING`, the request is successful. Note that if the child ECU doesn&#39;t respond, the grandchild response is not included by `UpdateResponse`. |
| Rollback | [RollbackRequest](#OtaClientV2-RollbackRequest) | [RollbackResponse](#OtaClientV2-RollbackResponse) | NOT YET |
| Status | [StatusRequest](#OtaClientV2-StatusRequest) | [StatusResponse](#OtaClientV2-StatusResponse) | `Status` service requests OTA client to retrieve OTA client status. Note that if the child ECU doesn&#39;t respond, the grandchild response is not contained by `StatusResponse`. |

 



## Scalar Value Types

| .proto Type | Notes | C++ | Java | Python | Go | C# | PHP | Ruby |
| ----------- | ----- | --- | ---- | ------ | -- | -- | --- | ---- |
| <a name="double" /> double |  | double | double | float | float64 | double | float | Float |
| <a name="float" /> float |  | float | float | float | float32 | float | float | Float |
| <a name="int32" /> int32 | Uses variable-length encoding. Inefficient for encoding negative numbers – if your field is likely to have negative values, use sint32 instead. | int32 | int | int | int32 | int | integer | Bignum or Fixnum (as required) |
| <a name="int64" /> int64 | Uses variable-length encoding. Inefficient for encoding negative numbers – if your field is likely to have negative values, use sint64 instead. | int64 | long | int/long | int64 | long | integer/string | Bignum |
| <a name="uint32" /> uint32 | Uses variable-length encoding. | uint32 | int | int/long | uint32 | uint | integer | Bignum or Fixnum (as required) |
| <a name="uint64" /> uint64 | Uses variable-length encoding. | uint64 | long | int/long | uint64 | ulong | integer/string | Bignum or Fixnum (as required) |
| <a name="sint32" /> sint32 | Uses variable-length encoding. Signed int value. These more efficiently encode negative numbers than regular int32s. | int32 | int | int | int32 | int | integer | Bignum or Fixnum (as required) |
| <a name="sint64" /> sint64 | Uses variable-length encoding. Signed int value. These more efficiently encode negative numbers than regular int64s. | int64 | long | int/long | int64 | long | integer/string | Bignum |
| <a name="fixed32" /> fixed32 | Always four bytes. More efficient than uint32 if values are often greater than 2^28. | uint32 | int | int | uint32 | uint | integer | Bignum or Fixnum (as required) |
| <a name="fixed64" /> fixed64 | Always eight bytes. More efficient than uint64 if values are often greater than 2^56. | uint64 | long | int/long | uint64 | ulong | integer/string | Bignum |
| <a name="sfixed32" /> sfixed32 | Always four bytes. | int32 | int | int | int32 | int | integer | Bignum or Fixnum (as required) |
| <a name="sfixed64" /> sfixed64 | Always eight bytes. | int64 | long | int/long | int64 | long | integer/string | Bignum |
| <a name="bool" /> bool |  | bool | boolean | boolean | bool | bool | boolean | TrueClass/FalseClass |
| <a name="string" /> string | A string must always contain UTF-8 encoded or 7-bit ASCII text. | string | String | str/unicode | string | string | string | String (UTF-8) |
| <a name="bytes" /> bytes | May contain any arbitrary sequence of bytes. | string | ByteString | str | []byte | ByteString | string | String (ASCII-8BIT) |

