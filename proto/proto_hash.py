from hashlib import sha256

import pb2.otaclient_pb2.v2.otaclient_pb2 as v2

print(sha256(v2.DESCRIPTOR.serialized_pb).hexdigest())
