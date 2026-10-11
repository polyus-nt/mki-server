from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .errors import HubError
from .protocol import COMMANDS, PROBE, WRITE_COMMANDS
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


class GasMeasurement(BaseModel):
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
    lock_output_enabled: bool
    received_at: datetime


class RelayState(BaseModel):
    level: int
    received_at: datetime


class Acknowledgement(BaseModel):
    acknowledged: bool
    received_at: datetime


class PwmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pwm: StrictInt = Field(ge=0, le=255)


class LockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lock_state: StrictBool


class RelayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: StrictInt = Field(ge=0, le=10)


Index2 = Annotated[int, Path(ge=0, le=1)]


def create_app(settings: Settings | None = None, transport_factory=SerialTransport):
    @asynccontextmanager
    async def lifespan(app):
        configured = settings if settings is not None else Settings.from_env()
        service = HubService(configured, transport_factory)
        app.state.service = service
        try:
            await service.start()
            yield
        finally:
            await service.close()

    app = FastAPI(title="МС-ТЮК: теплица-химреактор", version="0.1.0", lifespan=lifespan,
                  description="Отдельный chemreactor API. Одна FIFO, без авторизации.")

    @app.exception_handler(HubError)
    async def hub_error_handler(request, error):
        return JSONResponse(error.as_dict(), status_code=error.http_status)

    async def read(request: Request, name: str, idx: int):
        return await request.app.state.service.read(COMMANDS[name], idx)

    @app.get("/health", tags=["service"])
    async def health(request: Request):
        service = request.app.state.service
        return JSONResponse(service.health(), status_code=200 if service.connected else 503)

    @app.get("/api/v1/capabilities", tags=["service"])
    async def capabilities():
        return {"greenhouse": "chemreactor", "protocol_version": 2,
                "channels": {name: {"count": item.count, "node": item.node,
                                     "command": f"0x{item.code:02X}"}
                             for name, item in COMMANDS.items() if name not in ("lock", "relay")},
                "lock": {"node": 16, "indexed": False, "physical_locks": 2},
                "relay_fan": {"node": 16, "indexed": False, "levels": [0, 10], "break_ms": 250},
                "actuator_routes": [{"gateway_node": 14, "global_indices": [0, 1], "local_indices": [0, 1]}],
                "admin_enabled": True, "authentication": "none"}

    @app.get("/api/v1/hub/probe", tags=["hub"])
    async def probe(request: Request):
        return await request.app.state.service.read(PROBE, 2)

    @app.get("/api/v1/temperature/{idx}", response_model=Measurement, tags=["sensors"])
    async def temperature(idx: Index2, request: Request):
        return await read(request, "temperature", idx)

    @app.get("/api/v1/pressure/{idx}", response_model=Measurement, tags=["sensors"])
    async def pressure(idx: Index2, request: Request):
        return await read(request, "pressure", idx)

    @app.get("/api/v1/humidity/{idx}", response_model=Measurement, tags=["sensors"])
    async def humidity(idx: Index2, request: Request):
        return await read(request, "humidity", idx)

    @app.get("/api/v1/gas/{idx}", response_model=GasMeasurement, tags=["sensors"])
    async def gas(idx: Index2, request: Request):
        return await read(request, "gas", idx)

    @app.get("/api/v1/doors/{idx}/state", response_model=DoorState, tags=["doors"])
    async def door(idx: Index2, request: Request):
        return await read(request, "door", idx)

    @app.get("/api/v1/fans/{idx}/pwm", response_model=Reading, tags=["fans"])
    async def pwm(idx: Index2, request: Request):
        return await read(request, "pwm", idx)

    @app.post("/api/v1/fans/{idx}/pwm", response_model=Reading, tags=["fans"])
    async def set_pwm(idx: Index2, body: PwmRequest, request: Request):
        return await request.app.state.service.read(WRITE_COMMANDS["pwm"], idx, bytes([body.pwm]) + bytes(7))

    @app.get("/api/v1/fans/{idx}/rpm", response_model=Reading, tags=["fans"])
    async def rpm(idx: Index2, request: Request):
        return await read(request, "rpm", idx)

    @app.get("/api/v1/lock/state", response_model=LockState, tags=["lmp8"])
    async def lock(request: Request):
        return await read(request, "lock", 0)

    @app.post("/api/v1/admin/lock", response_model=Acknowledgement, tags=["admin"])
    async def set_lock(body: LockRequest, request: Request):
        return await request.app.state.service.read(WRITE_COMMANDS["lock"], 0, bytes([body.lock_state]) + bytes(7))

    @app.get("/api/v1/relay-fan/level", response_model=RelayState, tags=["fans"])
    async def relay_level(request: Request):
        return await read(request, "relay", 0)

    @app.post("/api/v1/relay-fan/level", response_model=RelayState, tags=["fans"])
    async def set_relay_level(body: RelayRequest, request: Request):
        return await request.app.state.service.read(WRITE_COMMANDS["relay"], 0, bytes([body.level]) + bytes(7))

    return app


app = create_app()
