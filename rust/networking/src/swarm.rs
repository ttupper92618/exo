use std::pin::Pin;

use crate::swarm::transport::tcp_transport;
use crate::{alias, discovery};
pub use behaviour::{Behaviour, BehaviourEvent};
use futures_lite::{Stream, StreamExt};
use libp2p::{PeerId, SwarmBuilder, gossipsub, identity, swarm::SwarmEvent};
use tokio::sync::{mpsc, oneshot};

/// The current version of the network: this prevents devices running different versions of the
/// software from interacting with each other.
///
/// THE RULE (#659): any change to wire behavior in this crate — protocol ids,
/// topics, message framing, behaviour composition — bumps this constant IN
/// THE SAME COMMIT and adds an entry to `rust/networking/WIRE_COMPAT.md`
/// (CI enforces the pairing). The pnet pre-shared key derives from this
/// value, so a bump makes wire-incompatible builds fail loudly at connect
/// instead of half-working: the telemetry-isolation change (31e3f333)
/// shipped without a bump, and a fresh build against a stale fleet
/// produced a fully-synced node that was invisible to membership because
/// its telemetry protocol had no peers while events flowed fine.
pub const NETWORK_VERSION: &[u8] = b"v0.0.2";
pub const OVERRIDE_VERSION_ENV_VAR: &str = "SKULK_LIBP2P_NAMESPACE";
const ELECTION_TOPIC: &str = "election_messages";
const ELECTION_PROTOCOL_PREFIX: &str = "/skulk/election/meshsub";
const TELEMETRY_TOPIC: &str = "telemetry";
const TELEMETRY_PROTOCOL_PREFIX: &str = "/skulk/telemetry/meshsub";

// Uses oneshot senders to emulate function calling apis while avoiding requiring unique ownership
// of the Swarm.
pub enum ToSwarm {
    ListenAddresses {
        result_sender: oneshot::Sender<Vec<String>>,
    },
    Unsubscribe {
        topic: String,
        result_sender: oneshot::Sender<bool>,
    },
    Subscribe {
        topic: String,
        result_sender: oneshot::Sender<Result<bool, gossipsub::SubscriptionError>>,
    },
    Publish {
        topic: String,
        data: Vec<u8>,
        result_sender: oneshot::Sender<Result<gossipsub::MessageId, gossipsub::PublishError>>,
    },
}
pub enum FromSwarm {
    Message {
        from: PeerId,
        topic: String,
        data: Vec<u8>,
    },
    Discovered {
        peer_id: PeerId,
        // The connection's observed remote endpoint (#662): the Python side
        // records the authenticated session as a first-class topology path,
        // so a member whose advertised addresses are all unreachable (a
        // NAT'd/proxied remote node) still gets a truthful topology edge.
        remote_ip: String,
        remote_tcp_port: u16,
    },
    Expired {
        peer_id: PeerId,
    },
}

pub struct Swarm {
    swarm: libp2p::Swarm<Behaviour>,
    from_client: mpsc::Receiver<ToSwarm>,
}

impl Swarm {
    pub fn into_stream(self) -> Pin<Box<dyn Stream<Item = FromSwarm> + Send>> {
        let Swarm {
            mut swarm,
            mut from_client,
        } = self;
        let stream = async_stream::stream! {
            loop {
                tokio::select! {
                    msg = from_client.recv() => {
                        let Some(msg) = msg else { break };
                        on_message(&mut swarm, msg);
                    }
                    event = swarm.next() => {
                        let Some(event) = event else { break };
                        if let Some(item) = filter_swarm_event(event) {
                            yield item;
                        }
                    }
                }
            }
        };
        Box::pin(stream)
    }
}

