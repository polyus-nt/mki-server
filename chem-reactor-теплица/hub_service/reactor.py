"""Реактор: отдельные HTTP-методы поверх общей аппаратной FIFO."""
from decimal import Decimal
from typing import Annotated
from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator
from .reactor_protocol import READ, WRITE

Index3 = Annotated[int, Path(ge=0, le=2)]
router = APIRouter(prefix="/api/v1/reactor", tags=["reactor"])

class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Speed(Body):
    rate: Decimal = Field(ge=-7000, le=7000, decimal_places=3, allow_inf_nan=False)
    @field_validator("rate", mode="before")
    @classmethod
    def numeric(cls, v):
        if type(v) not in (int,float,Decimal):
            raise ValueError("Expected a JSON number")
        return v

class Enable(Body):
    enabled: StrictBool

class Micro(Body):
    divisor: StrictInt
    @field_validator("divisor")
    @classmethod
    def valid_divisor(cls,v):
        if v not in (4,8,16,32):
            raise ValueError("Expected 4,8,16,32")
        return v

class Voltage(Body):
    millivolts: StrictInt = Field(ge=0,le=6000)

class Current(Body):
    milliamps: Decimal = Field(ge=0,le=50,decimal_places=3,allow_inf_nan=False)
    @field_validator("milliamps", mode="before")
    @classmethod
    def numeric(cls,v):
        return Speed.numeric(v)

async def exchange(request, name, idx=0, data=bytes(8), write=False):
    return await request.app.state.service.read((WRITE if write else READ)[name],idx,data)

def integer(value, signed=False):
    return int(value).to_bytes(4,"big",signed=signed)+bytes(4)

@router.get("/temperature")
async def temperature(request: Request):
    return await exchange(request,"temperature")

@router.get("/motors/{idx}/state")
async def motor_state(idx: Index3, request: Request):
    # Три последовательных чтения, не атомарный снимок. Все проходят одну FIFO.
    state=await exchange(request,"motor",idx)
    config=await exchange(request,"config",idx)
    timing=await exchange(request,"timing",idx)
    factor=60/(config["full_steps_per_rev"]*config["microstep"])
    return {**state, **config, **timing, "current_rpm": state["current_sps"]*factor,
            "target_rpm": state["target_sps"]*factor, "measured": False}

@router.post("/motors/{idx}/sps")
async def set_sps(idx: Index3, body: Speed, request: Request):
    return await exchange(request,"sps",idx,integer(body.rate*1000,True),True)

@router.post("/motors/{idx}/rpm")
async def set_rpm(idx: Index3, body: Speed, request: Request):
    # Ограничение после пересчёта с текущим микрошагом проверяется самой Mega.
    return await exchange(request,"rpm",idx,integer(body.rate*1000,True),True)

@router.post("/motors/{idx}/stop")
async def stop(idx: Index3, request: Request):
    return await exchange(request,"stop",idx,write=True)

@router.post("/motors/stop")
async def stop_all(request: Request):
    return await exchange(request,"stop_all",write=True)

@router.post("/motors/{idx}/enable")
async def enable(idx: Index3, body: Enable, request: Request):
    return await exchange(request,"enable",idx,bytes([body.enabled])+bytes(7),True)

@router.post("/motors/{idx}/microstep")
async def micro(idx: Index3, body: Micro, request: Request):
    return await exchange(request,"micro",idx,bytes([body.divisor])+bytes(7),True)

@router.get("/stm/{idx}/voltage")
async def voltage(idx: Index3, request: Request):
    return await exchange(request,"voltage",idx)

@router.get("/stm/{idx}/current")
async def current(idx: Index3, request: Request):
    return await exchange(request,"current",idx)

@router.get("/stm/{idx}/voltage/setpoint")
async def voltage_setpoint(idx: Index3, request: Request):
    return await exchange(request,"voltage_setpoint",idx)

@router.get("/stm/{idx}/current/setpoint")
async def current_setpoint(idx: Index3, request: Request):
    return await exchange(request,"current_setpoint",idx)

@router.post("/stm/{idx}/voltage/setpoint")
async def set_voltage(idx: Index3, body: Voltage, request: Request):
    return await exchange(request,"voltage",idx,integer(body.millivolts),True)

@router.post("/stm/{idx}/current/setpoint")
async def set_current(idx: Index3, body: Current, request: Request):
    return await exchange(request,"current",idx,integer(body.milliamps*1000),True)
