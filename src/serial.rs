//! Hardware API v1: one bounded FIFO, one serial owner, never automatic retries.
use crate::{database::Database, state::SharedState};
use serde::Serialize;
use serde_json::{Value, json};
use std::collections::{HashMap, VecDeque};
use std::io::{Read, Write};
use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};
use tokio::sync::oneshot;

pub const HOST_TIMEOUT: Duration = Duration::from_secs(5);
pub const QUEUE_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_WAITING: usize = 64;
const MAX_DECISIONS: usize = 4096;
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Command {
    Temperature(u8),
    Voc(u8),
    Door(u8),
    Pressure(u8),
    Humidity(u8),
    #[allow(dead_code)] // Reserved wire command; no dust tool before units/calibration are agreed.
    Dust(u8),
    Pwm(u8),
    Rpm(u8),
    SetPwm {
        idx: u8,
        pwm: u8,
    },
    GetMode,
    SetMode {
        idx: u8,
        mode: u8,
    },
}
impl Command {
    pub fn fields(&self) -> (u8, u8, u8, u8) {
        match *self {
            Self::Temperature(i) => (13, 0x20, i, 0),
            Self::Voc(i) => (13, 0x21, i, 0),
            Self::Door(i) => (13, 0x22, i, 0),
            Self::Pressure(i) => (13, 0x23, i, 0),
            Self::Humidity(i) => (13, 0x24, i, 0),
            Self::Dust(i) => (13, 0x25, i, 0),
            Self::Pwm(i) => (14, 0x30, i, 0),
            Self::Rpm(i) => (14, 0x31, i, 0),
            Self::SetPwm { idx, pwm } => (14, 0x32, idx, pwm),
            Self::GetMode => (14, 0x40, 255, 0),
            Self::SetMode { idx, mode } => (14, 0x41, idx, mode),
        }
    }
    pub fn validate(&self) -> Result<(), &'static str> {
        let (_, code, idx, value) = self.fields();
        let count = match code {
            0x20 | 0x23 | 0x24 => 3,
            0x21 | 0x22 | 0x25 => 2,
            _ => 6,
        };
        if code != 0x40 && idx >= count {
            return Err("invalid index");
        }
        if code == 0x41 && value > 2 {
            return Err("invalid mode");
        }
        Ok(())
    }
    pub fn mutating(&self) -> bool {
        matches!(self, Self::SetPwm { .. } | Self::SetMode { .. })
    }
    pub fn admin(&self) -> bool {
        matches!(self, Self::GetMode | Self::SetMode { .. })
    }
    pub fn frame(&self, tx_id: u16) -> Frame {
        let (node, command, idx, value) = self.fields();
        let mut data = [0; 8];
        data[0] = value;
        Frame {
            version: 1,
            kind: 1,
            tx_id,
            node,
            command,
            status: 0,
            idx,
            data,
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Frame {
    pub version: u8,
    pub kind: u8,
    pub tx_id: u16,
    pub node: u8,
    pub command: u8,
    pub status: u8,
    pub idx: u8,
    pub data: [u8; 8],
}
pub fn crc16(bytes: &[u8]) -> u16 {
    let mut crc = 0xffffu16;
    for b in bytes {
        crc ^= u16::from(*b) << 8;
        for _ in 0..8 {
            crc = if crc & 0x8000 != 0 {
                (crc << 1) ^ 0x1021
            } else {
                crc << 1
            };
        }
    }
    crc
}
impl Frame {
    pub fn encode(&self) -> [u8; 20] {
        let mut b = [0; 20];
        b[..4].copy_from_slice(&[0xa5, 0x5a, self.version, self.kind]);
        b[4..6].copy_from_slice(&self.tx_id.to_be_bytes());
        b[6..10].copy_from_slice(&[self.node, self.command, self.status, self.idx]);
        b[10..18].copy_from_slice(&self.data);
        let c = crc16(&b[2..18]);
        b[18..].copy_from_slice(&c.to_be_bytes());
        b
    }
    pub fn decode(b: &[u8]) -> Option<Self> {
        if b.len() != 20
            || b[..2] != [0xa5, 0x5a]
            || crc16(&b[2..18]) != u16::from_be_bytes([b[18], b[19]])
        {
            return None;
        }
        Some(Self {
            version: b[2],
            kind: b[3],
            tx_id: u16::from_be_bytes([b[4], b[5]]),
            node: b[6],
            command: b[7],
            status: b[8],
            idx: b[9],
            data: b[10..18].try_into().ok()?,
        })
    }
    fn matches(&self, r: &Frame) -> bool {
        self.version == 1
            && self.kind == 2
            && self.tx_id == r.tx_id
            && self.node == r.node
            && self.command == r.command
            && self.idx == r.idx
    }
    pub fn result(&self) -> Result<Value, &'static str> {
        let d = self.data;
        if self.status > 12 {
            return Err("unknown hardware status");
        }
        if self.status != 0 {
            if d != [0; 8] {
                return Err("nonzero error payload");
            }
            let mut result = json!({"ok":false,"error":{"code":status_name(self.status)}});
            if matches!(self.status, 4 | 5) {
                result["value"] = Value::Null;
            }
            if matches!(self.command, 0x32 | 0x41) && matches!(self.status, 7 | 9 | 12) {
                result["error"]["outcome"] = json!("unknown");
            }
            return Ok(result);
        }
        match self.command {
            0x20 | 0x21 | 0x23 | 0x24 => {
                let age = u16::from_be_bytes([d[5], d[6]]);
                if !matches!(d[4], 1 | 3) || d[7] != 0 || age == 65535 {
                    return Err("invalid measurement quality");
                }
                let raw = u32::from_be_bytes(d[..4].try_into().unwrap());
                let (value, unit) = match self.command {
                    0x20 => {
                        let signed = raw as i32;
                        if !(-4000..=8500).contains(&signed) {
                            return Err("temperature out of BMP280 range");
                        }
                        (json!(f64::from(signed) / 100.0), "degC")
                    }
                    0x21 => {
                        if !(1..=500).contains(&raw) {
                            return Err("VOC Index out of range");
                        }
                        (json!(raw), "VOC Index")
                    }
                    0x24 => {
                        if raw > 10000 {
                            return Err("humidity out of range");
                        }
                        (json!(f64::from(raw) / 100.0), "%RH")
                    }
                    _ => {
                        if raw == 0 {
                            return Err("invalid pressure");
                        }
                        (json!(raw), "Pa")
                    }
                };
                Ok(
                    json!({"ok":true,"value":value,"unit":unit,"valid":true,"fresh":d[4]&2!=0,"age_ms":age}),
                )
            }
            0x31 => {
                if d[4..] != [0; 4] {
                    return Err("nonzero RPM padding");
                }
                Ok(
                    json!({"ok":true,"value":u32::from_be_bytes(d[..4].try_into().unwrap()),"unit":"rpm"}),
                )
            }
            0x40 | 0x41 => {
                if d[0] > 2 || (if d[0] == 0 { d[1] != 255 } else { d[1] >= 6 }) || d[2..] != [0; 6]
                {
                    return Err("invalid mode payload");
                }
                Ok(json!({"ok":true,"mode":d[0],"idx":if d[0]==0{Value::Null}else{json!(d[1])}}))
            }
            0x22 | 0x30 | 0x32 => {
                if d[1..] != [0; 7] {
                    return Err("nonzero scalar padding");
                }
                if self.command == 0x22 && d[0] > 1 {
                    return Err("door must be bool");
                }
                Ok(match self.command {
                    0x22 => json!({"ok":true,"value":d[0]!=0}),
                    0x32 => json!({"ok":true,"applied_pwm":d[0]}),
                    _ => json!({"ok":true,"value":d[0]}),
                })
            }
            _ => Err("successful response has no defined decoder"),
        }
    }
}
fn needs_recovery(status: u8) -> bool {
    matches!(status, 7 | 9 | 12)
}
fn status_name(c: u8) -> &'static str {
    [
        "OK",
        "INVALID_INDEX",
        "INVALID_ARGUMENT",
        "UNKNOWN_COMMAND",
        "NOT_IMPLEMENTED",
        "SENSOR_UNAVAILABLE",
        "BUSY",
        "DEVICE_TIMEOUT",
        "UNSUPPORTED_VERSION",
        "INTERNAL_ERROR",
        "REQUEST_ID_CONFLICT",
        "INVALID_NODE",
        "TRANSPORT_OVERFLOW",
    ][usize::from(c)]
}
/// Sliding parsers preserve CR/LF/magic inside binary payloads.
#[derive(Default)]
pub struct Parser {
    envelope: VecDeque<u8>,
    frame: VecDeque<u8>,
    first: Option<Instant>,
    last: Option<Instant>,
}
impl Parser {
    pub fn feed(&mut self, byte: u8, now: Instant) -> Option<Frame> {
        self.envelope.push_back(byte);
        if self.envelope.len() < 5 {
            return None;
        }
        let e: Vec<u8> = self.envelope.iter().copied().collect();
        if e[0] != b'\r'
            || e[4] != b'\n'
            || !e[1..3]
                .iter()
                .all(|b| b.is_ascii_digit() || (b'A'..=b'F').contains(b))
        {
            self.envelope.pop_front();
            return None;
        }
        self.envelope.clear();
        if e[1..3] != *b"63" {
            return None;
        }
        self.feed_payload(e[3], now)
    }
    pub fn feed_payload(&mut self, byte: u8, now: Instant) -> Option<Frame> {
        if self
            .last
            .is_some_and(|t| now.duration_since(t) >= Duration::from_millis(250))
            || self
                .first
                .is_some_and(|t| now.duration_since(t) >= Duration::from_secs(1))
        {
            self.frame.clear();
            self.first = None;
        }
        if self.frame.is_empty() {
            self.first = Some(now);
        }
        self.last = Some(now);
        self.frame.push_back(byte);
        while self.frame.len() >= 2 && (self.frame[0] != 0xa5 || self.frame[1] != 0x5a) {
            self.frame.pop_front();
            self.first = Some(now);
        }
        if self.frame.len() < 20 {
            return None;
        }
        let bytes: Vec<u8> = self.frame.iter().copied().collect();
        if let Some(f) = Frame::decode(&bytes)
            && matches!(f.kind, 1 | 2)
        {
            self.frame.clear();
            self.first = None;
            return Some(f);
        }
        self.frame.pop_front();
        self.first = Some(now);
        None
    }
}
pub fn envelope(a: u8, b: u8) -> [u8; 5] {
    const HEX: &[u8] = b"0123456789ABCDEF";
    [
        b'\r',
        HEX[usize::from(a >> 4)],
        HEX[usize::from(a & 15)],
        b,
        b'\n',
    ]
}
#[derive(Clone, Debug, Serialize)]
pub struct Outcome {
    pub decision_id: Option<String>,
    pub tx_id: Option<u16>,
    pub result: Value,
}
struct Job {
    id: Option<String>,
    command: Command,
    queued: Instant,
    tx_id: u16,
    sent: Option<Instant>,
    waiters: Vec<oneshot::Sender<Outcome>>,
}
struct Record {
    command: Command,
    outcome: Option<Outcome>,
}
pub struct CommandBroker {
    waiting: VecDeque<Job>,
    inflight: Option<Job>,
    records: HashMap<String, Record>,
    next_tx: u32,
    online: bool,
}
impl Default for CommandBroker {
    fn default() -> Self {
        Self {
            waiting: VecDeque::new(),
            inflight: None,
            records: HashMap::new(),
            next_tx: 1,
            online: false,
        }
    }
}
impl CommandBroker {
    pub fn metrics(&self) -> (usize, usize, u32) {
        (
            self.waiting.len(),
            self.records.len(),
            65536u32.saturating_sub(self.next_tx),
        )
    }
    pub fn submit(
        &mut self,
        id: Option<String>,
        command: Command,
    ) -> Result<oneshot::Receiver<Outcome>, &'static str> {
        command.validate()?;
        if let Some(ref id) = id {
            if id.is_empty() || id.len() > 128 {
                return Err("decision_id must have 1..128 bytes");
            }
            if let Some(record) = self.records.get(id) {
                if record.command != command {
                    return Err("decision_id conflict: different command or arguments");
                }
                let (tx, rx) = oneshot::channel();
                if let Some(outcome) = &record.outcome {
                    let _ = tx.send(outcome.clone());
                } else {
                    let j = self
                        .waiting
                        .iter_mut()
                        .chain(self.inflight.iter_mut())
                        .find(|j| j.id.as_ref() == Some(id))
                        .unwrap();
                    if j.waiters.len() >= 64 {
                        return Err("too many duplicate waiters");
                    }
                    j.waiters.push(tx);
                }
                return Ok(rx);
            }
            if self.records.len() >= MAX_DECISIONS {
                return Err(
                    "decision retention limit reached; restart requires operator reconciliation",
                );
            }
        }
        if !self.online {
            return Err("serial disconnected");
        }
        if self.waiting.len() >= MAX_WAITING {
            return Err("queue full");
        }
        if self.next_tx > u32::from(u16::MAX) {
            return Err(
                "hardware transaction IDs exhausted; restart requires operator reconciliation",
            );
        }
        let (tx, rx) = oneshot::channel();
        if let Some(ref id) = id {
            self.records.insert(
                id.clone(),
                Record {
                    command: command.clone(),
                    outcome: None,
                },
            );
        }
        let tx_id = self.next_tx as u16;
        self.next_tx += 1;
        self.waiting.push_back(Job {
            id,
            command,
            queued: Instant::now(),
            tx_id,
            sent: None,
            waiters: vec![tx],
        });
        Ok(rx)
    }
    fn finish(&mut self, j: Job, result: Value) -> Outcome {
        let o = Outcome {
            decision_id: j.id.clone(),
            tx_id: Some(j.tx_id),
            result,
        };
        if let Some(id) = &j.id {
            self.records.get_mut(id).unwrap().outcome = Some(o.clone());
        }
        for w in j.waiters {
            let _ = w.send(o.clone());
        }
        o
    }
    pub fn take_next(&mut self, now: Instant) -> Option<Frame> {
        if self.inflight.is_some() {
            return None;
        }
        self.expire_waiting(now);
        let mut j = self.waiting.pop_front()?;
        j.sent = Some(now);
        let f = j.command.frame(j.tx_id);
        self.inflight = Some(j);
        Some(f)
    }
    pub fn accept(&mut self, r: &Frame) -> Option<(Command, Outcome)> {
        let j = self.inflight.as_ref()?;
        if !r.matches(&j.command.frame(j.tx_id)) {
            return None;
        }
        let mut result = r.result().ok()?;
        if let Command::SetMode { idx, mode } = j.command {
            if r.status == 0
                && (r.data[0] != mode || r.data[1] != if mode == 0 { 255 } else { idx })
            {
                return None;
            }
        }
        if j.command.mutating() && matches!(r.status, 7 | 9 | 12) {
            result["error"]["outcome"] = json!("unknown");
        }
        let j = self.inflight.take().unwrap();
        let c = j.command.clone();
        Some((c, self.finish(j, result)))
    }
    fn expire_waiting(&mut self, now: Instant) {
        let mut survivors = VecDeque::new();
        while let Some(j) = self.waiting.pop_front() {
            if now.duration_since(j.queued) >= QUEUE_TIMEOUT {
                self.finish(
                    j,
                    json!({"ok":false,"error":{"code":"QUEUE_TIMEOUT","outcome":"not_sent"}}),
                );
            } else {
                survivors.push_back(j);
            }
        }
        self.waiting = survivors;
    }
    pub fn expire(&mut self, now: Instant) -> bool {
        self.expire_waiting(now);
        if self
            .inflight
            .as_ref()
            .is_some_and(|j| now.duration_since(j.sent.unwrap()) >= HOST_TIMEOUT)
        {
            let j = self.inflight.take().unwrap();
            let unknown = j.command.mutating();
            self.finish(j,json!({"ok":false,"error":{"code":"HOST_TIMEOUT","outcome":if unknown{"unknown"}else{"no_result"}}}));
            true
        } else {
            false
        }
    }
    fn disconnect(&mut self) {
        self.online = false;
        if let Some(j) = self.inflight.take() {
            let unknown = j.command.mutating();
            self.finish(j,json!({"ok":false,"error":{"code":"DISCONNECTED","outcome":if unknown{"unknown"}else{"no_result"}}}));
        }
        while let Some(j) = self.waiting.pop_front() {
            self.finish(
                j,
                json!({"ok":false,"error":{"code":"DISCONNECTED","outcome":"not_sent"}}),
            );
        }
    }
}
/// MCP has only UserApi, not the broker or an administrative interface.
#[derive(Clone)]
pub struct UserApi {
    broker: Arc<Mutex<CommandBroker>>,
}
impl UserApi {
    pub fn new(broker: Arc<Mutex<CommandBroker>>) -> Self {
        Self { broker }
    }
    pub async fn execute(&self, id: Option<String>, c: Command) -> Result<Outcome, &'static str> {
        if c.admin() {
            return Err("administrative command is not part of User API");
        }
        let rx = self
            .broker
            .lock()
            .map_err(|_| "broker unavailable")?
            .submit(id, c)?;
        rx.await.map_err(|_| "serial owner stopped")
    }
}
enum Control {
    Connect(String),
    Disconnect,
    Stop,
}
pub struct SerialRuntime {
    control: mpsc::SyncSender<Control>,
    pub notices: mpsc::Receiver<String>,
}
impl SerialRuntime {
    pub fn start(
        broker: Arc<Mutex<CommandBroker>>,
        state: SharedState,
        database: Database,
    ) -> Self {
        let (control, controls) = mpsc::sync_channel(16);
        let (notice, notices) = mpsc::sync_channel(32);
        std::thread::spawn(move || {
            let mut port: Option<Box<dyn serialport::SerialPort>> = None;
            let mut parser = Parser::default();
            let mut quiet_until = Instant::now();
            let mut recovery_cap = Instant::now();
            let mut poll_disabled = false;
            let mut last_poll = Instant::now();
            let mut poll_idx = 0usize;
            let start = Instant::now();
            let polls: Vec<Command> = (0..3)
                .map(Command::Temperature)
                .chain((0..3).map(Command::Pressure))
                .chain((0..2).map(Command::Voc))
                .chain((0..3).map(Command::Humidity))
                .chain((0..2).map(Command::Door))
                .chain((0..6).map(Command::Pwm))
                .chain((0..6).map(Command::Rpm))
                .collect();
            loop {
                let mut stop = false;
                loop {
                    let c = match controls.try_recv() {
                        Ok(c) => c,
                        Err(mpsc::TryRecvError::Empty) => break,
                        Err(mpsc::TryRecvError::Disconnected) => {
                            stop = true;
                            break;
                        }
                    };
                    broker.lock().unwrap().disconnect();
                    port = None;
                    parser = Parser::default();
                    state.set_connected(false);
                    match c {
                        Control::Connect(name) => match serialport::new(&name, 9600)
                            .timeout(Duration::from_millis(10))
                            .open()
                        {
                            Ok(p) => {
                                port = Some(p);
                                quiet_until = Instant::now() + Duration::from_secs(1);
                                recovery_cap = quiet_until + Duration::from_secs(2);
                                broker.lock().unwrap().online = true;
                                state.set_connected(true);
                                let _ = notice.try_send(format!(
                                    "Подключено: {name}; ожидание тишины перед обменом"
                                ));
                            }
                            Err(e) => {
                                let _ = notice.try_send(format!("Ошибка подключения: {e}"));
                            }
                        },
                        Control::Disconnect => {
                            let _ = notice.try_send("Отключено".into());
                        }
                        Control::Stop => stop = true,
                    }
                }
                if stop {
                    broker.lock().unwrap().disconnect();
                    state.set_connected(false);
                    break;
                }
                let now = Instant::now();
                if broker.lock().unwrap().expire(now) {
                    parser = Parser::default();
                    quiet_until = now + Duration::from_secs(1);
                    recovery_cap = quiet_until + Duration::from_secs(2);
                    let _ = notice.try_send("Тайм-аут: не повторяем команду, ждём тишины".into());
                }
                let Some(p) = port.as_mut() else {
                    std::thread::sleep(Duration::from_millis(10));
                    continue;
                };
                let mut input = [0u8; 256];
                let error = match p.read(&mut input) {
                    Ok(n) => {
                        if n != 0 && Instant::now() < quiet_until {
                            quiet_until = Instant::now() + Duration::from_secs(1);
                            parser = Parser::default();
                        } else {
                            for byte in &input[..n] {
                                if let Some(f) = parser.feed(*byte, Instant::now()) {
                                    let accepted = { broker.lock().unwrap().accept(&f) };
                                    if let Some((c, o)) = accepted {
                                        // An error reply can precede the hub's recovery quiet period.
                                        // Keep the next FIFO command queued until the bus settles.
                                        if needs_recovery(f.status) {
                                            parser = Parser::default();
                                            quiet_until = Instant::now() + Duration::from_secs(1);
                                            recovery_cap = quiet_until + Duration::from_secs(2);
                                        }
                                        state.apply(&c, &o.result);
                                        database.record_telemetry(
                                            start.elapsed().as_millis() as i64,
                                            &f.encode(),
                                            &state.snapshot(),
                                        );
                                    }
                                }
                            }
                        }
                        None
                    }
                    Err(e)
                        if matches!(
                            e.kind(),
                            std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                        ) =>
                    {
                        None
                    }
                    Err(e) => Some(e.to_string()),
                };
                if let Some(e) = error {
                    broker.lock().unwrap().disconnect();
                    state.set_connected(false);
                    port = None;
                    let _ = notice.try_send(format!("Ошибка порта: {e}"));
                    continue;
                }
                if Instant::now() < quiet_until {
                    if Instant::now() >= recovery_cap {
                        broker.lock().unwrap().disconnect();
                        state.set_connected(false);
                        port = None;
                        let _ = notice.try_send(
                            "Шина не затихла за 3 с: обмен остановлен, очередь завершена".into(),
                        );
                    }
                    continue;
                }
                let mut locked = broker.lock().unwrap();
                // Background polling is in the same FIFO, and yields to all pending callers.
                if !poll_disabled
                    && locked.waiting.is_empty()
                    && locked.inflight.is_none()
                    && last_poll.elapsed() >= Duration::from_millis(250)
                {
                    if let Err(e) = locked.submit(None, polls[poll_idx].clone()) {
                        poll_disabled = true;
                        let _ = notice.try_send(e.into());
                    }
                    poll_idx = (poll_idx + 1) % polls.len();
                    last_poll = Instant::now();
                }
                let next = locked.take_next(Instant::now());
                drop(locked);
                if let Some(f) = next {
                    let p = port.as_mut().unwrap();
                    let mut failure = None;
                    for byte in f.encode() {
                        if let Err(e) = p.write_all(&envelope(12, byte)) {
                            failure = Some(e.to_string());
                            break;
                        }
                        if let Err(e) = p.flush() {
                            failure = Some(e.to_string());
                            break;
                        }
                        std::thread::sleep(Duration::from_millis(2));
                    }
                    if let Some(e) = failure {
                        broker.lock().unwrap().disconnect();
                        state.set_connected(false);
                        port = None;
                        let _ =
                            notice.try_send(format!("Ошибка отправки; результат неизвестен: {e}"));
                    }
                }
            }
        });
        Self { control, notices }
    }
    pub fn connect(&self, p: String) {
        let _ = self.control.try_send(Control::Connect(p));
    }
    pub fn disconnect(&self) {
        let _ = self.control.try_send(Control::Disconnect);
    }
}
impl Drop for SerialRuntime {
    fn drop(&mut self) {
        let _ = self.control.try_send(Control::Stop);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn broker() -> CommandBroker {
        let mut b = CommandBroker::default();
        b.online = true;
        b
    }
    fn response(r: &Frame, d: [u8; 8]) -> Frame {
        let mut f = r.clone();
        f.kind = 2;
        f.data = d;
        f
    }
    #[test]
    fn crc_and_golden_vectors() {
        assert_eq!(crc16(b"123456789"), 0x29b1);
        let v: Value =
            serde_json::from_str(include_str!("../docs/protocol/hardware-v1-vectors.json"))
                .unwrap();
        for c in v["round_trips"].as_array().unwrap() {
            for k in ["request_hex", "response_hex"] {
                let b: Vec<u8> = c[k]
                    .as_str()
                    .unwrap()
                    .split_whitespace()
                    .map(|s| u8::from_str_radix(s, 16).unwrap())
                    .collect();
                assert_eq!(Frame::decode(&b).unwrap().encode().to_vec(), b);
            }
            let bytes: Vec<u8> = c["response_hex"]
                .as_str()
                .unwrap()
                .split_whitespace()
                .map(|s| u8::from_str_radix(s, 16).unwrap())
                .collect();
            let mut actual = Frame::decode(&bytes).unwrap().result().unwrap();
            let mut expected = c["api_result"].clone();
            // JSON 100 and 100.0 denote the same numeric quantity, not different API units.
            if actual["value"].is_number() && expected["value"].is_number() {
                assert_eq!(
                    actual["value"].as_f64(),
                    expected["value"].as_f64(),
                    "{}",
                    c["name"]
                );
                actual.as_object_mut().unwrap().remove("value");
                expected.as_object_mut().unwrap().remove("value");
            }
            assert_eq!(actual, expected, "{}", c["name"]);
        }
    }
    #[test]
    fn parser_resync_noise_crc_and_binary_delimiters() {
        let mut f = Command::Rpm(5).frame(9);
        f.kind = 2;
        f.data = [0xa5, 0x5a, 13, 10, 0, 0, 0, 0];
        let bytes = f.encode();
        let now = Instant::now();
        let mut p = Parser::default();
        for b in [0xff, 13, 1, 2, 3, 4] {
            assert!(p.feed(b, now).is_none());
        }
        let mut bad = bytes;
        bad[18] ^= 1;
        let mut found = Vec::new();
        for b in bad.into_iter().chain(bytes) {
            for e in envelope(99, b) {
                if let Some(f) = p.feed(e, now) {
                    found.push(f);
                }
            }
        }
        assert_eq!(found, vec![f]);
        for b in &bytes[..10] {
            p.feed_payload(*b, now);
        }
        let later = now + Duration::from_secs(2);
        let frames: Vec<_> = bytes
            .into_iter()
            .filter_map(|b| p.feed_payload(b, later))
            .collect();
        assert_eq!(frames.len(), 1);
    }
    #[test]
    fn fifo_actual_results_duplicates_conflicts_correlations() {
        let mut b = broker();
        let mut rx = b
            .submit(Some("x".into()), Command::SetPwm { idx: 5, pwm: 100 })
            .unwrap();
        let mut duplicate = b
            .submit(Some("x".into()), Command::SetPwm { idx: 5, pwm: 100 })
            .unwrap();
        assert!(
            b.submit(Some("x".into()), Command::SetPwm { idx: 5, pwm: 99 })
                .is_err()
        );
        let mut second = b.submit(None, Command::Pwm(0)).unwrap();
        let r = b.take_next(Instant::now()).unwrap();
        assert!(rx.try_recv().is_err());
        assert!(b.take_next(Instant::now()).is_none());
        let mut wrong = response(&r, [230, 0, 0, 0, 0, 0, 0, 0]);
        wrong.tx_id += 1;
        assert!(b.accept(&wrong).is_none());
        wrong.tx_id = r.tx_id;
        wrong.data[7] = 1;
        assert!(b.accept(&wrong).is_none());
        wrong.data[7] = 0;
        b.accept(&wrong).unwrap();
        assert_eq!(rx.try_recv().unwrap().result["applied_pwm"], 230);
        assert_eq!(duplicate.try_recv().unwrap().result["applied_pwm"], 230);
        assert!(second.try_recv().is_err());
        let next = b.take_next(Instant::now()).unwrap();
        assert!(next.tx_id > r.tx_id);
        b.accept(&response(&next, [0; 8]));
        assert!(second.try_recv().unwrap().result["ok"].as_bool().unwrap());
        let mut repeat = b
            .submit(Some("x".into()), Command::SetPwm { idx: 5, pwm: 100 })
            .unwrap();
        assert_eq!(repeat.try_recv().unwrap().result["applied_pwm"], 230);
    }
    #[test]
    fn deadlines_disconnect_capacity_and_no_tx_reuse() {
        let mut b = broker();
        let mut rx = b.submit(None, Command::SetPwm { idx: 0, pwm: 50 }).unwrap();
        let first = b.take_next(Instant::now()).unwrap();
        assert!(b.expire(Instant::now() + HOST_TIMEOUT));
        assert_eq!(rx.try_recv().unwrap().result["error"]["outcome"], "unknown");
        assert!(b.inflight.is_none());
        let mut waiting = b.submit(None, Command::Pwm(0)).unwrap();
        b.disconnect();
        assert_eq!(
            waiting.try_recv().unwrap().result["error"]["outcome"],
            "not_sent"
        );
        b.online = true;
        b.submit(None, Command::Pwm(0)).unwrap();
        let next = b.take_next(Instant::now()).unwrap();
        assert!(next.tx_id > first.tx_id);
        b.next_tx = 65536;
        assert!(b.submit(None, Command::Pwm(0)).is_err());
        let mut b = broker();
        let mut rx = b.submit(None, Command::Pwm(0)).unwrap();
        b.expire(Instant::now() + QUEUE_TIMEOUT);
        assert_eq!(
            rx.try_recv().unwrap().result["error"]["code"],
            "QUEUE_TIMEOUT"
        );
        for _ in 0..MAX_WAITING {
            b.submit(None, Command::Pwm(0)).unwrap();
        }
        assert!(b.submit(None, Command::Pwm(0)).is_err());
    }
    #[tokio::test]
    async fn user_api_denies_admin() {
        let u = UserApi::new(Arc::new(Mutex::new(broker())));
        assert!(u.execute(None, Command::GetMode).await.is_err());
        assert!(
            u.execute(None, Command::SetMode { idx: 0, mode: 1 })
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn user_api_waits_for_actual_reply_not_queue_ack() {
        let broker = Arc::new(Mutex::new(broker()));
        let user = UserApi::new(broker.clone());
        let pending = tokio::spawn(async move {
            user.execute(
                Some("await-test".into()),
                Command::SetPwm { idx: 2, pwm: 100 },
            )
            .await
            .unwrap()
        });
        tokio::task::yield_now().await;
        assert!(!pending.is_finished());
        let request = broker.lock().unwrap().take_next(Instant::now()).unwrap();
        assert!(!pending.is_finished());
        broker
            .lock()
            .unwrap()
            .accept(&response(&request, [230, 0, 0, 0, 0, 0, 0, 0]))
            .unwrap();
        assert_eq!(pending.await.unwrap().result["applied_pwm"], 230);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn actual_serial_owner_round_trip_over_pseudo_terminal() {
        use std::ffi::CStr;
        use std::os::fd::FromRawFd;
        // A PTY is a test transport, not proof of GPIO/half-duplex behavior on the stand.
        let (mut master_fd, mut slave_fd) = (0, 0);
        let mut name = [0i8; 128];
        unsafe {
            assert_eq!(
                libc::openpty(
                    &mut master_fd,
                    &mut slave_fd,
                    name.as_mut_ptr(),
                    std::ptr::null(),
                    std::ptr::null()
                ),
                0
            );
            assert_ne!(libc::fcntl(master_fd, libc::F_SETFL, libc::O_NONBLOCK), -1);
        }
        let name = unsafe { CStr::from_ptr(name.as_ptr()) }
            .to_str()
            .unwrap()
            .to_owned();
        let mut master = unsafe { std::fs::File::from_raw_fd(master_fd) };
        let _slave = unsafe { std::fs::File::from_raw_fd(slave_fd) };
        let broker = Arc::new(Mutex::new(CommandBroker::default()));
        let state = SharedState::default();
        let runtime = SerialRuntime::start(
            broker.clone(),
            state.clone(),
            Database::open(":memory:").unwrap(),
        );
        runtime.connect(name);
        let deadline = Instant::now() + Duration::from_secs(3);
        while !state.snapshot().connected {
            assert!(Instant::now() < deadline, "worker did not open the PTY");
            std::thread::sleep(Duration::from_millis(5));
        }
        let mut first = broker
            .lock()
            .unwrap()
            .submit(Some("pty-pwm".into()), Command::SetPwm { idx: 5, pwm: 100 })
            .unwrap();
        let mut second = broker
            .lock()
            .unwrap()
            .submit(None, Command::Humidity(2))
            .unwrap();
        let mut third = broker
            .lock()
            .unwrap()
            .submit(None, Command::SetPwm { idx: 0, pwm: 50 })
            .unwrap();
        let mut fourth = broker
            .lock()
            .unwrap()
            .submit(None, Command::Pwm(0))
            .unwrap();
        let read_request = |master: &mut std::fs::File| {
            let end = Instant::now() + Duration::from_secs(4);
            let mut packets = Vec::new();
            while packets.len() < 100 {
                let mut byte = [0];
                match master.read(&mut byte) {
                    Ok(1) => packets.push(byte[0]),
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(2))
                    }
                    other => panic!("unexpected PTY read: {other:?}"),
                }
                assert!(
                    Instant::now() < end,
                    "no complete request from serial owner"
                );
            }
            let data: Vec<_> = packets
                .chunks_exact(5)
                .map(|packet| {
                    assert_eq!(&packet[..3], b"\r0C");
                    assert_eq!(packet[4], b'\n');
                    packet[3]
                })
                .collect();
            Frame::decode(&data).unwrap()
        };
        let request = read_request(&mut master);
        assert_eq!(request.command, 0x32);
        assert_eq!(request.idx, 5);
        assert_eq!(request.data[0], 100);
        assert!(first.try_recv().is_err());
        assert!(second.try_recv().is_err());
        let mut wrong = response(&request, [230, 0, 0, 0, 0, 0, 0, 0]);
        wrong.tx_id += 1;
        for byte in wrong.encode() {
            master.write_all(&envelope(99, byte)).unwrap();
        }
        std::thread::sleep(Duration::from_millis(25));
        assert!(first.try_recv().is_err());
        wrong.tx_id = request.tx_id;
        for byte in wrong.encode() {
            master.write_all(&envelope(99, byte)).unwrap();
        }
        let deadline = Instant::now() + Duration::from_secs(2);
        let result = loop {
            match first.try_recv() {
                Ok(o) => break o,
                Err(_) => {
                    assert!(Instant::now() < deadline);
                    std::thread::sleep(Duration::from_millis(5));
                }
            }
        };
        assert_eq!(result.result["applied_pwm"], 230);
        let request2 = read_request(&mut master);
        assert_eq!(request2.command, 0x24);
        assert_eq!(request2.idx, 2);
        for byte in response(&request2, [0, 0, 0x15, 0xae, 3, 0, 0, 0]).encode() {
            master.write_all(&envelope(99, byte)).unwrap();
        }
        let deadline = Instant::now() + Duration::from_secs(2);
        let result = loop {
            match second.try_recv() {
                Ok(o) => break o,
                Err(_) => {
                    assert!(Instant::now() < deadline);
                    std::thread::sleep(Duration::from_millis(5));
                }
            }
        };
        assert_eq!(result.result["value"], 55.5);
        let request3 = read_request(&mut master);
        assert_eq!(request3.command, 0x32);
        let mut failure = response(&request3, [0; 8]);
        failure.status = 7;
        let failure_time = Instant::now();
        for byte in failure.encode() {
            master.write_all(&envelope(99, byte)).unwrap();
        }
        std::thread::sleep(Duration::from_millis(300));
        let mut scratch = [0; 1];
        assert_eq!(
            master.read(&mut scratch).unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock,
            "next request was sent while hub still recovering"
        );
        assert_eq!(
            third.try_recv().unwrap().result["error"]["outcome"],
            "unknown"
        );
        let request4 = read_request(&mut master);
        assert!(failure_time.elapsed() >= Duration::from_millis(900));
        assert_eq!(request4.command, 0x30);
        for byte in response(&request4, [50, 0, 0, 0, 0, 0, 0, 0]).encode() {
            master.write_all(&envelope(99, byte)).unwrap();
        }
        let deadline = Instant::now() + Duration::from_secs(2);
        while fourth.try_recv().is_err() {
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
        let mut awaiting_close = broker
            .lock()
            .unwrap()
            .submit(None, Command::Pwm(1))
            .unwrap();
        drop(runtime);
        let deadline = Instant::now() + Duration::from_secs(2);
        let closed = loop {
            match awaiting_close.try_recv() {
                Ok(o) => break o,
                Err(_) => {
                    assert!(Instant::now() < deadline);
                    std::thread::sleep(Duration::from_millis(5));
                }
            }
        };
        assert_eq!(closed.result["error"]["code"], "DISCONNECTED");
        assert!(!state.snapshot().connected);
    }
}
