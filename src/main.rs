use std::collections::VecDeque;
use std::io::{Read, Write};
use std::time::{Duration, Instant};

use eframe::egui;
use egui_plot::{HLine, Legend, Line, Plot, PlotPoints};
use serialport::SerialPort;

const COMPUTER_ADDR: u8 = 99;
const BOARD_ADDR: u8 = 12;
const MARKER: u8 = 12;
const ETALON_TEMP_CODE: u8 = 10;
const DEFAULT_ETALON_TEMPERATURE: u8 = 25;
const TELEMETRY_SIZE: usize = 16;
const MAX_POINTS: usize = 1_000;
const MA_WINDOW: usize = 7;
const SERIAL_POLL_INTERVAL: Duration = Duration::from_millis(10);

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

struct TelemetryCollector {
    capturing: bool,
    buffer: Vec<u8>,
}

impl TelemetryCollector {
    fn new() -> Self {
        Self {
            capturing: false,
            buffer: Vec::with_capacity(TELEMETRY_SIZE),
        }
    }

    fn reset(&mut self) {
        self.capturing = false;
        self.buffer.clear();
    }

    fn is_temperature_permission(&self, value: u8) -> bool {
        self.capturing && self.buffer == [ETALON_TEMP_CODE] && value == MARKER
    }

    fn feed(&mut self, value: u8) -> Option<Vec<u8>> {
        if !self.capturing {
            if value == MARKER {
                self.capturing = true;
                self.buffer.clear();
            }
            return None;
        }

        if self.buffer.len() < TELEMETRY_SIZE {
            self.buffer.push(value);
            return None;
        }

        if value == MARKER {
            self.capturing = false;
            return Some(std::mem::take(&mut self.buffer));
        }

        self.reset();
        None
    }
}

#[derive(Clone, Copy)]
struct Telemetry {
    inside: f64,
    outside: f64,
    rpm: [u32; 4],
    reference: i8,
}

fn signed_temperature(value: u8) -> i8 {
    value as i8
}

fn decode_temperature(whole: u8, fraction: u8) -> f64 {
    f64::from(signed_temperature(whole)) + (f64::from(fraction) * 99.0 / 255.0).round() / 100.0
}

fn decode_rpm(whole: u8, fraction: u8) -> u32 {
    let encoded = u32::from(u16::from_be_bytes([whole, fraction]));
    (encoded * 5_000 + 32_767) / 65_535
}

fn decode_telemetry(payload: &[u8]) -> Option<Telemetry> {
    if payload.len() != TELEMETRY_SIZE {
        return None;
    }
    Some(Telemetry {
        inside: decode_temperature(payload[2], payload[3]),
        outside: decode_temperature(payload[4], payload[5]),
        rpm: [
            decode_rpm(payload[6], payload[7]),
            decode_rpm(payload[8], payload[9]),
            decode_rpm(payload[10], payload[11]),
            decode_rpm(payload[12], payload[13]),
        ],
        reference: signed_temperature(payload[14]),
    })
}

#[derive(Default)]
struct TelemetryStore {
    times: VecDeque<f64>,
    inside_ma: VecDeque<f64>,
    outside_ma: VecDeque<f64>,
    rpm: [VecDeque<u32>; 4],
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
        for (values, rpm) in self.rpm.iter_mut().zip(sample.rpm) {
            values.push_back(rpm);
        }
        while self.times.len() > MAX_POINTS {
            self.times.pop_front();
            self.inside_ma.pop_front();
            self.outside_ma.pop_front();
            for values in &mut self.rpm {
                values.pop_front();
            }
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
}

impl CommutatorApp {
    fn new() -> Self {
        let ports = list_ports();
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
                self.started_at = Instant::now();
                self.status = format!("Подключено к {} (9600 бод).", self.selected_port);
                self.port = Some(port);
            }
            Err(error) => self.status = format!("Ошибка подключения: {error}"),
        }
    }

    fn poll_serial(&mut self) {
        if self.port.is_none() || self.last_poll.elapsed() < SERIAL_POLL_INTERVAL {
            return;
        }
        self.last_poll = Instant::now();
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
        if self.collector.is_temperature_permission(data) {
            self.collector.reset();
            println!("Board granted permission to send the reference temperature.");
            self.send_reference_temperature();
            return;
        }
        if let Some(payload) = self.collector.feed(data) {
            if let Some(sample) = decode_telemetry(&payload) {
                self.store
                    .add(self.started_at.elapsed().as_secs_f64(), sample);
                self.status = format!(
                    "Температура внутри: {:.2} °C | Снаружи: {:.2} °C | Эталон: {} °C",
                    sample.inside, sample.outside, sample.reference
                );
            } else {
                self.status = format!("Некорректный кадр телеметрии: {:02X?}", payload);
            }
        }
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

    fn draw_rpm_plot(&self, ui: &mut egui::Ui, index: usize, maximum: f64) {
        Plot::new(format!("rpm-{}", index + 1))
            .height(145.0)
            .include_y(0.0)
            .include_y(maximum)
            .allow_scroll(false)
            .show(ui, |plot_ui| {
                plot_ui.line(
                    Line::new(self.store.rpm_points(&self.store.rpm[index]))
                        .name(format!("Кулер {}", index + 1)),
                );
            });
    }
}

impl eframe::App for CommutatorApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.poll_serial();
        ctx.request_repaint_after(SERIAL_POLL_INTERVAL);

        egui::TopBottomPanel::top("controls").show(ctx, |ui| {
            ui.horizontal_wrapped(|ui| {
                egui::ComboBox::from_label("COM-порт")
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
                if ui
                    .button(if self.port.is_some() {
                        "Отключиться"
                    } else {
                        "Подключиться"
                    })
                    .clicked()
                {
                    self.toggle_connection();
                }
                ui.separator();
                ui.add_enabled(
                    self.port.is_some(),
                    egui::DragValue::new(&mut self.reference_temperature)
                        .range(1..=255)
                        .prefix("Эталон: ")
                        .suffix(" °C"),
                );
                ui.label("Температура будет отправлена после разрешения платы.");
            });
            ui.label(&self.status);
        });

        egui::CentralPanel::default().show(ctx, |ui| {
            ui.heading("Температурный стенд");
            Plot::new("temperatures")
                .height(280.0)
                .legend(Legend::default())
                .allow_scroll(false)
                .show(ui, |plot_ui| {
                    plot_ui.line(
                        Line::new(self.store.points(&self.store.inside_ma)).name("Внутри, MA(7)"),
                    );
                    plot_ui.line(
                        Line::new(self.store.points(&self.store.outside_ma)).name("Снаружи, MA(7)"),
                    );
                    plot_ui.hline(HLine::new(f64::from(self.reference_temperature)).name("Эталон"));
                });
            ui.columns(2, |columns| {
                self.draw_rpm_plot(&mut columns[0], 0, 3_000.0);
                self.draw_rpm_plot(&mut columns[1], 1, 3_000.0);
                self.draw_rpm_plot(&mut columns[0], 2, 3_000.0);
                self.draw_rpm_plot(&mut columns[1], 3, 5_000.0);
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
    eframe::run_native(
        "TJC Commutator",
        eframe::NativeOptions {
            viewport: egui::ViewportBuilder::default().with_inner_size([1_200.0, 850.0]),
            ..Default::default()
        },
        Box::new(|_creation_context| Ok(Box::new(CommutatorApp::new()))),
    )
}
