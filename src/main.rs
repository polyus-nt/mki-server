#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

use std::collections::VecDeque;
use std::io::{Read, Write};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use eframe::egui;
use egui::{Color32, RichText, Stroke};
use egui_plot::{Legend, Line, LineStyle, Plot, PlotBounds, PlotPoints, Polygon};
use serialport::SerialPort;

mod database;
mod mcp;
mod serial;
mod state;

use database::Database;
use serial::CommandBroker;
use state::{SharedState, SystemState};

const COMPUTER_ADDR: u8 = 99;
const BOARD_ADDR: u8 = 12;
const MARKER: u8 = 12;
const APPLICATION_NAME: &str = "Теплоизоляционный блок";
const APPLICATION_VERSION: &str = env!("CARGO_PKG_VERSION");
const ETALON_TEMP_CODE: u8 = 10;
const DEFAULT_ETALON_TEMPERATURE: u8 = 25;
const TELEMETRY_VERSION: u8 = 1;
const TELEMETRY_SIZE: usize = 29;
const MAX_TELEMETRY_FRAME_AGE: Duration = Duration::from_secs(3);
const MAX_TELEMETRY_CANDIDATES: usize = 32;
const MAX_POINTS: usize = 1_000;
const MA_WINDOW: usize = 7;
const SERIAL_POLL_INTERVAL: Duration = Duration::from_millis(10);
const ACCENT: Color32 = Color32::from_rgb(71, 148, 255);
const SUCCESS: Color32 = Color32::from_rgb(82, 208, 151);
const DANGER: Color32 = Color32::from_rgb(242, 104, 118);
const CARD: Color32 = Color32::from_rgb(255, 255, 255);
const CARD_BORDER: Color32 = Color32::from_rgb(213, 221, 233);

fn encode_packet(address: u8, data: u8) -> Vec<u8> {
    let mut packet = Vec::with_capacity(5);
    packet.push(b'\r');
    packet.extend_from_slice(format!("{address:02X}").as_bytes());
    packet.push(data);
    packet.push(b'\n');
    packet
}

fn decode_packet(packet: &[u8]) -> Option<(u8, u8)> {
    if packet.len() != 5 || packet[0] != b'\r' || packet[4] != b'\n' {
        return None;
    }
    let address = std::str::from_utf8(&packet[1..3]).ok()?;
    Some((u8::from_str_radix(address, 16).ok()?, packet[3]))
}

struct TelemetryCandidate {
    payload: Vec<u8>,
    started_at: Instant,
}

struct TelemetryCollector {
    candidates: Vec<TelemetryCandidate>,
}

impl TelemetryCollector {
    fn new() -> Self {
        Self {
            candidates: Vec::with_capacity(MAX_TELEMETRY_CANDIDATES),
        }
    }

    fn reset(&mut self) {
        self.candidates.clear();
    }

    fn is_temperature_permission(&self, value: u8) -> bool {
        value == MARKER
            && self
                .candidates
                .iter()
                .any(|candidate| candidate.payload == [ETALON_TEMP_CODE])
    }

    fn feed(&mut self, value: u8, now: Instant) -> Option<Vec<u8>> {
        self.candidates.retain(|candidate| {
            now.duration_since(candidate.started_at) <= MAX_TELEMETRY_FRAME_AGE
        });

        let mut completed = None;
        self.candidates.retain_mut(|candidate| {
            if candidate.payload.len() < TELEMETRY_SIZE {
                candidate.payload.push(value);
                return true;
            }

            if value == MARKER && completed.is_none() && is_plausible_telemetry(&candidate.payload)
            {
                completed = Some(std::mem::take(&mut candidate.payload));
            }
            false
        });

        // Marker is allowed in the payload, so it is also kept as data for all
        // existing candidates above. In parallel it may start a new frame.
        if value == MARKER {
            self.candidates.push(TelemetryCandidate {
                payload: Vec::with_capacity(TELEMETRY_SIZE),
                started_at: now,
            });
            if self.candidates.len() > MAX_TELEMETRY_CANDIDATES {
                self.candidates.remove(0);
            }
        }

        completed
    }
}

fn is_plausible_temperature(value: u8) -> bool {
    (-55..=125).contains(&signed_temperature(value))
}

fn is_plausible_telemetry(payload: &[u8]) -> bool {
    payload.len() == TELEMETRY_SIZE
        && payload[0] == TELEMETRY_VERSION
        && payload[1] <= 1 // Hall sensor
        && is_plausible_temperature(payload[2])
        && is_plausible_temperature(payload[4])
        && payload[7] <= 60 // Target
        && payload[8] <= 1 // Door open
        && payload[10] <= 3 // Mode
        && payload[11] <= 2 // Door policy
}

