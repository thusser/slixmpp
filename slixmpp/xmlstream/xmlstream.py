"""
    slixmpp.xmlstream.xmlstream
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    This module provides the module for creating and
    interacting with generic XML streams, along with
    the necessary eventing infrastructure.

    Part of Slixmpp: The Slick XMPP Library

    :copyright: (c) 2011 Nathanael C. Fritz
    :license: MIT, see LICENSE for more details
"""

from __future__ import with_statement, unicode_literals

import asyncio
import functools
import base64
import copy
import logging
import signal
import socket as Socket
import ssl
import sys
import threading
import time
import random
import weakref
import uuid
import errno

from xml.parsers.expat import ExpatError
import xml.etree.ElementTree

import slixmpp
from slixmpp.util import Queue, QueueEmpty, safedict
from slixmpp.thirdparty.statemachine import StateMachine
from slixmpp.xmlstream import tostring, cert
from slixmpp.xmlstream.stanzabase import StanzaBase, ET, ElementBase
from slixmpp.xmlstream.handler import Waiter, XMLCallback
from slixmpp.xmlstream.matcher import MatchXMLMask
from slixmpp.xmlstream.resolver import resolve, default_resolver

#: The time in seconds to wait before timing out waiting for response stanzas.
RESPONSE_TIMEOUT = 30

#: The time in seconds to wait for events from the event queue, and also the
#: time between checks for the process stop signal.
WAIT_TIMEOUT = 1.0

#: The number of threads to use to handle XML stream events. This is not the
#: same as the number of custom event handling threads.
#: :data:`HANDLER_THREADS` must be at least 1. For Python implementations
#: with a GIL, this should be left at 1, but for implemetnations without
#: a GIL increasing this value can provide better performance.
HANDLER_THREADS = 1

#: The time in seconds to delay between attempts to resend data
#: after an SSL error.
SSL_RETRY_DELAY = 0.5

#: The maximum number of times to attempt resending data due to
#: an SSL error.
SSL_RETRY_MAX = 10

#: Maximum time to delay between connection attempts is one hour.
RECONNECT_MAX_DELAY = 600

#: Maximum number of attempts to connect to the server before quitting
#: and raising a 'connect_failed' event. Setting this to ``None`` will
#: allow infinite reconnection attempts, and using ``0`` will disable
#: reconnections. Defaults to ``None``.
RECONNECT_MAX_ATTEMPTS = None


log = logging.getLogger(__name__)


class RestartStream(Exception):
    """
    Exception to restart stream processing, including
    resending the stream header.
    """


