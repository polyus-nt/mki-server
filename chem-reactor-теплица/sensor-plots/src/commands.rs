//! Ручные команды идут через FastAPI и тот же последовательный поток, что и графики.
use serde_json::{json, Value};

#[cfg(all(test, feature = "gui"))]
mod http_tests {
    use super::*;
    use std::{
        io::{Read, Write},
        net::TcpListener,
        thread,
        time::Duration,
    };

    // Локальный HTTP-сервер без FastAPI и аппаратуры: проверяет реальный запрос клиента.
    fn mock_server(status: &str, body: &str) -> (String, thread::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let redirect = if status.starts_with("302") {
            "Location: http://127.0.0.1:1/must-not-follow\r\n"
        } else {
            ""
        };
        let response = format!(
            "HTTP/1.1 {status}\r\n{redirect}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let worker = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut bytes = Vec::new();
            loop {
                let mut buf = [0; 1024];
                let n = socket.read(&mut buf).unwrap();
                assert!(n > 0);
                bytes.extend_from_slice(&buf[..n]);
                if let Some(end) = bytes.windows(4).position(|s| s == b"\r\n\r\n") {
                    let header = String::from_utf8_lossy(&bytes[..end]).to_lowercase();
                    let size: usize = header
                        .lines()
                        .find_map(|s| s.strip_prefix("content-length:"))
                        .map(|s| s.trim().parse().unwrap())
                        .unwrap_or(0);
                    if bytes.len() >= end + 4 + size {
                        break;
                    }
                }
            }
            socket.write_all(response.as_bytes()).unwrap();
            String::from_utf8(bytes).unwrap()
        });
        (url, worker)
    }
    fn client() -> reqwest::blocking::Client {
        reqwest::blocking::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(3))
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .build()
            .unwrap()
    }
    #[test]
    fn sends_post_json_and_shows_actual_response() {
        let (url, server) = mock_server("200 OK", r#"{"idx":1,"pwm":230}"#);
        let response = gui::execute(&client(), &url, &command("set_pwm 1 100").unwrap()).unwrap();
        let request = server.join().unwrap();
        assert!(request.starts_with("POST /api/v1/fans/1/pwm HTTP/1.1\r\n"));
        assert!(request
            .to_lowercase()
            .contains("content-type: application/json"));
        assert_eq!(
            serde_json::from_str::<Value>(request.split_once("\r\n\r\n").unwrap().1).unwrap(),
            json!({"pwm":100})
        );
        assert!(response.contains("200 OK"));
        assert!(response.contains("230")); // Показываем применённое значение, а не ожидаемое.
    }
    #[test]
    fn displays_http_errors_and_does_not_follow_redirects() {
        let (url, server) = mock_server("503 Service Unavailable", r#"{"error":"DEVICE_TIMEOUT"}"#);
        let response =
            gui::execute(&client(), &url, &command("set_pwm 0 100").unwrap()).unwrap_err();
        assert!(response.contains("503"));
        assert!(response.contains("DEVICE_TIMEOUT"));
        assert!(server
            .join()
            .unwrap()
            .starts_with("POST /api/v1/fans/0/pwm"));
        let (url, server) = mock_server("302 Found", "not followed");
        assert!(gui::execute(&client(), &url, &command("temp 0").unwrap())
            .unwrap_err()
            .contains("302"));
        server.join().unwrap();
    }
    #[test]
    fn response_size_is_bounded() {
        let (url, server) = mock_server("200 OK", &"x".repeat(65537));
        assert!(gui::execute(&client(), &url, &command("temp 0").unwrap())
            .unwrap_err()
            .contains("64 KiB"));
        server.join().unwrap();
    }
}

const CHEM: bool = true;

#[derive(Debug, PartialEq)]
pub struct Command {
    pub path: String,
    pub body: Option<Value>,
}

pub fn help() -> &'static str {
    if CHEM {
        "temp/pressure/humidity/gas 0..1 | door/pwm/rpm 0..1\nlock | relay | capabilities | probe\nset_pwm idx 0..255 | set_door_lock 0|1 (общий выход)\nset_relay_level 0..10"
    } else {
        "temp/pressure/humidity 0..2 | voc 0..1 | dust\ndoor/lock 0..1 | pwm/rpm 0..5 | vacuum | mode | capabilities | probe\nset_pwm idx 0..255 | set_door_lock idx 0|1\nset_vacuum 0|1 | set_mode mode idx (mode 0..2, idx 0..5)"
    }
}

