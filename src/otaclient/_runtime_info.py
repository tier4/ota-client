import sys

RUN_AS_PYINSTALLER_BUNDLE = getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
