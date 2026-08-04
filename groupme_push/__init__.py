"""A client for the GroupMe push service (Faye over websockets)."""

from groupme_push.client import PushClient
from groupme_push.exceptions import (
    AuthenticationError,
    ConnectionTimeout,
    GroupMePushError,
    HandshakeError,
)

__version__ = "0.0.5"

__all__ = [
    "PushClient",
    "GroupMePushError",
    "AuthenticationError",
    "HandshakeError",
    "ConnectionTimeout",
    "__version__",
]
