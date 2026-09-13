use std::pin::Pin;
use std::sync::Arc;

use crate::r#const::MPSC_CHANNEL_SIZE;
use crate::ext::{ByteArrayExt as _, FutureExt, PyErrExt as _};
use crate::ext::{ResultExt as _, TokioMpscSenderExt as _};
use crate::ident::PyKeypair;
use crate::networking::exception::{
    PyAllQueuesFullError, PyMessageTooLargeError, PyNoPeersSubscribedToTopicError,
};
use crate::pyclass;
use futures_lite::{Stream, StreamExt as _};
use libp2p::gossipsub::PublishError;
use networking::swarm::{FromSwarm, ToSwarm, create_swarm};
use networking::zenoh_session::{ZenohConfig, ZenohSession};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::{PyModule, PyModuleMethods as _};
use pyo3::types::PyBytes;
use pyo3::{Bound, Py, PyAny, PyErr, PyResult, Python, pymethods};
use pyo3_stub_gen::derive::{
    gen_methods_from_python, gen_stub_pyclass, gen_stub_pyclass_complex_enum, gen_stub_pymethods,
};
use tokio::sync::{Mutex, mpsc, oneshot};

mod exception {
    use pyo3::types::PyTuple;
    use pyo3::{exceptions::PyException, prelude::*};
    use pyo3_stub_gen::derive::*;

    #[gen_stub_pyclass]
    #[pyclass(frozen, extends=PyException, name="NoPeersSubscribedToTopicError")]
    pub struct PyNoPeersSubscribedToTopicError {}

    impl PyNoPeersSubscribedToTopicError {
        const MSG: &'static str = "\
        No peers are currently subscribed to receive messages on this topic. \
        Wait for peers to subscribe or check your network connectivity.";

        ///   Creates a new  [ `PyErr` ]  of this type.
        ///
        ///   [`PyErr`] :  https://docs.rs/pyo3/latest/pyo3/struct.PyErr.html   "PyErr in pyo3"
        pub(crate) fn new_err() -> PyErr {
            PyErr::new::<Self, _>(()) // TODO: check if this needs to be replaced???
        }
    }

    #[gen_stub_pymethods]
    #[pymethods]
    impl PyNoPeersSubscribedToTopicError {
        #[new]
        #[pyo3(signature = (*args))]
        #[allow(unused_variables)]
        pub(crate) fn new(args: &Bound<'_, PyTuple>) -> Self {
            Self {}
        }

        fn __repr__(&self) -> String {
            format!("PeerId(\"{}\")", Self::MSG)
        }

        fn __str__(&self) -> String {
            Self::MSG.to_string()
        }
    }

    #[gen_stub_pyclass]
    #[pyclass(frozen, extends=PyException, name="AllQueuesFullError")]
    pub struct PyAllQueuesFullError {}

    impl PyAllQueuesFullError {
        const MSG: &'static str =
            "All libp2p peers are unresponsive, resend the message or reconnect.";

        ///   Creates a new  [ `PyErr` ]  of this type.
        ///
        ///   [`PyErr`] :  https://docs.rs/pyo3/latest/pyo3/struct.PyErr.html   "PyErr in pyo3"
        pub(crate) fn new_err() -> PyErr {
            PyErr::new::<Self, _>(()) // TODO: check if this needs to be replaced???
        }
    }

    #[gen_stub_pymethods]
    #[pymethods]
    impl PyAllQueuesFullError {
        #[new]
        #[pyo3(signature = (*args))]
        #[allow(unused_variables)]
        pub(crate) fn new(args: &Bound<'_, PyTuple>) -> Self {
            Self {}
        }

        fn __repr__(&self) -> String {
            format!("PeerId(\"{}\")", Self::MSG)
        }

        fn __str__(&self) -> String {
            Self::MSG.to_string()
        }
    }

    #[gen_stub_pyclass]
    #[pyclass(frozen, extends=PyException, name="MessageTooLargeError")]
    pub struct PyMessageTooLargeError {}

