use crate::serial::Command;
use serde::Serialize;
use serde_json::Value;
use std::sync::{Arc, RwLock};
#[derive(Clone, Debug, Default, Serialize)]
pub struct SystemState {
    pub connected: bool,
    pub received_wall_clock_ms: i64,
    pub temperature: [Option<Value>; 3],
    pub pressure: [Option<Value>; 3],
    pub voc: [Option<Value>; 2],
    pub humidity: [Option<Value>; 3],
    pub dust: [Option<Value>; 2],
    pub doors: [Option<Value>; 2],
    pub pwm: [Option<Value>; 6],
    pub rpm: [Option<Value>; 6],
}
#[derive(Clone, Default)]
pub struct SharedState(Arc<RwLock<SystemState>>);
impl SharedState {
    pub fn snapshot(&self) -> SystemState {
        self.0.read().expect("state lock poisoned").clone()
    }
    pub fn set_connected(&self, c: bool) {
        self.0.write().unwrap().connected = c;
    }
    pub fn apply(&self, c: &Command, r: &Value) {
        let mut s = self.0.write().unwrap();
        s.received_wall_clock_ms = crate::database::wall_clock_ms();
        // Include reception time: a device age alone does not age the GUI cache.
        let mut sample = r.clone();
        sample["received_wall_clock_ms"] = s.received_wall_clock_ms.into();
        match *c {
            Command::Temperature(i) => s.temperature[usize::from(i)] = Some(sample),
            Command::Pressure(i) => s.pressure[usize::from(i)] = Some(sample),
            Command::Voc(i) => s.voc[usize::from(i)] = Some(sample),
            Command::Humidity(i) => s.humidity[usize::from(i)] = Some(sample),
            Command::Dust(i) => s.dust[usize::from(i)] = Some(sample),
            Command::Door(i) => s.doors[usize::from(i)] = Some(sample),
            Command::Pwm(i) | Command::SetPwm { idx: i, .. } => {
                s.pwm[usize::from(i)] = Some(sample)
            }
            Command::Rpm(i) => s.rpm[usize::from(i)] = Some(sample),
            _ => {}
        }
    }
}
