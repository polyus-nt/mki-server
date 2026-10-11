//! Модель ответа и ограниченная история: не зависит от графического окружения.
use serde::Deserialize;
use std::collections::VecDeque;

#[derive(Clone, Copy, Debug)]
pub enum Layout {
    Scalar,
    Analog,
}
#[derive(Clone, Debug)]
pub struct Channel {
    pub label: String,
    pub path: String,
    pub unit: &'static str,
    pub layout: Layout,
}
#[derive(Debug, Deserialize)]
pub struct Quality {
    pub valid: bool,
    pub fresh: bool,
    pub age_ms: u32,
    pub received_at: String,
}
#[derive(Debug, Deserialize)]
struct Scalar {
    value: f64,
    unit: String,
    #[serde(flatten)]
    quality: Quality,
}
#[derive(Debug, Deserialize)]
struct Analog {
    adc: u16,
    voltage_mv: u16,
    voltage_v: f64,
    #[serde(flatten)]
    quality: Quality,
}
#[derive(Debug)]
pub struct Reading {
    pub values: Vec<f64>,
    pub quality: Quality,
}

pub fn parse(channel: &Channel, body: &str) -> Result<Reading, String> {
    match channel.layout {
        Layout::Scalar => {
            let r: Scalar = serde_json::from_str(body).map_err(|e| format!("JSON: {e}"))?;
            if r.unit != channel.unit {
                return Err(format!("Единицы {} вместо {}", r.unit, channel.unit));
            }
            if !r.value.is_finite() {
                return Err("Не конечное число".into());
            }
            Ok(Reading {
                values: vec![r.value],
                quality: r.quality,
            })
        }
        Layout::Analog => {
            let r: Analog = serde_json::from_str(body).map_err(|e| format!("JSON: {e}"))?;
            if r.adc > 1023
                || r.voltage_mv > 5000
                || !r.voltage_v.is_finite()
                || (r.voltage_v - f64::from(r.voltage_mv) / 1000.0).abs() > 0.001
            {
                return Err("Некорректный ADC / напряжение".into());
            }
            Ok(Reading {
                values: vec![f64::from(r.adc), r.voltage_v],
                quality: r.quality,
            })
        }
    }
}

pub struct History {
    pub points: VecDeque<(f64, Option<f64>)>,
    capacity: usize,
}
impl History {
    pub fn new(capacity: usize) -> Self {
        Self {
            points: VecDeque::new(),
            capacity: capacity.max(1),
        }
    }
    pub fn push(&mut self, time: f64, value: Option<f64>) {
        if self.points.len() == self.capacity {
            self.points.pop_front();
        }
        self.points
            .push_back((time, value.filter(|v| v.is_finite())));
    }
    pub fn observe(&mut self, time: f64, value: f64, quality: &Quality, max_age_ms: u32) {
        // fresh — общий флаг, его уже мог прочитать другой клиент.
        // Здесь рисуем HTTP-наблюдения, а не уникальные аппаратные измерения.
        if !quality.valid || quality.age_ms >= 65534 || quality.age_ms > max_age_ms {
            self.push(time, None);
        } else {
            self.push(time, Some(value));
        }
    }
    pub fn segments(&self) -> Vec<Vec<[f64; 2]>> {
        let mut result = Vec::new();
        let mut current = Vec::new();
        for &(time, value) in &self.points {
            if let Some(y) = value {
                current.push([time, y]);
            } else if !current.is_empty() {
                result.push(std::mem::take(&mut current));
            }
        }
        if !current.is_empty() {
            result.push(current);
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn scalar() -> Channel {
        Channel {
            label: "T".into(),
            path: "/t".into(),
            unit: "degC",
            layout: Layout::Scalar,
        }
    }
    #[test]
    fn scalar_json_and_quality() {
        let r = parse(&scalar(), r#"{"idx":0,"value":-12.34,"unit":"degC","valid":true,"fresh":false,"age_ms":123,"received_at":"2026-10-11T01:00:00Z"}"#).unwrap();
        assert_eq!(r.values, vec![-12.34]);
        assert!(!r.quality.fresh);
        assert_eq!(r.quality.age_ms, 123);
    }
    #[test]
    fn dust_without_index_and_gas_with_index() {
        let channel = Channel {
            layout: Layout::Analog,
            ..scalar()
        };
        for prefix in ["", "\"idx\":1,"] {
            let body = format!(
                r#"{{{prefix}"adc":512,"voltage_mv":2502,"voltage_v":2.502,"valid":true,"fresh":true,"age_ms":0,"received_at":"now"}}"#
            );
            assert_eq!(parse(&channel, &body).unwrap().values, vec![512.0, 2.502]);
        }
    }
    #[test]
    fn malformed_error_and_wrong_units_rejected() {
        assert!(parse(&scalar(), r#"{"error":"DEVICE_TIMEOUT"}"#).is_err());
        assert!(parse(
            &scalar(),
            r#"{"value":42,"unit":"Pa","valid":true,"fresh":true,"age_ms":0,"received_at":"now"}"#
        )
        .is_err());
        assert!(parse(&scalar(), "not JSON").is_err());
    }
    #[test]
    fn history_is_bounded_and_invalid_creates_gap() {
        let mut h = History::new(3);
        h.push(0.0, Some(1.0));
        h.push(1.0, Some(2.0));
        h.push(2.0, None);
        h.push(3.0, Some(4.0));
        assert_eq!(h.points.len(), 3);
        assert_eq!(h.segments(), vec![vec![[1.0, 2.0]], vec![[3.0, 4.0]]]);
        h.push(4.0, Some(f64::NAN));
        assert_eq!(h.points.back().unwrap().1, None);
    }
    #[test]
    fn cached_valid_http_snapshot_is_plotted_and_invalid_is_not_zero() {
        let mut h = History::new(10);
        let mut q = Quality {
            valid: true,
            fresh: true,
            age_ms: 0,
            received_at: "now".into(),
        };
        h.observe(0.0, 55.0, &q, 10000);
        q.fresh = false;
        h.observe(1.0, 55.0, &q, 10000);
        assert_eq!(h.points.len(), 2);
        assert_eq!(h.points.back().unwrap().1, Some(55.0));
        q.valid = false;
        h.observe(2.0, 0.0, &q, 10000);
        assert_eq!(h.points.back().unwrap().1, None);
    }
    #[test]
    fn stale_and_saturated_age_create_gaps_even_if_valid_and_fresh() {
        let mut h = History::new(10);
        let mut q = Quality {
            valid: true,
            fresh: true,
            age_ms: 10000,
            received_at: "now".into(),
        };
        h.observe(0.0, 42.0, &q, 10000);
        assert_eq!(h.points.back().unwrap().1, Some(42.0));
        q.age_ms = 10001;
        h.observe(1.0, 42.0, &q, 10000);
        assert_eq!(h.points.back().unwrap().1, None);
        q.age_ms = 65534;
        h.observe(2.0, 42.0, &q, u32::MAX);
        assert_eq!(h.points.back().unwrap().1, None);
    }
    #[test]
    fn corrupt_analog_response_is_rejected() {
        let channel = Channel {
            layout: Layout::Analog,
            ..scalar()
        };
        for fields in [
            "\"adc\":1024,\"voltage_mv\":2502,\"voltage_v\":2.502",
            "\"adc\":512,\"voltage_mv\":2502,\"voltage_v\":0.0",
        ] {
            let body =
                format!(r#"{{{fields},"valid":true,"fresh":true,"age_ms":0,"received_at":"now"}}"#);
            assert!(parse(&channel, &body).is_err());
        }
    }
}
