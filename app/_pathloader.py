"""Backward compatibility for directly running main.py under app."""


def _load_path():
    import sys
    from pathlib import Path

    # add otaclient project base folder to the sys.path
    project_dir = Path(__file__).absolute().parent.parent
    sys.path.insert(0, str(project_dir))


_load_path()

del _load_path
