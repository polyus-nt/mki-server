use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use axum::{
    Json, Router,
    extract::State,
    http::{HeaderMap, StatusCode},
    routing::post,
};
use serde_json::{Value, json};

use crate::{
    database::Database,
    serial::{Command, CommandBroker, CommandStatus},
    state::SharedState,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Readonly,
    DryRun,
    Weights,
    Pid,
    Full,
}

impl Phase {
    pub fn from_environment() -> Self {
        match std::env::var("TJC_AGENT_PHASE").as_deref() {
            Ok("dry-run") => Self::DryRun,
            Ok("weights") => Self::Weights,
            Ok("pid") => Self::Pid,
            Ok("full") => Self::Full,
            _ => Self::Readonly,
        }
    }
    fn allows(self, tool: &str) -> bool {
        match self {
            Self::Readonly | Self::DryRun => false,
            Self::Weights => matches!(
                tool,
                "propose_fan_weights" | "set_door_policy" | "create_operator_alert"
            ),
            Self::Pid => matches!(
                tool,
                "propose_pid_change"
                    | "propose_fan_weights"
                    | "set_door_policy"
                    | "create_operator_alert"
            ),
            Self::Full => true,
        }
    }
    fn name(self) -> &'static str {
        match self {
            Self::Readonly => "readonly",
            Self::DryRun => "dry-run",
            Self::Weights => "weights",
            Self::Pid => "pid",
            Self::Full => "full",
        }
    }
}

#[derive(Clone)]
struct ServerState {
    token: String,
    phase: Phase,
    system: SharedState,
    database: Database,
    commands: Arc<Mutex<CommandBroker>>,
}

pub struct McpServer {
    pub token: String,
}

pub fn start(
    system: SharedState,
    database: Database,
    commands: Arc<Mutex<CommandBroker>>,
) -> McpServer {
    let token = std::env::var("TJC_MCP_TOKEN")
        .unwrap_or_else(|_| format!("{:032x}", rand::random::<u128>()));
    let server_state = ServerState {
        token: token.clone(),
        phase: Phase::from_environment(),
        system,
        database,
        commands,
    };
    std::thread::spawn(move || {
        let runtime = tokio::runtime::Runtime::new().expect("MCP Tokio runtime");
        runtime.block_on(async move {
            let app = Router::new()
                .route("/mcp", post(handle))
                .with_state(server_state);
            let address = SocketAddr::from(([127, 0, 0, 1], 8765));
            let listener = tokio::net::TcpListener::bind(address)
                .await
                .expect("bind loopback MCP");
            axum::serve(listener, app).await.expect("serve MCP");
        });
    });
    McpServer { token }
}

