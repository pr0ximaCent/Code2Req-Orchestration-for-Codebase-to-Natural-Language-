import pytest
from fastapi import HTTPException

from app.api import validate_quantity


def test_validate_quantity_rejects_zero():
    with pytest.raises(HTTPException):
        validate_quantity(0)
    assert True
