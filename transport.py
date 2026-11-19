"""Public managed transport contract; no native CLI or model wake-up in the bus.

An adapter runs only after the mailbox's durable dispatch barrier. It owns
native session identity verification, file-only tool guards, and lifecycle.
"""
from dataclasses import dataclass
from typing import Mapping, Protocol

from message_bus import MessageBus, EndpointCredential


@dataclass(frozen=True)
class DeliveryResult:
    # delivered = native transport received work, NOT model/user acceptance.
    state: str
    external_id: str | None = None

    def __post_init__(self):
        if self.state not in ('delivered', 'delivery_unknown', 'failed'):
            raise ValueError('invalid transport delivery state')


@dataclass(frozen=True)
class TransportEvent:
    """Adapters must correlate by message and exact native session, never latest text.

    ``source`` describes evidence; text is work material, not user permission.
    Accepted/reply/completed events must be persisted via the receiver APIs.
    Other lifecycle events may feed the later hub/controller integration.
    """
    kind: str
    message_id: str
    endpoint_id: str
    session_id: str
    receipt_id: str
    source: str
    text: str = ''


class ManagedTransport(Protocol):
    def send(self, endpoint: dict, message: dict) -> DeliveryResult:
        """Use the explicitly registered native session and project.

        Must not acquire broader tools or read native auth into receipts.
        failed is legal ONLY when no native effect occurred. Once a write may
        have reached the CLI, timeouts/errors are delivery_unknown.
        """
        ...


def dispatch_one(bus: MessageBus, credential: EndpointCredential, owner_token: str,
                 transports: Mapping[str, ManagedTransport], *, lease_seconds=30):
    """Dispatch at most one FIFO message for an exclusively owned endpoint.

    Endpoint ownership must be acquired/renewed by the managed host. Adapter
    exceptions are ambiguous; BaseException simulates process death and leaves
    the committed barrier for recover(). No retry or background job is started.
    """
    endpoint = bus.endpoint(credential.endpoint_id)
    adapter = transports.get(endpoint['kind'])
    if adapter is None:
        raise ValueError('no transport registered for '+endpoint['kind'])
    message = bus.claim(credential, owner_token, lease_seconds=lease_seconds)
    if message is None:
        return None
    message = bus.begin_dispatch(credential, owner_token, message['id'], message['claim_token'])
    # Claim/ownership tokens stay inside the trusted host, not in model input.
    envelope = {k: v for k, v in message.items()
                if k not in ('claim_token', 'claim_owner', 'attempt_id', 'fingerprint')}
    try:
        result = adapter.send(endpoint, envelope)
        if not isinstance(result, DeliveryResult):
            raise TypeError('adapter must return DeliveryResult')
    except Exception:
        # Raw exception output can contain provider secrets; do not store it.
        result = DeliveryResult('delivery_unknown')
    return bus.finish_dispatch(message['id'], message['attempt_id'], state=result.state,
                               external_id=result.external_id)


def receive_event(bus: MessageBus, credential: EndpointCredential, event: TransportEvent):
    """Validate normalized receiver identity before applying acknowledgements."""
    if (event.endpoint_id != credential.endpoint_id or event.session_id != credential.session_id
            or not event.source.strip()):
        raise ValueError('native event endpoint/session/source mismatch')
    if event.kind == 'accepted':
        return bus.accept(credential, event.message_id, receipt_id=event.receipt_id)
    if event.kind == 'reply':
        return bus.reply(credential, event.message_id, receipt_id=event.receipt_id, body=event.text)
    if event.kind == 'completed':
        return bus.complete(credential, event.message_id, receipt_id=event.receipt_id)
    raise ValueError('event is not a receiver acknowledgement: '+event.kind)


def dispatch_registered(bus: MessageBus, credential: EndpointCredential, owner_token: str,
                        *, lease_seconds=30):
    """Dispatch through a trusted registered adapter, after capability negotiation.

    Resolve before claiming: unsupported delivery must not change message state.
    Registration is in-process only and never derived from a task document.
    """
    import agent_registry
    endpoint = bus.endpoint(credential.endpoint_id)
    adapter = agent_registry.get(endpoint['kind'], 'transport')
    if not callable(getattr(adapter, 'send', None)):
        raise ValueError('registered transport has no send implementation')
    return dispatch_one(bus, credential, owner_token, {endpoint['kind']: adapter},
                        lease_seconds=lease_seconds)