fn on_message(swarm: &mut libp2p::Swarm<Behaviour>, message: ToSwarm) {
    match message {
        ToSwarm::ListenAddresses { result_sender } => {
            // Query the running swarm: the requested port may have been zero.
            _ = result_sender.send(swarm.listeners().map(ToString::to_string).collect());
        }
        ToSwarm::Subscribe {
            topic,
            result_sender,
        } => {
            let topic_id = gossipsub::IdentTopic::new(topic.clone());
            let result = if topic == ELECTION_TOPIC {
                // Keep the default-protocol subscription during the migration
                // window so old and new binaries can still elect while nodes
                // are restarted one at a time. New peers also use the isolated
                // protocol, whose independent handler queue preserves liveness.
                let isolated = swarm
                    .behaviour_mut()
                    .election_gossipsub
                    .subscribe(&topic_id);
                let legacy = swarm.behaviour_mut().gossipsub.subscribe(&topic_id);
                // Registration succeeds if EITHER protocol subscribes; only a
                // double failure is a real error. `isolated.and_then(..)` would
                // abort election registration whenever the isolated subscribe
                // failed even though the legacy subscription worked, defeating
                // the rolling-upgrade guarantee this dual subscription exists
                // for.
                match (isolated, legacy) {
                    (Ok(isolated_subscribed), Ok(legacy_subscribed)) => {
                        Ok(isolated_subscribed || legacy_subscribed)
                    }
                    (Ok(isolated_subscribed), Err(_)) => Ok(isolated_subscribed),
                    (Err(_), Ok(legacy_subscribed)) => Ok(legacy_subscribed),
                    (Err(isolated_err), Err(_)) => Err(isolated_err),
                }
            } else if topic == TELEMETRY_TOPIC {
                swarm
                    .behaviour_mut()
                    .telemetry_gossipsub
                    .subscribe(&topic_id)
            } else {
                swarm.behaviour_mut().gossipsub.subscribe(&topic_id)
            };
            _ = result_sender.send(result);
        }
        ToSwarm::Unsubscribe {
            topic,
            result_sender,
        } => {
            let topic_id = gossipsub::IdentTopic::new(topic.clone());
            let result = if topic == ELECTION_TOPIC {
                let isolated = swarm
                    .behaviour_mut()
                    .election_gossipsub
                    .unsubscribe(&topic_id);
                let legacy = swarm.behaviour_mut().gossipsub.unsubscribe(&topic_id);
                isolated || legacy
            } else if topic == TELEMETRY_TOPIC {
                swarm
                    .behaviour_mut()
                    .telemetry_gossipsub
                    .unsubscribe(&topic_id)
            } else {
                swarm.behaviour_mut().gossipsub.unsubscribe(&topic_id)
            };
            _ = result_sender.send(result);
        }
        ToSwarm::Publish {
            topic,
            data,
            result_sender,
        } => {
            let topic_id = gossipsub::IdentTopic::new(topic.clone());
            let result = if topic == ELECTION_TOPIC {
                let isolated = swarm
                    .behaviour_mut()
                    .election_gossipsub
                    .publish(topic_id.clone(), data.clone());
                let legacy = swarm.behaviour_mut().gossipsub.publish(topic_id, data);
                combine_election_publish_results(isolated, legacy)
            } else if topic == TELEMETRY_TOPIC {
                swarm
                    .behaviour_mut()
                    .telemetry_gossipsub
                    .publish(topic_id, data)
            } else {
                swarm.behaviour_mut().gossipsub.publish(topic_id, data)
            };
            _ = result_sender.send(result);
        }
    }
}

fn combine_election_publish_results(
    isolated: Result<gossipsub::MessageId, gossipsub::PublishError>,
    legacy: Result<gossipsub::MessageId, gossipsub::PublishError>,
) -> Result<gossipsub::MessageId, gossipsub::PublishError> {
    match (isolated, legacy) {
        (Ok(message_id), _) | (_, Ok(message_id)) => Ok(message_id),
        (Err(isolated_error), Err(legacy_error)) => {
            if matches!(
                isolated_error,
                gossipsub::PublishError::NoPeersSubscribedToTopic
            ) {
                Err(legacy_error)
            } else {
                Err(isolated_error)
            }
        }
    }
}

