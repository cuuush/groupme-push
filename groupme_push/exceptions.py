class GroupMePushError(Exception):
    """Base class for every error raised by groupme-push."""


class AuthenticationError(GroupMePushError):
    """The access token was rejected by GroupMe."""


class HandshakeError(GroupMePushError):
    """The Faye handshake did not return a usable client id."""


class ConnectionTimeout(GroupMePushError):
    """The websocket did not finish connecting in time."""
