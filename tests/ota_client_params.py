#!/usr/bin/env python3


PRIVATE_PEM = """\
-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEAw7LzhjVP4FF5fSz2KsSophKXUvZ7gYfWi5QxEJN5CjyOhNot
W5UKr7KZ2vM1dGKuKJoDBH8dWyu1tUjJ8iXe4aiPSz6B/txu95E2jKZmgE1BddcW
auYKZ4Rg/C5u2z3BjDwTMyGQ4n9jvTD4vKWUpKcqeSeKar6BYey7s8krG/tkS0KP
0cszuOwQlDye2aTHswrTRfQHfzBQ5eXjb+yRUhRXKhqt6ju+rQQcVETbZ2B5gW2p
oPo1EHDF78vz6dlDgJ2llrOqXv8bveGn0YHxmG6+BCaGR+CYT7kD+LsfYsB27QC8
6jTDBG5gDjvQFKhaW24qCQ8egFC1WK8MFrScfwIDAQABAoIBAQCOhFECyOpdsxfl
iAvRw5wKLXnUajoxb6pXxdkheDRqtwUXTQhBLL42i7uuDvY7xu2MFfTinmvTjHZt
ChA/b0LsVWPdaS0kXIXXdwIh1cDJ6PkmBziFKvgiwO4cWPhUY5oDNXDKCMTJnfoV
uoc6Yt9oNXEiACC2cfwpQ0zCtlrVqaz8ZO2U8raYyNobOm+H7Z8MQMpxgJjgIBNs
hsZLyapAbT3eGdfm/hA20XhDUBKfqUKrxOrphbh/laVtdnGixC/RFcvVg/0ElV41
noQfQ2FRTMESvJtXIaWVg/ad9LbqEbKEcGpFTgl/0F/x7z8z+C+jA+jEBs3lkWVN
4aWjuYjZAoGBAOgaTs0BhHT9UWKcUItOeQvALv/H6Yji7COzbLNEtRkBhATOJxlQ
zSvPa3GhC32+B9QSxCu922/fHhp1F3qltUrzl+yGY3Ud+JGdUYzkCFfcV/JeHtdZ
nrSa6x5li+ToWejoXDhbFd/rXNdSGEOpLQx4YW8KaR5i7RBbYeVNfENVAoGBANfZ
IAjE+oAg97iofHTEQ0KWS7eshLlGtpxzYVHtAc9WT6dXj1+JJyGwcJv0wJgPHtJ+
ZXw+VZKRLelQJiIIbQM2dRkeIashzlEgMFytxBcl/Kku6VxybCcXYMTMBJjqQT2h
KOz+md7Ko5vfFq9vEAeQ3g40TiQi6DUF7psM9YiDAoGACzzC1fAd3qApIZIZ4DTD
bYs3e1DNgMAj2LKmL6PC9Nv67VSh1frwhA99zzmR2duqe10RPGDrz+XIilVi9qAx
P9i9YUk8ZX42+63XNfAQa1iStXxTF/AR3AKoIYefF00clUcyt9PJIlc93nruC9CU
CseFSCAD6OG3QpR6D+UJgCUCgYEAowKRFM5eOGGtc8GarDyEZ0dGS6J4YcwroR/q
AYsycLlIUqLn3kigSusLQFypDq1so59dWWViDtyhhbBH/C/M1D5OVPfSiYFwZQgg
Pf3lN24y0DpjdrPbRfJ73GQPnMRdHQQW+lSVNBJpWRAz+62ut2gKq0OJN/U81L50
Ipi43a0CgYAoFtDRcZsif+mG9c7d6GwcSjkQZjebCJsfCmypKhIPD44mSDX4mTxv
+Xz/Ptasz5yougeH1lWcpLaEuYSfPQEOT0NGZkkeQ9QB/InctHAYvJQCl2Qo2QLU
9sKuv2yuyAFhW/YtpHfveh9BemY857a4hTlYW0Ngg0oJTaMxWZsF3Q==
-----END RSA PRIVATE KEY-----
"""

POLICY_JSON = """\
{
  "Statement": [{
    "Resource": "https://d30joxnqr7qetz.cloudfront.net/*",
    "Condition": {
      "DateLessThan": {
        "AWS:EpochTime": 1640698309
      }
    }
  }]
}
"""

ECU_INFO = """\
main_ecu:
  ecu_name: 'Autoware ECU' 
  ecu_type: 'autoware'
  ecu_id: '1'
  version: '0.1.0'
  independent: True
  ip_addr: ''
sub_ecus:
- ecu_name: 'Perception ECU'
  ecu_type: 'perception'
  ecu_id: '2'
  version: '0.2.0'
  independent: True
  ip_addr: ''
- ecu_name: 'Loging ECU'
  ecu_type: 'log'
  ecu_id: '3'
  version: '0.3.0'
  independent: True
  ip_addr: ''
"""
