#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]
use eframe::egui::{self, RichText};
use egui_plot::{Legend, Line, Plot, PlotPoints};
use serde_json::Value;
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
mod database;
mod mcp;
mod serial;
mod state;
use serial::{Command, CommandBroker, Outcome, SerialRuntime, UserApi};
use state::SharedState;
struct App {
    runtime: SerialRuntime,
    broker: Arc<Mutex<CommandBroker>>,
    state: SharedState,
    ports: Vec<String>,
    port: String,
    status: String,
    token: String,
    show_token: bool,
    fan: u8,
    pwm: u8,
    mode: u8,
    pending: Vec<tokio::sync::oneshot::Receiver<Outcome>>,
    history: [VecDeque<[f64; 2]>; 3],
    last_sample: [i64; 3],
    rpm_history: [VecDeque<[f64; 2]>; 6],
    last_rpm: [i64; 6],
    start: Instant,
}
impl App {
    fn new() -> Self {
        let database = database::Database::open("tjc-commutator.sqlite").expect("open database");
        let broker = Arc::new(Mutex::new(CommandBroker::with_database(database.clone())));
        let state = SharedState::default();
        let runtime = SerialRuntime::start(broker.clone(), state.clone(), database.clone());
        let server = mcp::start(state.clone(), database, UserApi::new(broker.clone()));
        let ports = ports();
        let port = ports.first().cloned().unwrap_or_default();
        Self {
            runtime,
            broker,
            state,
            ports,
            port,
            status: "Выберите порт. Прошивки должны использовать Hardware API v2.".into(),
            token: server.token,
            show_token: false,
            fan: 0,
            pwm: 0,
            mode: 0,
            pending: Vec::new(),
            history: Default::default(),
            last_sample: [0; 3],
            rpm_history: Default::default(),
            last_rpm: [0; 6],
            start: Instant::now(),
        }
    }
    fn submit(&mut self, c: Command) {
        // GUI and MCP share the same broker; GUI supplies a unique decision_id.
        let id = format!("gui-{:032x}", rand::random::<u128>());
        match self.broker.lock().unwrap().submit(Some(id), c) {
            Ok(rx) => {
                self.pending.push(rx);
                self.status = "Ожидаем реальный ответ оборудования…".into();
            }
            Err(e) => self.status = e.into(),
        }
    }
    fn sample(v: &Option<Value>) -> String {
        match v {
            None => "—".into(),
            Some(v) => {
                if v["ok"] == false {
                    return format!("Ошибка: {}", v["error"]["code"]);
                }
                let value = v
                    .get("value")
                    .or_else(|| v.get("applied_pwm"))
                    .cloned()
                    .unwrap_or(Value::Null);
                let mut text = if v.get("adc").is_some() {
                    format!("ADC {}; {} V", v["adc"], v["voltage"])
                } else {
                    format!("{} {}", value, v["unit"].as_str().unwrap_or(""))
                };
                let received = v["received_wall_clock_ms"].as_i64().unwrap_or(0);
                let local = (database::wall_clock_ms() - received).max(0) as u64;
                text.push_str(&format!("; кэш +{local} мс"));
                if let Some(age) = v["age_ms"].as_u64() {
                    text.push_str(&format!(
                        "; {}: возраст датчика {age} мс",
                        if v["fresh"] == true { "fresh" } else { "stale" }
                    ));
                }
                text
            }
        }
    }
}
impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        while let Ok(n) = self.runtime.notices.try_recv() {
            self.status = n;
        }
        self.pending.retain_mut(|r| match r.try_recv() {
            Ok(o) => {
                self.status = o.result.to_string();
                false
            }
            Err(tokio::sync::oneshot::error::TryRecvError::Empty) => true,
            Err(_) => {
                self.status = "Владелец порта остановлен".into();
                false
            }
        });
        let state = self.state.snapshot();
        for i in 0..6 {
            if let Some(v) = &state.rpm[i] {
                let timestamp = v["received_wall_clock_ms"].as_i64().unwrap_or(0);
                if timestamp != self.last_rpm[i] && v["ok"] == true {
                    if let Some(value) = v["value"].as_f64() {
                        self.rpm_history[i].push_back([self.start.elapsed().as_secs_f64(), value]);
                        if self.rpm_history[i].len() > 1000 {
                            self.rpm_history[i].pop_front();
                        }
                    }
                    self.last_rpm[i] = timestamp;
                }
            }
        }
        for i in 0..3 {
            if let Some(v) = &state.temperature[i] {
                let timestamp = v["received_wall_clock_ms"].as_i64().unwrap_or(0);
                if timestamp != self.last_sample[i] && v["ok"] == true {
                    if let Some(value) = v["value"].as_f64() {
                        self.history[i].push_back([self.start.elapsed().as_secs_f64(), value]);
                        if self.history[i].len() > 1000 {
                            self.history[i].pop_front();
                        }
                    }
                    self.last_sample[i] = timestamp;
                }
            }
        }
        ctx.request_repaint_after(Duration::from_millis(50));
        egui::TopBottomPanel::top("connection").show(ctx, |ui| {
            ui.heading("МС-ТЮК — аппаратный API");
            ui.horizontal(|ui| {
                egui::ComboBox::from_id_salt("port")
                    .selected_text(&self.port)
                    .show_ui(ui, |ui| {
                        for p in &self.ports {
                            ui.selectable_value(&mut self.port, p.clone(), p);
                        }
                    });
                if ui.button("Обновить порты").clicked() {
                    self.ports = ports();
                }
                if state.connected {
                    if ui.button("Отключиться").clicked() {
                        self.runtime.disconnect();
                    }
                } else if ui.button("Подключиться").clicked() {
                    self.runtime.connect(self.port.clone());
                }
                ui.label(if state.connected {
                    "9600 бод; порт открыт"
                } else {
                    "Нет связи"
                });
            });
            ui.horizontal(|ui| {
                ui.label("MCP: http://127.0.0.1:8765/mcp");
                ui.checkbox(&mut self.show_token, "Показать token");
                if self.show_token {
                    ui.label(&self.token);
                    if ui.button("Копировать").clicked() {
                        ctx.copy_text(self.token.clone());
                    }
                }
            });
            ui.label(&self.status);
            let (waiting, decisions, ids) = self.broker.lock().unwrap().metrics();
            ui.label(format!(
                "Очередь: {waiting}/64; кэш решений: {decisions}/4096; до смены сессии: {ids}"
            ));
        });
        egui::CentralPanel::default().show(ctx, |ui| {
            egui::ScrollArea::vertical().show(ui, |ui| {
                ui.heading("Датчики");
                egui::Grid::new("sensors").striped(true).show(ui, |ui| {
                    ui.label("idx");
                    ui.label("Температура BMP280");
                    ui.label("Давление BMP280");
                    ui.label("Влажность DHT11");
                    ui.end_row();
                    for i in 0..3 {
                        ui.label(i.to_string());
                        ui.label(Self::sample(&state.temperature[i]));
                        ui.label(Self::sample(&state.pressure[i]));
                        ui.label(Self::sample(&state.humidity[i]));
                        ui.end_row();
                    }
                });
                for i in 0..2 {
                    ui.label(format!("VOC {i}: {}   Дверь {i}: {}",
                        Self::sample(&state.voc[i]), Self::sample(&state.doors[i])));
                }
                Plot::new("temperature").height(220.0).legend(Legend::default()).show(ui, |plot| {
                    for i in 0..3 {
                        let points: PlotPoints = self.history[i].iter().copied().collect();
                        plot.line(Line::new(points).name(format!("BMP280 {i}, °C")));
                    }
                });
                ui.separator();
                for i in 0..2 {
                    ui.label(format!("GP2Y1010AU0F {i}: {} (без концентрации)", Self::sample(&state.dust[i])));
                }
                ui.heading("Шесть вентиляторов");
                Plot::new("rpm").height(180.0).legend(Legend::default()).show(ui, |plot| {
                    for i in 0..6 {
                        let points: PlotPoints = self.rpm_history[i].iter().copied().collect();
                        plot.line(Line::new(points).name(format!("Fan {i}, RPM")));
                    }
                });
                egui::Grid::new("fans").striped(true).show(ui, |ui| {
                    ui.label("idx");
                    ui.label("Применённый PWM");
                    ui.label("RPM");
                    ui.end_row();
                    for i in 0..6 {
                        ui.label(i.to_string());
                        ui.label(Self::sample(&state.pwm[i]));
                        ui.label(Self::sample(&state.rpm[i]));
                        ui.end_row();
                    }
                });
                ui.add_enabled_ui(state.connected, |ui| {
                    ui.horizontal(|ui| {
                        ui.add(egui::DragValue::new(&mut self.fan).range(0..=5).prefix("Вентилятор "));
                        ui.add(egui::DragValue::new(&mut self.pwm).range(0..=255).prefix("PWM "));
                        if ui.button("Установить PWM").clicked() {
                            self.submit(Command::SetPwm { idx: self.fan, pwm: self.pwm });
                        }
                    });
                    ui.collapsing("Admin API — только локальная админка", |ui| {
                        ui.label("Режим неисправности относится к одному вентилятору. Не публикуется в MCP.");
                        ui.add(egui::DragValue::new(&mut self.mode).range(0..=2).prefix("Режим "));
                        if ui.button("Применить режим").clicked() {
                            self.submit(Command::SetMode { idx: self.fan, mode: self.mode });
                        }
                        if ui.button("Прочитать режим").clicked() { self.submit(Command::GetMode); }
                    });
                });
                ui.label(RichText::new("Пыль: ADC и напряжение без калибровки концентрации. Замки и пылесос ещё не подключены. Ошибки/тайм-ауты не вызывают повтор управляющей команды.").weak());
            });
        });
    }
}
fn ports() -> Vec<String> {
    serialport::available_ports()
        .map(|p| p.into_iter().map(|p| p.port_name).collect())
        .unwrap_or_default()
}
fn main() -> eframe::Result<()> {
    eframe::run_native(
        "МС-ТЮК Commutator",
        eframe::NativeOptions {
            viewport: egui::ViewportBuilder::default().with_inner_size([1200.0, 900.0]),
            ..Default::default()
        },
        Box::new(|_| Ok(Box::new(App::new()))),
    )
}
