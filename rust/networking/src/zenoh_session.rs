//! Zenoh peer session for the Skulk data plane (Phase 1, flag-gated).
//!
//! This runs ALONGSIDE the libp2p [`crate::swarm::Swarm`]: control, telemetry,
//! and election stay on gossipsub; only the DATA topic (per-token output) is
//! routed here when the `zenoh_data_plane` flag is on. The session is a Zenoh
//! `peer` with gossip discovery. Explicitly configured fleets use fixed connect
//! endpoints with multicast scouting disabled. A zero-config installation uses
//! multicast scouting so local peers discover each other without a fleet-
//! specific endpoint list.
//!
//! Ordering discipline: publishers are declared `Reliable` with
//! `CongestionControl::Block` on a single fixed `Priority`, so a single
//! publisher's samples on one key are delivered FIFO — the property that lets
//! Phase 3 delete the app-layer reorder buffer.

use std::collections::HashMap;
use std::sync::Arc;

use tokio::sync::Mutex;
use tokio::sync::mpsc;
use tokio::sync::mpsc::error::TrySendError;
use zenoh::Session;
use zenoh::pubsub::{Publisher, Subscriber};
use zenoh::qos::{CongestionControl, Priority, Reliability};

use crate::alias::{AnyError, AnyResult};

/// Bound on buffered inbound samples awaiting the Python consumer. The Zenoh
/// subscribe callback is non-blocking and drops on a full channel rather than
/// growing memory without limit if the consumer falls behind (DATA is
/// best-effort; the API reorder buffer + idle backstop tolerate gaps, the same
/// as the gossipsub path's queue-full drop).
const INBOUND_BUFFER_CAPACITY: usize = 4096;

/// Endpoint configuration for the Zenoh peer session.
#[derive(Debug, Clone, Default)]
pub struct ZenohConfig {
    /// Local endpoints to listen on, e.g. `tcp/0.0.0.0:7447`.
    pub listen_endpoints: Vec<String>,
    /// Peer endpoints to connect to in an explicitly routed deployment.
    pub connect_endpoints: Vec<String>,
    /// Whether local multicast scouting discovers peers when no explicit
    /// fleet endpoint list is available.
    pub multicast_scouting: bool,
    /// Optional session namespace (#308): a non-wildcard key-expr prefix that
    /// Zenoh transparently prepends to every published/subscribed key. Foreign
    /// peers on a different namespace cannot subscribe to this fleet's `data`,
    /// restoring parity with the libp2p private-namespace isolation. The caller
    /// must pass a valid, already-sanitized key-expr segment (derived from
    /// `SKULK_LIBP2P_NAMESPACE`); `None` leaves keys unprefixed (legacy).
    pub namespace: Option<String>,
}

fn json_str_array(items: &[String]) -> String {
    // JSON5 array of quoted strings; endpoints are operator-controlled config.
    let quoted: Vec<String> = items.iter().map(|e| format!("{e:?}")).collect();
    format!("[{}]", quoted.join(","))
}

/// A live Zenoh peer session plus the publishers/subscribers declared on it.
///
/// `recv` pulls the next inbound `(topic, payload)` delivered to any declared
/// subscriber, demuxed by the caller (the Python data-plane consumer keys on the
/// `command_id` carried inside the payload, exactly as the gossipsub path does).
pub struct ZenohSession {
    session: Session,
    // Publishers are kept behind `Arc` so `publish` can clone the handle out
    // under the lock and release the guard BEFORE the `put().await` (#309): the
    // lock then only guards the map, never an in-flight network put, so
    // concurrent publishes to different per-command keys don't serialize and a
    // `Block`-stalled put can't hold the map lock.
    publishers: Mutex<HashMap<String, Arc<Publisher<'static>>>>,
    subscribers: Mutex<HashMap<String, Subscriber<()>>>,
    inbound_tx: mpsc::Sender<(String, Vec<u8>)>,
    inbound_rx: Mutex<mpsc::Receiver<(String, Vec<u8>)>>,
}

impl ZenohSession {
    /// Return actual bound session locators without changing connectivity.
    pub async fn listen_addresses(&self) -> Vec<String> {
        self.session
            .info()
            .locators()
            .await
            .iter()
            .map(ToString::to_string)
            .collect()
    }

