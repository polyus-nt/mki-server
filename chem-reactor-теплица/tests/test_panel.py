import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from hub_service.app import create_app
from hub_service.settings import Settings
from test_api import ChainPort

ROOT=Path(__file__).resolve().parents[1]

class PanelTests(unittest.TestCase):
    def test_static_panel_does_not_call_hardware_or_change_openapi(self):
        app=create_app(Settings("fake"),ChainPort)
        with TestClient(app) as c:
            r=c.get("/panel")
            self.assertEqual(r.status_code,200)
            self.assertIn("text/html",r.headers["content-type"])
            self.assertIn("connect-src 'self'",r.headers["content-security-policy"])
            self.assertIn("frame-ancestors 'none'",r.headers["content-security-policy"])
            self.assertIn('id="allow-writes" type="checkbox"',r.text)
            self.assertNotIn('id="allow-writes" type="checkbox" checked',r.text)
            self.assertIn('id="stop-all"',r.text)
            self.assertIn("Как проверить устройство",r.text)
            for name in ("panel.js","panel-core.js","panel.css"):
                asset=c.get("/panel/assets/"+name)
                self.assertEqual(asset.status_code,200)
                self.assertTrue(asset.content)
            self.assertEqual(app.state.service.transport.requests,[])
            self.assertEqual(len(c.get("/openapi.json").json()["paths"]),26)
            self.assertNotIn("/panel",c.get("/openapi.json").json()["paths"])

    def test_no_remote_assets_and_no_com_access(self):
        js=(ROOT/"hub_service/static/panel.js").read_text(encoding="utf-8")
        html=(ROOT/"hub_service/static/panel.html").read_text(encoding="utf-8")
        self.assertNotIn("navigator.serial",js)
        self.assertNotIn("setInterval(",js)
        self.assertNotIn("https://",html)
        self.assertNotIn("onclick=",html)
        self.assertIn('api("/health")',js)

if __name__=="__main__": unittest.main()