pub fn command(input: &str) -> Result<Command, String> {
    let parts: Vec<_> = input.split_whitespace().collect();
    let name = match parts.first().copied().unwrap_or("") {
        "get_temp" => "temp",
        "get_pressure" => "pressure",
        "get_humidity" => "humidity",
        "get_voc" => "voc",
        "get_gas" => "gas",
        "get_dust" => "dust",
        "get_door_state" => "door",
        "get_pwm" => "pwm",
        "get_rpm" => "rpm",
        "get_door_lock" => "lock",
        "get_vacuum" => "vacuum",
        "get_mode" => "mode",
        "get_relay_level" => "relay",
        "setpwm" => "set_pwm",
        "setlock" => "set_door_lock",
        "setvacuum" => "set_vacuum",
        "setmode" => "set_mode",
        "setrelay" => "set_relay_level",
        name => name,
    };
    let fans = if CHEM { 1 } else { 5 };
    // Только известные методы; произвольные URL и неоднозначные аргументы запрещены.
    let (route, limits): (&str, &[u16]) = match name {
        "temp" => ("temperature", &[if CHEM { 1 } else { 2 }]),
        "pressure" | "humidity" => (name, &[if CHEM { 1 } else { 2 }]),
        "voc" if !CHEM => ("voc", &[1]),
        "gas" if CHEM => ("gas", &[1]),
        "door" => ("doors", &[1]),
        "pwm" | "rpm" => (name, &[fans]),
        "lock" if !CHEM => ("locks", &[1]),
        "lock" => ("lock/state", &[]),
        "relay" if CHEM => ("relay-fan/level", &[]),
        "dust" if !CHEM => ("dust", &[]),
        "vacuum" if !CHEM => ("vacuum/state", &[]),
        "mode" if !CHEM => ("admin/mode", &[]),
        "capabilities" => ("capabilities", &[]),
        "probe" => ("hub/probe", &[]),
        "set_pwm" => ("", &[fans, 255]),
        "set_door_lock" if CHEM => ("", &[1]),
        "set_door_lock" => ("", &[1, 1]),
        "set_vacuum" if !CHEM => ("", &[1]),
        "set_mode" if !CHEM => ("", &[2, fans]),
        "set_relay_level" if CHEM => ("", &[10]),
        _ => return Err("Неизвестная команда. Откройте справку команд.".into()),
    };
    if parts.len() != limits.len() + 1 {
        return Err(format!("{}: нужно аргументов {}", name, limits.len()));
    }
    let args: Vec<u16> = parts[1..]
        .iter()
        .zip(limits)
        .map(|(s, max)| {
            let v: u16 = s
                .parse()
                .map_err(|_| format!("Аргумент {s}: нужно целое число 0..{max}"))?;
            if v > *max {
                return Err(format!("Аргумент {s}: допустимо 0..{max}"));
            }
            Ok(v)
        })
        .collect::<Result<_, String>>()?;
    let (path, body) = match name {
        "set_pwm" => (
            format!("fans/{}/pwm", args[0]),
            Some(json!({"pwm": args[1]})),
        ),
        "set_door_lock" if CHEM => (
            "admin/lock".into(),
            Some(json!({"lock_state": args[0] == 1})),
        ),
        "set_door_lock" => (
            format!("admin/locks/{}", args[0]),
            Some(json!({"lock_state": args[1] == 1})),
        ),
        "set_vacuum" => ("admin/vacuum".into(), Some(json!({"mode": args[0]}))),
        "set_mode" => (
            "admin/mode".into(),
            Some(json!({"mode": args[0], "idx": args[1]})),
        ),
        "set_relay_level" => ("relay-fan/level".into(), Some(json!({"level": args[0]}))),
        "pwm" | "rpm" => (format!("fans/{}/{}", args[0], route), None),
        "door" | "lock" if !args.is_empty() => (format!("{route}/{}/state", args[0]), None),
        _ if args.is_empty() => (route.into(), None),
        _ => (format!("{route}/{}", args[0]), None),
    };
    Ok(Command {
        path: format!("/api/v1/{path}"),
        body,
    })
}