    impl PyMessageTooLargeError {
        const MSG: &'static str = "Gossipsub message exceeds max_transmit_size. Reduce prompt length or increase the limit.";

        pub(crate) fn new_err() -> PyErr {
            PyErr::new::<Self, _>(())
        }
    }

    #[gen_stub_pymethods]
    #[pymethods]
    impl PyMessageTooLargeError {
        #[new]
        #[pyo3(signature = (*args))]
        #[allow(unused_variables)]
        pub(crate) fn new(args: &Bound<'_, PyTuple>) -> Self {
            Self {}
        }

        fn __repr__(&self) -> String {
            format!("MessageTooLargeError(\"{}\")", Self::MSG)
        }

        fn __str__(&self) -> String {
            Self::MSG.to_string()
        }
    }
}

#[gen_stub_pyclass]
#[pyclass(name = "NetworkingHandle")]
struct PyNetworkingHandle {
    // channels
    pub to_swarm: mpsc::Sender<ToSwarm>,
    pub swarm: Arc<Mutex<Pin<Box<dyn Stream<Item = FromSwarm> + Send>>>>,
}

#[gen_stub_pyclass_complex_enum]
#[pyclass]
enum PyFromSwarm {
    Connection {
        peer_id: String,
        connected: bool,
        // Observed remote endpoint of the established connection; empty
        // string / 0 on the disconnect event (#662).
        remote_ip: String,
        remote_tcp_port: u16,
    },
    Message {
        origin: String,
        topic: String,
        data: Py<PyBytes>,
    },
}
impl From<FromSwarm> for PyFromSwarm {
    fn from(value: FromSwarm) -> Self {
        match value {
            FromSwarm::Discovered {
                peer_id,
                remote_ip,
                remote_tcp_port,
            } => Self::Connection {
                peer_id: peer_id.to_base58(),
                connected: true,
                remote_ip,
                remote_tcp_port,
            },
            FromSwarm::Expired { peer_id } => Self::Connection {
                peer_id: peer_id.to_base58(),
                connected: false,
                remote_ip: String::new(),
                remote_tcp_port: 0,
            },
            FromSwarm::Message { from, topic, data } => Self::Message {
                origin: from.to_base58(),
                topic: topic,
                data: data.pybytes(),
            },
        }
    }
}

#[gen_stub_pymethods]
#[pymethods]
impl PyNetworkingHandle {
    // NOTE: `async fn`s here that use `.await` will wrap the future in `.allow_threads_py()`
    //       immediately beforehand to release the interpreter.
    //       SEE: https://pyo3.rs/v0.26.0/async-await.html#detaching-from-the-interpreter-across-await

    // ---- Lifecycle management methods ----

    #[new]
    #[pyo3(signature = (identity, bootstrap_peers=None, listen_port=0))]
    fn py_new(
        identity: Bound<'_, PyKeypair>,
        bootstrap_peers: Option<Vec<String>>,
        listen_port: u16,
    ) -> PyResult<Self> {
        // create communication channels
        let (to_swarm, from_client) = mpsc::channel(MPSC_CHANNEL_SIZE);

        // get identity
        let identity = identity.borrow().0.clone();

        // create networking swarm (within tokio context!! or it crashes)
        let _guard = pyo3_async_runtimes::tokio::get_runtime().enter();
        let swarm = create_swarm(
            identity,
            from_client,
            bootstrap_peers.unwrap_or_default(),
            listen_port,
        )
        .pyerr()?
        .into_stream();

        Ok(Self {
            swarm: Arc::new(Mutex::new(swarm)),
            to_swarm,
        })
    }

