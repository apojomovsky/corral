"""Socket-family helpers shared by the loopback servers.

One copy of the bind-to-family rule, used by the dashboard API and the
hub alike. Standard library only.
"""

import ipaddress
import socket


def server_family(bind: str) -> socket.AddressFamily:
    """The socket family for a bind: IPv6 for IPv6 literals, else IPv4.

    Names stay on IPv4: they resolve at bind time, and the default family
    keeps today's behavior for them.
    """
    try:
        address = ipaddress.ip_address(str(bind or ""))
    except ValueError:
        return socket.AF_INET
    return socket.AF_INET6 if address.version == 6 else socket.AF_INET
