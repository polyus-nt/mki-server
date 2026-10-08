use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{Connection, Result, params};
use serde_json::{Value, json};

use crate::state::SystemState;

#[derive(Clone)]
pub struct Database {
    connection: Arc<Mutex<Connection>>,
    run_id: i64,
}

impl Database {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let connection = Connection::open(path)?;
        connection.execute_batch(
            "
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY, started_wall_clock_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telemetry (
              id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
              monotonic_ms INTEGER NOT NULL, wall_clock_ms INTEGER NOT NULL,
              raw BLOB NOT NULL, decoded_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
              wall_clock_ms INTEGER NOT NULL, kind TEXT NOT NULL, details_json TEXT NOT NULL,
              pending INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS configurations (
              id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
              wall_clock_ms INTEGER NOT NULL, stable_version INTEGER NOT NULL,
              candidate_version INTEGER, config_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tool_calls (
              id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
              wall_clock_ms INTEGER NOT NULL, tool TEXT NOT NULL, decision_id TEXT,
              phase TEXT NOT NULL, arguments_json TEXT NOT NULL, result_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_decisions (
              id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
              decision_id TEXT NOT NULL UNIQUE, wall_clock_ms INTEGER NOT NULL,
              phase TEXT NOT NULL, status TEXT NOT NULL, details_json TEXT NOT NULL
            );",
        )?;
        let now = wall_clock_ms();
        connection.execute("INSERT INTO runs(started_wall_clock_ms) VALUES(?)", [now])?;
        Ok(Self {
            run_id: connection.last_insert_rowid(),
            connection: Arc::new(Mutex::new(connection)),
        })
    }

    pub fn record_telemetry(&self, monotonic_ms: i64, raw: &[u8], state: &SystemState) {
        let decoded = serde_json::to_string(state).unwrap_or_else(|_| "{}".to_owned());
        let _ = self
            .connection
            .lock()
            .expect("database lock poisoned")
            .execute(
                "INSERT INTO telemetry(run_id, monotonic_ms, wall_clock_ms, raw, decoded_json)
             VALUES(?, ?, ?, ?, ?)",
                params![self.run_id, monotonic_ms, wall_clock_ms(), raw, decoded],
            );
    }

    pub fn record_tool_call(
        &self,
        tool: &str,
        decision_id: Option<&str>,
        phase: &str,
        arguments_json: &str,
        result_json: &str,
    ) {
        let _ = self.connection.lock().expect("database lock poisoned").execute(
            "INSERT INTO tool_calls(run_id, wall_clock_ms, tool, decision_id, phase, arguments_json, result_json)
             VALUES(?, ?, ?, ?, ?, ?, ?)",
            params![self.run_id, wall_clock_ms(), tool, decision_id, phase, arguments_json, result_json],
        );
    }

    pub fn record_event(&self, kind: &str, details: &Value) {
        let _ = self
            .connection
            .lock()
            .expect("database lock poisoned")
            .execute(
                "INSERT INTO events(run_id, wall_clock_ms, kind, details_json) VALUES(?, ?, ?, ?)",
                params![self.run_id, wall_clock_ms(), kind, details.to_string()],
            );
    }

    pub fn pending_events(&self) -> Vec<Value> {
        let connection = self.connection.lock().expect("database lock poisoned");
        let Ok(mut statement) = connection.prepare(
            "SELECT id, wall_clock_ms, kind, details_json FROM events WHERE run_id = ? AND pending = 1 ORDER BY id",
        ) else { return Vec::new(); };
        statement.query_map([self.run_id], |row| {
            let details: String = row.get(3)?;
            Ok(json!({"id":row.get::<_, i64>(0)?,"wall_clock_ms":row.get::<_, i64>(1)?,"kind":row.get::<_, String>(2)?,"details":serde_json::from_str::<Value>(&details).unwrap_or(Value::Null)}))
        }).map(|rows| rows.filter_map(Result::ok).collect()).unwrap_or_default()
    }

    /// Query new temperature responses by physical channel, not a guessed inside/outside mapping.
    /// Legacy rows and tables are retained unchanged, but are not re-labelled as BMP280 readings.
    pub fn temperature_history(
        &self,
        idx: u8,
        duration_seconds: u64,
        bucket_seconds: u64,
    ) -> Vec<Value> {
        let earliest = wall_clock_ms() - duration_seconds as i64 * 1000;
        let bucket_ms = bucket_seconds as i64 * 1000;
        let path = format!("$.temperature[{idx}].value");
        let connection = self.connection.lock().expect("database lock poisoned");
        let Ok(mut statement) = connection.prepare(
            "SELECT (wall_clock_ms / ?1) * ?1 AS bucket,
                    AVG(json_extract(decoded_json, ?4)), MIN(json_extract(decoded_json, ?4)),
                    MAX(json_extract(decoded_json, ?4))
             FROM telemetry WHERE run_id = ?2 AND wall_clock_ms >= ?3
             AND length(raw) = 20 AND substr(raw, 8, 1) = X'20' AND substr(raw, 10, 1) = ?5
             GROUP BY bucket ORDER BY bucket",
        ) else {
            return Vec::new();
        };
        statement
            .query_map(
                params![bucket_ms, self.run_id, earliest, path, vec![idx]],
                |row| {
                    Ok(json!({"idx":idx, "wall_clock_ms":row.get::<_, i64>(0)?,
                      "avg_c":row.get::<_, Option<f64>>(1)?, "min_c":row.get::<_, Option<f64>>(2)?,
                      "max_c":row.get::<_, Option<f64>>(3)?}))
                },
            )
            .map(|rows| rows.filter_map(Result::ok).collect())
            .unwrap_or_default()
    }
}

pub fn wall_clock_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{serial::Command, state::SharedState};
    #[test]
    fn channel_history_ignores_cached_other_responses_and_preserves_legacy_rows() {
        let db = Database::open(":memory:").unwrap();
        let state = SharedState::default();
        let t0 = Command::Temperature(0).frame(1).encode();
        let t2 = Command::Temperature(2).frame(2).encode();
        state.apply(&Command::Temperature(0), &json!({"ok":true,"value":-0.25}));
        db.record_telemetry(1, &t0, &state.snapshot());
        state.apply(&Command::Temperature(2), &json!({"ok":true,"value":25.5}));
        db.record_telemetry(2, &t2, &state.snapshot());
        state.apply(&Command::Temperature(0), &json!({"ok":true,"value":99}));
        db.record_telemetry(3, &Command::Rpm(0).frame(3).encode(), &state.snapshot());
        db.connection.lock().unwrap().execute("INSERT INTO telemetry(run_id,monotonic_ms,wall_clock_ms,raw,decoded_json)VALUES(?,4,?,X'01','{\"inside_c\":100}')",params![db.run_id,wall_clock_ms()]).unwrap();
        assert_eq!(db.temperature_history(0, 60, 60)[0]["avg_c"], -0.25);
        assert_eq!(db.temperature_history(2, 60, 60)[0]["avg_c"], 25.5);
        assert!(db.temperature_history(1, 60, 60).is_empty());
        let total: i64 = db
            .connection
            .lock()
            .unwrap()
            .query_row("SELECT COUNT(*)FROM telemetry", [], |r| r.get(0))
            .unwrap();
        assert_eq!(total, 4);
    }
}