fn filter_swarm_event(event: SwarmEvent<BehaviourEvent>) -> Option<FromSwarm> {
    match event {
        SwarmEvent::Behaviour(
            BehaviourEvent::Gossipsub(gossipsub::Event::Message {
                message:
                    gossipsub::Message {
                        source: Some(peer_id),
                        topic,
                        data,
                        ..
                    },
                ..
            })
            | BehaviourEvent::ElectionGossipsub(gossipsub::Event::Message {
                message:
                    gossipsub::Message {
                        source: Some(peer_id),
                        topic,
                        data,
                        ..
                    },
                ..
            })
            | BehaviourEvent::TelemetryGossipsub(gossipsub::Event::Message {
                message:
                    gossipsub::Message {
                        source: Some(peer_id),
                        topic,
                        data,
                        ..
                    },
                ..
            }),
        ) => Some(FromSwarm::Message {
            from: peer_id,
            topic: topic.into_string(),
            data,
        }),
        SwarmEvent::Behaviour(BehaviourEvent::Discovery(
            discovery::Event::ConnectionEstablished {
                peer_id,
                remote_ip,
                remote_tcp_port,
                ..
            },
        )) => Some(FromSwarm::Discovered {
            peer_id,
            remote_ip: remote_ip.to_string(),
            remote_tcp_port,
        }),
        SwarmEvent::Behaviour(BehaviourEvent::Discovery(discovery::Event::ConnectionClosed {
            peer_id,
            ..
        })) => Some(FromSwarm::Expired { peer_id }),
        _ => None,
    }
}

/// Create and configure a swarm.
///
/// - `listen_port`: TCP port to listen on. `0` lets the OS assign one.
/// - `bootstrap_peers`: multiaddrs to dial for environments without mDNS.
pub fn create_swarm(
    keypair: identity::Keypair,
    from_client: mpsc::Receiver<ToSwarm>,
    bootstrap_peers: Vec<String>,
    listen_port: u16,
) -> alias::AnyResult<Swarm> {
    let parsed_bootstrap_peers: Vec<libp2p::Multiaddr> = bootstrap_peers
        .iter()
        .filter(|s| !s.is_empty())
        .filter_map(|s| s.parse().ok())
        .collect();

    let mut swarm = SwarmBuilder::with_existing_identity(keypair)
        .with_tokio()
        .with_other_transport(tcp_transport)?
        .with_behaviour(|keypair| Behaviour::new(keypair, parsed_bootstrap_peers))?
        .build();

    swarm.listen_on(format!("/ip4/0.0.0.0/tcp/{listen_port}").parse()?)?;
    Ok(Swarm { swarm, from_client })
}

mod transport {
    use crate::alias;
    use crate::swarm::{NETWORK_VERSION, OVERRIDE_VERSION_ENV_VAR};
    use futures_lite::{AsyncRead, AsyncWrite};
    use keccak_const::Sha3_256;
    use libp2p::core::muxing;
    use libp2p::core::transport::Boxed;
    use libp2p::pnet::{PnetError, PnetOutput};
    use libp2p::{PeerId, Transport, identity, noise, pnet, yamux};
    use std::{env, sync::LazyLock};

    /// Key used for networking's private network; parametrized on the [`NETWORK_VERSION`].
    /// See [`pnet_upgrade`] for more.
    static PNET_PRESHARED_KEY: LazyLock<[u8; 32]> = LazyLock::new(|| {
        // Seed for the libp2p private-network pre-shared key. Changing this is a
        // wire-compatibility break (nodes with a different seed cannot form a
        // cluster), so it lands only as a coordinated whole-fleet upgrade (#324).
        //
        // NETWORK_VERSION ALWAYS contributes to the key; a configured
        // namespace layers cluster isolation on top rather than replacing
        // it. The previous either/or derivation silently disabled the wire
        // version gate on every deployment that sets a namespace — which
        // the installed service template does by default — so a version
        // bump changed nothing exactly where the loud-connect-failure
        // guarantee was needed (#659 review, P1).
        // The NUL delimiter keeps (version, namespace) pairs injective in
        // the hashed byte stream: without it ("v0.0.21", "x") and
        // ("v0.0.2", "1x") hash identically (#659 review). NUL cannot
        // appear in either field (a version literal here; env vars are
        // NUL-free on POSIX). The delimiter lives INSIDE the namespace arm
        // so unset and set-to-empty stay distinct streams, matching the
        // Python token mirror exactly: a pair of nodes disagreeing there
        // would share a pnet key while splitting Zenoh namespaces (the #312
        // silent-DATA-drop shape; second review round).
        let builder = Sha3_256::new()
            .update(b"skulk_discovery_network")
            .update(NETWORK_VERSION);

        if let Ok(var) = env::var(OVERRIDE_VERSION_ENV_VAR) {
            let bytes = var.into_bytes();
            builder.update(b"\0").update(&bytes)
        } else {
            builder
        }
        .finalize()
    });