async fn handle(
    State(state): State<ServerState>,
    headers: HeaderMap,
    Json(request): Json<Value>,
) -> Result<Json<Value>, StatusCode> {
    let expected = format!("Bearer {}", state.token);
    if headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        != Some(expected.as_str())
    {
        return Err(StatusCode::UNAUTHORIZED);
    }
    let id = request.get("id").cloned().unwrap_or(Value::Null);
    let method = request.get("method").and_then(Value::as_str).unwrap_or("");
    let result = match method {
        "initialize" => {
            json!({"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"cooling_controller","version":"0.1.1"}})
        }
        "tools/list" => json!({"tools": tool_catalog()}),
        "tools/call" => call_tool(&state, request.get("params").unwrap_or(&Value::Null)),
        _ => {
            return Ok(Json(
                json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message":"method not found"}}),
            ));
        }
    };
    Ok(Json(json!({"jsonrpc":"2.0","id":id,"result":result})))
}

fn tool_catalog() -> Vec<Value> {
    [
        "get_system_state",
        "get_temperature_history",
        "get_control_config",
        "get_pending_events",
        "propose_pid_change",
        "propose_fan_weights",
        "set_door_policy",
        "confirm_config",
        "rollback_config",
        "probe_fan",
        "clear_emergency",
        "create_operator_alert",
    ]
    .into_iter()
    .map(|name| json!({"name":name,"inputSchema":{"type":"object"}}))
    .collect()
}

fn result(value: Value) -> Value {
    json!({"content":[{"type":"text","text":value.to_string()}],"structuredContent":value})
}

fn call_tool(state: &ServerState, params: &Value) -> Value {
    let name = params.get("name").and_then(Value::as_str).unwrap_or("");
    let arguments = params
        .get("arguments")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let response = match name {
        "get_system_state" | "get_control_config" => {
            result(serde_json::to_value(state.system.snapshot()).unwrap_or_else(|_| json!({})))
        }
        "get_temperature_history" => {
            let duration = arguments
                .get("durationSeconds")
                .and_then(Value::as_u64)
                .filter(|value| (60..=3600).contains(value));
            let bucket = arguments
                .get("bucketSeconds")
                .and_then(Value::as_u64)
                .filter(|value| (1..=60).contains(value));
            match (duration, bucket) {
                (Some(duration), Some(bucket)) => {
                    result(json!(state.database.temperature_history(duration, bucket)))
                }
                _ => result(
                    json!({"status":"rejected","reason":"durationSeconds 60..3600 and bucketSeconds 1..60 are required"}),
                ),
            }
        }
        "get_pending_events" => result(json!(state.database.pending_events())),
        "create_operator_alert" => {
            state.database.record_event("OPERATOR_ALERT", &arguments);
            result(json!({"status":"recorded"}))
        }
        _ => write_tool(state, name, &arguments),
    };
    let decision_id = arguments.get("decision_id").and_then(Value::as_str);
    state.database.record_tool_call(
        name,
        decision_id,
        state.phase.name(),
        &arguments.to_string(),
        &response.to_string(),
    );
    response
}

fn write_tool(state: &ServerState, name: &str, arguments: &Value) -> Value {
    let decision_id = match arguments
        .get("decision_id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        Some(value) => value,
        None => return result(json!({"status":"rejected","reason":"decision_id is required"})),
    };
    if state.database.decision_exists(decision_id) {
        return result(json!({"status":"duplicate","decision_id":decision_id}));
    }
    let command = match command_from_arguments(name, arguments, &state.system.snapshot()) {
        Ok(command) => command,
        Err(reason) => return result(json!({"status":"rejected","reason":reason})),
    };
    if !state.phase.allows(name) {
        let status = if state.phase == Phase::DryRun {
            "validated"
        } else {
            "rejected"
        };
        state.database.remember_decision(
            decision_id,
            state.phase.name(),
            status,
            &arguments.to_string(),
        );
        return result(
            json!({"status":status,"phase":state.phase.name(),"decision_id":decision_id}),
        );
    }
    let status = state
        .commands
        .lock()
        .expect("command broker lock poisoned")
        .submit(decision_id.to_owned(), command);
    state.database.remember_decision(
        decision_id,
        state.phase.name(),
        "queued",
        &arguments.to_string(),
    );
    result(json!({"status":status_name(&status),"decision_id":decision_id}))
}

fn number(value: &Value, name: &str, maximum: u64) -> Result<u16, String> {
    value
        .get(name)
        .and_then(Value::as_u64)
        .filter(|value| *value <= maximum)
        .map(|value| value as u16)
        .ok_or_else(|| format!("{name} is missing or out of range"))
}

fn command_from_arguments(
    name: &str,
    value: &Value,
    current: &crate::state::SystemState,
) -> Result<Command, String> {
    match name {
        "propose_pid_change" => {
            let expected_version = number(value, "expected_version", u16::MAX as u64)?;
            let coefficient = value
                .get("coefficient")
                .and_then(Value::as_str)
                .ok_or("coefficient is required")?;
            let next = number(value, "value_milli", 65_535)?;
            let previous = match coefficient {
                "kp" => current.kp_milli,
                "ki" => current.ki_milli,
                "kd" => current.kd_milli,
                _ => return Err("coefficient must be kp, ki, or kd".into()),
            };
            if !(coefficient == "kd" && previous == 0 && next > 0)
                && (previous == 0 || u32::from(next.abs_diff(previous)) * 10 > u32::from(previous))
            {
                return Err("PID change exceeds 10 percent".into());
            }
            let (kp, ki, kd) = match coefficient {
                "kp" => (next, current.ki_milli, current.kd_milli),
                "ki" => (current.kp_milli, next, current.kd_milli),
                "kd" => (current.kp_milli, current.ki_milli, next),
                _ => return Err("coefficient must be kp, ki, or kd".into()),
            };
            Ok(Command::ProposePid {
                expected_version,
                kp,
                ki,
                kd,
            })
        }
        "propose_fan_weights" => {
            let expected_version = number(value, "expected_version", u16::MAX as u64)?;
            let list = value
                .get("weights")
                .and_then(Value::as_array)
                .filter(|list| list.len() == 4)
                .ok_or("four weights are required")?;
            let mut weights = [0; 4];
            for (index, item) in list.iter().enumerate() {
                weights[index] = item
                    .as_u64()
                    .filter(|weight| *weight <= 255)
                    .ok_or("weight out of range")? as u8;
            }
            if weights.iter().all(|weight| *weight == 0) {
                return Err("at least one weight must be nonzero".into());
            }
            Ok(Command::ProposeWeights {
                expected_version,
                weights,
            })
        }
        "set_door_policy" => Ok(Command::SetDoorPolicy {
            expected_version: number(value, "expected_version", u16::MAX as u64)?,
            policy: number(value, "policy", 2)? as u8,
            limited_pwm: number(value, "limited_pwm", 255)? as u8,
        }),
        "confirm_config" => Ok(Command::Confirm {
            candidate_version: number(value, "candidate_version", u16::MAX as u64)?,
        }),
        "rollback_config" => Ok(Command::Rollback {
            candidate_version: number(value, "candidate_version", u16::MAX as u64)?,
        }),
        "probe_fan" => Ok(Command::ProbeFan {
            fan_index: number(value, "fan_index", 3)? as u8,
        }),
        "clear_emergency" => Ok(Command::ClearEmergency),
        _ => Err("unknown tool".into()),
    }
}

fn status_name(status: &CommandStatus) -> &'static str {
    match status {
        CommandStatus::Queued => "queued",
        CommandStatus::Applied => "applied",
        CommandStatus::Rejected(_) => "rejected",
        CommandStatus::Timeout => "timeout",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pid_change_preserves_other_coefficients_and_enforces_ten_percent() {
        let state = crate::state::SystemState {
            kp_milli: 200,
            ki_milli: 10,
            kd_milli: 0,
            ..Default::default()
        };
        let command = command_from_arguments(
            "propose_pid_change",
            &json!({"expected_version": 2, "coefficient":"kp", "value_milli":220}),
            &state,
        )
        .unwrap();
        assert_eq!(
            command.actuator_bytes(),
            vec![0x09, 0, 2, 0, 220, 0, 10, 0, 0]
        );
        assert!(
            command_from_arguments(
                "propose_pid_change",
                &json!({"expected_version": 2, "coefficient":"kp", "value_milli":221}),
                &state,
            )
            .is_err()
        );
    }
}