#[derive(Clone, Copy)]
struct Telemetry {
    hall: bool,
    inside: f64,
    outside: f64,
    inside_valid: bool,
    inside_fresh: bool,
    outside_valid: bool,
    outside_fresh: bool,
    pwm: [u8; 4],
    rpm: [u32; 4],
    reference: i8,
    door_open: bool,
    door_stage: u8,
    mode: u8,
    door_policy: u8,
    stable_version: u16,
    candidate_version: Option<u16>,
    failed_mask: u8,
}

fn signed_temperature(value: u8) -> i8 {
    value as i8
}

fn decode_temperature(whole: u8, fraction: u8) -> f64 {
    f64::from(signed_temperature(whole)) + (f64::from(fraction) * 99.0 / 255.0).round() / 100.0
}

fn decode_rpm(whole: u8, fraction: u8) -> u32 {
    u32::from(u16::from_be_bytes([whole, fraction]))
}

fn decode_telemetry(payload: &[u8]) -> Option<Telemetry> {
    if payload.len() != TELEMETRY_SIZE {
        return None;
    }
    let sensor_flags = payload[6];
    let candidate_version = u16::from_be_bytes([payload[14], payload[15]]);
    Some(Telemetry {
        hall: payload[1] != 0,
        inside: decode_temperature(payload[2], payload[3]),
        outside: decode_temperature(payload[4], payload[5]),
        inside_valid: sensor_flags & 0x01 != 0,
        inside_fresh: sensor_flags & 0x02 != 0,
        outside_valid: sensor_flags & 0x04 != 0,
        outside_fresh: sensor_flags & 0x08 != 0,
        pwm: [payload[16], payload[17], payload[18], payload[19]],
        rpm: [
            decode_rpm(payload[20], payload[21]),
            decode_rpm(payload[22], payload[23]),
            decode_rpm(payload[24], payload[25]),
            decode_rpm(payload[26], payload[27]),
        ],
        reference: payload[7] as i8,
        door_open: payload[8] != 0,
        door_stage: payload[9],
        mode: payload[10],
        door_policy: payload[11],
        stable_version: u16::from_be_bytes([payload[12], payload[13]]),
        candidate_version: (candidate_version != u16::MAX).then_some(candidate_version),
        failed_mask: payload[28],
    })
}

#[cfg(test)]
mod telemetry_collector_tests {
    use super::*;

    fn payload() -> [u8; TELEMETRY_SIZE] {
        [
            1, // Format version
            1, // Hall
            23, 0, // Temperature inside
            18, 0,    // Temperature outside
            0x0f, // Both sensors valid and fresh
            25,   // Target
            0, 1, // Door open and stage
            0, 0, // Mode and door policy
            0, 1, // Stable version
            0xff, 0xff, // No candidate
            64, 64, 64, 64, // Applied PWM
            0x03, 0x03, // RPM 1: the OK byte is valid payload
            0x0f, 0xa0, // RPM 2: 4000
            0x17, 0x70, // RPM 3: 6000
            0, 0, // RPM 4
            0, // Failed mask
        ]
    }

