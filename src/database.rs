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

    pub fn remember_decision(
        &self,
        decision_id: &str,
        phase: &str,
        status: &str,
        details_json: &str,
    ) -> bool {
        self.connection.lock().expect("database lock poisoned").execute(
            "INSERT OR IGNORE INTO agent_decisions(decision_id, run_id, wall_clock_ms, phase, status, details_json)
             VALUES(?, ?, ?, ?, ?, ?)",
            params![decision_id, self.run_id, wall_clock_ms(), phase, status, details_json],
        ).map(|changed| changed == 1).unwrap_or(false)
    }

    pub fn decision_exists(&self, decision_id: &str) -> bool {
        self.connection
            .lock()
            .expect("database lock poisoned")
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM agent_decisions WHERE decision_id = ?)",
                [decision_id],
                |row| row.get(0),
            )
            .unwrap_or(false)
    }

    pub fn update_decision(&self, decision_id: &str, status: &str) {
        let _ = self
            .connection
            .lock()
            .expect("database lock poisoned")
            .execute(
                "UPDATE agent_decisions SET status = ? WHERE decision_id = ?",
                params![status, decision_id],
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

    pub fn record_configuration(&self, state: &SystemState) {
        let config = serde_json::to_string(state).unwrap_or_else(|_| "{}".to_owned());
        let _ = self.connection.lock().expect("database lock poisoned").execute(
            "INSERT INTO configurations(run_id, wall_clock_ms, stable_version, candidate_version, config_json)
             VALUES(?, ?, ?, ?, ?)",
            params![self.run_id, wall_clock_ms(), state.stable_version, state.candidate_version, config],
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

    pub fn temperature_history(&self, duration_seconds: u64, bucket_seconds: u64) -> Vec<Value> {
        let earliest = wall_clock_ms() - duration_seconds as i64 * 1000;
        let bucket_ms = bucket_seconds as i64 * 1000;
        let connection = self.connection.lock().expect("database lock poisoned");
        let Ok(mut statement) = connection.prepare(
            "SELECT (wall_clock_ms / ?1) * ?1 AS bucket, AVG(json_extract(decoded_json, '$.inside_c')),
                    MIN(json_extract(decoded_json, '$.inside_c')), MAX(json_extract(decoded_json, '$.inside_c')),
                    AVG(json_extract(decoded_json, '$.target_c'))
             FROM telemetry WHERE run_id = ?2 AND wall_clock_ms >= ?3 GROUP BY bucket ORDER BY bucket",
        ) else { return Vec::new(); };
        statement.query_map(params![bucket_ms, self.run_id, earliest], |row| {
            Ok(json!({"wall_clock_ms":row.get::<_, i64>(0)?,"inside_avg_c":row.get::<_, Option<f64>>(1)?,"inside_min_c":row.get::<_, Option<f64>>(2)?,"inside_max_c":row.get::<_, Option<f64>>(3)?,"target_avg_c":row.get::<_, Option<f64>>(4)?}))
        }).map(|rows| rows.filter_map(Result::ok).collect()).unwrap_or_default()
    }
}

pub fn wall_clock_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}
