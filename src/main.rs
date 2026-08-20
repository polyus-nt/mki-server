use std::collections::VecDeque;
use std::io::{Read, Write};
use std::time::{Duration, Instant};

use eframe::egui;
use egui::{Color32, RichText, Stroke};
use egui_plot::{Legend, Line, LineStyle, Plot, PlotBounds, PlotPoints, Polygon};
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
    door_stage: u8,
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
        door_stage: payload[15],
    })
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
                self.store = TelemetryStore::default();
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
        ui.label(
            RichText::new(format!(
                "Кулер {} — обороты (до {} RPM)",
                index + 1,
                maximum as u32
            ))
            .size(16.0)
            .strong(),
        );
        Self::card_frame().show(ui, |ui| {
            Plot::new(format!("rpm-{}", index + 1))
                .height(100.0)
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
                            RichText::new("TJC COMMUTATOR")
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

        egui::SidePanel::right("fan-plots")
            .default_width(380.0)
            .min_width(320.0)
            .max_width(460.0)
            .resizable(true)
            .frame(
                egui::Frame::default()
                    .fill(Color32::from_rgb(244, 247, 252))
                    .inner_margin(egui::Margin::same(16)),
            )
            .show(ctx, |ui| {
                ui.heading("Кулеры");
                self.draw_rpm_plot(ui, 0, 3_000.0);
                self.draw_rpm_plot(ui, 1, 3_000.0);
                self.draw_rpm_plot(ui, 2, 3_000.0);
                self.draw_rpm_plot(ui, 3, 5_000.0);
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
                                    .name("Внутри, MA(7)")
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
                                    .name("Снаружи, MA(7)")
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
    eframe::run_native(
        "TJC Commutator",
        eframe::NativeOptions {
            viewport: egui::ViewportBuilder::default().with_inner_size([1_200.0, 850.0]),
            ..Default::default()
        },
        Box::new(|creation_context| Ok(Box::new(CommutatorApp::new(&creation_context.egui_ctx)))),
    )
}
