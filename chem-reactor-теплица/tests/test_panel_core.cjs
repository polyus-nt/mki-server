"use strict";
const assert = require("node:assert/strict");
const C = require("../hub_service/static/panel-core.js");
assert.equal(C.number("-1234.125", -7000, 7000), -1234.125);
assert.equal(C.number("0", 0, 6000, 0), 0);
assert.equal(C.number("50.000", 0, 50), 50);
for (const v of ["", "NaN", "Infinity", "1e3", "1,5", ".5", "1.", "0.0001", "7000.001"])
  assert.throws(() => C.number(v, -7000, 7000), undefined, v);
for (const v of ["1.5", "1.0", "-1", "6001"])
  assert.throws(() => C.number(v, 0, 6000, 0), undefined, v);
const schema = {paths:{
  "/health":{get:{}},
  "/api/v1/reactor/motors/{idx}/state":{get:{parameters:[{name:"idx",in:"path",schema:{type:"integer",minimum:0,maximum:2}}]}},
  "/api/v1/reactor/motors/stop":{post:{}}
}};
assert.deepEqual(C.expandReads(schema), ["/health", ...[0,1,2].map(i=>"/api/v1/reactor/motors/"+i+"/state")]);
assert.throws(() => C.expandReads({paths:{"/api/v1/a/{idx}":{get:{}}}}));
assert.throws(() => C.expandReads({paths:{"//outside/api":{get:{}}}}));
assert.throws(() => C.expandReads({paths:{"/api/v1/a":{get:{parameters:[{name:"q",in:"query",required:true}]}}}}));
assert.equal(C.success(200,{acknowledged:true}),true);
for (const [status, body] of [[503,{error:{message:"BUSY"}}],[200,{valid:false}],[200,null],[200,[]],[422,{detail:[]}],[302,{}]])
  assert.equal(C.success(status, body),false);
console.log("PASS: strict numeric inputs, bounded read-only API discovery, error handling");