    /// Make the Swarm run on a private network, as to not clash with public libp2p nodes and
    /// also different-versioned instances of this same network.
    /// This is implemented as an additional "upgrade" ontop of existing [`libp2p::Transport`] layers.
    async fn pnet_upgrade<TSocket>(
        socket: TSocket,
        _: impl Sized,
    ) -> Result<PnetOutput<TSocket>, PnetError>
    where
        TSocket: AsyncRead + AsyncWrite + Send + Unpin + 'static,
    {
        use pnet::{PnetConfig, PreSharedKey};
        PnetConfig::new(PreSharedKey::new(*PNET_PRESHARED_KEY))
            .handshake(socket)
            .await
    }

    /// TCP/IP transport layer configuration.
    pub fn tcp_transport(
        keypair: &identity::Keypair,
    ) -> alias::AnyResult<Boxed<(PeerId, muxing::StreamMuxerBox)>> {
        use libp2p::{
            core::upgrade::Version,
            tcp::{Config, tokio},
        };

        // `TCP_NODELAY` enabled => avoid latency
        let tcp_config = Config::default().nodelay(true);

        // V1 + lazy flushing => 0-RTT negotiation
        let upgrade_version = Version::V1Lazy;

        // Noise is faster than TLS + we don't care much for security
        let noise_config = noise::Config::new(keypair)?;

        // Use default Yamux config for multiplexing
        let yamux_config = yamux::Config::default();

        // Create new Tokio-driven TCP/IP transport layer
        let base_transport = tokio::Transport::new(tcp_config)
            .and_then(pnet_upgrade)
            .upgrade(upgrade_version)
            .authenticate(noise_config)
            .multiplex(yamux_config);

        // Return boxed transport (to flatten complex type)
        Ok(base_transport.boxed())
    }
}

mod behaviour {
    use crate::{
        alias, discovery,
        swarm::{ELECTION_PROTOCOL_PREFIX, TELEMETRY_PROTOCOL_PREFIX},
    };
    use libp2p::swarm::NetworkBehaviour;
    use libp2p::{gossipsub, identity};

    /// Behavior of the Swarm which composes all desired behaviors:
    /// Right now its just [`discovery::Behaviour`] and [`gossipsub::Behaviour`].
    #[derive(NetworkBehaviour)]
    pub struct Behaviour {
        pub discovery: discovery::Behaviour,
        pub gossipsub: gossipsub::Behaviour,
        pub election_gossipsub: gossipsub::Behaviour,
        pub telemetry_gossipsub: gossipsub::Behaviour,
    }

    impl Behaviour {
        pub fn new(
            keypair: &identity::Keypair,
            bootstrap_peers: Vec<libp2p::Multiaddr>,
        ) -> alias::AnyResult<Self> {
            Ok(Self {
                discovery: discovery::Behaviour::new(keypair, bootstrap_peers)?,
                gossipsub: gossipsub_behaviour(keypair, None),
                // Election traffic negotiates a separate protocol and therefore
                // owns a distinct connection-handler queue. Bulk control or
                // telemetry fan-out can no longer consume its liveness capacity.
                election_gossipsub: gossipsub_behaviour(keypair, Some(ELECTION_PROTOCOL_PREFIX)),
                // Telemetry is lossy and independently capacity-bounded. Giving
                // it a distinct negotiated protocol prevents its handler queues
                // from consuming command, event, or event-log repair capacity.
                telemetry_gossipsub: gossipsub_behaviour(keypair, Some(TELEMETRY_PROTOCOL_PREFIX)),
            })
        }
    }

