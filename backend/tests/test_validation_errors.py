"""A 422 keeps the `{detail: string}` shape every client reads; FastAPI's
default sends a list, which the web showed as '[object Object]'."""
from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.main import validation_error_handler


class B(BaseModel):
    n: int = Field(le=5)


def make_client() -> TestClient:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    @app.post("/x")
    async def x(body: B, q: str = Query("ok", min_length=1)):
        return {"n": body.n}

    return TestClient(app)


def test_validation_error_detail_is_a_string():
    res = make_client().post("/x", json={"n": 99})

    assert res.status_code == 422
    body = res.json()
    assert isinstance(body["detail"], str)
    assert body["detail"].startswith("n: ")
    assert "errors" in body
    assert body["errors"][0]["loc"] == ["body", "n"]


def test_several_errors_are_joined():
    res = make_client().post("/x?q=", json={"n": "many"})

    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "n: " in detail and "q: " in detail and "; " in detail


def test_list_indexes_and_decode_positions_are_not_names():
    res = make_client().post(
        "/x", content=b"not json", headers={"content-type": "application/json"}
    )

    assert res.status_code == 422
    assert res.json()["detail"].startswith("body: ")


def test_the_app_registers_the_handler():
    from app.main import app

    assert app.exception_handlers[RequestValidationError] is validation_error_handler
