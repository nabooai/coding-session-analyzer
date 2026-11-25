"""Outbound calls to an address an admin typed (an AI Models endpoint): https only, and only to a public
address. The address is resolved once, checked, and then connected to by IP, so a DNS answer that
changes between the check and the connection cannot reach an internal host."""
import http.client
import ipaddress
import socket
import urllib.parse
import urllib.request


_TRANSLATION = [ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("2002::/16")]   # NAT64 and 6to4 embed an IPv4


def resolve(host, port):
    """Every address `host` resolves to, as strings."""
    return sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def public_address(host, port=443):
    """The first public address of `host`; ValueError with a sentence when it has none or any is internal."""
    try:
        addresses = resolve(host, port)
    except OSError:
        raise ValueError(f"The endpoint {host} did not resolve.") from None
    for a in addresses:
        ip = ipaddress.ip_address(a)
        ip = ip.ipv4_mapped or ip if ip.version == 6 else ip
        if not ip.is_global or any(ip in net for net in _TRANSLATION):
            where = "is" if host == a else f"points at {a}, which is"
            raise ValueError(f"The endpoint {host} {where} not a public address. CSA only calls public https "
                             f"endpoints from here. A private gateway goes through ANTHROPIC_BASE_URL in the "
                             f"csa-llm Secret.")
    if not addresses:
        raise ValueError(f"The endpoint {host} did not resolve.")
    return addresses[0]


def check_url(url):
    """Raise ValueError unless `url` is https, names a host, carries no credentials, and resolves publicly."""
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        raise ValueError("The endpoint is a full https:// address without a user name or password.")
    public_address(u.hostname, u.port or 443)


class _Connection(http.client.HTTPSConnection):
    def connect(self):
        ip = public_address(self.host, self.port)
        sock = socket.create_connection((ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _Handler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_Connection, req)


def opener():
    """urllib's opener without proxies, plain http or redirects: a model API never redirects, and
    following one would send the provider's key to wherever it points."""
    director = urllib.request.OpenerDirector()
    for h in (_Handler(), urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
        director.add_handler(h)
    return director
