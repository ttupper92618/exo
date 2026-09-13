import time
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from copy import copy
from dataclasses import dataclass
from itertools import count
from math import inf
from os import PathLike
from pathlib import Path
from typing import cast

from anyio import (
    BrokenResourceError,
    ClosedResourceError,
    Semaphore,
    WouldBlock,
    fail_after,
    get_cancelled_exc_class,
    move_on_after,
    sleep_forever,
)
from filelock import FileLock
from loguru import logger
from pydantic import ValidationError
from skulk_pyo3_bindings import (
    AllQueuesFullError,
    Keypair,
    MessageTooLargeError,
    NetworkingHandle,
    NoPeersSubscribedToTopicError,
    PyFromSwarm,
    ZenohHandle,
)

from skulk.extensions.host_network import (
    HostNetwork,
    namespace_fingerprint,
    tcp_endpoints,
)
from skulk.shared.constants import SKULK_NODE_ID_KEYPAIR
from skulk.shared.models.model_cards import ModelId
from skulk.shared.types.chunks import DataChunk, ErrorChunk
from skulk.shared.types.diagnostics import (
    DataPlaneEgressDiagnostics,
    TelemetryPlaneDiagnostics,
)
from skulk.shared.types.telemetry import NodeTelemetry
from skulk.utils.channels import Receiver, Sender, channel
from skulk.utils.pydantic_ext import CamelCaseModel
from skulk.utils.task_group import TaskGroup

from .connection_message import ConnectionMessage
from .data_plane import DataPlaneEgressObserver
from .provider_streams import (
    ProviderStreamPacket,
    provider_stream_rejection_packets,
)
from .realtime_audio import RealtimeAudioPacket
from .speech_media import SpeechMediaPacket
from .telemetry_plane import NO_PEER_WARNING_AFTER_SECONDS, TelemetryPlaneObserver
from .topics import (
    AUTHORITY_MESSAGES,
    CONNECTION_MESSAGES,
    DATA,
    ELECTION_MESSAGES,
    PROVIDER_DATA,
    REALTIME_AUDIO,
    SPEECH_MEDIA,
    TELEMETRY,
    TRACE_DATA,
    VISION_MEDIA,
    PublishPolicy,
    TypedTopic,
)
from .vision_media import VisionMediaPacket

# Bound on the fast ingress into per-command Zenoh DATA workers. The dispatcher
# does no network awaits, so this absorbs scheduling jitter without coupling
# TopicRouter to one remote publish. The command queues below own sustained
# pressure and fail only their stream when full.
_ZENOH_DATA_OUTBOUND_BUFFER = 2048
# Each command gets an independent queue and publish task. A slow or missing
# owner can fill only that command's bounded queue; it cannot stall unrelated
# local or remote streams. Stream and owner caps bound the total task/memory
# footprint under adversarial admission.
_ZENOH_DATA_STREAM_BUFFER = 256
_ZENOH_DATA_MAX_STREAMS_PER_OWNER = 64
_ZENOH_DATA_MAX_ACTIVE_STREAMS = 1024
_ZENOH_DATA_MAX_REJECTION_TASKS = 1024
_ZENOH_DATA_REJECTED_STREAM_TOMBSTONES = 4096
_ZENOH_DATA_PUBLISH_TIMEOUT_SECONDS = 5.0
# This is a router resource lease, not the API's 120-second post-output idle
# deadline. It deliberately allows long model prefill while ensuring a producer
# that disappears without a terminal frame cannot retain an admission slot
# forever. Every producer frame observed by egress renews the lease.
_ZENOH_DATA_STREAM_IDLE_LEASE_SECONDS = 30 * 60.0
# Inbound image uploads have a separate dispatcher so a large request cannot
# consume generated-output queue capacity. Small per-stream queues make the
# sender absorb sustained pressure while bounding serialized media in memory.
_ZENOH_VISION_OUTBOUND_BUFFER = 0
# A maximum upload is open + 64 chunks + completion. All 66 frames must fit
# before a newly scheduled per-stream publisher has a chance to drain.
_ZENOH_VISION_STREAM_BUFFER = 66
_ZENOH_VISION_MAX_STREAMS_PER_OWNER = 16
_ZENOH_VISION_MAX_ACTIVE_STREAMS = 16
_ZENOH_VISION_MAX_REJECTION_TASKS = 64
_ZENOH_VISION_REJECTED_STREAM_TOMBSTONES = 512
_ZENOH_VISION_STREAM_IDLE_LEASE_SECONDS = 5 * 60.0
# Network receive loops hand vision input to dedicated bounded consumers. The
# payload lane can retain one complete maximum-size stream (open, 64 chunks, and
# completion) while a prior frame is being delivered; the larger terminal lane
# carries only metadata and keeps acknowledgements/failures progressive during
# upload bursts.
_VISION_NETWORK_PAYLOAD_BUFFER = 66
_VISION_NETWORK_TERMINAL_BUFFER = 1024
# Election egress owns a small bounded queue and publish loop so a burst on the
# ordinary gossipsub topics cannot leave liveness traffic waiting in their FIFO.
_ELECTION_OUTBOUND_BUFFER = 128
# Consensus rounds are low-volume correctness traffic. A dedicated bounded
# Python egress queue prevents ordinary control bursts from queuing ahead of
# ballots and certificates while making overload backpressure explicit.
_AUTHORITY_OUTBOUND_BUFFER = 128
# Telemetry holds at most one serialized packet beyond the in-flight publish.
# Newer values remain in the keyed admission map and replace stale values there.
_TELEMETRY_OUTBOUND_BUFFER = 1
_TELEMETRY_ADMISSION_CAPACITY = 256


@dataclass(frozen=True, slots=True)
class OutboundPacket:
    """One serialized topic message awaiting network egress."""

    topic: str
    routing_key: str | None
    stream_key: str | None
    is_terminal: bool
    data: bytes


@dataclass(frozen=True, slots=True)
class _InboundVisionPacket:
    """One validated vision packet isolated from shared network receive loops."""

    packet: VisionMediaPacket
    origin: str | None
    outbound: bool = False


@dataclass(frozen=True, slots=True)
class _PendingTelemetry:
    """One latest-value telemetry reading awaiting serialized egress."""

    item: NodeTelemetry
    enqueued_at: float
    local_published: bool = False


class TelemetrySender:
    """Non-blocking producer handle for latest-value telemetry admission."""

    def __init__(self, router: "TelemetryTopicRouter") -> None:
        self._router = router
        self._closed = False

    async def send(self, item: NodeTelemetry) -> None:
        """Offer a reading without waiting for network capacity."""

        self.send_nowait(item)

    def send_nowait(self, item: NodeTelemetry) -> None:
        """Offer a reading, coalescing or evicting stale pending data."""

        if self._closed:
            raise ClosedResourceError
        self._router.offer(item)

    def clone(self) -> "TelemetrySender":
        """Return an independently closable handle to the same admission map."""

        if self._closed:
            raise ClosedResourceError
        return TelemetrySender(self._router)

    def close(self) -> None:
        """Close this producer handle without closing shared telemetry egress."""

        self._closed = True


