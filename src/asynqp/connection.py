import asyncio
import sys
from . import channel
from . import spec
from .util import Synchroniser
from .exceptions import AMQPError


class ConnectionInfo(object):
    def __init__(self, username, password, virtual_host):
        self.username = username
        self.password = password
        self.virtual_host = virtual_host


class Connection(object):
    """
    A Connection is a long-lasting mode of communication with a remote server.
    Each Connection occupies a single TCP connection, and may carry multiple Channels.
    A Connection communicates with a single virtual host on the server; virtual hosts are
    sandboxed and may not communicate with one another.

    Applications are advised to use one Connection for each AMQP peer it needs to communicate with;
    if you need to perform multiple concurrent tasks you should open multiple Channels.

    Attributes:
        connection.closed: a Future which is done when the handshake to close the connection has finished

    Methods:
        connection.open_channel: Open a new channel on this connection. This method is a coroutine.
        connection.close: Close the connection. This method is a coroutine.
    """
    def __init__(self, loop, protocol, synchroniser, sender, dispatcher, connection_info):
        self.loop = loop
        self.protocol = protocol
        self.synchroniser = synchroniser
        self.sender = sender
        self.dispatcher = dispatcher
        self.connection_info = connection_info

        self.closing = asyncio.Future(loop=loop)
        self.closing.add_done_callback(lambda fut: self.dispatcher.closing.set_result(fut.result()))  # bit hacky

        self.next_channel_num = 1

    @asyncio.coroutine
    def open_channel(self):
        """
        Open a new channel on this connection.
        This method is a coroutine.

        Return value:
            The new Channel object.
        """
        handler = channel.ChannelFrameHandler(self.protocol, self.next_channel_num, self.loop, self.connection_info)
        with (yield from handler.synchroniser.sync(spec.ChannelOpenOK)) as fut:
            self.dispatcher.add_handler(self.next_channel_num, handler)

            self.sender.send_ChannelOpen(self.next_channel_num)
            self.next_channel_num += 1

            yield from fut
            return handler.channel

    @asyncio.coroutine
    def close(self):
        """
        Close the connection by handshaking with the server.
        This method is a coroutine.
        """
        with (yield from self.synchroniser.sync(spec.ConnectionCloseOK)) as fut:
            self.closing.set_result(True)
            self.sender.send_Close(0, 'Connection closed by application', 0, 0)
            yield from fut


class ConnectionFrameHandler(object):
    def __init__(self, protocol, dispatcher, loop, connection_info):
        self.synchroniser = Synchroniser(loop)
        self.sender = ConnectionMethodSender(protocol)
        self.connection = Connection(loop, protocol, self.synchroniser, self.sender, dispatcher, connection_info)

        self.protocol = protocol
        self.connection_info = connection_info
        self.opened = asyncio.Future(loop=loop)

    def handle(self, frame):
        try:
            self.synchroniser.check_expected(frame)
        except AMQPError:
            self.sender.send_Close(spec.UNEXPECTED_FRAME, "got a bad message", *frame.payload.method_type)
            return

        method_type = type(frame.payload)
        method_name = method_type.__name__

        try:
            handler = getattr(self, 'handle_' + method_name)
        except AttributeError as e:
            raise AMQPError('No handler defined for {} on the connection'.format(method_name)) from e
        else:
            handler(frame)

    def handle_ConnectionStart(self, frame):
        self.sender.send_StartOK(
            {"product": "asynqp",
             "version": "0.1",  # todo: use pkg_resources to inspect the package
             "platform": sys.version},
            'AMQPLAIN',
            {'LOGIN': self.connection_info.username, 'PASSWORD': self.connection_info.password},
            'en_US'
        )

    def handle_ConnectionTune(self, frame):  # just agree with whatever the server wants. Make this configurable in future
        self.connection_info.frame_max = frame.payload.frame_max
        self.sender.send_TuneOK(frame.payload.channel_max, frame.payload.frame_max, frame.payload.heartbeat)
        self.sender.send_Open(self.connection_info.virtual_host)
        self.protocol.start_heartbeat(frame.payload.heartbeat)

    def handle_ConnectionOpenOK(self, frame):
        self.opened.set_result(True)

    def handle_ConnectionClose(self, frame):
        self.connection.closing.set_result(True)
        self.sender.send_CloseOK()
        self.protocol.transport.close()

    def handle_ConnectionCloseOK(self, frame):
        self.protocol.transport.close()
        self.synchroniser.succeed()


class ConnectionMethodSender(object):
    channel_id = 0

    def __init__(self, protocol):
        self.protocol = protocol

    def send_StartOK(self, client_properties, mechanism, response, locale):
        method = spec.ConnectionStartOK(client_properties, mechanism, response, locale)
        self.protocol.send_method(self.channel_id, method)

    def send_TuneOK(self, channel_max, frame_max, heartbeat):
        self.protocol.send_method(self.channel_id, spec.ConnectionTuneOK(channel_max, frame_max, heartbeat))

    def send_Open(self, virtual_host):
        self.protocol.send_method(self.channel_id, spec.ConnectionOpen(virtual_host, '', False))

    def send_Close(self, status_code, message, class_id, method_id):
        method = spec.ConnectionClose(status_code, message, class_id, method_id)
        self.protocol.send_method(self.channel_id, method)

    def send_CloseOK(self):
        self.protocol.send_method(self.channel_id, spec.ConnectionCloseOK())

    def send_ChannelOpen(self, channel_id):
        self.protocol.send_method(channel_id, spec.ChannelOpen(''))