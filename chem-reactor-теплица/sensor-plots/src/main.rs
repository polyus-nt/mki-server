use chem_sensor_plots::{parse, Channel, History, Layout, Reading};
use eframe::egui;
use egui_plot::{Legend, Line, Plot, Points};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver},
        Arc,
    },
    thread,
    time::{Duration, Instant},
};

const TITLE: &str = "Химреактор — датчики";
const DEFAULT_URL: &str = "http://127.0.0.1:8001";
fn channels() -> Vec<Channel> {
    let mut c = Vec::new();
    for (name, label, unit, count) in [
        ("temperature", "Температура", "degC", 2),
        ("pressure", "Давление", "Pa", 2),
        ("humidity", "Влажность", "%RH", 2),
        ("gas", "MQ-2", "ADC/V", 2),
    ] {
        for idx in 0..count {
            c.push(Channel {
                label: format!("{label} {idx}"),
                path: format!("/api/v1/{name}/{idx}"),
                unit,
                layout: if name == "gas" {
                    Layout::Analog
                } else {
                    Layout::Scalar
                },
            });
        }
    }

    c
}
struct Options {
    url: String,
    interval: Duration,
    timeout: Duration,
    points: usize,
    max_age_ms: u32,
}
impl Options {
    fn args() -> Result<Self, String> {
        let mut o = Self {
            url: DEFAULT_URL.into(),
            interval: Duration::from_secs(2),
            timeout: Duration::from_secs(45),
            points: 1800,
            max_age_ms: 10000,
        };
        let mut args = std::env::args().skip(1);
        while let Some(arg) = args.next() {
            if arg == "--help" {
                println!(
                    "--url URL --interval SECONDS --timeout SECONDS --points COUNT --max-age-ms MS"
                );
                std::process::exit(0);
            }
            let value = args
                .next()
                .ok_or_else(|| format!("Нет значения для {arg}"))?;
            match arg.as_str() {
                "--url" => o.url = value.trim_end_matches('/').into(),
                "--interval" | "--timeout" => {
                    let v: f64 = value.parse().map_err(|_| format!("Некорректный {arg}"))?;
                    if !v.is_finite() || !(0.1..=3600.0).contains(&v) {
                        return Err(format!("{arg}: диапазон 0.1..3600 с"));
                    }
                    if arg == "--interval" {
                        o.interval = Duration::from_secs_f64(v);
                    } else {
                        o.timeout = Duration::from_secs_f64(v);
                    }
                }
                "--max-age-ms" => {
                    o.max_age_ms = value.parse().map_err(|_| "Некорректный --max-age-ms")?;
                    if o.max_age_ms > 65533 {
                        return Err("--max-age-ms: диапазон 0..65533".into());
                    }
                }
                "--points" => {
                    o.points = value.parse().map_err(|_| "Некорректный --points")?;
                    if !(10..=100000).contains(&o.points) {
                        return Err("--points: диапазон 10..100000".into());
                    }
                }
                _ => return Err(format!("Неизвестный аргумент {arg}")),
            }
        }
        let url = reqwest::Url::parse(&o.url).map_err(|e| format!("URL: {e}"))?;
        if !["http", "https"].contains(&url.scheme())
            || url.host_str().is_none()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err("Нужен базовый HTTP(S) URL без query/fragment".into());
        }
        Ok(o)
    }
}
struct Event {
    index: usize,
    time: f64,
    reading: Result<Reading, String>,
}
struct State {
    histories: Vec<History>,
    status: String,
    last_sample: Option<Instant>,
}
struct App {
    channels: Vec<Channel>,
    states: Vec<State>,
    events: Receiver<Event>,
    stop: Arc<AtomicBool>,
    url: String,
    max_age_ms: u32,
    console: chem_sensor_plots::commands::gui::Console,
}
impl App {
    fn new(o: Options, ctx: egui::Context) -> Self {
        let channels = channels();
        let states = channels
            .iter()
            .map(|c| State {
                histories: (0..if matches!(c.layout, Layout::Analog) {
                    2
                } else {
                    1
                })
                    .map(|_| History::new(o.points))
                    .collect(),
                status: "Ожидание первого ответа".into(),
                last_sample: None,
            })
            .collect();
        let (console, commands) = chem_sensor_plots::commands::gui::console(ctx.clone());
        let (tx, events) = mpsc::sync_channel(64);
        let stop = Arc::new(AtomicBool::new(false));
        let worker_stop = stop.clone();
        let worker_channels = channels.clone();
        let worker_url = o.url.clone();
        thread::spawn(move || {
            let client = match reqwest::blocking::Client::builder()
                .timeout(o.timeout)
                .connect_timeout(Duration::from_secs(5))
                .redirect(reqwest::redirect::Policy::none())
                .retry(reqwest::retry::never())
                .build()
            {
                Ok(client) => client,
                Err(e) => {
                    let _ = tx.try_send(Event {
                        index: 0,
                        time: 0.0,
                        reading: Err(format!("HTTP client: {e}")),
                    });
                    ctx.request_repaint();
                    return;
                }
            };
            let start = Instant::now();
            while !worker_stop.load(Ordering::Relaxed) {
                // Один последовательный цикл: не забиваем FIFO сервера параллельными запросами.
                for (index, channel) in worker_channels.iter().enumerate() {
                    if worker_stop.load(Ordering::Relaxed) {
                        return;
                    }
                    commands.process_pending(&client, &worker_url);
                    if worker_stop.load(Ordering::Relaxed) {
                        return;
                    }
                    let result = (|| {
                        let response = client
                            .get(format!("{worker_url}{}", channel.path))
                            .send()
                            .map_err(|e| format!("HTTP: {e}"))?;
                        let status = response.status();
                        // Ограничиваем ответ, включая страницы ошибок стороннего сервера.
                        use std::io::Read;
                        let mut body = String::new();
                        response
                            .take(65537)
                            .read_to_string(&mut body)
                            .map_err(|e| format!("Тело ответа: {e}"))?;
                        if body.len() > 65536 {
                            return Err("Ответ больше 64 KiB".into());
                        }
                        if !status.is_success() {
                            return Err(format!(
                                "HTTP {status}: {}",
                                body.chars().take(240).collect::<String>()
                            ));
                        }
                        parse(channel, &body)
                    })();
                    if tx
                        .send(Event {
                            index,
                            time: start.elapsed().as_secs_f64(),
                            reading: result,
                        })
                        .is_err()
                    {
                        return;
                    }
                    ctx.request_repaint();
                }
                let until = Instant::now() + o.interval;
                while Instant::now() < until && !worker_stop.load(Ordering::Relaxed) {
                    commands.process_pending(&client, &worker_url);
                    thread::sleep(Duration::from_millis(50));
                }
            }
        });
        Self {
            channels,
            states,
            events,
            stop,
            url: o.url,
            max_age_ms: o.max_age_ms,
            console,
        }
    }
}
impl Drop for App {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}
impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _: &mut eframe::Frame) {
        while let Ok(event) = self.events.try_recv() {
            let state = &mut self.states[event.index];
            state.last_sample = Some(Instant::now());
            match event.reading {
                Ok(r) => {
                    state.status = format!(
                        "значения={:?} | valid={} fresh={} age={} мс | received_at={} | {}",
                        r.values,
                        r.quality.valid,
                        r.quality.fresh,
                        r.quality.age_ms,
                        r.quality.received_at,
                        if r.quality.age_ms >= 65534 || r.quality.age_ms > self.max_age_ms {
                            "stale: не рисуем"
                        } else if !r.quality.fresh {
                            "cached: снимок кэша"
                        } else {
                            "fresh: снимок ответа"
                        }
                    );
                    for (i, h) in state.histories.iter_mut().enumerate() {
                        h.observe(event.time, r.values[i], &r.quality, self.max_age_ms);
                    }
                }
                Err(e) => {
                    state.status = e;
                    for h in &mut state.histories {
                        h.push(event.time, None);
                    }
                }
            }
        }
        egui::TopBottomPanel::top("header").show(ctx, |ui| {
            ui.heading(TITLE);
            ui.label(format!(
                "FastAPI · {} · время X: секунды после запуска",
                self.url
            ));
            ui.label("Колесо — масштаб; перетаскивание — панорама; двойной щелчок — автоподбор.");
        });
        egui::TopBottomPanel::bottom("command_panel").show(ctx, |ui| {
            self.console.show(ui);
        });
        egui::CentralPanel::default().show(ctx, |ui| {
            egui::ScrollArea::vertical().show(ui, |ui| {
                for (index, (c, s)) in self.channels.iter().zip(&self.states).enumerate() {
                    ui.heading(&c.label);
                    ui.label(format!(
                        "{} | HTTP ответ {} с назад",
                        s.status,
                        s.last_sample
                            .map(|t| format!("{:.1}", t.elapsed().as_secs_f64()))
                            .unwrap_or_else(|| "ещё не получен".into())
                    ));
                    for (metric, h) in s.histories.iter().enumerate() {
                        let unit = if matches!(c.layout, Layout::Analog) {
                            if metric == 0 {
                                "ADC 0..1023"
                            } else {
                                "V"
                            }
                        } else {
                            c.unit
                        };
                        ui.label(unit);
                        Plot::new((index, metric))
                            .height(180.0)
                            .legend(Legend::default())
                            .show(ui, |plot| {
                                for points in h.segments() {
                                    let label = format!("{} [{unit}]", c.label);
                                    if points.len() == 1 {
                                        plot.points(Points::new(label, points).radius(3.0));
                                    } else {
                                        plot.line(Line::new(label, points));
                                    }
                                }
                            });
                    }
                    ui.separator();
                }
            });
        });
        ctx.request_repaint_after(Duration::from_secs(1));
    }
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let o =
        Options::args().map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;
    eframe::run_native(
        TITLE,
        eframe::NativeOptions::default(),
        Box::new(move |cc| Ok(Box::new(App::new(o, cc.egui_ctx.clone())))),
    )?;
    Ok(())
}

#[cfg(test)]
mod gui_tests {
    use super::*;
    #[test]
    fn route_set_matches_greenhouse_and_contains_only_sensor_reads() {
        let c = channels();
        assert_eq!(c.len(), 8);
        let paths: Vec<_> = c.iter().map(|c| c.path.as_str()).collect();
        assert!(paths.contains(&"/api/v1/gas/0"));
        assert!(paths.contains(&"/api/v1/gas/1"));
        assert!(!paths
            .iter()
            .any(|p| p.contains("dust") || p.contains("voc")));
        assert!(!paths
            .iter()
            .any(|p| p.contains("mode") || p.contains("pwm") || p.contains("admin")));
    }
}
