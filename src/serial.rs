//! The command broker is deliberately the sole route from GUI/MCP to the
//! physical serial owner.  It permits one in-flight Arduino request and keeps
//! `decision_id` idempotent before any bytes reach the board.

use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Command {
    GetControlConfig,
    ProposePid {
        expected_version: u16,
        kp: u16,
        ki: u16,
        kd: u16,
    },
    ProposeWeights {
        expected_version: u16,
        weights: [u8; 4],
    },
    SetDoorPolicy {
        expected_version: u16,
        policy: u8,
        limited_pwm: u8,
    },
    Confirm {
        candidate_version: u16,
    },
    Rollback {
        candidate_version: u16,
    },
    ProbeFan {
        fan_index: u8,
    },
    ClearEmergency,
}

impl Command {
    pub fn actuator_bytes(&self) -> Vec<u8> {
        let mut bytes = Vec::new();
        match self {
            Self::GetControlConfig => bytes.push(0x10),
            Self::ProposePid {
                expected_version,
                kp,
                ki,
                kd,
            } => {
                bytes.push(0x09);
                for value in [expected_version, kp, ki, kd] {
                    bytes.extend(value.to_be_bytes());
                }
            }
            Self::ProposeWeights {
                expected_version,
                weights,
            } => {
                bytes.push(0x0A);
                bytes.extend(expected_version.to_be_bytes());
                bytes.extend(weights);
            }
            Self::SetDoorPolicy {
                expected_version,
                policy,
                limited_pwm,
            } => {
                bytes.push(0x0D);
                bytes.extend(expected_version.to_be_bytes());
                bytes.push(*policy);
                bytes.push(*limited_pwm);
            }
            Self::Confirm { candidate_version } => {
                bytes.push(0x0B);
                bytes.extend(candidate_version.to_be_bytes());
            }
            Self::Rollback { candidate_version } => {
                bytes.push(0x0C);
                bytes.extend(candidate_version.to_be_bytes());
            }
            Self::ProbeFan { fan_index } => {
                bytes.extend([0x0E, *fan_index]);
            }
            Self::ClearEmergency => bytes.push(0x0F),
        }
        bytes
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CommandStatus {
    Queued,
    Applied,
    Rejected(u8),
    Timeout,
}

#[derive(Clone, Debug)]
pub struct QueuedCommand {
    pub decision_id: String,
    pub command: Command,
    pub queued_at: Instant,
    sent: bool,
}

#[derive(Default)]
pub struct CommandBroker {
    waiting: VecDeque<QueuedCommand>,
    inflight: Option<QueuedCommand>,
    results: HashMap<String, CommandStatus>,
}

impl CommandBroker {
    pub fn submit(&mut self, decision_id: String, command: Command) -> CommandStatus {
        if let Some(status) = self.results.get(&decision_id) {
            return status.clone();
        }
        if self
            .inflight
            .as_ref()
            .is_some_and(|item| item.decision_id == decision_id)
            || self
                .waiting
                .iter()
                .any(|item| item.decision_id == decision_id)
        {
            return CommandStatus::Queued;
        }
        self.waiting.push_back(QueuedCommand {
            decision_id,
            command,
            queued_at: Instant::now(),
            sent: false,
        });
        CommandStatus::Queued
    }

    pub fn begin_next(&mut self) -> Option<&QueuedCommand> {
        if self.inflight.is_none() {
            self.inflight = self.waiting.pop_front();
        }
        self.inflight.as_ref()
    }

    pub fn take_next_to_send(&mut self) -> Option<QueuedCommand> {
        self.begin_next();
        let command = self.inflight.as_mut()?;
        if command.sent {
            return None;
        }
        command.sent = true;
        command.queued_at = Instant::now();
        Some(command.clone())
    }

    pub fn finish(&mut self, status_byte: Option<u8>) -> Option<(String, CommandStatus)> {
        let command = self.inflight.take()?;
        let status = match status_byte {
            Some(0x03) => CommandStatus::Applied,
            Some(value) => CommandStatus::Rejected(value),
            None => CommandStatus::Timeout,
        };
        self.results
            .insert(command.decision_id.clone(), status.clone());
        Some((command.decision_id, status))
    }

    pub fn expire(&mut self, now: Instant) -> Option<(String, CommandStatus)> {
        if self
            .inflight
            .as_ref()
            .is_some_and(|item| now.duration_since(item.queued_at) >= Duration::from_secs(2))
        {
            return self.finish(None);
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn serializes_and_deduplicates_decisions() {
        let mut broker = CommandBroker::default();
        let command = Command::ClearEmergency;
        assert_eq!(
            broker.submit("same".into(), command.clone()),
            CommandStatus::Queued
        );
        assert_eq!(broker.submit("same".into(), command), CommandStatus::Queued);
        assert_eq!(
            broker.begin_next().unwrap().command.actuator_bytes(),
            vec![0x0F]
        );
        assert_eq!(
            broker.finish(Some(0x03)),
            Some(("same".into(), CommandStatus::Applied))
        );
        assert_eq!(
            broker.submit("same".into(), Command::ClearEmergency),
            CommandStatus::Applied
        );
    }
}