#[cfg(feature = "gui")]
pub mod gui {
    use super::*;
    use eframe::egui;
    use std::{
        collections::VecDeque,
        sync::mpsc::{self, Receiver, SyncSender, TryRecvError},
    };

    pub struct Console {
        input: String,
        pending: bool,
        requests: SyncSender<(String, Command)>,
        responses: Receiver<(String, String)>,
        history: VecDeque<(String, String)>,
    }
    pub struct Worker {
        requests: Receiver<(String, Command)>,
        responses: SyncSender<(String, String)>,
        ctx: egui::Context,
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        #[test]
        fn queue_and_log_are_bounded_and_panel_renders_without_hardware() {
            let ctx = egui::Context::default();
            let (mut console, _worker) = console(ctx.clone());
            console
                .requests
                .try_send(("pwm 0".into(), command("pwm 0").unwrap()))
                .unwrap();
            assert!(console
                .requests
                .try_send(("pwm 1".into(), command("pwm 1").unwrap()))
                .is_err());
            for i in 0..30 {
                console.record(i.to_string(), "answer".into());
            }
            assert_eq!(console.history.len(), 20);
            assert_eq!(console.history.front().unwrap().0, "10");
            let _ = ctx.run(egui::RawInput::default(), |ctx| {
                egui::CentralPanel::default().show(ctx, |ui| console.show(ui));
            });
        }
    }
    pub fn console(ctx: egui::Context) -> (Console, Worker) {
        let (tx, requests) = mpsc::sync_channel(1);
        let (responses, rx) = mpsc::sync_channel(1);
        (
            Console {
                input: String::new(),
                pending: false,
                requests: tx,
                responses: rx,
                history: VecDeque::new(),
            },
            Worker {
                requests,
                responses,
                ctx,
            },
        )
    }

