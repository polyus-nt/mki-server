import os
import contextlib
import io
import unittest
from fastapi.testclient import TestClient
from hub_service.app import create_app
from hub_service.settings import Settings
from test_api import ChainPort, console

class ReactorTests(unittest.TestCase):
    def test_full_api(self):
        with TestClient(create_app(Settings("fake"),ChainPort)) as c:
            self.assertEqual(c.get("/api/v1/capabilities").json()["reactor"]["node"],17)
            self.assertEqual(c.get("/api/v1/reactor/temperature").status_code,200)
            for idx in range(3):
                root=f"/api/v1/reactor/motors/{idx}"
                self.assertEqual(c.post(root+"/microstep",json={"divisor":8}).status_code,200)
                self.assertEqual(c.post(root+"/rpm",json={"rate":60}).status_code,200)
                state=c.get(root+"/state").json()
                self.assertEqual(state["target_sps"],1600)
                self.assertEqual(state["target_rpm"],60)
                self.assertFalse(state["measured"])
                self.assertEqual(c.post(root+"/microstep",json={"divisor":4}).status_code,503)
                self.assertEqual(c.post(root+"/enable",json={"enabled":False}).status_code,503)
                self.assertEqual(c.post(root+"/sps",json={"rate":-1234.125}).status_code,200)
                self.assertEqual(c.get(root+"/state").json()["target_sps"],-1234.125)
                self.assertEqual(c.post(root+"/rpm",json={"rate":300}).status_code,422)
                self.assertEqual(c.post(root+"/stop").status_code,200)
                self.assertEqual(c.post(root+"/enable",json={"enabled":True}).status_code,200)
                self.assertEqual(c.post(root+"/microstep",json={"divisor":16}).status_code,503)
                c.post(root+"/enable",json={"enabled":False})
                stm=f"/api/v1/reactor/stm/{idx}"
                self.assertEqual(c.post(stm+"/voltage/setpoint",json={"millivolts":3001}).json()["value"],3001)
                self.assertEqual(c.get(stm+"/voltage/setpoint").json()["value"],3001)
                self.assertEqual(c.post(stm+"/current/setpoint",json={"milliamps":12.345}).json()["value"],12.345)
                self.assertEqual(c.get(stm+"/current/setpoint").json()["value"],12.345)
                self.assertEqual(c.get(stm+"/voltage").status_code,200)
                self.assertEqual(c.get(stm+"/current").status_code,200)
            self.assertEqual(c.post("/api/v1/reactor/motors/stop").status_code,200)

    def test_validation_before_queue(self):
        app=create_app(Settings("fake"),ChainPort)
        with TestClient(app) as c:
            cases=[("/motors/0/sps",{"rate":x}) for x in (True,"100",7000.001,-7001,0.0001)]
            cases += [("/motors/0/microstep",{"divisor":x}) for x in (True,"8",2,64)]
            cases += [("/motors/0/enable",{"enabled":x}) for x in (0,1,"false")]
            cases += [("/stm/0/voltage/setpoint",{"millivolts":x}) for x in (True,1.5,6001,-1)]
            cases += [("/stm/0/current/setpoint",{"milliamps":x}) for x in (True,"1",50.001,-1,1.2345)]
            cases += [("/motors/3/sps",{"rate":100}),("/motors/0/sps",{"rate":100,"extra":1})]
            for path,body in cases:
                r=c.post("/api/v1/reactor"+path,json=body)
                self.assertEqual(r.status_code,422,(path,body,r.text))
            self.assertEqual(app.state.service.transport.requests,[])

    def test_console_and_read_only_selftest(self):
        class Client:
            def __init__(self,c): self.c=c
            def request(self,path,body=None):
                r=self.c.get(path) if body is None else self.c.post(path,json=body)
                return r.status_code==200,r.status_code,r.json(),0
            def get(self,path): return self.request(path)
        app=create_app(Settings("fake"),ChainPort)
        with TestClient(app) as c, contextlib.redirect_stdout(io.StringIO()):
            client=Client(c)
            for cmd in ("reactor_temp","motor 2","motor_micro 0 1/4","motor_rpm 0 60",
                        "motor_sps 1 -500.125","motor_stop 0","motor_stop",
                        "motor_enable 0 1","motor_enable 0 0",
                        "stm 2 set_u 2049","stm 2 set_i 12.345","stm 2 get_setted_u","stm 2 get_i"):
                self.assertTrue(console.execute(client,cmd),cmd)
            before=len(app.state.service.transport.requests)
            self.assertTrue(console.selftest(client))
            sent=app.state.service.transport.requests[before:]
            self.assertTrue(all(r.command not in (0x61,0x62,0x63,0x64,0x65,0x6a,0x70,0x71,0x32,0x42,0x51) for r in sent))
            for cmd in ("motor_micro 0 1/2","motor_rpm 3 1","motor_sps 0 nan","stm 0 set_i 50.001"):
                with self.assertRaises(ValueError): console.execute(client,cmd)

if __name__=="__main__": unittest.main()
