from ._curses_utils import *


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