# A significant current limitation of the TopicRouter is that it is not capable
# of preventing feedback, as it does not ask for a system id so cannot tell
# which message is coming/going to which system.
# This is currently only relevant for Election
class TopicRouter[T: CamelCaseModel]:
    def __init__(
        self,
        topic: TypedTopic[T],
        networking_sender: Sender[OutboundPacket],
        max_buffer_size: float = inf,
        local_routing_key: str | None = None,
        data_plane_egress_observer: DataPlaneEgressObserver | None = None,
    ):
        self.topic: TypedTopic[T] = topic
        self.senders: set[Sender[T]] = set()
        self.origin_senders: set[Sender[tuple[str | None, T]]] = set()
        receiver_buffer_size = (
            0
            if topic.topic == VISION_MEDIA.topic and max_buffer_size == inf
            else max_buffer_size
        )
        send, recv = channel[T](receiver_buffer_size)
        self.receiver: Receiver[T] = recv
        self._sender: Sender[T] = send
        self.networking_sender = networking_sender
        self.local_routing_key = local_routing_key
        self._data_plane_egress_observer = data_plane_egress_observer

    async def run(self):
        logger.debug(f"Topic Router {self.topic} ready to send")
        with self.receiver as items:
            async for item in items:
                if (
                    self.topic.routing_key is not None
                    and self.topic.publish_policy is PublishPolicy.Always
                ):
                    if self._routes_to_local_node(item):
                        # Same-node output reaches its owning API directly and
                        # never waits for or duplicates through network egress.
                        await self.publish(item, origin=None)
                        if self._data_plane_egress_observer is not None:
                            self._data_plane_egress_observer.record_local_short_circuit()
                        continue
                    # A remote owner's API is the only local consumer for this
                    # frame. Publishing it on the producer would waste decode
                    # work and classify every frame as late in diagnostics.
                    await self._send_out(item)
                    continue
                # Check if we should send to network
                if (
                    len(self.senders) == 0
                    and len(self.origin_senders) == 0
                    and self.topic.publish_policy is PublishPolicy.Minimal
                ):
                    await self._send_out(item)
                    continue
                if self.topic.publish_policy is PublishPolicy.Always:
                    await self._send_out(item)
                # Then publish to all senders
                await self.publish(item, origin=None)

    async def shutdown(self):
        logger.debug(f"Shutting down Topic Router {self.topic}")
        # Close all the things!
        for sender in self.senders:
            sender.close()
        for sender in self.origin_senders:
            sender.close()
        self._sender.close()
        self.receiver.close()

    async def publish(self, item: T, origin: str | None = None):
        """
        Publish item T on this topic to all senders.
        NB: this sends to ALL receivers, potentially including receivers held by the object doing the sending.
        You should handle your own output if you hold a sender + receiver pair.
        """
        to_clear: set[Sender[T]] = set()
        for sender in copy(self.senders):
            try:
                await sender.send(item)
            except (ClosedResourceError, BrokenResourceError):
                to_clear.add(sender)
        self.senders -= to_clear

        origin_to_clear: set[Sender[tuple[str | None, T]]] = set()
        for sender in copy(self.origin_senders):
            try:
                await sender.send((origin, item))
            except (ClosedResourceError, BrokenResourceError):
                origin_to_clear.add(sender)
        self.origin_senders -= origin_to_clear

    async def publish_bytes(self, data: bytes, origin: str | None):
        # Wire-format payloads are deserialized strictly (extra="forbid"). During
        # rolling upgrades, an older node may receive a message containing fields
        # it doesn't know about. Catch the validation failure so the gossipsub
        # receive loop survives - dropping the message is recoverable; tearing
        # down the loop is not.
        try:
            item = self.topic.deserialize(data)
        except (ValidationError, ValueError, UnicodeDecodeError) as exception:
            logger.opt(exception=exception).warning(
                f"Dropping malformed or schema-incompatible message on topic "
                f"{self.topic.topic} from {origin}"
            )
            return
        if self.topic.topic == VISION_MEDIA.topic and not self._routes_to_local_node(
            item
        ):
            return
        await self.publish(item, origin=origin)

    async def publish_to_network(self, item: T) -> None:
        """Route one validated item directly to this topic's network egress."""

        await self._send_out(item)

    def new_sender(self) -> Sender[T]:
        return self._sender.clone()

    def _routes_to_local_node(self, item: T) -> bool:
        if self.local_routing_key is None or self.topic.routing_key is None:
            return False
        return self.topic.routing_key(item) == self.local_routing_key

    async def _send_out(self, item: T):
        logger.trace(f"TopicRouter {self.topic.topic} sending {item}")
        # The routing key (Zenoh data plane only) addresses this message to a
        # single subscriber; None broadcasts on the bare topic (#279 Phase 2).
        routing_key = (
            self.topic.routing_key(item) if self.topic.routing_key is not None else None
        )
        started_at = time.monotonic()
        stream_key = (
            self.topic.stream_key(item) if self.topic.stream_key is not None else None
        )
        is_terminal = (
            self.topic.is_terminal(item)
            if self.topic.is_terminal is not None
            else False
        )
        try:
            serialized = self.topic.serialize(item)
        except Exception as exception:  # noqa: BLE001 - wire boundary containment
            # A malformed or unexpectedly large item must fail in isolation;
            # terminating TopicRouter.run would drop every later message on
            # the topic and turn one provider bug into a transport outage.
            logger.opt(exception=exception).warning(
                f"Dropping unserializable message on topic {self.topic.topic}"
            )
            if routing_key is not None and self._data_plane_egress_observer is not None:
                self._data_plane_egress_observer.record_dropped(routing_key)
            return
        await self.networking_sender.send(
            OutboundPacket(
                topic=str(self.topic.topic),
                routing_key=routing_key,
                stream_key=stream_key,
                is_terminal=is_terminal,
                data=serialized,
            )
        )
        if (
            self.topic.routing_key is not None
            and self._data_plane_egress_observer is not None
        ):
            self._data_plane_egress_observer.record_enqueue_latency(
                time.monotonic() - started_at
            )


class TelemetryTopicRouter(TopicRouter[NodeTelemetry]):
    """Bounded latest-value admission before isolated telemetry egress."""

    def __init__(
        self,
        topic: TypedTopic[NodeTelemetry],
        networking_sender: Sender[OutboundPacket],
        observer: TelemetryPlaneObserver,
        *,
        admission_capacity: int = _TELEMETRY_ADMISSION_CAPACITY,
    ) -> None:
        super().__init__(topic, networking_sender)
        wake_send, wake_recv = channel[None](1)
        self._wake_send = wake_send
        self._wake_recv = wake_recv
        self._pending: OrderedDict[str, _PendingTelemetry] = OrderedDict()
        self._admission_capacity = admission_capacity
        self._observer = observer

    def new_telemetry_sender(self) -> TelemetrySender:
        """Return a producer handle backed by this router's coalescing map."""

        return TelemetrySender(self)

    def offer(self, item: NodeTelemetry) -> None:
        """Keep only the latest reading for one bounded telemetry identity."""

        self._observer.record_offered()
        key = item.coalescing_key()
        if key in self._pending:
            self._pending.pop(key)
            self._observer.record_coalesced()
        elif len(self._pending) >= self._admission_capacity:
            self._pending.popitem(last=False)
            self._observer.record_dropped()
        self._pending[key] = _PendingTelemetry(item=item, enqueued_at=time.monotonic())
        self._observer.record_depth(
            pending=len(self._pending),
            network_queue=(self.networking_sender.statistics().current_buffer_used),
        )
        with suppress(WouldBlock):
            self._wake_send.send_nowait(None)

    def _send_out_nowait(self, item: NodeTelemetry) -> bool:
        """Offer one telemetry packet without coupling local delivery to egress.

        Returns:
            ``True`` when the reading was queued or cannot be retried. ``False``
            when bounded network capacity is full and the latest value must stay
            pending for the egress loop's next wakeup.
        """

        try:
            serialized = self.topic.serialize(item)
        except Exception as exception:  # noqa: BLE001 - wire boundary containment
            self._observer.record_publish_failure()
            logger.opt(exception=exception).warning(
                "Dropping unserializable message on isolated telemetry egress"
            )
            return True
        packet = OutboundPacket(
            topic=str(self.topic.topic),
            routing_key=None,
            stream_key=None,
            is_terminal=False,
            data=serialized,
        )
        try:
            self.networking_sender.send_nowait(packet)
        except (BrokenResourceError, ClosedResourceError):
            self._observer.record_publish_failure()
        except WouldBlock:
            return False
        return True

    def wake_egress(self) -> None:
        """Wake pending latest values after one network packet leaves the queue."""

        with suppress(ClosedResourceError, WouldBlock):
            self._wake_send.send_nowait(None)

    async def run(self) -> None:
        """Drain admitted latest values while producers continue to coalesce."""

        logger.debug(f"Telemetry Topic Router {self.topic} ready to send")
        with self._wake_recv as wakeups:
            async for _ in wakeups:
                # Process each value present at wake time at most once. Values
                # that cannot enter the one-packet network queue stay pending,
                # but every newly offered value still reaches local readers.
                for _ in range(len(self._pending)):
                    key, pending = self._pending.popitem(last=False)
                    # Local readers never wait for a remote peer or transport queue.
                    if not pending.local_published:
                        await self.publish(pending.item, origin=None)
                    if not self._send_out_nowait(pending.item):
                        self._pending[key] = _PendingTelemetry(
                            item=pending.item,
                            enqueued_at=pending.enqueued_at,
                            local_published=True,
                        )

    async def shutdown(self) -> None:
        """Close telemetry admission and inherited local receivers."""

        self._wake_send.close()
        self._wake_recv.close()
        await super().shutdown()

    def pending_enqueued_at(self) -> tuple[float, ...]:
        """Return pending timestamps for aggregate age diagnostics."""

        return tuple(pending.enqueued_at for pending in self._pending.values())

    @property
    def pending_count(self) -> int:
        """Number of distinct latest-value keys awaiting egress."""

        return len(self._pending)