class XMLStream(object):
    """
    An XML stream connection manager and event dispatcher.

    The XMLStream class abstracts away the issues of establishing a
    connection with a server and sending and receiving XML "stanzas".
    A stanza is a complete XML element that is a direct child of a root
    document element. Two streams are used, one for each communication
    direction, over the same socket. Once the connection is closed, both
    streams should be complete and valid XML documents.

    Three types of events are provided to manage the stream:
        :Stream: Triggered based on received stanzas, similar in concept
                 to events in a SAX XML parser.
        :Custom: Triggered manually.
        :Scheduled: Triggered based on time delays.

    Typically, stanzas are first processed by a stream event handler which
    will then trigger custom events to continue further processing,
    especially since custom event handlers may run in individual threads.

    :param socket: Use an existing socket for the stream. Defaults to
                   ``None`` to generate a new socket.
    :param string host: The name of the target server.
    :param int port: The port to use for the connection. Defaults to 0.
    """

    def __init__(self, socket=None, host='', port=0):
        # The asyncio.Transport object provided by the connection_made()
        # callback when we are connected
        self.transport = None

        # The socket the is used internally by the transport object
        self.socket = None

        self.parser = None
        self.xml_depth = 0
        self.xml_root = None

        self.force_starttls = None
        self.disable_starttls = None

        # A dict of {name: handle}
        self.scheduled_events = {}

        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE
        #: Most XMPP servers support TLSv1, but OpenFire in particular
        #: does not work well with it. For OpenFire, set
        #: :attr:`ssl_version` to use ``SSLv23``::
        #:
        #:     import ssl
        #:     xmpp.ssl_version = ssl.PROTOCOL_SSLv23
        self.ssl_version = ssl.PROTOCOL_TLSv1

        #: The list of accepted ciphers, in OpenSSL Format.
        #: It might be useful to override it for improved security
        #: over the python defaults.
        self.ciphers = None

        #: Path to a file containing certificates for verifying the
        #: server SSL certificate. A non-``None`` value will trigger
        #: certificate checking.
        #:
        #: .. note::
        #:
        #:     On Mac OS X, certificates in the system keyring will
        #:     be consulted, even if they are not in the provided file.
        self.ca_certs = None

        #: Path to a file containing a client certificate to use for
        #: authenticating via SASL EXTERNAL. If set, there must also
        #: be a corresponding `:attr:keyfile` value.
        self.certfile = None

        #: Path to a file containing the private key for the selected
        #: client certificate to use for authenticating via SASL EXTERNAL.
        self.keyfile = None

        self._der_cert = None

        #: The time in seconds to wait for events from the event queue,
        #: and also the time between checks for the process stop signal.
        self.wait_timeout = WAIT_TIMEOUT

        #: The time in seconds to wait before timing out waiting
        #: for response stanzas.
        self.response_timeout = RESPONSE_TIMEOUT

        #: The current amount to time to delay attempting to reconnect.
        #: This value doubles (with some jitter) with each failed
        #: connection attempt up to :attr:`reconnect_max_delay` seconds.
        self.reconnect_delay = None

        #: Maximum time to delay between connection attempts is one hour.
        self.reconnect_max_delay = RECONNECT_MAX_DELAY

        #: Maximum number of attempts to connect to the server before
        #: quitting and raising a 'connect_failed' event. Setting to
        #: ``None`` allows infinite reattempts, while setting it to ``0``
        #: will disable reconnection attempts. Defaults to ``None``.
        self.reconnect_max_attempts = RECONNECT_MAX_ATTEMPTS

        #: The time in seconds to delay between attempts to resend data
        #: after an SSL error.
        self.ssl_retry_max = SSL_RETRY_MAX

        #: The maximum number of times to attempt resending data due to
        #: an SSL error.
        self.ssl_retry_delay = SSL_RETRY_DELAY

        #: The connection state machine tracks if the stream is
        #: ``'connected'`` or ``'disconnected'``.
        self.state = StateMachine(('disconnected', 'connected'))
        self.state._set_state('disconnected')

        #: The default port to return when querying DNS records.
        self.default_port = int(port)

        #: The domain to try when querying DNS records.
        self.default_domain = ''

        #: The expected name of the server, for validation.
        self._expected_server_name = ''
        self._service_name = ''

        #: The desired, or actual, address of the connected server.
        self.address = (host, int(port))

        #: Enable connecting to the server directly over SSL, in
        #: particular when the service provides two ports: one for
        #: non-SSL traffic and another for SSL traffic.
        self.use_ssl = False

        #: If set to ``True``, attempt to connect through an HTTP
        #: proxy based on the settings in :attr:`proxy_config`.
        self.use_proxy = False

        #: If set to ``True``, attempt to use IPv6.
        self.use_ipv6 = True

        #: If set to ``True``, allow using the ``dnspython`` DNS library
        #: if available. If set to ``False``, the builtin DNS resolver
        #: will be used, even if ``dnspython`` is installed.
        self.use_dnspython = True

        #: Use CDATA for escaping instead of XML entities. Defaults
        #: to ``False``.
        self.use_cdata = False

        #: An optional dictionary of proxy settings. It may provide:
        #: :host: The host offering proxy services.
        #: :port: The port for the proxy service.
        #: :username: Optional username for accessing the proxy.
        #: :password: Optional password for accessing the proxy.
        self.proxy_config = {}

        #: The default namespace of the stream content, not of the
        #: stream wrapper itself.
        self.default_ns = ''

        self.default_lang = None
        self.peer_default_lang = None

        #: The namespace of the enveloping stream element.
        self.stream_ns = ''

        #: The default opening tag for the stream element.
        self.stream_header = "<stream>"

        #: The default closing tag for the stream element.
        self.stream_footer = "</stream>"

        #: If ``True``, periodically send a whitespace character over the
        #: wire to keep the connection alive. Mainly useful for connections
        #: traversing NAT.
        self.whitespace_keepalive = True

        #: The default interval between keepalive signals when
        #: :attr:`whitespace_keepalive` is enabled.
        self.whitespace_keepalive_interval = 300

        #: An :class:`~threading.Event` to signal that the application
        #: is stopping, and that all threads should shutdown.
        self.stop = threading.Event()

        #: An :class:`~threading.Event` to signal receiving a closing
        #: stream tag from the server.
        self.stream_end_event = threading.Event()
        self.stream_end_event.set()

        #: An :class:`~threading.Event` to signal the start of a stream
        #: session. Until this event fires, the send queue is not used
        #: and data is sent immediately over the wire.
        self.session_started_event = threading.Event()

        #: The default time in seconds to wait for a session to start
        #: after connecting before reconnecting and trying again.
        self.session_timeout = 45

        #: Flag for controlling if the session can be considered ended
        #: if the connection is terminated.
        self.end_session_on_disconnect = True

        #: A queue of string data to be sent over the stream.
        self.send_queue = Queue()
        self.send_queue_lock = threading.Lock()
        self.send_lock = threading.RLock()

        self.__failed_send_stanza = None

        #: A mapping of XML namespaces to well-known prefixes.
        self.namespace_map = {StanzaBase.xml_ns: 'xml'}

        self.__thread = {}
        self.__root_stanza = []
        self.__handlers = []
        self.__event_handlers = {}
        self.__filters = {'in': [], 'out': [], 'out_sync': []}
        self.__thread_count = 0
        self.__thread_cond = threading.Condition()
        self.__active_threads = set()
        self._use_daemons = False
        self._disconnect_wait_for_threads = True

        self._id = 0
        self._id_lock = threading.Lock()

        #: We use an ID prefix to ensure that all ID values are unique.
        self._id_prefix = '%s-' % uuid.uuid4()

        #: The :attr:`auto_reconnnect` setting controls whether or not
        #: the stream will be restarted in the event of an error.
        self.auto_reconnect = True

        #: The :attr:`disconnect_wait` setting is the default value
        #: for controlling if the system waits for the send queue to
        #: empty before ending the stream. This may be overridden by
        #: passing ``wait=True`` or ``wait=False`` to :meth:`disconnect`.
        #: The default :attr:`disconnect_wait` value is ``False``.
        self.disconnect_wait = False

        #: A list of DNS results that have not yet been tried.
        self.dns_answers = []

        #: The service name to check with DNS SRV records. For
        #: example, setting this to ``'xmpp-client'`` would query the
        #: ``_xmpp-client._tcp`` service.
        self.dns_service = None

        self.add_event_handler('connected', self._session_timeout_check)
        self.add_event_handler('disconnected', self._remove_schedules)
        self.add_event_handler('session_start', self._start_keepalive)
        self.add_event_handler('session_start', self._cert_expiration)

    def use_signals(self, signals=None):
        """Register signal handlers for ``SIGHUP`` and ``SIGTERM``.

        By using signals, a ``'killed'`` event will be raised when the
        application is terminated.

        If a signal handler already existed, it will be executed first,
        before the ``'killed'`` event is raised.

        :param list signals: A list of signal names to be monitored.
                             Defaults to ``['SIGHUP', 'SIGTERM']``.
        """
        if signals is None:
            signals = ['SIGHUP', 'SIGTERM']

        existing_handlers = {}
        for sig_name in signals:
            if hasattr(signal, sig_name):
                sig = getattr(signal, sig_name)
                handler = signal.getsignal(sig)
                if handler:
                    existing_handlers[sig] = handler

        def handle_kill(signum, frame):
            """
            Capture kill event and disconnect cleanly after first
            spawning the ``'killed'`` event.
            """

            if signum in existing_handlers and \
                   existing_handlers[signum] != handle_kill:
                existing_handlers[signum](signum, frame)

            self.event("killed", direct=True)
            self.disconnect()

        try:
            for sig_name in signals:
                if hasattr(signal, sig_name):
                    sig = getattr(signal, sig_name)
                    signal.signal(sig, handle_kill)
            self.__signals_installed = True
        except:
            log.debug("Can not set interrupt signal handlers. " + \
                      "Slixmpp is not running from a main thread.")

    def new_id(self):
        """Generate and return a new stream ID in hexadecimal form.

        Many stanzas, handlers, or matchers may require unique
        ID values. Using this method ensures that all new ID values
        are unique in this stream.
        """
        with self._id_lock:
            self._id += 1
            return self.get_id()

    def get_id(self):
        """Return the current unique stream ID in hexadecimal form."""
        return "%s%X" % (self._id_prefix, self._id)

    def connect(self, host='', port=0, use_ssl=False,
                force_starttls=True, disable_starttls=False):
        """Create a new socket and connect to the server.

        :param host: The name of the desired server for the connection.
        :param port: Port to connect to on the server.
        :param use_ssl: Flag indicating if SSL should be used by connecting
                        directly to a port using SSL.  If it is False, the
                        connection will be upgraded to SSL/TLS later, using
                        STARTTLS.  Only use this value for old servers that
                        have specific port for SSL/TLS
        TODO fix the comment
        :param force_starttls: If True, the connection will be aborted if
                               the server does not initiate a STARTTLS
                               negociation.  If None, the connection will be
                               upgraded to TLS only if the server initiate
                               the STARTTLS negociation, otherwise it will
                               connect in clear.  If False it will never
                               upgrade to TLS, even if the server provides
                               it.  Use this for example if you’re on
                               localhost

        """
        self.stop.clear()

        if host and port:
            self.address = (host, int(port))
        try:
            Socket.inet_aton(self.address[0])
        except (Socket.error, ssl.SSLError):
            self.default_domain = self.address[0]

        # Respect previous TLS usage.
        if use_ssl is not None:
            self.use_ssl = use_ssl
        if force_starttls is not None:
            self.force_starttls = force_starttls
        if disable_starttls is not None:
            self.disable_starttls = disable_starttls

        loop = asyncio.get_event_loop()
        connect_routine = loop.create_connection(lambda: self,
                                                 self.address[0],
                                                 self.address[1],
                                                 ssl=self.use_ssl)
        asyncio.async(connect_routine)

    def init_parser(self):
        self.xml_depth = 0
        self.xml_root = None
        self.parser = xml.etree.ElementTree.XMLPullParser(("start", "end"))

    def connection_made(self, transport):
        self.transport = transport
        self.socket = self.transport.get_extra_info("socket")
        self.init_parser()
        self.send_raw(self.stream_header)

    def data_received(self, data):
        self.parser.feed(data)
        for event, xml in self.parser.read_events():
            if event == 'start':
                if self.xml_depth == 0:
                    # We have received the start of the root element.
                    self.xml_root = xml
                    log.debug('RECV: %s', tostring(self.xml_root, xmlns=self.default_ns,
                                                         stream=self,
                                                         top_level=True,
                                                         open_only=True))
                    # Perform any stream initialization actions, such
                    # as handshakes.
                    self.stream_end_event.clear()
                    self.start_stream_handler(self.xml_root)

                    # We have a successful stream connection, so reset
                    # exponential backoff for new reconnect attempts.
                    self.reconnect_delay = 1.0
                self.xml_depth += 1
            if event == 'end':
                self.xml_depth -= 1
                if self.xml_depth == 0:
                    # The stream's root element has closed,
                    # terminating the stream.
                    log.debug("End of stream recieved")
                    self.stream_end_event.set()
                    return False
                elif self.xml_depth == 1:
                    # We only raise events for stanzas that are direct
                    # children of the root element.
                    try:
                        self.__spawn_event(xml)
                    except RestartStream:
                        return True
                    if self.xml_root is not None:
                        # Keep the root element empty of children to
                        # save on memory use.
                        self.xml_root.clear()

    def connection_lost(self):
        self.parser = None
        self.transport = None
        self.socket = None

    def _session_timeout_check(self, event=None):
        """
        Add check to ensure that a session is established within
        a reasonable amount of time.
        """

        def _handle_session_timeout():
            if not self.session_started_event.is_set():
                log.debug("Session start has taken more " + \
                          "than %d seconds", self.session_timeout)
                self.disconnect(reconnect=self.auto_reconnect)

        self.schedule("Session timeout check",
                self.session_timeout,
                _handle_session_timeout)

    def disconnect(self, reconnect=False, wait=None, send_close=True):
        """Terminate processing and close the XML streams.

        Optionally, the connection may be reconnected and
        resume processing afterwards.

        If the disconnect should take place after all items
        in the send queue have been sent, use ``wait=True``.

        .. warning::

            If you are constantly adding items to the queue
            such that it is never empty, then the disconnect will
            not occur and the call will continue to block.

        :param reconnect: Flag indicating if the connection
                          and processing should be restarted.
                          Defaults to ``False``.
        :param wait: Flag indicating if the send queue should
                     be emptied before disconnecting, overriding
                     :attr:`disconnect_wait`.
        :param send_close: Flag indicating if the stream footer
                           should be sent before terminating the
                           connection. Setting this to ``False``
                           prevents error loops when trying to
                           disconnect after a socket error.
        """
        # TODO
        pass

    def abort(self):
        self.session_started_event.clear()
        self.set_stop()
        if self._disconnect_wait_for_threads:
            self._wait_for_threads()
        try:
            self.socket.shutdown(Socket.SHUT_RDWR)
            self.socket.close()
        except Socket.error:
            pass
        self.state.transition_any(['connected', 'disconnected'], 'disconnected', func=lambda: True)
        self.event("killed", direct=True)

    def reconnect(self, reattempt=True, wait=False, send_close=True):
        """Reset the stream's state and reconnect to the server."""
        log.debug("reconnecting...")
        self.connect()

    def configure_socket(self):
        """Set timeout and other options for self.socket.

        Meant to be overridden.
        """
        pass

    def configure_dns(self, resolver, domain=None, port=None):
        """
        Configure and set options for a :class:`~dns.resolver.Resolver`
        instance, and other DNS related tasks. For example, you
        can also check :meth:`~socket.socket.getaddrinfo` to see
        if you need to call out to ``libresolv.so.2`` to
        run ``res_init()``.

        Meant to be overridden.

        :param resolver: A :class:`~dns.resolver.Resolver` instance
                         or ``None`` if ``dnspython`` is not installed.
        :param domain: The initial domain under consideration.
        :param port: The initial port under consideration.
        """
        pass

    def start_tls(self):
        """Perform handshakes for TLS.

        If the handshake is successful, the XML stream will need
        to be restarted.
        """
        loop = asyncio.get_event_loop()
        ssl_connect_routine = loop.create_connection(lambda: self, ssl=self.ssl_context,
                                                     sock=self.socket,
                                                     server_hostname=self.address[0])
        asyncio.async(ssl_connect_routine)

    def _cert_expiration(self, event):
        """Schedule an event for when the TLS certificate expires."""

        if not self._der_cert:
            log.warn("TLS or SSL was enabled, but no certificate was found.")
            return

        def restart():
            if not self.event_handled('ssl_expired_cert'):
                log.warn("The server certificate has expired. Restarting.")
                self.reconnect()
            else:
                pem_cert = ssl.DER_cert_to_PEM_cert(self._der_cert)
                self.event('ssl_expired_cert', pem_cert)

        cert_ttl = cert.get_ttl(self._der_cert)
        if cert_ttl is None:
            return

        if cert_ttl.days < 0:
            log.warn('CERT: Certificate has expired.')
            restart()

        try:
            total_seconds = cert_ttl.total_seconds()
        except AttributeError:
            # for Python < 2.7
            total_seconds = (cert_ttl.microseconds + (cert_ttl.seconds + cert_ttl.days * 24 * 3600) * 10**6) / 10**6

        log.info('CERT: Time until certificate expiration: %s' % cert_ttl)
        self.schedule('Certificate Expiration',
                      total_seconds,
                      restart)

    def _start_keepalive(self, event):
        """Begin sending whitespace periodically to keep the connection alive.

        May be disabled by setting::

            self.whitespace_keepalive = False

        The keepalive interval can be set using::

            self.whitespace_keepalive_interval = 300
        """
        self.schedule('Whitespace Keepalive',
                      self.whitespace_keepalive_interval,
                      self.send_raw,
                      args=(' ',),
                      repeat=True)

    def _remove_schedules(self, event):
        """Remove whitespace keepalive and certificate expiration schedules."""
        self.cancel_schedule('Whitespace Keepalive')
        self.cancel_schedule('Certificate Expiration')

    def start_stream_handler(self, xml):
        """Perform any initialization actions, such as handshakes,
        once the stream header has been sent.

        Meant to be overridden.
        """
        pass

    def register_stanza(self, stanza_class):
        """Add a stanza object class as a known root stanza.

        A root stanza is one that appears as a direct child of the stream's
        root element.

        Stanzas that appear as substanzas of a root stanza do not need to
        be registered here. That is done using register_stanza_plugin() from
        slixmpp.xmlstream.stanzabase.

        Stanzas that are not registered will not be converted into
        stanza objects, but may still be processed using handlers and
        matchers.

        :param stanza_class: The top-level stanza object's class.
        """
        self.__root_stanza.append(stanza_class)

    def remove_stanza(self, stanza_class):
        """Remove a stanza from being a known root stanza.

        A root stanza is one that appears as a direct child of the stream's
        root element.

        Stanzas that are not registered will not be converted into
        stanza objects, but may still be processed using handlers and
        matchers.
        """
        self.__root_stanza.remove(stanza_class)

    def add_filter(self, mode, handler, order=None):
        """Add a filter for incoming or outgoing stanzas.

        These filters are applied before incoming stanzas are
        passed to any handlers, and before outgoing stanzas
        are put in the send queue.

        Each filter must accept a single stanza, and return
        either a stanza or ``None``. If the filter returns
        ``None``, then the stanza will be dropped from being
        processed for events or from being sent.

        :param mode: One of ``'in'`` or ``'out'``.
        :param handler: The filter function.
        :param int order: The position to insert the filter in
                          the list of active filters.
        """
        if order:
            self.__filters[mode].insert(order, handler)
        else:
            self.__filters[mode].append(handler)

    def del_filter(self, mode, handler):
        """Remove an incoming or outgoing filter."""
        self.__filters[mode].remove(handler)

    def add_handler(self, mask, pointer, name=None, disposable=False,
                    threaded=False, filter=False, instream=False):
        """A shortcut method for registering a handler using XML masks.

        The use of :meth:`register_handler()` is preferred.

        :param mask: An XML snippet matching the structure of the
                     stanzas that will be passed to this handler.
        :param pointer: The handler function itself.
        :parm name: A unique name for the handler. A name will
                    be generated if one is not provided.
        :param disposable: Indicates if the handler should be discarded
                           after one use.
        :param threaded: **DEPRECATED**.
                       Remains for backwards compatibility.
        :param filter: **DEPRECATED**.
                       Remains for backwards compatibility.
        :param instream: Indicates if the handler should execute during
                         stream processing and not during normal event
                         processing.
        """
        # To prevent circular dependencies, we must load the matcher
        # and handler classes here.

        if name is None:
            name = 'add_handler_%s' % self.new_id()
        self.register_handler(
                XMLCallback(name,
                    MatchXMLMask(mask, self.default_ns),
                    pointer,
                    once=disposable,
                    instream=instream))

    def register_handler(self, handler, before=None, after=None):
        """Add a stream event handler that will be executed when a matching
        stanza is received.

        :param handler:
                The :class:`~slixmpp.xmlstream.handler.base.BaseHandler`
                derived object to execute.
        """
        if handler.stream is None:
            self.__handlers.append(handler)
            handler.stream = weakref.ref(self)

    def remove_handler(self, name):
        """Remove any stream event handlers with the given name.

        :param name: The name of the handler.
        """
        idx = 0
        for handler in self.__handlers:
            if handler.name == name:
                self.__handlers.pop(idx)
                return True
            idx += 1
        return False

    def get_dns_records(self, domain, port=None):
        """Get the DNS records for a domain.

        :param domain: The domain in question.
        :param port: If the results don't include a port, use this one.
        """
        if port is None:
            port = self.default_port

        resolver = default_resolver()
        self.configure_dns(resolver, domain=domain, port=port)

        return resolve(domain, port, service=self.dns_service,
                                     resolver=resolver,
                                     use_ipv6=self.use_ipv6,
                                     use_dnspython=self.use_dnspython)

    def pick_dns_answer(self, domain, port=None):
        """Pick a server and port from DNS answers.

        Gets DNS answers if none available.
        Removes used answer from available answers.

        :param domain: The domain in question.
        :param port: If the results don't include a port, use this one.
        """
        if not self.dns_answers:
            self.dns_answers = self.get_dns_records(domain, port)

        if sys.version_info < (3, 0):
            return self.dns_answers.next()
        else:
            return next(self.dns_answers)

    def add_event_handler(self, name, pointer,
                          threaded=False, disposable=False):
        """Add a custom event handler that will be executed whenever
        its event is manually triggered.

        :param name: The name of the event that will trigger
                     this handler.
        :param pointer: The function to execute.
        :param threaded: If set to ``True``, the handler will execute
                         in its own thread. Defaults to ``False``.
        :param disposable: If set to ``True``, the handler will be
                           discarded after one use. Defaults to ``False``.
        """
        if not name in self.__event_handlers:
            self.__event_handlers[name] = []
        self.__event_handlers[name].append((pointer, threaded, disposable))

    def del_event_handler(self, name, pointer):
        """Remove a function as a handler for an event.

        :param name: The name of the event.
        :param pointer: The function to remove as a handler.
        """
        if not name in self.__event_handlers:
            return

        # Need to keep handlers that do not use
        # the given function pointer
        def filter_pointers(handler):
            return handler[0] != pointer

        self.__event_handlers[name] = list(filter(
            filter_pointers,
            self.__event_handlers[name]))

    def event_handled(self, name):
        """Returns the number of registered handlers for an event.

        :param name: The name of the event to check.
        """
        return len(self.__event_handlers.get(name, []))

    def event(self, name, data={}, direct=False):
        """Manually trigger a custom event.

        :param name: The name of the event to trigger.
        :param data: Data that will be passed to each event handler.
                     Defaults to an empty dictionary, but is usually
                     a stanza object.
        :param direct: Runs the event directly if True, skipping the
                       event queue. All event handlers will run in the
                       same thread.
        """
        log.debug("Event triggered: " + name)

        handlers = self.__event_handlers.get(name, [])
        for handler in handlers:
            #TODO:  Data should not be copied, but should be read only,
            #       but this might break current code so it's left for future.

            out_data = copy.copy(data) if len(handlers) > 1 else data
            old_exception = getattr(data, 'exception', None)
            if direct:
                try:
                    handler[0](out_data)
                except Exception as e:
                    error_msg = 'Error processing event handler: %s'
                    log.exception(error_msg,  str(handler[0]))
                    if old_exception:
                        old_exception(e)
                    else:
                        self.exception(e)
            else:
                self.run_event(('event', handler, out_data))
            if handler[2]:
                # If the handler is disposable, we will go ahead and
                # remove it now instead of waiting for it to be
                # processed in the queue.
                with self.__event_handlers_lock:
                    try:
                        h_index = self.__event_handlers[name].index(handler)
                        self.__event_handlers[name].pop(h_index)
                    except:
                        pass

    def schedule(self, name, seconds, callback, args=tuple(),
                 kwargs={}, repeat=False):
        """Schedule a callback function to execute after a given delay.

        :param name: A unique name for the scheduled callback.
        :param seconds: The time in seconds to wait before executing.
        :param callback: A pointer to the function to execute.
        :param args: A tuple of arguments to pass to the function.
        :param kwargs: A dictionary of keyword arguments to pass to
                       the function.
        :param repeat: Flag indicating if the scheduled event should
                       be reset and repeat after executing.
        """
        loop = asyncio.get_event_loop()
        cb = functools.partial(callback, *args, **kwargs)
        if repeat:
            handle = loop.call_later(seconds, self._execute_and_reschedule,
                                     name, cb, seconds)
        else:
            handle = loop.call_later(seconds, self._execute_and_unschedule,
                                     name, cb)

        # Save that handle, so we can just cancel this scheduled event by
        # canceling scheduled_events[name]
        self.scheduled_events[name] = handle

    def cancel_schedule(self, name):
        try:
            handle = self.scheduled_events.pop(name)
            handle.cancel()
        except KeyError:
            log.debug("Tried to cancel unscheduled event: %s" % (name,))

    def _execute_and_reschedule(self, name, cb, seconds):
        """Simple method that calls the given callback, and then schedule itself to
        be called after the given number of seconds.
        """
        cb()
        loop = asyncio.get_event_loop()
        handle = loop.call_later(seconds, self._execute_and_reschedule,
                                 name, cb, seconds)
        self.scheduled_events[name] = handle

    def _execute_and_unschedule(self, name, cb):
        """
        Execute the callback and remove the handler for it.
        """
        cb()
        del self.scheduled_events[name]

    def incoming_filter(self, xml):
        """Filter incoming XML objects before they are processed.

        Possible uses include remapping namespaces, or correcting elements
        from sources with incorrect behavior.

        Meant to be overridden.
        """
        return xml

    def send(self, data, mask=None, timeout=None, now=False, use_filters=True):
        """A wrapper for :meth:`send_raw()` for sending stanza objects.

        May optionally block until an expected response is received.

        :param data: The :class:`~slixmpp.xmlstream.stanzabase.ElementBase`
                     stanza to send on the stream.
        :param mask: **DEPRECATED**
                     An XML string snippet matching the structure
                     of the expected response. Execution will block
                     in this thread until the response is received
                     or a timeout occurs.
        :param int timeout: Time in seconds to wait for a response before
                       continuing. Defaults to :attr:`response_timeout`.
        :param bool now: Indicates if the send queue should be skipped,
                        sending the stanza immediately. Useful mainly
                        for stream initialization stanzas.
                        Defaults to ``False``.
        :param bool use_filters: Indicates if outgoing filters should be
                                 applied to the given stanza data. Disabling
                                 filters is useful when resending stanzas.
                                 Defaults to ``True``.
        """
        if timeout is None:
            timeout = self.response_timeout
        if hasattr(mask, 'xml'):
            mask = mask.xml

        if isinstance(data, ElementBase):
            if use_filters:
                for filter in self.__filters['out']:
                    data = filter(data)
                    if data is None:
                        return

        if mask is not None:
            log.warning("Use of send mask waiters is deprecated.")
            wait_for = Waiter("SendWait_%s" % self.new_id(),
                              MatchXMLMask(mask))
            self.register_handler(wait_for)

        if isinstance(data, ElementBase):
            with self.send_queue_lock:
                if use_filters:
                    for filter in self.__filters['out_sync']:
                        data = filter(data)
                        if data is None:
                            return
                str_data = tostring(data.xml, xmlns=self.default_ns,
                                              stream=self,
                                              top_level=True)
                self.send_raw(str_data)
        else:
            self.send_raw(data)
        if mask is not None:
            return wait_for.wait(timeout)

    def send_xml(self, data, mask=None, timeout=None, now=False):
        """Send an XML object on the stream, and optionally wait
        for a response.

        :param data: The :class:`~xml.etree.ElementTree.Element` XML object
                     to send on the stream.
        :param mask: **DEPRECATED**
                     An XML string snippet matching the structure
                     of the expected response. Execution will block
                     in this thread until the response is received
                     or a timeout occurs.
        :param int timeout: Time in seconds to wait for a response before
                       continuing. Defaults to :attr:`response_timeout`.
        :param bool now: Indicates if the send queue should be skipped,
                        sending the stanza immediately. Useful mainly
                        for stream initialization stanzas.
                        Defaults to ``False``.
        """
        if timeout is None:
            timeout = self.response_timeout
        return self.send(tostring(data), mask, timeout, now)

    def send_raw(self, data):
        """Send raw data across the stream.

        :param string data: Any bytes or utf-8 string value.
        """
        if isinstance(data, str):
            data = data.encode('utf-8')
        if not self.transport:
            logger.error("Cannot send data, we are not connected.")
        else:
            self.transport.write(data)

    def _start_thread(self, name, target, track=True):
        self.__thread[name] = threading.Thread(name=name, target=target)
        self.__thread[name].daemon = self._use_daemons
        self.__thread[name].start()

        if track:
            self.__active_threads.add(name)
            with self.__thread_cond:
                self.__thread_count += 1

    def _end_thread(self, name, early=False):
        with self.__thread_cond:
            curr_thread = threading.current_thread().name
            if curr_thread in self.__active_threads:
                self.__thread_count -= 1
                self.__active_threads.remove(curr_thread)

                if early:
                    log.debug('Threading deadlock prevention!')
                    log.debug(("Marked %s thread as ended due to " + \
                               "disconnect() call. %s threads remain.") % (
                                   name, self.__thread_count))
                else:
                    log.debug("Stopped %s thread. %s threads remain." % (
                        name, self.__thread_count))

            else:
                log.debug(("Finished exiting %s thread after early " + \
                           "termination from disconnect() call. " + \
                           "%s threads remain.") % (
                               name, self.__thread_count))

            if self.__thread_count == 0:
                self.__thread_cond.notify()

    def set_stop(self):
        self.stop.set()

    def _build_stanza(self, xml, default_ns=None):
        """Create a stanza object from a given XML object.

        If a specialized stanza type is not found for the XML, then
        a generic :class:`~slixmpp.xmlstream.stanzabase.StanzaBase`
        stanza will be returned.

        :param xml: The :class:`~xml.etree.ElementTree.Element` XML object
                    to convert into a stanza object.
        :param default_ns: Optional default namespace to use instead of the
                           stream's current default namespace.
        """
        if default_ns is None:
            default_ns = self.default_ns
        stanza_type = StanzaBase
        for stanza_class in self.__root_stanza:
            if xml.tag == "{%s}%s" % (default_ns, stanza_class.name) or \
               xml.tag == stanza_class.tag_name():
                stanza_type = stanza_class
                break
        stanza = stanza_type(self, xml)
        if stanza['lang'] is None and self.peer_default_lang:
            stanza['lang'] = self.peer_default_lang
        return stanza

    def __spawn_event(self, xml):
        """
        Analyze incoming XML stanzas and convert them into stanza
        objects if applicable and queue stream events to be processed
        by matching handlers.

        :param xml: The :class:`~slixmpp.xmlstream.stanzabase.ElementBase`
                    stanza to analyze.
        """
        # Apply any preprocessing filters.
        xml = self.incoming_filter(xml)

        # Convert the raw XML object into a stanza object. If no registered
        # stanza type applies, a generic StanzaBase stanza will be used.
        stanza = self._build_stanza(xml)
        for filter in self.__filters['in']:
            if stanza is not None:
                stanza = filter(stanza)
        if stanza is None:
            return

        log.debug("RECV: %s", stanza)

        # Match the stanza against registered handlers. Handlers marked
        # to run "in stream" will be executed immediately; the rest will
        # be queued.
        unhandled = True
        matched_handlers = [h for h in self.__handlers if h.match(stanza)]
        for handler in matched_handlers:
            if len(matched_handlers) > 1:
                stanza_copy = copy.copy(stanza)
            else:
                stanza_copy = stanza
            handler.prerun(stanza_copy)
            self.run_event(('stanza', handler, stanza_copy))
            try:
                if handler.check_delete():
                    self.__handlers.remove(handler)
            except:
                pass  # not thread safe
            unhandled = False

        # Some stanzas require responses, such as Iq queries. A default
        # handler will be executed immediately for this case.
        if unhandled:
            stanza.unhandled()

    def _threaded_event_wrapper(self, func, args):
        """Capture exceptions for event handlers that run
        in individual threads.

        :param func: The event handler to execute.
        :param args: Arguments to the event handler.
        """
        # this is always already copied before this is invoked
        orig = args[0]
        try:
            func(*args)
        except Exception as e:
            error_msg = 'Error processing event handler: %s'
            log.exception(error_msg, str(func))
            if hasattr(orig, 'exception'):
                orig.exception(e)
            else:
                self.exception(e)

    def run_event(self, event):
        etype, handler = event[0:2]
        args = event[2:]
        orig = copy.copy(args[0])

        if etype == 'stanza':
            try:
                handler.run(args[0])
            except Exception as e:
                error_msg = 'Error processing stream handler: %s'
                log.exception(error_msg, handler.name)
                orig.exception(e)
        elif etype == 'schedule':
            name = args[2]
            try:
                log.debug('Scheduled event: %s: %s', name, args[0])
                handler(*args[0], **args[1])
            except Exception as e:
                log.exception('Error processing scheduled task')
                self.exception(e)
        elif etype == 'event':
            func, threaded, disposable = handler
            try:
                if threaded:
                    x = threading.Thread(
                            name="Event_%s" % str(func),
                            target=self._threaded_event_wrapper,
                            args=(func, args))
                    x.daemon = self._use_daemons
                    x.start()
                else:
                    func(*args)
            except Exception as e:
                error_msg = 'Error processing event handler: %s'
                log.exception(error_msg, str(func))
                if hasattr(orig, 'exception'):
                    orig.exception(e)
                else:
                    self.exception(e)

    def _send_thread(self):
        """Extract stanzas from the send queue and send them on the stream."""
        try:
            while not self.stop.is_set():
                while not self.stop.is_set() and \
                      not self.session_started_event.is_set():
                    self.session_started_event.wait(timeout=0.1)                            # Wait for session start
                if self.__failed_send_stanza is not None:
                    data = self.__failed_send_stanza
                    self.__failed_send_stanza = None
                else:
                    data = self.send_queue.get()                                            # Wait for data to send
                    if data is None:
                        continue
                log.debug("SEND: %s", data)
                enc_data = data.encode('utf-8')
                total = len(enc_data)
                sent = 0
                count = 0
                tries = 0
                try:
                    with self.send_lock:
                        while sent < total and not self.stop.is_set() and \
                              self.session_started_event.is_set():
                            try:
                                sent += self.socket.send(enc_data[sent:])
                                count += 1
                            except Socket.error as serr:
                                if serr.errno != errno.EINTR:
                                    raise
                            except ssl.SSLError as serr:
                                if tries >= self.ssl_retry_max:
                                    log.debug('SSL error: max retries reached')
                                    self.exception(serr)
                                    log.warning("Failed to send %s", data)
                                    if not self.stop.is_set():
                                        self.disconnect(self.auto_reconnect,
                                                        send_close=False)
                                    log.warning('SSL write error: retrying')
                                if not self.stop.is_set():
                                    time.sleep(self.ssl_retry_delay)
                                tries += 1
                    if count > 1:
                        log.debug('SENT: %d chunks', count)
                    self.send_queue.task_done()
                except (Socket.error, ssl.SSLError) as serr:
                    self.event('socket_error', serr, direct=True)
                    log.warning("Failed to send %s", data)
                    if not self.stop.is_set():
                        self.__failed_send_stanza = data
                        self._end_thread('send')
                        self.disconnect(self.auto_reconnect, send_close=False)
                        return
        except Exception as ex:
            log.exception('Unexpected error in send thread: %s', ex)
            self.exception(ex)
            if not self.stop.is_set():
                self._end_thread('send')
                self.disconnect(self.auto_reconnect)
                return

        self._end_thread('send')

    def exception(self, exception):
        """Process an unknown exception.

        Meant to be overridden.

        :param exception: An unhandled exception object.
        """
        pass