    #[gen_stub(skip)]
    fn recv<'py>(&'py self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let swarm = Arc::clone(&self.swarm);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            swarm
                .try_lock()
                .map_err(|_| PyRuntimeError::new_err("called recv twice concurrently"))?
                .next()
                .await
                .ok_or(PyErr::receiver_channel_closed())
                .map(PyFromSwarm::from)
        })
    }

    // ---- Gossipsub management methods ----

    /// Return actual bound listener addresses, including OS-assigned TCP ports.
    async fn listen_addresses(&self) -> PyResult<Vec<String>> {
        let (tx, rx) = oneshot::channel();
        self.to_swarm
            .send_py(ToSwarm::ListenAddresses { result_sender: tx })
            .allow_threads_py()
            .await?;
        rx.allow_threads_py()
            .await
            .map_err(|_| PyErr::receiver_channel_closed())
    }

    /// Subscribe to a `GossipSub` topic.
    ///
    /// Returns `True` if the subscription worked. Returns `False` if we were already subscribed.
    async fn gossipsub_subscribe(&self, topic: String) -> PyResult<bool> {
        let (tx, rx) = oneshot::channel();

        // send off request to subscribe
        self.to_swarm
            .send_py(ToSwarm::Subscribe {
                topic,
                result_sender: tx,
            })
            .allow_threads_py() // allow-threads-aware async call
            .await?;

        // wait for response & return any errors
        rx.allow_threads_py() // allow-threads-aware async call
            .await
            .map_err(|_| PyErr::receiver_channel_closed())?
            .pyerr()
    }

    /// Unsubscribes from a `GossipSub` topic.
    ///
    /// Returns `True` if we were subscribed to this topic. Returns `False` if we were not subscribed.
    async fn gossipsub_unsubscribe(&self, topic: String) -> PyResult<bool> {
        let (tx, rx) = oneshot::channel();

        // send off request to unsubscribe
        self.to_swarm
            .send_py(ToSwarm::Unsubscribe {
                topic,
                result_sender: tx,
            })
            .allow_threads_py() // allow-threads-aware async call
            .await?;

        // wait for response & convert any errors
        rx.allow_threads_py() // allow-threads-aware async call
            .await
            .map_err(|_| PyErr::receiver_channel_closed())
    }

    /// Publishes a message with multiple topics to the `GossipSub` network.
    ///
    /// If no peers are found that subscribe to this topic, throws `NoPeersSubscribedToTopicError` exception.
    async fn gossipsub_publish(&self, topic: String, data: Py<PyBytes>) -> PyResult<()> {
        let (tx, rx) = oneshot::channel();

        // send off request to subscribe
        let data = Python::attach(|py| Vec::from(data.as_bytes(py)));
        self.to_swarm
            .send_py(ToSwarm::Publish {
                topic,
                data,
                result_sender: tx,
            })
            .allow_threads_py() // allow-threads-aware async call
            .await?;

        // wait for response & return any errors => ignore messageID for now!!!
        let _ = rx
            .allow_threads_py() // allow-threads-aware async call
            .await
            .map_err(|_| PyErr::receiver_channel_closed())?
            .map_err(|e| match e {
                PublishError::AllQueuesFull(_) => PyAllQueuesFullError::new_err(),
                PublishError::MessageTooLarge => PyMessageTooLargeError::new_err(),
                PublishError::NoPeersSubscribedToTopic => {
                    PyNoPeersSubscribedToTopicError::new_err()
                }
                e => PyRuntimeError::new_err(e.to_string()),
            })?;
        Ok(())
    }
}

/// One inbound Zenoh sample handed to Python: the key (topic) it arrived on and
/// its raw payload. Mirrors the `Message` arm of [`PyFromSwarm`]; the data-plane
/// consumer demuxes by the `command_id` carried inside `data`.
#[gen_stub_pyclass]
#[pyclass(name = "ZenohMessage")]
struct PyZenohMessage {
    #[pyo3(get)]
    topic: String,
    #[pyo3(get)]
    data: Py<PyBytes>,
}

/// Handle to the Zenoh peer session backing the data plane (Phase 1).
///
/// Separate from [`PyNetworkingHandle`] (libp2p): only the DATA topic is routed
/// here when the `zenoh_data_plane` flag is on. Methods mirror the gossipsub
/// surface (subscribe / publish / recv) so the Python `Router` can treat it as
/// an alternate transport backend.
#[gen_stub_pyclass]
#[pyclass(name = "ZenohHandle")]
struct PyZenohHandle {
    session: Arc<ZenohSession>,
}