class Router:
    async def host_network(
        self, network_version: str, namespace_token: str
    ) -> HostNetwork:
        """Describe live listeners without dialing peers or exposing namespace secrets."""
        control = tcp_endpoints(await self._net.listen_addresses(), multiaddr=True)
        data = (
            tcp_endpoints(await self._zenoh.listen_addresses(), multiaddr=False)
            if self._zenoh is not None
            else ()
        )
        return HostNetwork(
            node_id=self._node_id,
            network_version=network_version,
            namespace_fingerprint=namespace_fingerprint(namespace_token),
            control=control,
            data_transport="zenoh" if self._zenoh is not None else "gossipsub",
            data=data,
        )

    @classmethod
    def create(
        cls,
        identity: Keypair,
        bootstrap_peers: Sequence[str] = (),
        listen_port: int = 0,
        zenoh_listen_endpoints: Sequence[str] | None = None,
        zenoh_connect_endpoints: Sequence[str] = (),
        node_id: str | None = None,
        zenoh_namespace: str | None = None,
        zenoh_multicast_scouting: bool = False,
    ) -> "Router":
        # When zenoh_listen_endpoints is provided the data plane (DATA topic)
        # rides a Zenoh peer session instead of gossipsub (the zenoh_data_plane
        # flag, decided by the caller). All other topics stay on libp2p.
        # zenoh_namespace (#308) isolates this fleet's keys from foreign peers.
        zenoh: ZenohHandle | None = None
        if zenoh_listen_endpoints is not None:
            zenoh = ZenohHandle(
                list(zenoh_listen_endpoints),
                list(zenoh_connect_endpoints),
                zenoh_namespace,
                zenoh_multicast_scouting,
            )
        # The Zenoh data plane addresses output per owner (key data/<node_id>),
        # so the Router subscribes only to its own id; default to the keypair's
        # node id when the caller doesn't override it.
        resolved_node_id = node_id if node_id is not None else identity.to_node_id()
        return cls(
            handle=NetworkingHandle(identity, list(bootstrap_peers), listen_port),
            zenoh=zenoh,
            node_id=resolved_node_id,
        )

    def __init__(
        self,
        handle: NetworkingHandle,
        zenoh: ZenohHandle | None = None,
        node_id: str = "",
    ):
        self.topic_routers: dict[str, TopicRouter[CamelCaseModel]] = {}
        send, recv = channel[OutboundPacket]()
        self.networking_receiver: Receiver[OutboundPacket] = recv
        self._net: NetworkingHandle = handle
        # Optional Zenoh transport for the data plane; None is the explicit
        # gossipsub fallback.
        self._zenoh: ZenohHandle | None = zenoh
        # This node's id, used as the Zenoh data-plane subscription suffix so a
        # node receives only output addressed to it (#279 Phase 2). Required when
        # Zenoh is enabled: an empty id would subscribe to "data/" and the node
        # would never receive its addressed chunks, silently losing output (#310
        # review). `create()` always resolves it from the keypair, so this only
        # guards a raw misuse of the constructor.
        if zenoh is not None and not node_id:
            raise ValueError(
                "Router requires a non-empty node_id when the Zenoh data plane "
                "is enabled (it is the data/<node_id> subscription key)."
            )
        self._node_id: str = node_id
        self._tmp_networking_sender: Sender[OutboundPacket] | None = send
        election_send, election_recv = channel[OutboundPacket](
            _ELECTION_OUTBOUND_BUFFER
        )
        self._election_out_send = election_send
        self._election_out_recv = election_recv
        authority_send, authority_recv = channel[OutboundPacket](
            _AUTHORITY_OUTBOUND_BUFFER
        )
        self._authority_out_send = authority_send
        self._authority_out_recv = authority_recv
        telemetry_send, telemetry_recv = channel[OutboundPacket](
            _TELEMETRY_OUTBOUND_BUFFER
        )
        self._telemetry_out_send = telemetry_send
        self._telemetry_out_recv = telemetry_recv
        self._telemetry_plane_observer = TelemetryPlaneObserver(
            admission_capacity=_TELEMETRY_ADMISSION_CAPACITY,
            network_queue_capacity=_TELEMETRY_OUTBOUND_BUFFER,
        )
        # Live libp2p connections by peer: (refcount, first observed remote
        # endpoint). Feeds current_session_connections (#662).
        self._session_connections: dict[str, tuple[int, tuple[str, int] | None]] = {}
        # Live swarm connections, from PyFromSwarm.Connection updates. Gates
        # the telemetry no-peer mismatch warning (#660): a lone node hits
        # no-peer outcomes as its normal state and must not be told to
        # suspect a wire mismatch (PR #663 review).
        self._connected_swarm_peers: set[str] = set()
        # Dedicated outbound channel for the Zenoh DATA plane (#309): the DATA
        # path publishes with CongestionControl::Block, so a stuck/slow
        # subscriber can stall its egress. Draining it on its OWN loop (not the
        # shared networking loop that also carries control-plane commands/events)
        # confines that backpressure to DATA. The channel is BOUNDED (#312
        # review) so a stalled subscriber backpressures the producer instead of
        # growing memory without limit and OOMing the node. Only created when
        # Zenoh is on.
        self._zenoh_out_send: Sender[OutboundPacket] | None = None
        self._zenoh_out_recv: Receiver[OutboundPacket] | None = None
        vision_send, vision_recv = channel[OutboundPacket](
            _ZENOH_VISION_OUTBOUND_BUFFER
        )
        self._zenoh_vision_out_send = vision_send
        self._zenoh_vision_out_recv = vision_recv
        vision_ingress_send, vision_ingress_recv = channel[_InboundVisionPacket](
            _VISION_NETWORK_PAYLOAD_BUFFER
        )
        self._vision_network_ingress_send = vision_ingress_send
        self._vision_network_ingress_recv = vision_ingress_recv
        vision_terminal_send, vision_terminal_recv = channel[_InboundVisionPacket](
            _VISION_NETWORK_TERMINAL_BUFFER
        )
        self._vision_network_terminal_send = vision_terminal_send
        self._vision_network_terminal_recv = vision_terminal_recv
        self._vision_network_ingress_drops = 0
        self._data_plane_egress_observer = DataPlaneEgressObserver()
        self._vision_media_egress_observer = DataPlaneEgressObserver()
        self._gossipsub_queue_drop_count = 0
        self._last_gossipsub_queue_warning = 0.0
        self._zenoh_idle_stream_reclaim_count = 0
        self._last_zenoh_idle_stream_reclaim_warning = 0.0
        if zenoh is not None:
            zsend, zrecv = channel[OutboundPacket](_ZENOH_DATA_OUTBOUND_BUFFER)
            self._zenoh_out_send = zsend
            self._zenoh_out_recv = zrecv
        self._id_count = count()
        self._tg: TaskGroup = TaskGroup()

    def uses_zenoh(self, topic: str) -> bool:
        """Whether ``topic`` is routed over the Zenoh data plane."""

        return self._zenoh is not None and topic in (
            DATA.topic,
            PROVIDER_DATA.topic,
            REALTIME_AUDIO.topic,
            SPEECH_MEDIA.topic,
            TRACE_DATA.topic,
            VISION_MEDIA.topic,
        )

    async def zenoh_connected_peer_count(self) -> int | None:
        """Count live Zenoh peer transports, or ``None`` when DATA rides gossipsub.

        Zero with cluster peers advertising Zenoh means this node's data plane
        is isolated (every remote stream will fail with transport errors) even
        though the libp2p control plane is healthy, e.g. a zero-config remote
        member that multicast scouting cannot reach. Callers advertise the
        count so cluster health can name the condition.
        """
        if self._zenoh is None:
            return None
        return await self._zenoh.zenoh_connected_peer_count()

    async def register_topic[T: CamelCaseModel](self, topic: TypedTopic[T]):
        if topic.topic == VISION_MEDIA.topic:
            send = self._zenoh_vision_out_send.clone()
        elif self.uses_zenoh(topic.topic):
            # DATA on Zenoh egresses via its own loop so Block backpressure
            # can't stall the shared control-plane publish loop (#309).
            assert self._zenoh_out_send is not None
            send = self._zenoh_out_send.clone()
        elif topic.topic == ELECTION_MESSAGES.topic:
            send = self._election_out_send.clone()
        elif topic.topic == AUTHORITY_MESSAGES.topic:
            send = self._authority_out_send.clone()
        elif topic.topic == TELEMETRY.topic:
            send = self._telemetry_out_send.clone()
        else:
            send = self._tmp_networking_sender
            if send:
                self._tmp_networking_sender = None
            else:
                send = self.networking_receiver.clone_sender()
        local_routing_key = self._node_id if topic.routing_key is not None else None
        if topic.topic == TELEMETRY.topic:
            router = cast(
                TopicRouter[T],
                TelemetryTopicRouter(
                    cast(TypedTopic[NodeTelemetry], topic),
                    send,
                    self._telemetry_plane_observer,
                ),
            )
        else:
            if topic.topic == VISION_MEDIA.topic:
                producer_buffer_size = 0
            elif topic.topic == AUTHORITY_MESSAGES.topic:
                # Authority admission must be bounded before serialization as
                # well as after it. Otherwise callers can fill TopicRouter's
                # producer queue without limit while the dedicated egress
                # queue is correctly applying backpressure downstream.
                producer_buffer_size = _AUTHORITY_OUTBOUND_BUFFER
            else:
                producer_buffer_size = inf
            router = TopicRouter[T](
                topic,
                send,
                # Vision producer admission is a rendezvous: packet retention
                # lives only in the bounded, observable egress/stream queues.
                max_buffer_size=producer_buffer_size,
                local_routing_key=local_routing_key,
                data_plane_egress_observer=(
                    self._vision_media_egress_observer
                    if topic.topic == VISION_MEDIA.topic
                    else self._data_plane_egress_observer
                    if topic.routing_key is not None
                    else None
                ),
            )
        self.topic_routers[topic.topic] = cast(TopicRouter[CamelCaseModel], router)
        if self._tg.is_running():
            await self._networking_subscribe(topic.topic)

    def sender[T: CamelCaseModel](self, topic: TypedTopic[T]) -> Sender[T]:
        if topic.topic == TELEMETRY.topic:
            raise ValueError("Use telemetry_sender() for bounded telemetry admission")
        router = self.topic_routers.get(topic.topic, None)
        # There's gotta be a way to do this without THIS many asserts
        assert router is not None
        assert router.topic == topic
        sender = cast(TopicRouter[T], router).new_sender()
        return sender

    def telemetry_sender(self) -> TelemetrySender:
        """Return a non-blocking sender for bounded latest-value telemetry."""

        router = self.topic_routers.get(TELEMETRY.topic)
        assert isinstance(router, TelemetryTopicRouter)
        return router.new_telemetry_sender()

    def receiver[T: CamelCaseModel](
        self,
        topic: TypedTopic[T],
        *,
        max_buffer_size: float = inf,
    ) -> Receiver[T]:
        router = self.topic_routers.get(topic.topic, None)
        # There's gotta be a way to do this without THIS many asserts

        assert router is not None
        assert router.topic == topic
        assert router.topic.model_type == topic.model_type

        receiver_buffer_size = (
            0
            if topic.topic == VISION_MEDIA.topic and max_buffer_size == inf
            else max_buffer_size
        )
        send, recv = channel[T](receiver_buffer_size)
        router.senders.add(cast(Sender[CamelCaseModel], send))

        return recv

    def receiver_with_origin[T: CamelCaseModel](
        self, topic: TypedTopic[T]
    ) -> Receiver[tuple[str | None, T]]:
        router = self.topic_routers.get(topic.topic, None)
        assert router is not None
        assert router.topic == topic
        assert router.topic.model_type == topic.model_type

        send, recv = channel[tuple[str | None, T]]()
        router.origin_senders.add(cast(Sender[tuple[str | None, CamelCaseModel]], send))
        return recv

    async def run(self):
        logger.debug("Starting Router")
        try:
            async with self._tg as tg:
                for topic in self.topic_routers:
                    router = self.topic_routers[topic]
                    tg.start_soon(router.run)
                tg.start_soon(self._networking_recv)
                tg.start_soon(self._networking_publish)
                tg.start_soon(self._election_networking_publish)
                tg.start_soon(self._authority_networking_publish)
                tg.start_soon(self._telemetry_networking_publish)
                tg.start_soon(self._vision_networking_publish)
                tg.start_soon(self._vision_networking_ingress)
                if self._zenoh is not None:
                    tg.start_soon(self._zenoh_recv)
                    # Dedicated DATA-plane egress loop so Block backpressure
                    # stays off the shared control-plane publish loop (#309).
                    tg.start_soon(self._zenoh_networking_publish)
                # subscribe to pending topics
                for topic in self.topic_routers:
                    await self._networking_subscribe(topic)
                # Router only shuts down if you cancel it.
                await sleep_forever()
        finally:
            with move_on_after(1, shield=True):
                for topic in self.topic_routers:
                    await self._networking_unsubscribe(str(topic))

    async def shutdown(self):
        logger.debug("Shutting down Router")
        self._tg.cancel_tasks()

    def data_plane_egress_diagnostics(self) -> DataPlaneEgressDiagnostics:
        """Return process-local DATA egress pressure and isolation metrics."""

        return self._data_plane_egress_observer.snapshot()

    def vision_media_egress_diagnostics(self) -> DataPlaneEgressDiagnostics:
        """Return process-local vision routing pressure and isolation metrics."""

        payload_stats = self._vision_network_ingress_recv.statistics()
        terminal_stats = self._vision_network_terminal_recv.statistics()
        return self._vision_media_egress_observer.snapshot().model_copy(
            update={
                "inbound_payload_queue_depth": payload_stats.current_buffer_used,
                "inbound_payload_queue_capacity": _VISION_NETWORK_PAYLOAD_BUFFER,
                "inbound_terminal_queue_depth": terminal_stats.current_buffer_used,
                "inbound_terminal_queue_capacity": _VISION_NETWORK_TERMINAL_BUFFER,
                "inbound_frames_dropped": self._vision_network_ingress_drops,
            }
        )

    def telemetry_plane_diagnostics(self) -> TelemetryPlaneDiagnostics:
        """Return bounded telemetry admission and isolated egress metrics."""

        router = self.topic_routers.get(TELEMETRY.topic)
        assert isinstance(router, TelemetryTopicRouter)
        network_depth = self._telemetry_out_recv.statistics().current_buffer_used
        return self._telemetry_plane_observer.snapshot(
            pending_enqueued_at=router.pending_enqueued_at(),
            pending_readings=router.pending_count,
            network_queue_depth=network_depth,
        )

    async def _networking_subscribe(self, topic: str):
        if self.uses_zenoh(topic):
            assert self._zenoh is not None
            # Subscribe only to output addressed to this node (data/<node_id>),
            # not the whole topic, so the serving worker's unicast reaches just
            # the owning API node instead of fanning out to every node (#279
            # Phase 2). The owner is keyed by node id; the bare topic is never
            # subscribed, so non-owners receive nothing.
            key = f"{topic}/{self._node_id}"
            await self._zenoh.zenoh_subscribe(key)
            logger.info(f"Subscribed to {key} (zenoh data plane)")
            return
        await self._net.gossipsub_subscribe(topic)
        logger.info(f"Subscribed to {topic}")

    async def _networking_unsubscribe(self, topic: str):
        if self.uses_zenoh(topic):
            # Zenoh subscribers are undeclared when the session closes; there is
            # no per-topic unsubscribe to issue here.
            return
        await self._net.gossipsub_unsubscribe(topic)
        logger.info(f"Unsubscribed from {topic}")

    async def _zenoh_recv(self):
        """Drain inbound DATA-plane samples from Zenoh into their topic router.

        Parallel to :meth:`_networking_recv` (which handles the gossipsub
        planes). Demux is by the ``command_id`` inside the payload, done
        downstream in the DATA topic's consumer, exactly as on gossipsub.
        """
        assert self._zenoh is not None
        try:
            while True:
                message = await self._zenoh.recv()
                # The sample key is data/<owner_node>; the TopicRouter is keyed by
                # the bare topic, so strip the routing suffix to find it (#279
                # Phase 2).
                topic = message.topic.split("/", 1)[0]
                if topic not in self.topic_routers:
                    logger.warning(
                        f"Received zenoh message on unknown or inactive topic "
                        f"{message.topic}"
                    )
                    continue
                # Vision delivery has its own bounded consumers so image decode
                # or component backpressure cannot stall generated DATA receive.
                if topic == VISION_MEDIA.topic:
                    self._offer_vision_network_packet(message.data, None)
                    continue
                # No origin peer id on the zenoh data plane; the data plane does
                # not use origin (output chunks never mutate State).
                await self.topic_routers[topic].publish_bytes(message.data, None)
        except Exception as exception:
            logger.opt(exception=exception).error(
                "Zenoh data-plane receive loop terminated unexpectedly"
            )
            raise

    def _track_session_connection(self, message: ConnectionMessage) -> None:
        """Maintain the live-connection snapshot for late consumers (#662).

        A worker recreated mid-session (master election recreates it) starts
        its session-edge ingress with a fresh receiver, so connections
        established BEFORE that receiver existed would never re-emit and the
        new session's topology would miss their edges. The router outlives
        worker recreation, so it keeps the authoritative per-peer refcount
        and one representative endpoint, and ``current_session_connections``
        hands a snapshot to each newly started ingress.
        """
        peer = str(message.node_id)
        if message.connected:
            count, endpoint = self._session_connections.get(peer, (0, None))
            if (
                endpoint is None
                and message.remote_ip is not None
                and message.remote_tcp_port is not None
            ):
                endpoint = (message.remote_ip, message.remote_tcp_port)
            self._session_connections[peer] = (count + 1, endpoint)
        else:
            count, endpoint = self._session_connections.get(peer, (1, None))
            if count <= 1:
                self._session_connections.pop(peer, None)
            else:
                self._session_connections[peer] = (count - 1, endpoint)

    def current_session_connections(self) -> dict[str, tuple[str, int, int]]:
        """Snapshot of live peers as ``(ip, port, live_connection_count)`` (#662).

        The live count rides along so a consumer seeding its own refcounts
        (the worker's session-edge ingress after a mid-session recreation)
        starts from the true number of connections: seeding one ref for a
        multi-homed peer would let the first later disconnect delete the
        session edge while other connections remain (PR #668 review).
        """
        return {
            peer: (endpoint[0], endpoint[1], count)
            for peer, (count, endpoint) in self._session_connections.items()
            if endpoint is not None
        }

    async def _networking_recv(self):
        try:
            while True:
                from_swarm = await self._net.recv()
                logger.debug(from_swarm)
                match from_swarm:
                    case PyFromSwarm.Message(origin, topic, data):
                        logger.trace(
                            f"Received message on {topic} from {origin} with payload {data}"
                        )
                        if topic not in self.topic_routers:
                            logger.warning(
                                f"Received message on unknown or inactive topic {topic}"
                            )
                            continue
                        if topic == VISION_MEDIA.topic:
                            self._offer_vision_network_packet(data, origin)
                            continue
                        router = self.topic_routers[topic]
                        await router.publish_bytes(data, origin)
                    case PyFromSwarm.Connection():
                        message = ConnectionMessage.from_update(from_swarm)
                        logger.trace(
                            f"Received message on connection_messages with payload {message}"
                        )
                        self._track_session_connection(message)
                        had_peers = bool(self._connected_swarm_peers)
                        if message.connected:
                            self._connected_swarm_peers.add(str(message.node_id))
                        else:
                            self._connected_swarm_peers.discard(str(message.node_id))
                        if not had_peers and self._connected_swarm_peers:
                            # First connection after a peerless stretch: give
                            # gossipsub subscription exchange its own grace
                            # window before any mismatch warning can fire.
                            self._telemetry_plane_observer.restart_no_peer_window()
                        if CONNECTION_MESSAGES.topic in self.topic_routers:
                            router = self.topic_routers[CONNECTION_MESSAGES.topic]
                            assert router.topic.model_type == ConnectionMessage
                            router = cast(TopicRouter[ConnectionMessage], router)
                            await router.publish(message)
                    case _:
                        logger.critical(
                            "failed to exhaustively check FromSwarm messages - logic error"
                        )
        except Exception as exception:
            logger.opt(exception=exception).error(
                "Gossipsub receive loop terminated unexpectedly"
            )
            raise

    async def _networking_publish(self):
        # Gossipsub control traffic plus DATA when Zenoh is off. DATA on Zenoh is
        # diverted to _zenoh_networking_publish at register time, while election
        # authority, election, and telemetry each have a dedicated Python
        # egress loop. routing_key is irrelevant to bare-topic gossipsub broadcast.
        with self.networking_receiver as networked_items:
            async for packet in networked_items:
                await self._publish_gossipsub_packet(packet)

    async def _election_networking_publish(self) -> None:
        """Publish election traffic independently from ordinary gossipsub egress."""

        with self._election_out_recv as election_items:
            async for packet in election_items:
                await self._publish_gossipsub_packet(packet)

    async def _authority_networking_publish(self) -> None:
        """Publish authority traffic independently from ordinary Python egress."""

        with self._authority_out_recv as authority_items:
            async for packet in authority_items:
                await self._publish_gossipsub_packet(packet)

    async def _telemetry_networking_publish(self) -> None:
        """Publish telemetry independently from correctness-critical egress."""

        with self._telemetry_out_recv as telemetry_items:
            async for packet in telemetry_items:
                try:
                    await self._publish_gossipsub_packet(packet, telemetry=True)
                except Exception as exception:  # noqa: BLE001 - lossy plane containment
                    self._telemetry_plane_observer.record_publish_failure()
                    logger.opt(exception=exception).warning(
                        "Isolated telemetry publish failed; latest values remain lossy"
                    )
                finally:
                    router = self.topic_routers.get(TELEMETRY.topic)
                    if isinstance(router, TelemetryTopicRouter):
                        router.wake_egress()

    async def _publish_gossipsub_packet(
        self, packet: OutboundPacket, *, telemetry: bool = False
    ) -> None:
        """Publish one packet while containing and aggregating transport pressure."""

        try:
            # Log the topic and payload SIZE, not the payload: loguru brace-args
            # defer formatting until TRACE is actually enabled, and rendering the
            # bytes (up to max_transmit_size) on every publish would add the very
            # overload amplification this path guards against.
            logger.trace(
                "Sending message on {} ({} bytes)", packet.topic, len(packet.data)
            )
            if len(packet.data) > 1024 * 1024:
                logger.warning(
                    "Sending overlarge payload, network performance may be temporarily degraded"
                )
            await self._net.gossipsub_publish(packet.topic, packet.data)
            if telemetry:
                self._telemetry_plane_observer.record_published(len(packet.data))
        except NoPeersSubscribedToTopicError:
            # Ordinary topics tolerate this quietly (a lone node publishing
            # before peers arrive is normal). Telemetry does NOT: a sustained
            # no-peer state on a node with live connections means its
            # heartbeats reach nobody and it is invisible to membership while
            # looking healthy locally — the wire-mismatch ghost (#660). Count
            # it distinctly and warn once the state persists; the warning is
            # gated on live swarm connections so a lone node is never told
            # to suspect a mismatch.
            if telemetry and self._telemetry_plane_observer.record_no_peer_publish(
                peers_connected=bool(self._connected_swarm_peers)
            ):
                logger.warning(
                    "Telemetry has had no subscribed peers for over "
                    f"{NO_PEER_WARNING_AFTER_SECONDS:.0f}s on the isolated "
                    "telemetry protocol; this node's heartbeats are reaching "
                    "nobody and it will not appear in cluster membership. If "
                    "other nodes are connected, suspect a wire-protocol/build "
                    "mismatch (rebuild bindings, check versions)."
                )
        except AllQueuesFullError:
            if telemetry:
                self._telemetry_plane_observer.record_publish_failure()
            self._gossipsub_queue_drop_count += 1
            now = time.monotonic()
            if now - self._last_gossipsub_queue_warning >= 10.0:
                logger.warning(
                    f"All peer queues full; dropped "
                    f"{self._gossipsub_queue_drop_count} gossipsub message(s) "
                    f"since the last report (latest topic={packet.topic})"
                )
                self._gossipsub_queue_drop_count = 0
                self._last_gossipsub_queue_warning = now
        except MessageTooLargeError:
            if telemetry:
                self._telemetry_plane_observer.record_publish_failure()
            logger.warning(
                f"Message too large for gossipsub on {packet.topic} "
                f"({len(packet.data)} bytes), dropping"
            )

    async def _zenoh_networking_publish(self, *, vision_ingress: bool = False):
        """Drain the DATA-plane outbound channel onto Zenoh (#309).

        Separate from `_networking_publish` so the DATA path's
        `CongestionControl::Block` can only ever stall DATA egress, never the
        shared control-plane publish loop. DATA is best-effort, so a publish
        failure is logged and dropped rather than allowed to tear the loop down.
        """
        if not vision_ingress:
            assert self._zenoh is not None
        receiver = (
            self._zenoh_vision_out_recv if vision_ingress else self._zenoh_out_recv
        )
        assert receiver is not None
        stream_buffer = (
            _ZENOH_VISION_STREAM_BUFFER if vision_ingress else _ZENOH_DATA_STREAM_BUFFER
        )
        max_streams_per_owner = (
            _ZENOH_VISION_MAX_STREAMS_PER_OWNER
            if vision_ingress
            else _ZENOH_DATA_MAX_STREAMS_PER_OWNER
        )
        max_active_streams = (
            _ZENOH_VISION_MAX_ACTIVE_STREAMS
            if vision_ingress
            else _ZENOH_DATA_MAX_ACTIVE_STREAMS
        )
        max_rejection_tasks = (
            _ZENOH_VISION_MAX_REJECTION_TASKS
            if vision_ingress
            else _ZENOH_DATA_MAX_REJECTION_TASKS
        )
        rejected_tombstones = (
            _ZENOH_VISION_REJECTED_STREAM_TOMBSTONES
            if vision_ingress
            else _ZENOH_DATA_REJECTED_STREAM_TOMBSTONES
        )
        stream_senders: dict[tuple[str, str, str], Sender[OutboundPacket]] = {}
        owner_stream_counts: dict[str, int] = {}
        rejected_streams: set[tuple[str, str, str]] = set()
        rejected_stream_order: deque[tuple[str, str, str]] = deque()
        rejection_slots = Semaphore(max_rejection_tasks)

        def reject_stream(stream: tuple[str, str, str]) -> None:
            rejected_streams.add(stream)
            rejected_stream_order.append(stream)
            while len(rejected_stream_order) > rejected_tombstones:
                rejected_streams.discard(rejected_stream_order.popleft())

        async with TaskGroup() as task_group:
            with receiver as items:
                async for packet in items:
                    observer = self._egress_observer_for_topic(packet.topic)
                    # Nodes subscribe only to data/<own_node_id>, never the bare
                    # topic, so a keyless message reaches no subscriber. Every serving
                    # task carries owner_node (#279 Phase 2), so this should not
                    # happen - warn loudly rather than drop silently (#310 review).
                    if not packet.routing_key:
                        logger.warning(
                            f"Zenoh DATA publish on {packet.topic} has no routing key "
                            f"(owner_node unset); no node subscribes to the bare topic, "
                            f"so this output would be lost. Dropping a chunk of "
                            f"{len(packet.data)} bytes."
                        )
                        continue
                    owner = packet.routing_key
                    if packet.stream_key is None:
                        logger.warning(
                            "Zenoh DATA frame has no command stream key; dropping it"
                        )
                        observer.record_dropped(owner)
                        continue
                    # Data-plane topics share this bounded dispatcher, but
                    # their independently generated ids occupy separate
                    # protocol namespaces. Topic identity must therefore be
                    # part of every queue and rejection key.
                    stream = (packet.topic, owner, packet.stream_key)
                    if stream in rejected_streams:
                        observer.record_dropped(owner)
                        if packet.is_terminal:
                            rejected_streams.discard(stream)
                        continue
                    sender = stream_senders.get(stream)
                    if sender is None:
                        if (
                            len(stream_senders) >= max_active_streams
                            or owner_stream_counts.get(owner, 0)
                            >= max_streams_per_owner
                        ):
                            observer.record_dropped(owner)
                            reject_stream(stream)
                            logger.warning(
                                "Zenoh DATA stream admission full for owner; "
                                "rejecting the affected command"
                            )
                            try:
                                rejection_slots.acquire_nowait()
                            except WouldBlock:
                                # The rejection path is itself bounded. Global
                                # backpressure applies only after a second full
                                # stream-cap worth of rejections is already in
                                # flight, rather than creating unbounded tasks.
                                await rejection_slots.acquire()
                            task_group.start_soon(
                                self._publish_zenoh_data_rejection,
                                packet,
                                owner,
                                rejection_slots,
                                True,
                            )
                            continue
                        sender, receiver = channel[OutboundPacket](stream_buffer)
                        stream_senders[stream] = sender
                        owner_stream_counts[owner] = (
                            owner_stream_counts.get(owner, 0) + 1
                        )
                        observer.record_stream_opened(owner)
                        task_group.start_soon(
                            self._publish_zenoh_data_stream,
                            stream,
                            receiver,
                            stream_senders,
                            owner_stream_counts,
                            reject_stream,
                            vision_ingress,
                        )
                    try:
                        sender.send_nowait(packet)
                    except WouldBlock:
                        observer.record_dropped(owner)
                        logger.warning(
                            "Zenoh DATA command queue full; dropping frame so "
                            "unrelated streams remain progressive"
                        )
                        if packet.is_terminal or packet.topic in (
                            REALTIME_AUDIO.topic,
                            SPEECH_MEDIA.topic,
                            VISION_MEDIA.topic,
                        ):
                            sender.close()
                            reject_stream(stream)
                            try:
                                rejection_slots.acquire_nowait()
                            except WouldBlock:
                                await rejection_slots.acquire()
                            task_group.start_soon(
                                self._publish_zenoh_data_rejection,
                                packet,
                                owner,
                                rejection_slots,
                                False,
                            )
                        continue
                    except ClosedResourceError:
                        observer.record_dropped(owner)
                        logger.warning(
                            "Zenoh DATA command queue closed before a late frame; "
                            "dropping it without affecting unrelated streams"
                        )
                        continue
                    observer.record_enqueued(owner)

    async def _vision_networking_publish(self) -> None:
        """Drain bounded vision ingress independently from control and output."""

        await self._zenoh_networking_publish(vision_ingress=True)

    def _offer_vision_network_packet(self, data: bytes, origin: str | None) -> None:
        """Admit one network frame without awaiting a vision component consumer."""

        try:
            packet = VISION_MEDIA.deserialize(data)
        except (ValidationError, ValueError, UnicodeDecodeError) as exception:
            logger.opt(exception=exception).warning(
                f"Dropping malformed or schema-incompatible message on topic "
                f"{VISION_MEDIA.topic} from {origin}"
            )
            return
        if str(packet.target_node) != self._node_id:
            return
        # Completion is part of the ordered upload sequence. Sending it through
        # the independent terminal lane would let it overtake open/chunk frames.
        terminal_lane = packet.kind in ("accepted", "cancelled", "transport_failed")
        sender = (
            self._vision_network_terminal_send
            if terminal_lane
            else self._vision_network_ingress_send
        )
        try:
            sender.send_nowait(_InboundVisionPacket(packet=packet, origin=origin))
        except (BrokenResourceError, ClosedResourceError):
            return
        except WouldBlock:
            self._vision_network_ingress_drops += 1
            if packet.kind in ("opened", "chunk", "completed"):
                failure = packet.transport_failure(
                    "Vision media ingress capacity is exhausted on the target node"
                )
                try:
                    self._vision_network_terminal_send.send_nowait(
                        _InboundVisionPacket(
                            packet=failure,
                            origin=None,
                            outbound=True,
                        )
                    )
                except (WouldBlock, BrokenResourceError, ClosedResourceError):
                    self._vision_network_ingress_drops += 1

    async def _vision_networking_ingress(self) -> None:
        """Deliver payload and terminal lanes independently of shared receive."""

        async with TaskGroup() as task_group:
            task_group.start_soon(
                self._drain_vision_network_lane,
                self._vision_network_ingress_recv,
            )
            task_group.start_soon(
                self._drain_vision_network_lane,
                self._vision_network_terminal_recv,
            )
            await sleep_forever()

    async def _drain_vision_network_lane(
        self, receiver: Receiver[_InboundVisionPacket]
    ) -> None:
        """Drain one isolated vision lane into local delivery or reverse egress."""

        with receiver as items:
            async for item in items:
                topic_router = self.topic_routers.get(VISION_MEDIA.topic)
                if topic_router is None:
                    continue
                if item.outbound:
                    await topic_router.publish_to_network(item.packet)
                else:
                    await topic_router.publish(item.packet, origin=item.origin)

    def _egress_observer_for_topic(self, topic: str) -> DataPlaneEgressObserver:
        """Select the independent observer for one routed data topic."""

        if topic == VISION_MEDIA.topic:
            return self._vision_media_egress_observer
        return self._data_plane_egress_observer

    async def _publish_routed_data_packet(
        self, topic: str, owner: str, data: bytes
    ) -> None:
        """Publish one addressed packet on Zenoh or isolated vision fallback."""

        if self.uses_zenoh(topic):
            assert self._zenoh is not None
            await self._zenoh.zenoh_publish(f"{topic}/{owner}", data)
            return
        if topic == VISION_MEDIA.topic:
            await self._net.gossipsub_publish(topic, data)
            return
        raise RuntimeError(f"No routed data transport is active for {topic}")

    async def _publish_zenoh_data_rejection(
        self,
        packet: OutboundPacket,
        owner: str,
        rejection_slots: Semaphore,
        include_started: bool,
        *,
        failure_message: str = (
            "DATA transport rejected the stream because remote egress "
            "capacity is exhausted"
        ),
        failure_sequence_offset: int = 0,
    ) -> None:
        """Tell one admitted API that router stream capacity rejected its call."""

        try:
            if packet.topic == DATA.topic:
                rejection_topic = cast(TypedTopic[CamelCaseModel], DATA)
            elif packet.topic == PROVIDER_DATA.topic:
                rejection_topic = cast(TypedTopic[CamelCaseModel], PROVIDER_DATA)
            elif packet.topic == REALTIME_AUDIO.topic:
                rejection_topic = cast(TypedTopic[CamelCaseModel], REALTIME_AUDIO)
            elif packet.topic == SPEECH_MEDIA.topic:
                rejection_topic = cast(TypedTopic[CamelCaseModel], SPEECH_MEDIA)
            elif packet.topic == VISION_MEDIA.topic:
                rejection_topic = cast(TypedTopic[CamelCaseModel], VISION_MEDIA)
            else:
                return
            original = rejection_topic.deserialize(packet.data)
            if isinstance(original, DataChunk):
                failed_sequence = (
                    1
                    if include_started
                    else original.sequence + failure_sequence_offset
                )
                rejection_frames: list[CamelCaseModel] = []
                if include_started:
                    rejection_frames.append(
                        DataChunk(
                            command_id=original.command_id,
                            kind="started",
                            sequence=0,
                            owner_node=original.owner_node,
                        )
                    )
                rejection_frames.append(
                    DataChunk(
                        command_id=original.command_id,
                        kind="failed",
                        chunk=ErrorChunk(
                            model=ModelId("unknown"),
                            error_message=failure_message,
                        ),
                        sequence=failed_sequence,
                        owner_node=original.owner_node,
                    )
                )
            elif isinstance(original, ProviderStreamPacket):
                rejection_frames = list(
                    provider_stream_rejection_packets(
                        original,
                        include_started=include_started,
                        failure_message=failure_message,
                        failure_sequence_offset=failure_sequence_offset,
                    )
                )
            elif isinstance(
                original,
                (RealtimeAudioPacket, SpeechMediaPacket, VisionMediaPacket),
            ):
                rejection_frames = [original.transport_failure(failure_message)]
            else:
                return
            for frame in rejection_frames:
                data = rejection_topic.serialize(frame)
                rejection_owner = (
                    rejection_topic.routing_key(frame)
                    if rejection_topic.routing_key is not None
                    else owner
                )
                if rejection_owner is None:
                    continue
                started_at = time.monotonic()
                try:
                    rejection_router = self.topic_routers.get(packet.topic)
                    if (
                        rejection_owner == self._node_id
                        and rejection_router is not None
                    ):
                        await rejection_router.publish_bytes(data, None)
                    else:
                        with fail_after(_ZENOH_DATA_PUBLISH_TIMEOUT_SECONDS):
                            await self._publish_routed_data_packet(
                                packet.topic, rejection_owner, data
                            )
                except get_cancelled_exc_class():
                    raise
                except Exception as exception:
                    self._egress_observer_for_topic(
                        packet.topic
                    ).record_publish_failure(
                        rejection_owner, time.monotonic() - started_at
                    )
                    logger.opt(exception=exception).warning(
                        "Zenoh DATA admission rejection publish failed"
                    )
                else:
                    self._egress_observer_for_topic(packet.topic).record_published(
                        rejection_owner,
                        len(data),
                        time.monotonic() - started_at,
                    )
        except Exception as exception:  # noqa: BLE001 - rejection isolation
            # Rejection is a best-effort terminal for one stream. Malformed
            # input or a serializer bug must neither leak the bounded slot nor
            # fail the DATA publisher task group.
            logger.opt(exception=exception).warning(
                "Dropping malformed Zenoh DATA admission rejection"
            )
        finally:
            rejection_slots.release()

    async def _publish_zenoh_data_stream(
        self,
        stream: tuple[str, str, str],
        receiver: Receiver[OutboundPacket],
        stream_senders: dict[tuple[str, str, str], Sender[OutboundPacket]],
        owner_stream_counts: dict[str, int],
        reject_stream: Callable[[tuple[str, str, str]], None],
        vision_ingress: bool = False,
    ) -> None:
        """Publish one command independently so blocked owners cannot stall peers."""

        topic, owner, _stream_id = stream
        observer = self._egress_observer_for_topic(topic)
        last_packet: OutboundPacket | None = None
        last_published_packet: OutboundPacket | None = None
        try:
            with receiver as packets:
                while True:
                    packet: OutboundPacket | None = None
                    with move_on_after(
                        _ZENOH_VISION_STREAM_IDLE_LEASE_SECONDS
                        if vision_ingress
                        else _ZENOH_DATA_STREAM_IDLE_LEASE_SECONDS
                    ) as idle_scope:
                        try:
                            packet = await anext(packets)
                        except StopAsyncIteration:
                            return
                    if idle_scope.cancelled_caught:
                        reject_stream(stream)
                        sender = stream_senders.get(stream)
                        if sender is not None:
                            sender.close()
                        observer.record_stream_idle_reclaimed(owner)
                        self._record_zenoh_idle_stream_reclaim(topic)
                        if last_packet is not None:
                            rejection_slot = Semaphore(1)
                            rejection_slot.acquire_nowait()
                            await self._publish_zenoh_data_rejection(
                                last_published_packet or last_packet,
                                owner,
                                rejection_slot,
                                last_published_packet is None,
                                failure_message=(
                                    "DATA transport reclaimed the stream after its "
                                    "egress idle lease expired"
                                ),
                                failure_sequence_offset=(
                                    1 if last_published_packet is not None else 0
                                ),
                            )
                        return
                    assert packet is not None
                    last_packet = packet
                    observer.record_dequeued(owner)
                    started_at = time.monotonic()
                    try:
                        with fail_after(_ZENOH_DATA_PUBLISH_TIMEOUT_SECONDS):
                            await self._publish_routed_data_packet(
                                packet.topic, owner, packet.data
                            )
                    except get_cancelled_exc_class():
                        raise
                    except Exception as exception:
                        observer.record_publish_failure(
                            owner, time.monotonic() - started_at
                        )
                        logger.opt(exception=exception).warning(
                            "Zenoh DATA command publish failed; dropping frame "
                            "without blocking unrelated streams"
                        )
                        if packet.topic in (
                            REALTIME_AUDIO.topic,
                            SPEECH_MEDIA.topic,
                            VISION_MEDIA.topic,
                        ):
                            reject_stream(stream)
                            rejection_slot = Semaphore(1)
                            rejection_slot.acquire_nowait()
                            await self._publish_zenoh_data_rejection(
                                packet,
                                owner,
                                rejection_slot,
                                False,
                            )
                            return
                    else:
                        last_published_packet = packet
                        observer.record_published(
                            owner,
                            len(packet.data),
                            time.monotonic() - started_at,
                        )
                    if packet.is_terminal:
                        return
        finally:
            sender = stream_senders.pop(stream, None)
            if sender is not None:
                sender.close()
            owner_stream_counts[owner] = max(0, owner_stream_counts.get(owner, 1) - 1)
            if owner_stream_counts[owner] == 0:
                owner_stream_counts.pop(owner, None)
            observer.record_stream_closed(owner)

    def _record_zenoh_idle_stream_reclaim(self, topic: str) -> None:
        """Rate-limit payload-free warnings for reclaimed command queues."""

        self._zenoh_idle_stream_reclaim_count += 1
        now = time.monotonic()
        if now - self._last_zenoh_idle_stream_reclaim_warning < 10.0:
            return
        logger.warning(
            f"Reclaimed {self._zenoh_idle_stream_reclaim_count} idle Zenoh DATA "
            f"stream(s) since the last report (latest topic={topic})"
        )
        self._zenoh_idle_stream_reclaim_count = 0
        self._last_zenoh_idle_stream_reclaim_warning = now


