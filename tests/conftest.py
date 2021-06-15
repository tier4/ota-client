import pytest
import sys
import os


@pytest.fixture(autouse=True)
def pythonpath():
    sys.path.append(
        os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + "/../app/")
    )