    fn frame(payload: &[u8]) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(payload.len() + 2);
        bytes.push(MARKER);
        bytes.extend_from_slice(payload);
        bytes.push(MARKER);
        bytes
    }

    fn collect(bytes: &[u8]) -> Vec<Vec<u8>> {
        let mut collector = TelemetryCollector::new();
        let now = Instant::now();
        bytes
            .iter()
            .filter_map(|byte| collector.feed(*byte, now))
            .collect()
    }

    #[test]
    fn accepts_a_complete_frame() {
        let expected = payload();
        assert_eq!(collect(&frame(&expected)), vec![expected.to_vec()]);
    }

    #[test]
    fn keeps_marker_values_inside_the_payload() {
        let mut expected = payload();
        expected[3] = MARKER;
        assert_eq!(collect(&frame(&expected)), vec![expected.to_vec()]);
    }

    #[test]
    fn resynchronizes_after_a_lost_payload_byte() {
        let expected = payload();
        let mut damaged = frame(&expected);
        damaged.remove(1); // Drop Hall: the first damaged frame is implausible.
        damaged.extend(frame(&expected));

        assert_eq!(collect(&damaged), vec![expected.to_vec()]);
    }

    #[test]
    fn resynchronizes_after_an_extra_payload_byte() {
        let expected = payload();
        let mut damaged = frame(&expected);
        damaged.insert(5, 0x7f);
        damaged.extend(frame(&expected));

        assert_eq!(collect(&damaged), vec![expected.to_vec()]);
    }

    #[test]
    fn discards_a_stale_fragment_before_the_next_frame() {
        let expected = payload();
        let mut collector = TelemetryCollector::new();
        let started_at = Instant::now();
        collector.feed(MARKER, started_at);
        collector.feed(1, started_at);

        let next_frame_at = started_at + MAX_TELEMETRY_FRAME_AGE + Duration::from_millis(1);
        assert_eq!(collector.feed(MARKER, next_frame_at), None);

        for byte in expected {
            assert_eq!(collector.feed(byte, next_frame_at), None);
        }
        assert_eq!(
            collector.feed(MARKER, next_frame_at),
            Some(expected.to_vec())
        );
    }

    #[test]
    fn recognizes_the_reference_temperature_permission() {
        let mut collector = TelemetryCollector::new();
        let now = Instant::now();
        collector.feed(MARKER, now);
        collector.feed(ETALON_TEMP_CODE, now);

        assert!(collector.is_temperature_permission(MARKER));
    }
}

#[derive(Default)]
struct TelemetryStore {
    times: VecDeque<f64>,
    inside_ma: VecDeque<f64>,
    outside_ma: VecDeque<f64>,
    reference_temperatures: VecDeque<i8>,
    rpm: [VecDeque<u32>; 4],
    door_stages: VecDeque<u8>,
    inside_window: VecDeque<f64>,
    outside_window: VecDeque<f64>,
    inside_sum: f64,
    outside_sum: f64,
}

impl TelemetryStore {
    fn add(&mut self, elapsed: f64, sample: Telemetry) {
        self.times.push_back(elapsed);
        self.inside_window.push_back(sample.inside);
        self.inside_sum += sample.inside;
        self.outside_window.push_back(sample.outside);
        self.outside_sum += sample.outside;
        if self.inside_window.len() > MA_WINDOW {
            self.inside_sum -= self.inside_window.pop_front().unwrap();
        }
        if self.outside_window.len() > MA_WINDOW {
            self.outside_sum -= self.outside_window.pop_front().unwrap();
        }
        self.inside_ma
            .push_back(self.inside_sum / self.inside_window.len() as f64);
        self.outside_ma
            .push_back(self.outside_sum / self.outside_window.len() as f64);
        self.reference_temperatures.push_back(sample.reference);
        for (values, rpm) in self.rpm.iter_mut().zip(sample.rpm) {
            values.push_back(rpm);
        }
        self.door_stages.push_back(sample.door_stage);
        while self.times.len() > MAX_POINTS {
            self.times.pop_front();
            self.inside_ma.pop_front();
            self.outside_ma.pop_front();
            self.reference_temperatures.pop_front();
            for values in &mut self.rpm {
                values.pop_front();
            }
            self.door_stages.pop_front();
        }
    }

    fn points(&self, values: &VecDeque<f64>) -> PlotPoints<'static> {
        self.times
            .iter()
            .zip(values)
            .map(|(x, y)| [*x, *y])
            .collect()
    }

    fn rpm_points(&self, values: &VecDeque<u32>) -> PlotPoints<'static> {
        self.times
            .iter()
            .zip(values)
            .map(|(x, y)| [*x, f64::from(*y)])
            .collect()
    }

    fn reference_points(&self) -> PlotPoints<'static> {
        self.times
            .iter()
            .zip(&self.reference_temperatures)
            .map(|(x, reference)| [*x, f64::from(*reference)])
            .collect()
    }

    fn plot_bounds(&self, values: &VecDeque<f64>, include_reference: bool) -> PlotBounds {
        let mut low = f64::INFINITY;
        let mut high = f64::NEG_INFINITY;
        for value in values {
            low = low.min(*value);
            high = high.max(*value);
        }
        if include_reference {
            for reference in &self.reference_temperatures {
                let value = f64::from(*reference);
                low = low.min(value);
                high = high.max(value);
            }
        }
        if !low.is_finite() {
            low = -1.0;
            high = 1.0;
        } else {
            let padding = ((high - low) * 0.1).max(1.0);
            low -= padding;
            high += padding;
        }
        let start = self.times.front().copied().unwrap_or(0.0);
        PlotBounds::from_min_max([start, low], [self.right_edge(), high])
    }

    fn stage_spans(&self, low: f64, high: f64) -> Vec<Polygon<'static>> {
        let mut polygons = Vec::new();
        let mut start_index = 0;
        let mut current_color = self
            .door_stages
            .front()
            .and_then(|stage| stage_color(*stage));
        for index in 1..=self.door_stages.len() {
            let next_color = self
                .door_stages
                .get(index)
                .and_then(|stage| stage_color(*stage));
            if index == self.door_stages.len() || next_color != current_color {
                if let Some(color) = current_color {
                    let start = self.times[start_index];
                    let end = if index < self.times.len() {
                        self.times[index]
                    } else {
                        self.right_edge()
                    };
                    polygons.push(
                        Polygon::new(vec![[start, low], [end, low], [end, high], [start, high]])
                            .fill_color(color)
                            .allow_hover(false),
                    );
                }
                start_index = index;
                current_color = next_color;
            }
        }
        polygons
    }

    fn right_edge(&self) -> f64 {
        match self.times.len() {
            0 => 1.0,
            1 => self.times[0] + 1.0,
            count => {
                self.times[count - 1] + (self.times[count - 1] - self.times[count - 2]).max(0.001)
            }
        }
    }
}