#[gen_stub_pymethods]
#[pymethods]
impl PyZenohHandle {
    #[new]
    #[pyo3(signature = (
        listen_endpoints=None,
        connect_endpoints=None,
        namespace=None,
        multicast_scouting=false
    ))]
    fn py_new(
        listen_endpoints: Option<Vec<String>>,
        connect_endpoints: Option<Vec<String>>,
        namespace: Option<String>,
        multicast_scouting: bool,
    ) -> PyResult<Self> {
        let config = ZenohConfig {
            listen_endpoints: listen_endpoints.unwrap_or_default(),
            connect_endpoints: connect_endpoints.unwrap_or_default(),
            multicast_scouting,
            // #308 namespace isolation; the Python caller passes an
            // already-validated key-expr segment (or None for the legacy
            // unprefixed behavior).
            namespace,
        };
        // Opening the session is async; block on it inside the shared tokio
        // runtime (the same runtime the swarm uses), matching `py_new` above.
        let runtime = pyo3_async_runtimes::tokio::get_runtime();
        let session = runtime.block_on(ZenohSession::open(config)).pyerr()?;
        Ok(Self {
            session: Arc::new(session),
        })
    }

    /// Return actual bound data-plane locators without changing connectivity.
    async fn listen_addresses(&self) -> PyResult<Vec<String>> {
        Ok(self.session.listen_addresses().allow_threads_py().await)
    }

    /// Subscribe to a Zenoh key (topic). Idempotent.
    async fn zenoh_subscribe(&self, topic: String) -> PyResult<()> {
        self.session
            .subscribe(&topic)
            .allow_threads_py()
            .await
            .pyerr()
    }

    /// Publish `data` on a Zenoh key (Reliable + Block + single priority).
    async fn zenoh_publish(&self, topic: String, data: Py<PyBytes>) -> PyResult<()> {
        let data = Python::attach(|py| Vec::from(data.as_bytes(py)));
        self.session
            .publish(&topic, data)
            .allow_threads_py()
            .await
            .pyerr()
    }

    /// Count the Zenoh peers this session currently holds a live transport to.
    ///
    /// Zero while cluster peers advertise Zenoh means this node's data plane
    /// is isolated (its remote streams will fail) even though the libp2p
    /// control plane is healthy; Python advertises the count so cluster
    /// health can say so instead of streams dying silently.
    async fn zenoh_connected_peer_count(&self) -> PyResult<usize> {
        let session = Arc::clone(&self.session);
        Ok(session.connected_peer_count().allow_threads_py().await)
    }

    /// Await the next inbound `(topic, data)` sample.
    #[gen_stub(skip)]
    fn recv<'py>(&'py self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let session = Arc::clone(&self.session);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let (topic, data) = session
                .recv()
                .await
                .ok_or(PyErr::receiver_channel_closed())?;
            Ok(PyZenohMessage {
                topic,
                data: data.pybytes(),
            })
        })
    }
}

pyo3_stub_gen::inventory::submit! {
    gen_methods_from_python! {
        r#"
            class PyNetworkingHandle:
                async def recv() -> PyFromSwarm: ...
        "#
    }
}

pyo3_stub_gen::inventory::submit! {
    gen_methods_from_python! {
        r#"
            class PyZenohHandle:
                async def recv() -> ZenohMessage: ...
        "#
    }
}

pub fn networking_submodule(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<exception::PyNoPeersSubscribedToTopicError>()?;
    m.add_class::<exception::PyAllQueuesFullError>()?;
    m.add_class::<exception::PyMessageTooLargeError>()?;

    m.add_class::<PyNetworkingHandle>()?;
    m.add_class::<PyFromSwarm>()?;
    m.add_class::<PyZenohHandle>()?;
    m.add_class::<PyZenohMessage>()?;

    Ok(())
}