    /// Один HTTP-запрос без повторной отправки, даже если ответ потерялся.
    pub fn execute(
        client: &reqwest::blocking::Client,
        url: &str,
        c: &Command,
    ) -> Result<String, String> {
        let request = if let Some(body) = &c.body {
            client
                .post(format!("{url}{}", c.path))
                .header(reqwest::header::CONTENT_TYPE, "application/json")
                .body(body.to_string())
        } else {
            client.get(format!("{url}{}", c.path))
        };
        let response = request.send().map_err(|e| format!("HTTP: {e}. Для записи результат неизвестен: команда могла исполниться. Автоповтора нет."))?;
        let status = response.status();
        use std::io::Read;
        let mut body = String::new();
        response
            .take(65537)
            .read_to_string(&mut body)
            .map_err(|e| format!("Тело ответа: {e}"))?;
        if body.len() > 65536 {
            return Err("Ответ больше 64 KiB".into());
        }
        let formatted = serde_json::from_str::<Value>(&body)
            .ok()
            .and_then(|v| serde_json::to_string_pretty(&v).ok())
            .unwrap_or(body);
        let prefix = if c.body.is_some() { "POST" } else { "GET" };
        let text = format!(
            "{prefix} {}\nHTTP {status}\n{}",
            c.path,
            formatted.chars().take(8000).collect::<String>()
        );
        if status.is_success() {
            Ok(text)
        } else {
            Err(text)
        }
    }
    impl Worker {
        /// Вызывается между чтениями датчиков и в паузах, не параллельно им.
        pub fn process_pending(&self, client: &reqwest::blocking::Client, url: &str) {
            if let Ok((input, c)) = self.requests.try_recv() {
                let result = match execute(client, url, &c) {
                    Ok(s) => s,
                    Err(s) => format!("Ошибка: {s}"),
                };
                let _ = self.responses.try_send((input, result));
                self.ctx.request_repaint();
            }
        }
    }
    impl Console {
        fn record(&mut self, input: String, output: String) {
            if self.history.len() == 20 {
                self.history.pop_front();
            }
            self.history.push_back((input, output));
        }
        pub fn show(&mut self, ui: &mut egui::Ui) {
            loop {
                match self.responses.try_recv() {
                    Ok((input, output)) => {
                        self.pending = false;
                        self.record(input, output);
                    }
                    Err(TryRecvError::Disconnected) if self.pending => {
                        self.pending = false;
                        self.record(
                            "HTTP".into(),
                            "Рабочий поток остановлен. Перезапустите приложение.".into(),
                        );
                        break;
                    }
                    Err(_) => break,
                }
            }
            ui.heading("Команды на платы");
            ui.label(if CHEM {
                "Команды записи физически переключают оборудование. Замки: 1 = обе двери закрыты; 0 = открыты."
            } else {
                "Команды записи физически переключают оборудование. 1 = замок закрыт / пылесос включён; 0 = открыт / выключен."
            });
            ui.collapsing("Справка команд", |ui| {
                ui.monospace(help());
            });
            let mut send = false;
            ui.horizontal(|ui| {
                let field = ui.add(
                    egui::TextEdit::singleline(&mut self.input)
                        .hint_text("Например: set_pwm 0 128")
                        .desired_width(350.0)
                        .char_limit(256),
                );
                let enter = field.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
                let click = ui
                    .add_enabled(!self.pending, egui::Button::new("Отправить"))
                    .clicked();
                send = !self.pending && (enter || click);
            });
            if send {
                let input = self.input.trim().to_owned();
                if input == "help" {
                    self.record(input, help().into());
                } else {
                    match command(&input) {
                        Err(e) => self.record(input, e),
                        Ok(c) => match self.requests.try_send((input.clone(), c)) {
                            Ok(()) => self.pending = true,
                            Err(e) => self.record(input, format!("Не отправлено: {e}")),
                        },
                    }
                }
            }
            if self.pending {
                ui.label("Ожидание текущего запроса и ответа команды…");
            }
            egui::ScrollArea::vertical()
                .id_salt("commands")
                .max_height(180.0)
                .stick_to_bottom(true)
                .show(ui, |ui| {
                    for (input, output) in &self.history {
                        ui.monospace(format!("> {input}\n{output}"));
                        ui.separator();
                    }
                });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reads_and_pwm_routes() {
        let c = command("get_pwm 1").unwrap();
        assert_eq!(c.path, "/api/v1/fans/1/pwm");
        assert!(c.body.is_none());
        assert_eq!(command("door 0").unwrap().path, "/api/v1/doors/0/state");
        assert_eq!(command("temp 0").unwrap().path, "/api/v1/temperature/0");
        let c = command("set_pwm 1 255").unwrap();
        assert_eq!(c.path, "/api/v1/fans/1/pwm");
        assert_eq!(c.body, Some(json!({"pwm":255})));
    }
    #[test]
    fn validates_before_sending() {
        for s in [
            "",
            "unknown",
            "set_pwm",
            "set_pwm 0 -1",
            "set_pwm 0 256",
            "set_pwm 6 1",
            "set_pwm 0 1 extra",
            "temp 3",
            "door 2",
            "dust 0",
            "set_pwm 0 1.5",
            "get https://example.com",
        ] {
            assert!(command(s).is_err(), "{s}");
        }
    }
    #[test]
    fn greenhouse_specific_controls() {
        if CHEM {
            assert!(command("set_mode 1 0").is_err());
            assert!(command("set_vacuum 1").is_err());
            assert!(command("set_pwm 2 100").is_err());
            assert!(command("voc 0").is_err());
            let lock = command("set_door_lock 1").unwrap();
            assert_eq!(lock.path, "/api/v1/admin/lock");
            assert_eq!(lock.body, Some(json!({"lock_state":true})));
            assert_eq!(
                command("set_relay_level 10").unwrap().body,
                Some(json!({"level":10}))
            );
            assert!(command("set_relay_level 11").is_err());
            assert!(command("set_door_lock 0 1").is_err());
        } else {
            assert!(command("set_relay_level 1").is_err());
            assert!(command("gas 0").is_err());
            assert_eq!(command("set_pwm 5 0").unwrap().body, Some(json!({"pwm":0})));
            let lock = command("set_door_lock 1 0").unwrap();
            assert_eq!(lock.path, "/api/v1/admin/locks/1");
            assert_eq!(lock.body, Some(json!({"lock_state":false})));
            assert_eq!(
                command("set_mode 2 5").unwrap().body,
                Some(json!({"mode":2,"idx":5}))
            );
            assert_eq!(
                command("set_vacuum 1").unwrap().body,
                Some(json!({"mode":1}))
            );
            assert!(command("set_mode 3 0").is_err());
            assert!(command("set_door_lock 0 2").is_err());
        }
    }
}
