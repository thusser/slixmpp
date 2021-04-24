
# Slixmpp: The Slick XMPP Library
# Copyright (C) 2011  Nathanael C. Fritz
# This file is part of Slixmpp.
# See the file LICENSE for copying permission.
from slixmpp.xmlstream import StanzaBase, ElementBase


class STARTTLS(StanzaBase):

    """
    """

    name = 'starttls'
    namespace = 'urn:ietf:params:xml:ns:xmpp-tls'
    interfaces = {'required'}
    plugin_attrib = name

    def get_required(self):
        """
        """
        return True


class Proceed(StanzaBase):

    """
    """

    name = 'proceed'
    namespace = 'urn:ietf:params:xml:ns:xmpp-tls'
    interfaces = set()


class Failure(StanzaBase):

    """
    """

    name = 'failure'
    namespace = 'urn:ietf:params:xml:ns:xmpp-tls'
    interfaces = set()
