"""Advertise the service on the LAN as `<hostname>.local`.

Two things get registered from one `ServiceInfo`:

* an **A record** for `<hostname>.local`, which is what makes
  `http://teams-interface.local:8787/` resolve from any machine on the LAN with
  mDNS (Windows 10+, macOS, Linux with avahi) -- no DNS server, no hosts file;
* a **`_http._tcp` service**, so the box also shows up in service browsers.

Optional: if `zeroconf` is missing the server still runs, it just is not
discoverable by name.
"""
from __future__ import annotations

import socket


def lan_ip() -> str:
    """The address other machines should reach us on.

    Asking the routing table which interface would be used to reach the
    internet beats `gethostbyname(gethostname())`, which on Windows often
    answers with a virtual adapter (WSL, Hyper-V, VPN).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))     # no packets are sent for UDP connect
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Advertiser:
    """Keeps the mDNS registration alive for the life of the server."""

    def __init__(self, hostname: str, port: int, ip: str = None):
        self.hostname = hostname.rstrip(".").removesuffix(".local")
        self.port = int(port)
        self.ip = ip or lan_ip()
        self.zc = None
        self.info = None
        self.error = None

    @property
    def fqdn(self) -> str:
        return "%s.local" % self.hostname

    def start(self) -> bool:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError as e:
            self.error = ("zeroconf is not installed (%s); serving on the IP "
                          "only. Fix: pip install zeroconf" % e)
            return False
        try:
            self.zc = Zeroconf()
            self.info = ServiceInfo(
                "_http._tcp.local.",
                "%s._http._tcp.local." % self.hostname,
                addresses=[socket.inet_aton(self.ip)],
                port=self.port,
                properties={"path": "/", "app": "teams-interface"},
                # This is the bit that publishes <hostname>.local as a name.
                server="%s." % self.fqdn,
            )
            self.zc.register_service(self.info, allow_name_change=True)
            return True
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e)
            self.stop()
            return False

    def stop(self):
        try:
            if self.zc is not None and self.info is not None:
                self.zc.unregister_service(self.info)
        except Exception:
            pass
        try:
            if self.zc is not None:
                self.zc.close()
        except Exception:
            pass
        self.zc = self.info = None

    def status(self) -> dict:
        return {"advertised": self.zc is not None, "hostname": self.fqdn,
                "ip": self.ip, "port": self.port, "error": self.error}
