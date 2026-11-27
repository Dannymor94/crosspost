"""Suppress validation input echo for routes carrying credentials."""

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request: Request):
            try:
                response = await handler(request)
            except RequestValidationError:
                response = JSONResponse({"detail": "Проверьте поля формы"}, status_code=422)
            except HTTPException as exc:
                response = JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
                )
            response.headers["Cache-Control"] = "no-store"
            return response

        return private_handler
