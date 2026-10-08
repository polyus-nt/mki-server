//! Loopback MCP exposes only UserApi. Administrative calls have no tools or capability.
use crate::{
    database::Database,
    serial::{Command, UserApi},
    state::SharedState,
};
use axum::{
    Json, Router,
    extract::State,
    http::{HeaderMap, StatusCode},
    routing::post,
};
use serde_json::{Value, json};
use std::net::SocketAddr;
#[derive(Clone)]
struct ServerState {
    token: String,
    user: UserApi,
    system: SharedState,
    database: Database,
    allow_write: bool,
}
pub struct McpServer {
    pub token: String,
}
pub fn start(system: SharedState, database: Database, user: UserApi) -> McpServer {
    let token = std::env::var("TJC_MCP_TOKEN")
        .unwrap_or_else(|_| format!("{:032x}", rand::random::<u128>()));
    let state = ServerState {
        token: token.clone(),
        user,
        system,
        database,
        allow_write: std::env::var("TJC_AGENT_PHASE").as_deref() == Ok("full"),
    };
    std::thread::spawn(move || {
        let rt = tokio::runtime::Runtime::new().expect("MCP runtime");
        rt.block_on(async move {
            let app = Router::new().route("/mcp", post(handle)).with_state(state);
            match tokio::net::TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], 8765))).await {
                Ok(listener) => {
                    if let Err(e) = axum::serve(listener, app).await {
                        eprintln!("MCP: {e}");
                    }
                }
                Err(e) => eprintln!("MCP bind: {e}"),
            }
        });
    });
    McpServer { token }
}
async fn handle(
    State(state): State<ServerState>,
    headers: HeaderMap,
    Json(req): Json<Value>,
) -> Result<Json<Value>, StatusCode> {
    if headers.get("authorization").and_then(|v| v.to_str().ok())
        != Some(format!("Bearer {}", state.token).as_str())
    {
        return Err(StatusCode::UNAUTHORIZED);
    }
    let id = req.get("id").cloned().unwrap_or(Value::Null);
    let method = req["method"].as_str().unwrap_or("");
    let result = match method {
        "initialize" => {
            json!({"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"mstjuk_hardware","version":env!("CARGO_PKG_VERSION")}})
        }
        "tools/list" => json!({"tools":tool_catalog()}),
        "tools/call" => call_tool(&state, req.get("params").unwrap_or(&Value::Null)).await,
        _ => {
            return Ok(Json(
                json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message":"method not found"}}),
            ));
        }
    };
    Ok(Json(json!({"jsonrpc":"2.0","id":id,"result":result})))
}
fn tool_catalog() -> Vec<Value> {
    let mut tools = Vec::new();
    for (name, max) in [
        ("get_temperature", 2),
        ("get_voc", 1),
        ("get_dust", 1),
        ("get_door_state", 1),
        ("get_pressure", 2),
        ("get_humidity", 2),
        ("get_fan_pwm", 5),
        ("get_fan_rpm", 5),
    ] {
        let description = if name == "get_dust" {
            "GP2Y1010AU0F raw ADC 0..1023 and calculated voltage in V; NOT dust concentration. idx 0..1."
        } else {
            "Actual hardware response through the shared FIFO; idx is zero-based."
        };
        tools.push(json!({"name":name,"description":description,"inputSchema":{"type":"object","properties":{"idx":{"type":"integer","minimum":0,"maximum":max},"decision_id":{"type":"string","minLength":1,"maxLength":128}},"required":["idx"],"additionalProperties":false}}));
    }
    tools.push(json!({"name":"set_fan_pwm","description":"Set PWM 0..255, returns the applied value. Requires TJC_AGENT_PHASE=full and a decision_id. No automatic retry.","inputSchema":{"type":"object","properties":{"idx":{"type":"integer","minimum":0,"maximum":5},"pwm":{"type":"integer","minimum":0,"maximum":255},"decision_id":{"type":"string","minLength":1,"maxLength":128}},"required":["idx","pwm","decision_id"],"additionalProperties":false}}));
    for name in [
        "get_system_state",
        "get_pending_events",
        "create_operator_alert",
    ] {
        tools.push(json!({"name":name,"inputSchema":{"type":"object"}}));
    }
    tools.push(json!({"name":"get_temperature_history","description":"SQLite history by BMP280 idx, grouped by bucket. No inside/outside placement is assumed. Not a hardware request.","inputSchema":{"type":"object","properties":{"idx":{"type":"integer","minimum":0,"maximum":2},"durationSeconds":{"type":"integer","minimum":60,"maximum":3600},"bucketSeconds":{"type":"integer","minimum":1,"maximum":60}},"required":["idx","durationSeconds","bucketSeconds"],"additionalProperties":false}}));
    tools
}
fn result(v: Value) -> Value {
    json!({"content":[{"type":"text","text":v.to_string()}],"structuredContent":v,"isError":v.get("ok").and_then(Value::as_bool)==Some(false)})
}
fn number(v: &Value, k: &str, max: u64) -> Result<u8, String> {
    v.get(k)
        .and_then(Value::as_u64)
        .filter(|v| *v <= max)
        .map(|v| v as u8)
        .ok_or_else(|| format!("{k} is missing or out of range"))
}
fn command(name: &str, a: &Value) -> Result<Command, String> {
    let max = match name {
        "get_temperature" | "get_pressure" | "get_humidity" => 2,
        "get_voc" | "get_door_state" | "get_dust" => 1,
        "get_fan_pwm" | "get_fan_rpm" | "set_fan_pwm" => 5,
        _ => return Err("unknown User API tool".into()),
    };
    let idx = number(a, "idx", max)?;
    let allowed = if name == "set_fan_pwm" {
        vec!["idx", "pwm", "decision_id"]
    } else {
        vec!["idx", "decision_id"]
    };
    if a.as_object()
        .is_some_and(|o| o.keys().any(|k| !allowed.contains(&k.as_str())))
    {
        return Err("unknown arguments".into());
    }
    Ok(match name {
        "get_temperature" => Command::Temperature(idx),
        "get_pressure" => Command::Pressure(idx),
        "get_humidity" => Command::Humidity(idx),
        "get_voc" => Command::Voc(idx),
        "get_dust" => Command::Dust(idx),
        "get_door_state" => Command::Door(idx),
        "get_fan_pwm" => Command::Pwm(idx),
        "get_fan_rpm" => Command::Rpm(idx),
        "set_fan_pwm" => Command::SetPwm {
            idx,
            pwm: number(a, "pwm", 255)?,
        },
        _ => unreachable!(),
    })
}
async fn call_tool(state: &ServerState, params: &Value) -> Value {
    let name = params["name"].as_str().unwrap_or("");
    let a = params
        .get("arguments")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let id = a
        .get("decision_id")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let response = match name {
        "get_system_state" => result(
            json!({"ok":true,"cache":state.system.snapshot(),"note":"Cached responses include reception timestamps; not a fresh hardware read."}),
        ),
        "get_temperature_history" => {
            let duration = a["durationSeconds"]
                .as_u64()
                .filter(|v| (60..=3600).contains(v));
            let bucket = a["bucketSeconds"].as_u64().filter(|v| (1..=60).contains(v));
            let idx = number(&a, "idx", 2).ok();
            match (idx, duration, bucket) {
                (Some(i), Some(d), Some(b)) => {
                    result(json!({"ok":true,"history":state.database.temperature_history(i,d,b)}))
                }
                _ => result(
                    json!({"ok":false,"error":{"code":"INVALID_ARGUMENT","message":"idx 0..2, durationSeconds 60..3600 and bucketSeconds 1..60 required"}}),
                ),
            }
        }
        "get_pending_events" => result(json!({"ok":true,"events":state.database.pending_events()})),
        "create_operator_alert" => {
            state.database.record_event("OPERATOR_ALERT", &a);
            result(json!({"ok":true,"status":"recorded"}))
        }
        _ => match command(name, &a) {
            Err(e) => result(json!({"ok":false,"error":{"code":"INVALID_ARGUMENT","message":e}})),
            Ok(c) => {
                if c.mutating() && !state.allow_write {
                    result(
                        json!({"ok":false,"error":{"code":"READONLY","message":"set TJC_AGENT_PHASE=full to enable PWM commands"}}),
                    )
                } else if c.mutating() && id.is_none() {
                    result(
                        json!({"ok":false,"error":{"code":"INVALID_ARGUMENT","message":"decision_id required"}}),
                    )
                } else if a.get("decision_id").is_some() && id.is_none() {
                    result(
                        json!({"ok":false,"error":{"code":"INVALID_ARGUMENT","message":"decision_id must be a string"}}),
                    )
                } else {
                    match state.user.execute(id.clone(), c).await {
                        Ok(outcome) => {
                            let mut v = outcome.result;
                            v["decision_id"] = json!(outcome.decision_id);
                            v["tx_id"] = json!(outcome.tx_id);
                            v["session_id"] = json!(outcome.session_id);
                            result(v)
                        }
                        Err(e) => result(
                            json!({"ok":false,"error":{"code":"REQUEST_REJECTED","message":e}}),
                        ),
                    }
                }
            }
        },
    };
    state.database.record_tool_call(
        name,
        id.as_deref(),
        if state.allow_write {
            "full"
        } else {
            "readonly"
        },
        &a.to_string(),
        &response.to_string(),
    );
    response
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn no_admin_or_pid_tools() {
        let names: Vec<_> = tool_catalog()
            .into_iter()
            .map(|v| v["name"].as_str().unwrap().to_owned())
            .collect();
        assert!(names.contains(&"get_humidity".into()));
        assert!(names.contains(&"set_fan_pwm".into()));
        for n in [
            "get_mode",
            "set_mode",
            "set_door_lock",
            "propose_pid_change",
            "confirm_config",
            "probe_fan",
        ] {
            assert!(!names.contains(&n.into()));
            assert!(command(n, &json!({"idx":0})).is_err());
        }
    }
    #[test]
    fn index_and_pwm_arguments() {
        assert!(command("get_temperature", &json!({"idx":2})).is_ok());
        assert!(command("get_temperature", &json!({"idx":3})).is_err());
        assert!(command("get_fan_rpm", &json!({"idx":5})).is_ok());
        assert!(command("set_fan_pwm", &json!({"idx":0,"pwm":256})).is_err());
    }
    #[tokio::test]
    async fn bearer_authentication_and_unknown_admin_tool() {
        use crate::serial::CommandBroker;
        use std::sync::{Arc, Mutex};
        let state = ServerState {
            token: "test-only-token".into(),
            user: UserApi::new(Arc::new(Mutex::new(CommandBroker::default()))),
            system: SharedState::default(),
            database: Database::open(":memory:").unwrap(),
            allow_write: true,
        };
        let request = json!({"jsonrpc":"2.0","id":1,"method":"tools/list"});
        assert_eq!(
            handle(
                State(state.clone()),
                HeaderMap::new(),
                Json(request.clone())
            )
            .await
            .unwrap_err(),
            StatusCode::UNAUTHORIZED
        );
        let mut headers = HeaderMap::new();
        headers.insert("authorization", "Bearer test-only-token".parse().unwrap());
        let response = handle(State(state.clone()), headers.clone(), Json(request))
            .await
            .unwrap()
            .0;
        assert!(
            response["result"]["tools"]
                .as_array()
                .unwrap()
                .iter()
                .all(|t| t["name"] != "set_mode")
        );
        let admin = json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"set_mode","arguments":{"idx":0,"mode":1}}});
        let response = handle(State(state), headers, Json(admin)).await.unwrap().0;
        assert_eq!(response["result"]["structuredContent"]["ok"], false);
    }
}
