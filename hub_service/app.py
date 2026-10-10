from contextlib import asynccontextmanager
from datetime import datetime
import hmac
from typing import Annotated

from fastapi import Depends, FastAPI, Path, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .errors import HubError
from .protocol import COMMANDS, PROBE
from .service import HubService
from .settings import Settings
from .transport import SerialTransport


class Reading(BaseModel):
    idx: int
    value: int | float
    unit: str
    received_at: datetime


class Measurement(Reading):
    valid: bool
    fresh: bool
    age_ms: int


class DustMeasurement(BaseModel):
    idx: int
    adc: int
    voltage_mv: int
    voltage_v: float
    valid: bool
    fresh: bool
    age_ms: int
    received_at: datetime


class DoorState(BaseModel):
    idx: int
    closed: bool
    received_at: datetime


class LockState(BaseModel):
    idx: int
    lock_output_enabled: bool
    received_at: datetime


class VacuumState(BaseModel):
    enabled: bool
    received_at: datetime


Index3 = Annotated[int, Path(ge=0, le=2)]
Index2 = Annotated[int, Path(ge=0, le=1)]
Index6 = Annotated[int, Path(ge=0, le=5)]


def create_app(settings: Settings | None = None, transport_factory=SerialTransport):
    @asynccontextmanager
    async def lifespan(app):
        # Импорт модуля не открывает COM-порт. Ровно один экземпляр на lifespan.
        configured = settings if settings is not None else Settings.from_env()
        service = HubService(configured, transport_factory)
        app.state.service = service
        try:
            await service.start()
            yield
        finally:
            await service.close()

    bearer = HTTPBearer(auto_error=False)

    async def authorize(request: Request,
                        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        token = request.app.state.service.settings.api_token
        if token and (credentials is None or not hmac.compare_digest(credentials.credentials.encode(), token.encode())):
            raise HubError("UNAUTHORIZED", "Нужен Bearer token", 401)

    app = FastAPI(title="МС-ТЮК Hub API", version="0.1.0", lifespan=lifespan,
                  dependencies=[Depends(authorize)],
                  description="Read-only API фаблаб-теплицы. Запросы ждут FIFO и ответа оборудования.")

    @app.exception_handler(HubError)
    async def hub_error_handler(request, error):
        headers = {"WWW-Authenticate": "Bearer"} if error.http_status == 401 else None
        return JSONResponse(error.as_dict(), status_code=error.http_status, headers=headers)

    async def read(request: Request, name: str, idx: int):
        return await request.app.state.service.read(COMMANDS[name], idx)

    @app.get("/health", tags=["service"])
    async def health(request: Request):
        service = request.app.state.service
        return JSONResponse(service.health(), status_code=200 if service.connected else 503)

    @app.get("/api/v1/capabilities", tags=["service"])
    async def capabilities():
        return {"greenhouse": "fablab", "protocol_version": 2, "access": "read-only",
                "channels": {name: {"count": item.count, "node": item.node,
                                    "command": f"0x{item.code:02X}"} for name, item in COMMANDS.items() if name != "vacuum"},
                "vacuum": {"node": 16, "command": "0x52", "indexed": False},
                "chemreactor": "not_implemented"}

    @app.get("/api/v1/hub/probe", tags=["hub"])
    async def probe(request: Request):
        # Неверный индекс обрабатывается самим хабом без обращения к шлюзу.
        return await request.app.state.service.read(PROBE, 3)

    @app.get("/api/v1/temperature/{idx}", response_model=Measurement, tags=["sensors"])
    async def temperature(idx: Index3, request: Request):
        return await read(request, "temperature", idx)

    @app.get("/api/v1/pressure/{idx}", response_model=Measurement, tags=["sensors"])
    async def pressure(idx: Index3, request: Request):
        return await read(request, "pressure", idx)

    @app.get("/api/v1/voc/{idx}", response_model=Measurement, tags=["sensors"])
    async def voc(idx: Index2, request: Request):
        return await read(request, "voc", idx)

    @app.get("/api/v1/humidity/{idx}", response_model=Measurement, tags=["sensors"])
    async def humidity(idx: Index3, request: Request):
        return await read(request, "humidity", idx)

    @app.get("/api/v1/dust/{idx}", response_model=DustMeasurement, tags=["sensors"])
    async def dust(idx: Index2, request: Request):
        return await read(request, "dust", idx)

    @app.get("/api/v1/doors/{idx}/state", response_model=DoorState, tags=["doors"])
    async def door(idx: Index2, request: Request):
        return await read(request, "door", idx)

    @app.get("/api/v1/fans/{idx}/pwm", response_model=Reading, tags=["fans"])
    async def pwm(idx: Index6, request: Request):
        return await read(request, "pwm", idx)

    @app.get("/api/v1/fans/{idx}/rpm", response_model=Reading, tags=["fans"])
    async def rpm(idx: Index6, request: Request):
        return await read(request, "rpm", idx)

    @app.get("/api/v1/locks/{idx}/state", response_model=LockState, tags=["lmp8"])
    async def locks(idx: Index2, request: Request):
        # Два независимых выхода. Это не состояние концевиков.
        return await read(request, "lock", idx)

    @app.get("/api/v1/vacuum/state", response_model=VacuumState, tags=["lmp8"])
    async def vacuum(request: Request):
        return await read(request, "vacuum", 0)

    return app


app = create_app()