fn stage_color(stage: u8) -> Option<Color32> {
    if stage > 5 {
        Some(Color32::from_rgba_unmultiplied(242, 104, 118, 72))
    } else if stage > 2 {
        Some(Color32::from_rgba_unmultiplied(255, 215, 106, 66))
    } else {
        None
    }
}

struct CommutatorApp {
    ports: Vec<String>,
    selected_port: String,
    port: Option<Box<dyn SerialPort>>,
    packet_buffer: Vec<u8>,
    collector: TelemetryCollector,
    reference_temperature: u8,
    store: TelemetryStore,
    started_at: Instant,
    last_poll: Instant,
    status: String,
    system_state: SharedState,
    database: Database,
    command_broker: Arc<Mutex<CommandBroker>>,
    _mcp_server: mcp::McpServer,
    control_response: Option<(u8, Vec<u8>)>,
    probe_fan: u8,
}

impl CommutatorApp {
    fn new(ctx: &egui::Context) -> Self {
        let mut visuals = egui::Visuals::light();
        visuals.panel_fill = Color32::from_rgb(244, 247, 252);
        visuals.window_fill = CARD;
        visuals.extreme_bg_color = Color32::from_rgb(238, 242, 248);
        visuals.faint_bg_color = Color32::from_rgb(235, 240, 247);
        visuals.selection.bg_fill = ACCENT;
        visuals.selection.stroke = Stroke::new(1.0, Color32::WHITE);
        visuals.widgets.inactive.bg_fill = Color32::from_rgb(238, 242, 248);
        visuals.widgets.hovered.bg_fill = Color32::from_rgb(222, 233, 251);
        visuals.widgets.active.bg_fill = ACCENT;
        ctx.set_visuals(visuals);

        let mut style = (*ctx.style()).clone();
        style.spacing.item_spacing = egui::vec2(10.0, 10.0);
        style.spacing.button_padding = egui::vec2(14.0, 8.0);
        style.spacing.interact_size.y = 34.0;
        ctx.set_style(style);

        let ports = list_ports();
        let database =
            Database::open("tjc-commutator.sqlite").expect("create SQLite telemetry database");
        let system_state = SharedState::default();
        let command_broker = Arc::new(Mutex::new(CommandBroker::default()));
        let mcp_server = mcp::start(
            system_state.clone(),
            database.clone(),
            command_broker.clone(),
        );
        eprintln!(
            "Cooling MCP listening on 127.0.0.1:8765/mcp; bearer token: {}",
            mcp_server.token
        );
        Self {
            selected_port: ports.first().cloned().unwrap_or_default(),
            ports,
            port: None,
            packet_buffer: Vec::new(),
            collector: TelemetryCollector::new(),
            reference_temperature: DEFAULT_ETALON_TEMPERATURE,
            store: TelemetryStore::default(),
            started_at: Instant::now(),
            last_poll: Instant::now(),
            status: "Выберите порт и подключитесь.".to_owned(),
            system_state,
            database,
            command_broker,
            _mcp_server: mcp_server,
            control_response: None,
            probe_fan: 0,
        }
    }