def get_node_id_keypair(
    path: str | bytes | PathLike[str] | PathLike[bytes] = SKULK_NODE_ID_KEYPAIR,
) -> Keypair:
    """
    Obtains the :class:`Keypair` associated with this node-ID.
    Obtain the :class:`PeerId` by from it.
    """
    # TODO(evan): bring back node id persistence once we figure out how to deal with duplicates
    return Keypair.generate()

    def lock_path(path: str | bytes | PathLike[str] | PathLike[bytes]) -> Path:
        return Path(str(path) + ".lock")

    # operate with cross-process lock to avoid race conditions
    with FileLock(lock_path(path)):
        with open(path, "a+b") as f:  # opens in append-mode => starts at EOF
            # if non-zero EOF, then file exists => use to get node-ID
            if f.tell() != 0:
                f.seek(0)  # go to start & read protobuf-encoded bytes
                protobuf_encoded = f.read()

                try:  # if decoded successfully, save & return
                    return Keypair.from_bytes(protobuf_encoded)
                except ValueError as e:  # on runtime error, assume corrupt file
                    logger.warning(f"Encountered error when trying to get keypair: {e}")

        # if no valid credentials, create new ones and persist
        with open(path, "w+b") as f:
            keypair = Keypair.generate()
            f.write(keypair.to_bytes())
            return keypair
