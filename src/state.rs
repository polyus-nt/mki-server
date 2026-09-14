use std::sync::{Arc, RwLock};

use serde::Serialize;

#[derive(Clone, Debug, Default, Serialize)]
pub struct SystemState {
    pub received_wall_clock_ms: i64,
    pub inside_c: Option<f64>,
    pub outside_c: Option<f64>,
    pub inside_valid: bool,
    pub inside_fresh: bool,
    pub outside_valid: bool,
    pub outside_fresh: bool,
    pub target_c: u8,
    pub door_open: bool,
    pub door_stage: u8,
    pub mode: u8,
    pub door_policy: u8,
    pub stable_version: u16,
    pub candidate_version: Option<u16>,
    pub pwm: [u8; 4],
    pub rpm: [u32; 4],
    pub failed_mask: u8,
    pub kp_milli: u16,
    pub ki_milli: u16,
    pub kd_milli: u16,
    pub weights: [u8; 4],
    pub limited_pwm: u8,
}

#[derive(Clone, Default)]
pub struct SharedState(Arc<RwLock<SystemState>>);

impl SharedState {
    pub fn snapshot(&self) -> SystemState {
        self.0.read().expect("system state lock poisoned").clone()
    }

    pub fn replace(&self, next: SystemState) {
        *self.0.write().expect("system state lock poisoned") = next;
    }
}