    fn refresh_ports(&mut self) {
        self.ports = list_ports();
        if !self.ports.contains(&self.selected_port) {
            self.selected_port = self.ports.first().cloned().unwrap_or_default();
        }
    }

    fn toggle_connection(&mut self) {
        if self.port.take().is_some() {
            self.status = "Отключено.".to_owned();
            return;
        }
        match serialport::new(&self.selected_port, 9_600)
            .timeout(Duration::from_millis(1))
            .open()
        {
            Ok(port) => {
                self.packet_buffer.clear();
                self.collector.reset();
                self.store = TelemetryStore::default();
                self.started_at = Instant::now();
                self.status = format!("Подключено к {} (9600 бод).", self.selected_port);
                self.port = Some(port);
                self.queue_operator_command(serial::Command::GetControlConfig);
            }
            Err(error) => self.status = format!("Ошибка подключения: {error}"),
        }
    }

    fn poll_serial(&mut self) {
        if self.port.is_none() || self.last_poll.elapsed() < SERIAL_POLL_INTERVAL {
            return;
        }
        self.last_poll = Instant::now();
        self.send_pending_command();
        let mut incoming = [0_u8; 1024];
        let read_result = self.port.as_mut().unwrap().read(&mut incoming);
        match read_result {
            Ok(count) if count > 0 => {
                self.packet_buffer.extend_from_slice(&incoming[..count]);
                self.process_packets();
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::TimedOut => {}
            Err(error) => self.status = format!("Ошибка COM-порта: {error}"),
        }
    }

    fn process_packets(&mut self) {
        loop {
            let Some(start) = self.packet_buffer.iter().position(|byte| *byte == b'\r') else {
                self.packet_buffer.clear();
                return;
            };
            if start > 0 {
                self.packet_buffer.drain(..start);
            }
            if self.packet_buffer.len() < 5 {
                return;
            }
            let packet: Vec<u8> = self.packet_buffer.drain(..5).collect();
            if packet[4] != b'\n' {
                self.packet_buffer.splice(0..0, packet.into_iter().skip(1));
                continue;
            }
            let Some((address, data)) = decode_packet(&packet) else {
                continue;
            };
            if address != COMPUTER_ADDR {
                continue;
            }
            self.process_data_byte(data);
        }
    }

    fn process_data_byte(&mut self, data: u8) {
        if let Some((command, payload)) = self.control_response.as_mut() {
            if *command == 0xFF {
                *command = data;
                return;
            }
            payload.push(data);
            let expected = if *command == 0x01 {
                21
            } else if *command == 0x10 {
                18
            } else {
                1
            };
            if payload.len() == expected {
                if *command == 0x10 && payload.len() == 18 && payload.last() == Some(&0x03) {
                    let mut current = self.system_state.snapshot();
                    current.stable_version = u16::from_be_bytes([payload[0], payload[1]]);
                    let candidate = u16::from_be_bytes([payload[2], payload[3]]);
                    current.candidate_version = (candidate != u16::MAX).then_some(candidate);
                    current.kp_milli = u16::from_be_bytes([payload[4], payload[5]]);
                    current.ki_milli = u16::from_be_bytes([payload[6], payload[7]]);
                    current.kd_milli = u16::from_be_bytes([payload[8], payload[9]]);
                    current.weights = [payload[10], payload[11], payload[12], payload[13]];
                    current.door_policy = payload[14];
                    current.limited_pwm = payload[15];
                    current.target_c = payload[16];
                    self.system_state.replace(current.clone());
                    self.database.record_configuration(&current);
                }
                let status = payload.last().copied();
                if let Some((decision_id, outcome)) = self
                    .command_broker
                    .lock()
                    .expect("command broker lock poisoned")
                    .finish(status)
                {
                    let label = match outcome {
                        serial::CommandStatus::Applied => "applied",
                        serial::CommandStatus::Rejected(_) => "rejected",
                        serial::CommandStatus::Timeout => "timeout",
                        serial::CommandStatus::Queued => "queued",
                    };
                    self.database.update_decision(&decision_id, label);
                    self.status = format!("Команда {decision_id}: {label}");
                }
                self.control_response = None;
            }
            return;
        }
        if data == 0x12
            && self
                .command_broker
                .lock()
                .expect("command broker lock poisoned")
                .begin_next()
                .is_some()
        {
            self.control_response = Some((0xFF, Vec::new()));
            return;
        }
        if self.collector.is_temperature_permission(data) {
            self.collector.reset();
            println!("Board granted permission to send the reference temperature.");
            self.send_reference_temperature();
            return;
        }
        if let Some(payload) = self.collector.feed(data, Instant::now()) {
            if let Some(sample) = decode_telemetry(&payload) {
                let state = SystemState {
                    received_wall_clock_ms: database::wall_clock_ms(),
                    inside_c: sample.inside_valid.then_some(sample.inside),
                    outside_c: sample.outside_valid.then_some(sample.outside),
                    inside_valid: sample.inside_valid,
                    inside_fresh: sample.inside_fresh,
                    outside_valid: sample.outside_valid,
                    outside_fresh: sample.outside_fresh,
                    target_c: sample.reference as u8,
                    door_open: sample.door_open,
                    door_stage: sample.door_stage,
                    mode: sample.mode,
                    door_policy: sample.door_policy,
                    stable_version: sample.stable_version,
                    candidate_version: sample.candidate_version,
                    pwm: sample.pwm,
                    rpm: sample.rpm,
                    failed_mask: sample.failed_mask,
                    ..Default::default()
                };
                self.system_state.replace(state.clone());
                self.database.record_telemetry(
                    (self.started_at.elapsed().as_secs_f64() * 1000.0) as i64,
                    &payload,
                    &state,
                );
                self.store
                    .add(self.started_at.elapsed().as_secs_f64(), sample);
                let candidate = sample
                    .candidate_version
                    .map(|version| version.to_string())
                    .unwrap_or_else(|| "—".to_owned());
                self.status = format!(
                    "Внутри: {:.2} °C ({}{}) | Снаружи: {:.2} °C ({}{}) | Цель: {} °C | \
                     Дверь: {} / стадия {} | Режим: {} | Hall: {} | PWM: {:?} | Отказы: {:04b} | \
                     Конфигурация: {} / {} | Политика двери: {}",
                    sample.inside,
                    if sample.inside_valid {
                        "valid"
                    } else {
                        "invalid"
                    },
                    if sample.inside_fresh {
                        ", fresh"
                    } else {
                        ", stale"
                    },
                    sample.outside,
                    if sample.outside_valid {
                        "valid"
                    } else {
                        "invalid"
                    },
                    if sample.outside_fresh {
                        ", fresh"
                    } else {
                        ", stale"
                    },
                    sample.reference,
                    if sample.door_open {
                        "открыта"
                    } else {
                        "закрыта"
                    },
                    sample.door_stage,
                    sample.mode,
                    if sample.hall { "1" } else { "0" },
                    sample.pwm,
                    sample.failed_mask,
                    sample.stable_version,
                    candidate,
                    sample.door_policy,
                );
            } else {
                self.status = format!("Некорректный кадр телеметрии: {:02X?}", payload);
            }
        }
    }

    fn send_pending_command(&mut self) {
        let Some(port) = self.port.as_mut() else {
            return;
        };
        let next = self
            .command_broker
            .lock()
            .expect("command broker lock poisoned")
            .take_next_to_send();
        let Some(next) = next else {
            return;
        };
        let mut wire = encode_packet(BOARD_ADDR, 0x11);
        for byte in next.command.actuator_bytes() {
            wire.extend(encode_packet(BOARD_ADDR, byte));
        }
        if let Err(error) = port.write_all(&wire) {
            let _ = self
                .command_broker
                .lock()
                .expect("command broker lock poisoned")
                .finish(Some(0xE3));
            self.status = format!("Не удалось отправить команду: {error}");
        }
    }

    fn queue_operator_command(&mut self, command: serial::Command) {
        let decision_id = format!("operator-{}", database::wall_clock_ms());
        let status = self
            .command_broker
            .lock()
            .expect("command broker lock poisoned")
            .submit(decision_id.clone(), command);
        self.database
            .remember_decision(&decision_id, "operator", "queued", "{}");
        self.status = format!("Операторская команда {decision_id}: {:?}", status);
    }

    fn send_reference_temperature(&mut self) {
        let packet = encode_packet(BOARD_ADDR, self.reference_temperature);
        match self
            .port
            .as_mut()
            .expect("connected serial port")
            .write_all(&packet)
        {
            Ok(()) => {
                self.status = format!(
                    "Эталонная температура {} °C отправлена плате.",
                    self.reference_temperature
                )
            }
            Err(error) => self.status = format!("Ошибка отправки: {error}"),
        }
    }

    fn draw_rpm_plot(&self, ui: &mut egui::Ui, index: usize, height: f32) {
        ui.label(
            RichText::new(format!("Кулер {} — raw RPM", index + 1))
                .size(16.0)
                .strong(),
        );
        Self::card_frame().show(ui, |ui| {
            Plot::new(format!("rpm-{}", index + 1))
                .height(height)
                .allow_scroll(false)
                .allow_drag(false)
                .allow_zoom(false)
                .auto_bounds([true, true])
                .show(ui, |plot_ui| {
                    plot_ui.set_auto_bounds([true, true]);
                    plot_ui.line(
                        Line::new(self.store.rpm_points(&self.store.rpm[index]))
                            .name(format!("Кулер {}", index + 1)),
                    );
                });
        });
    }

    fn card_frame() -> egui::Frame {
        egui::Frame::default()
            .fill(CARD)
            .stroke(Stroke::new(1.0, CARD_BORDER))
            .corner_radius(12)
            .inner_margin(egui::Margin::same(14))
    }
}

impl eframe::App for CommutatorApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if let Some((decision_id, outcome)) = self
            .command_broker
            .lock()
            .expect("command broker lock poisoned")
            .expire(Instant::now())
        {
            let label = match outcome {
                serial::CommandStatus::Timeout => "timeout",
                serial::CommandStatus::Applied => "applied",
                serial::CommandStatus::Rejected(_) => "rejected",
                serial::CommandStatus::Queued => "queued",
            };
            self.database.update_decision(&decision_id, label);
        }
        self.poll_serial();
        ctx.request_repaint_after(SERIAL_POLL_INTERVAL);

