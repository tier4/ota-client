#!/usr/bin/env python3


PRIVATE_PEM = """\
-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEAwT35Np4Mo1Zxl4UqD5vCCtjWFsGi5Hx6Jsa62K0uqOQ3PCgR
qRgCnZXXln2zLVNNbQM4p/Q6yWNyRiYFRPF/O6msZaodhlQE3i32ffZBk8x9Rfq+
5FHYwanCrbXMJ4FrJ/0RQ8NF06bQQ99jxDQ8Op2JzAp9Bb6qvo87V7KS6Pq4p0cG
OAuUKi6lceoIUHF+An111Focxqg7NOX6tx4R6uX3OPSY+PaPc0wrld1/0rlDsMlZ
efNPB1DoiVZr5y2+e3jeYWhEUIhNg36EO69Fmh4EX9CY23ZJieP+DYfl89VnpQLc
O2pIbwrV0+RnO6QNIV6lP6wX/ReefX1RDbmUOQIDAQABAoIBAAcDw+cqp5TKT4dL
izJT2iBgrKzYzJv0tM5xiljROI9i8WpangGAYJ/tz4yj9XwguP/LDSRzpiqnVH+d
Y7WT+qVkzGBIY5f7ts2F55u887Z0xttidcl9+xtUmT+LCOUqOoSSGC1YilFbjdXY
5xs5NjJ+g4EHpHAv8qzGMEZHsjOIzkLTjjYw6RJ6yI7r6kcUyjAdTLxQzg9K2FBE
ohxLA1tjK2akvrrTmvgtAmhqfJrYFo8QINUOyOVdX97rrWpWxPpZqbXzNcenm9Pv
Ekh0RDrug6YDSVpQVsYgZeS9ZXPp92OroLUKIOdJ08p6VDFoD4RAcespidiN1Th4
N2LcwP0CgYEA4d9F5z0mn4DstBiCrnkSbOy4RKxt0EzTqBU0yVtXhBR1ZgdGqz65
TqunmvUYyLGYByWzwEFhaJy2+PVIeNOHaIVaXN7FrQ3dpYVniRqvMCz4ntM8U5HM
32TVNTbUciVJDwPJnq+Y/T61JAjsoYSw1wp7HG7GhpMuVkjwX3xj4mcCgYEA2wR/
/K4oEy4PyxKL6pMj9fpio5W7npINbyukdEx7X6FwlwZnulY8MuwERgJ88DvZhqtK
+8Ulwj5z7L+vN9YvgI+2+u8c3OXCRzHAp+QkfT85kIkkiLGJcg7Dq/weEkfHEVMZ
Ie04fwLYbhMNb98rEXgQLGw73nOF4FStrAtY8F8CgYEAx0MJkC5KXHyIVXkqEHGO
57kN9seHOTQNpULQBrMmScciqpfQqFH1eInGmtWOv76st+Fy6jDDll5qrMb24GD8
HCFIzpVZHooU92jxJer8kiuaScNgfPkrHAkAbqmoUerCwRQ+UlfnR8KCWv/kgbll
qM/+O98eFKrTPhuqsxIxrBECgYAAvRIlavztm6EoAScBon9ji/WbMZ0RWtK6xj3m
un9MAkJb8ASXh0TqswsMpWOAd+My5g75rF+FOSqw6LCRkqJUX6exTu8c+5VdIhjR
OB67YWQzTZMW9upMvSoBwXbkfuN39nzGNYOUQhEyxdOsxebiRzJew/qrtF4GNKLl
SqCBJwKBgE5NdEBVYVSVeCr9h7T6nd/3LtlTY0wXHqkPQBM15VOa7sJcJ+oI3x70
NpG11oEodn/52Qkm/Dvq5ktiXLB9/piAMfOW6KQb7PdHJwoS9OaSQcDDPwXibimy
Z30FeKdFkeC9U7EfR3LAD69gLrY6yLhH6ve0yQqw+l9JTwoSwKlQ
-----END RSA PRIVATE KEY-----
"""

POLICY_JSON = """\
{
  "Statement": [{
    "Resource": "https://abcdef.ghijkl.mno/*",
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
- ecu_name: 'Logging ECU'
  ecu_type: 'log'
  ecu_id: '3'
  version: '0.3.0'
  independent: True
  ip_addr: ''
"""
