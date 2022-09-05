# NOTE: this file should only be loaded once by the program entry!

###### load path ######
def _path_load():
    import sys
    from pathlib import Path

    project_base = Path(__file__).absolute().parent.parent
    sys.path.extend([str(project_base), str(project_base / "app")])


_path_load()
######
