import pytest

from fpreporter import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "sub" / "fp.sqlite")
    yield c
    c.close()
