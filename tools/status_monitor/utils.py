from typing import List


class FormatValue:
    KB = 1000
    MB = 1000**2
    GB = 1000**3

    @classmethod
    def bytes_count(cls, bytes_count: int) -> str:
        if bytes_count > cls.GB:
            return f"{bytes_count / cls.GB:,.2f}GB"
        elif bytes_count > cls.MB:
            return f"{bytes_count / cls.MB:,.2f}MB"
        elif bytes_count > cls.KB:
            return f"{bytes_count / cls.KB:,.2f}KB"
        return f"{bytes_count:,}B"

    @classmethod
    def count(cls, count: int) -> str:
        return f"{count:,}"


def splitline_break_long_string(_str: str, length: int) -> List[str]:
    # first split the line
    _input = _str.splitlines()
    _output = []
    # search through all lines and break up long line
    for line in _input:
        _cuts = len(line) // length + 1
        for _cut in range(_cuts):
            _output.append(line[_cut * length : (_cut + 1) * length])
    return _output
