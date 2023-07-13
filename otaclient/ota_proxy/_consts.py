from multidict import istr
from .cache_control import OTAFileCacheControl

# uvicorn
REQ_TYPE_LIFESPAN = "lifespan"
REQ_TYPE_HTTP = "http"
RESP_TYPE_BODY = "http.response.body"
RESP_TYPE_START = "http.response.start"

METHOD_GET = "GET"

# headers
# for implementation convienience, we use lowercase for all headers.
HEADER_OTA_FILE_CACHE_CONTROL = istr(OTAFileCacheControl.HEADER_LOWERCASE)
HEADER_AUTHORIZATION = istr("authorization")
HEADER_COOKIE = istr("cookie")
HEADER_CONTENT_ENCODING = istr("content-encoding")
BHEADER_OTA_FILE_CACHE_CONTROL = OTAFileCacheControl.HEADER_LOWERCASE.encode("utf-8")
BHEADER_AUTHORIZATION = b"authorization"
BHEADER_COOKIE = b"cookie"
BHEADER_CONTENT_ENCODING = b"content-encoding"
