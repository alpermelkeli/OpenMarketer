"""Routes whose request holds a secret: a refused request is never quoted back.

FastAPI's answer to a request it cannot validate (422) repeats what was sent:
``input`` holds the offending value or the whole body, ``ctx`` may hold more of
it, and the place of an unexpected field is the name the client gave it. On a
route that takes a secret that would return the secret, so here an error keeps
its type, its message and a place made only of names this API defines.

This is a route class, set on the router of such routes, so every other route
keeps FastAPI's answer. It does not log, bound or read the body.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel


class SecretBodyRoute(APIRoute):
    """A route whose validation errors say what is wrong without repeating the request."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()
        fields = self._body_fields()

        async def handle_without_quoting(request: Request) -> Response:
            try:
                return await handle(request)
            except RequestValidationError as invalid:
                unquoted = [_unquoted(error, fields) for error in invalid.errors()]
            # Raised once the original is handled, so that nothing leads back to it: it
            # holds the body.
            raise RequestValidationError(unquoted)

        return handle_without_quoting

    def _body_fields(self) -> frozenset[str]:
        """The names of the fields the route's body has; none for a route without a body."""
        model = self.body_field.field_info.annotation if self.body_field else None
        if isinstance(model, type) and issubclass(model, BaseModel):
            return frozenset(model.model_fields)
        return frozenset()


def _unquoted(error: dict[str, Any], body_fields: frozenset[str]) -> dict[str, Any]:
    return {
        "type": error["type"],
        "loc": _place(error["loc"], body_fields),
        "msg": error["msg"],
    }


def _place(loc: Sequence[str | int], body_fields: frozenset[str]) -> tuple[str | int, ...]:
    """Where the error is, as far as this API's own names go.

    In a body that is the field, if it is one the route defines: the name of a
    field the client made up, or a position in text that is not JSON, is left out.
    """
    if not loc or loc[0] != "body":
        return tuple(loc)
    if len(loc) > 1 and loc[1] in body_fields:
        return ("body", loc[1])
    return ("body",)