        egui::TopBottomPanel::top("controls")
            .frame(
                egui::Frame::default()
                    .fill(Color32::from_rgb(244, 247, 252))
                    .inner_margin(egui::Margin::same(16)),
            )
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.vertical(|ui| {
                        ui.label(
                            RichText::new(APPLICATION_NAME)
                                .size(23.0)
                                .strong()
                                .color(Color32::from_rgb(30, 41, 59)),
                        );
                        ui.label(RichText::new("Temperature control console").weak());
                    });
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        let (label, color) = if self.port.is_some() {
                            ("●  CONNECTED", SUCCESS)
                        } else {
                            ("●  OFFLINE", DANGER)
                        };
                        ui.label(RichText::new(label).strong().color(color));
                    });
                });
                ui.separator();
                ui.add_space(4.0);
                ui.horizontal_wrapped(|ui| {
                    ui.label("Порт");
                    egui::ComboBox::from_id_salt("serial-port")
                        .selected_text(if self.selected_port.is_empty() {
                            "Не найден"
                        } else {
                            &self.selected_port
                        })
                        .show_ui(ui, |ui| {
                            for port in &self.ports {
                                ui.selectable_value(&mut self.selected_port, port.clone(), port);
                            }
                        });
                    if ui.button("Обновить порты").clicked() && self.port.is_none() {
                        self.refresh_ports();
                    }
                    let connection_label = if self.port.is_some() {
                        "Отключиться"
                    } else {
                        "Подключиться"
                    };
                    let connection_color = if self.port.is_some() { DANGER } else { ACCENT };
                    if ui
                        .add(
                            egui::Button::new(RichText::new(connection_label).strong())
                                .fill(connection_color),
                        )
                        .clicked()
                    {
                        self.toggle_connection();
                    }
                    ui.separator();
                    ui.add_enabled(
                        self.port.is_some(),
                        egui::DragValue::new(&mut self.reference_temperature)
                            .range(0..=60)
                            .prefix("Эталон: ")
                            .suffix(" °C"),
                    );
                    ui.label("Температура будет отправлена после разрешения платы.");
                });
                ui.horizontal_wrapped(|ui| {
                    ui.label("Ручное безопасное управление:");
                    if ui
                        .add_enabled(self.port.is_some(), egui::Button::new("Снять emergency"))
                        .clicked()
                    {
                        self.queue_operator_command(serial::Command::ClearEmergency);
                    }
                    ui.add_enabled(
                        self.port.is_some(),
                        egui::DragValue::new(&mut self.probe_fan)
                            .range(0..=3)
                            .prefix("Fan "),
                    );
                    if ui
                        .add_enabled(self.port.is_some(), egui::Button::new("Probe fan"))
                        .clicked()
                    {
                        self.queue_operator_command(serial::Command::ProbeFan {
                            fan_index: self.probe_fan,
                        });
                    }
                    let current = self.system_state.snapshot();
                    if let Some(candidate_version) = current.candidate_version {
                        if ui
                            .add_enabled(
                                self.port.is_some(),
                                egui::Button::new("Confirm candidate"),
                            )
                            .clicked()
                        {
                            self.queue_operator_command(serial::Command::Confirm {
                                candidate_version,
                            });
                        }
                        if ui
                            .add_enabled(
                                self.port.is_some(),
                                egui::Button::new("Rollback candidate"),
                            )
                            .clicked()
                        {
                            self.queue_operator_command(serial::Command::Rollback {
                                candidate_version,
                            });
                        }
                    }
                });
                ui.label(&self.status);
            });

        egui::SidePanel::right("fan-plots")
            .default_width(ctx.available_rect().width() * 0.5)
            .min_width(320.0)
            .resizable(true)
            .frame(
                egui::Frame::default()
                    .fill(Color32::from_rgb(244, 247, 252))
                    .inner_margin(egui::Margin::same(16)),
            )
            .show(ctx, |ui| {
                ui.spacing_mut().item_spacing.y = 6.0;
                ui.heading("Кулеры");
                let fan_plot_height = ((ui.available_height() - 212.0) / 4.0).max(80.0);
                self.draw_rpm_plot(ui, 0, fan_plot_height);
                self.draw_rpm_plot(ui, 1, fan_plot_height);
                self.draw_rpm_plot(ui, 2, fan_plot_height);
                self.draw_rpm_plot(ui, 3, fan_plot_height);
            });

        egui::CentralPanel::default()
            .frame(
                egui::Frame::default()
                    .fill(Color32::from_rgb(244, 247, 252))
                    .inner_margin(egui::Margin::same(16)),
            )
            .show(ctx, |ui| {
                ui.heading("Температурный стенд");
                Self::card_frame().show(ui, |ui| {
                    ui.label(
                        RichText::new("Температура внутри стенда")
                            .size(16.0)
                            .strong(),
                    );
                    let inside_bounds = self.store.plot_bounds(&self.store.inside_ma, true);
                    Plot::new("inside-temperature")
                        .height(280.0)
                        .legend(Legend::default())
                        .allow_scroll(false)
                        .allow_drag(false)
                        .allow_zoom(false)
                        .auto_bounds([false, false])
                        .show(ui, |plot_ui| {
                            plot_ui.set_plot_bounds(inside_bounds);
                            for span in self
                                .store
                                .stage_spans(inside_bounds.min()[1], inside_bounds.max()[1])
                            {
                                plot_ui.polygon(span);
                            }
                            plot_ui.line(
                                Line::new(self.store.points(&self.store.inside_ma))
                                    .name("Градусов Цельсия, MA(7)")
                                    .color(Color32::from_rgb(235, 99, 84))
                                    .width(2.5),
                            );
                            if !self.store.reference_temperatures.is_empty() {
                                plot_ui.line(
                                    Line::new(self.store.reference_points())
                                        .name("Эталон")
                                        .color(Color32::BLACK)
                                        .style(LineStyle::Dashed { length: 8.0 })
                                        .width(1.5),
                                );
                            }
                        });
                });
                ui.add_space(12.0);
                Self::card_frame().show(ui, |ui| {
                    ui.label(
                        RichText::new("Температура снаружи стенда")
                            .size(16.0)
                            .strong(),
                    );
                    Plot::new("outside-temperature")
                        .height(280.0)
                        .legend(Legend::default())
                        .allow_scroll(false)
                        .allow_drag(false)
                        .allow_zoom(false)
                        .auto_bounds([true, true])
                        .show(ui, |plot_ui| {
                            plot_ui.set_auto_bounds([true, true]);
                            plot_ui.line(
                                Line::new(self.store.points(&self.store.outside_ma))
                                    .name("Градусов Цельсия, MA(7)")
                                    .color(Color32::from_rgb(54, 139, 230))
                                    .width(2.5),
                            );
                        });
                });
            });
    }
}

fn list_ports() -> Vec<String> {
    serialport::available_ports()
        .map(|ports| ports.into_iter().map(|port| port.port_name).collect())
        .unwrap_or_default()
}

fn main() -> eframe::Result<()> {
    let window_title = format!("{APPLICATION_NAME} v{APPLICATION_VERSION}");
    eframe::run_native(
        &window_title,
        eframe::NativeOptions {
            viewport: egui::ViewportBuilder::default().with_inner_size([1_200.0, 850.0]),
            ..Default::default()
        },
        Box::new(|creation_context| Ok(Box::new(CommutatorApp::new(&creation_context.egui_ctx)))),
    )
}