    /// Open a Zenoh `peer` session with gossip on and configured discovery.
    pub async fn open(config: ZenohConfig) -> AnyResult<Self> {
        let mut zconfig = zenoh::Config::default();
        let set = |c: &mut zenoh::Config, key: &str, val: &str| -> AnyResult<()> {
            c.insert_json5(key, val)
                .map_err(|e| -> AnyError { format!("zenoh config {key}: {e}").into() })
        };
        set(&mut zconfig, "mode", "\"peer\"")?;
        set(
            &mut zconfig,
            "scouting/multicast/enabled",
            if config.multicast_scouting {
                "true"
            } else {
                "false"
            },
        )?;
        set(&mut zconfig, "scouting/gossip/enabled", "true")?;
        // Namespace isolation (#308): Zenoh transparently prefixes every key
        // with this non-wildcard key-expr, so a peer on a different namespace
        // never receives this fleet's `data` samples. The caller passes an
        // already-validated segment.
        if let Some(ns) = &config.namespace {
            set(&mut zconfig, "namespace", &format!("{ns:?}"))?;
        }
        if !config.listen_endpoints.is_empty() {
            set(
                &mut zconfig,
                "listen/endpoints",
                &json_str_array(&config.listen_endpoints),
            )?;
        }
        if !config.connect_endpoints.is_empty() {
            set(
                &mut zconfig,
                "connect/endpoints",
                &json_str_array(&config.connect_endpoints),
            )?;
        }

        let session = zenoh::open(zconfig)
            .await
            .map_err(|e| -> AnyError { format!("zenoh open: {e}").into() })?;
        let (inbound_tx, inbound_rx) = mpsc::channel(INBOUND_BUFFER_CAPACITY);
        Ok(Self {
            session,
            publishers: Mutex::new(HashMap::new()),
            subscribers: Mutex::new(HashMap::new()),
            inbound_tx,
            inbound_rx: Mutex::new(inbound_rx),
        })
    }

    /// Publish `data` on `topic` (Reliable + Block + single fixed priority).
    ///
    /// The publisher for a topic is declared once and reused, preserving the
    /// single-publisher-per-key FIFO ordering the data plane depends on.
    pub async fn publish(&self, topic: &str, data: Vec<u8>) -> AnyResult<()> {
        // Fast path: an existing publisher is cloned out under a short-lived
        // lock, which is then released before the put (#309). The guard never
        // spans the `put().await`, so a Block-stalled put can't hold the map.
        let existing = self.publishers.lock().await.get(topic).cloned();
        let publisher = match existing {
            Some(p) => p,
            None => {
                // Declare WITHOUT holding the lock across the await, then insert
                // with a double-check: a concurrent publish to the same key may
                // have declared first, in which case we keep the stored one (and
                // drop ours) so there stays exactly one publisher per key per
                // session (the single-publisher FIFO ordering the plane needs).
                let declared = Arc::new(
                    self.session
                        .declare_publisher(topic.to_string())
                        .congestion_control(CongestionControl::Block)
                        .priority(Priority::Data)
                        .reliability(Reliability::Reliable)
                        .await
                        .map_err(|e| -> AnyError {
                            format!("declare_publisher {topic}: {e}").into()
                        })?,
                );
                let mut publishers = self.publishers.lock().await;
                publishers
                    .entry(topic.to_string())
                    .or_insert(declared)
                    .clone()
            }
        };
        publisher
            .put(data)
            .await
            .map_err(|e| -> AnyError { format!("publish {topic}: {e}").into() })
    }

    /// Declare a subscriber on `topic`; inbound samples are forwarded to `recv`.
    ///
    /// Idempotent: subscribing to an already-subscribed topic is a no-op.
    pub async fn subscribe(&self, topic: &str) -> AnyResult<()> {
        // Check-and-release before declaring so the lock never spans the await
        // (#309); startup-only, but tidy alongside the publish refactor.
        if self.subscribers.lock().await.contains_key(topic) {
            return Ok(());
        }
        let tx = self.inbound_tx.clone();
        let subscriber = self
            .session
            .declare_subscriber(topic.to_string())
            .callback(move |sample| {
                let key = sample.key_expr().as_str().to_string();
                let payload = sample.payload().to_bytes().to_vec();
                // The Zenoh callback runs on a Zenoh thread and must not block,
                // so use the non-blocking try_send. On a full channel (consumer
                // fell behind) drop the sample rather than grow memory without
                // bound; on a closed channel (session teardown) stay silent.
                match tx.try_send((key, payload)) {
                    Ok(()) => {}
                    Err(TrySendError::Full(_)) => {
                        log::warn!("zenoh data-plane inbound buffer full; dropping a chunk");
                    }
                    Err(TrySendError::Closed(_)) => {}
                }
            })
            .await
            .map_err(|e| -> AnyError { format!("declare_subscriber {topic}: {e}").into() })?;
        // Re-lock and insert with a double-check: if a concurrent subscribe to
        // the same topic won the race, keep the existing one and drop ours.
        let mut subscribers = self.subscribers.lock().await;
        subscribers.entry(topic.to_string()).or_insert(subscriber);
        Ok(())
    }

    /// Await the next inbound `(topic, payload)`, or `None` once the session is
    /// closed and all senders are dropped.
    pub async fn recv(&self) -> Option<(String, Vec<u8>)> {
        let mut rx = self.inbound_rx.lock().await;
        rx.recv().await
    }

    /// Count the Zenoh peers this session currently holds a live transport to.
    ///
    /// This is the data plane's only connectivity ground truth: a node whose
    /// count stays at zero while cluster peers advertise Zenoh is isolated
    /// (e.g. a zero-config remote member that multicast scouting cannot
    /// reach), and every remote stream to or from it dies with transport
    /// errors while the control plane still looks healthy. Surfacing the
    /// count lets Python advertise isolation instead of failing silently.
    pub async fn connected_peer_count(&self) -> usize {
        self.session.info().peers_zid().await.count()
    }
}