    fn gossipsub_behaviour(
        keypair: &identity::Keypair,
        protocol_prefix: Option<&'static str>,
    ) -> gossipsub::Behaviour {
        use gossipsub::{ConfigBuilder, MessageAuthenticity, ValidationMode};

        let mut config = ConfigBuilder::default();
        config
            .max_transmit_size(8 * 1024 * 1024)
            .validation_mode(ValidationMode::Strict);
        if let Some(prefix) = protocol_prefix {
            config.protocol_id_prefix(prefix);
        }

        // build a gossipsub network behaviour
        //  => signed message authenticity + strict validation mode means the message-ID is
        //     automatically provided by gossipsub w/out needing to provide custom message-ID function
        gossipsub::Behaviour::new(
            MessageAuthenticity::Signed(keypair.clone()),
            config
                .build()
                .expect("the configuration should always be valid"),
        )
        .expect("creating gossipsub behavior should always work")
    }
}

#[cfg(test)]
mod tests {
    use super::{ELECTION_TOPIC, TELEMETRY_TOPIC, behaviour::Behaviour};
    use libp2p::{gossipsub, identity};

    #[test]
    fn election_topic_can_subscribe_on_both_gossipsub_behaviours() {
        let keypair = identity::Keypair::generate_ed25519();
        let mut behaviour = Behaviour::new(&keypair, Vec::new()).expect("valid behaviour");
        let election_topic = gossipsub::IdentTopic::new(ELECTION_TOPIC);

        assert!(
            behaviour
                .election_gossipsub
                .subscribe(&election_topic)
                .expect("valid subscription")
        );
        assert!(
            behaviour
                .gossipsub
                .subscribe(&election_topic)
                .expect("valid legacy subscription")
        );
        assert!(
            behaviour
                .election_gossipsub
                .topics()
                .any(|topic| topic == &election_topic.hash())
        );
        assert!(
            behaviour
                .gossipsub
                .topics()
                .any(|topic| topic == &election_topic.hash())
        );
    }

    #[test]
    fn ordinary_topics_remain_on_the_shared_gossipsub_behaviour() {
        let keypair = identity::Keypair::generate_ed25519();
        let mut behaviour = Behaviour::new(&keypair, Vec::new()).expect("valid behaviour");
        let control_topic = gossipsub::IdentTopic::new("global_events");

        assert!(
            behaviour
                .gossipsub
                .subscribe(&control_topic)
                .expect("valid subscription")
        );
        assert!(
            behaviour
                .gossipsub
                .topics()
                .any(|topic| topic == &control_topic.hash())
        );
        assert!(
            !behaviour
                .election_gossipsub
                .topics()
                .any(|topic| topic == &control_topic.hash())
        );
        assert!(
            !behaviour
                .telemetry_gossipsub
                .topics()
                .any(|topic| topic == &control_topic.hash())
        );
    }

    #[test]
    fn telemetry_topic_uses_only_the_isolated_gossipsub_behaviour() {
        let keypair = identity::Keypair::generate_ed25519();
        let mut behaviour = Behaviour::new(&keypair, Vec::new()).expect("valid behaviour");
        let telemetry_topic = gossipsub::IdentTopic::new(TELEMETRY_TOPIC);

        assert!(
            behaviour
                .telemetry_gossipsub
                .subscribe(&telemetry_topic)
                .expect("valid telemetry subscription")
        );
        assert!(
            behaviour
                .telemetry_gossipsub
                .topics()
                .any(|topic| topic == &telemetry_topic.hash())
        );
        assert!(
            !behaviour
                .gossipsub
                .topics()
                .any(|topic| topic == &telemetry_topic.hash())
        );
        assert!(
            !behaviour
                .election_gossipsub
                .topics()
                .any(|topic| topic == &telemetry_topic.hash())
        );
    }
}
