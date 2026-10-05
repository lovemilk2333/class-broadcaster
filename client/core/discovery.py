"""UDP discovery for locating and verifying MKCB servers."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import random
import socket
import struct
import subprocess
import sys
import time
from typing import Iterable, List, Optional, Tuple

from pydantic import BaseModel

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .protocol.bson import BSONError, decode, encode
from .protocol.packet import Packet, PacketError

DISCOVERY_REQ = 0x0001
DISCOVERY_RESP = 0x0002
SIGNATURE_DOMAIN = b"MKCB-DISCOVERY-V1\x00"
DEFAULT_DISCOVERY_PORT = 39001
LIMITED_BROADCAST = "255.255.255.255"


class LocalInterface(BaseModel):
    """One usable IPv4 interface for multi-homed discovery sends."""

    name: str = ""
    address: str
    network: str
    broadcast: str

    class Config:
        frozen = True


def _parse_ip_addr_output(text: str) -> list[LocalInterface]:
    """Parse `ip -4 -o addr show` lines into LocalInterface rows."""
    results: list[LocalInterface] = []
    seen: set[str] = set()
    for line in text.splitlines():
        # 2: enp0s31f6    inet 10.0.0.100/16 brd 10.0.255.255 scope global ...
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        name = parts[1].rstrip(":")
        cidr = parts[3]
        try:
            iface = ipaddress.IPv4Interface(cidr)
        except ValueError:
            continue
        if iface.ip.is_loopback or iface.ip.is_link_local:
            continue
        broadcast = str(iface.network.broadcast_address)
        if "brd" in parts:
            try:
                broadcast = str(ipaddress.IPv4Address(parts[parts.index("brd") + 1]))
            except (ValueError, IndexError):
                pass
        key = str(iface.ip)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            LocalInterface(
                name=name,
                address=str(iface.ip),
                network=str(iface.network),
                broadcast=broadcast,
            )
        )
    return results


def _interfaces_via_ioctl() -> list[LocalInterface]:
    """Linux fallback using SIOCGIFCONF / SIOCGIFNETMASK / SIOCGIFBRDADDR."""
    try:
        import fcntl  # Linux / *BSD
    except ImportError:
        return []
    results: list[LocalInterface] = []
    seen: set[str] = set()
    try:
        names = [name for name, _ in socket.if_nameindex()]
    except (OSError, AttributeError):
        return []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            try:
                ifreq = struct.pack("256s", name[:15].encode("ascii", "ignore"))
                flags = struct.unpack(
                    "H",
                    fcntl.ioctl(sock.fileno(), 0x8913, ifreq)[16:18],  # SIOCGIFFLAGS
                )[0]
                # IFF_UP | IFF_RUNNING roughly; skip down interfaces.
                if not (flags & 0x1):
                    continue
                addr_bin = fcntl.ioctl(sock.fileno(), 0x8915, ifreq)[20:24]  # SIOCGIFADDR
                mask_bin = fcntl.ioctl(sock.fileno(), 0x891B, ifreq)[20:24]  # SIOCGIFNETMASK
                address = socket.inet_ntoa(addr_bin)
                netmask = socket.inet_ntoa(mask_bin)
                if address.startswith("127.") or address.startswith("169.254."):
                    continue
                iface = ipaddress.IPv4Interface("{}/{}".format(address, netmask))
                try:
                    brd_bin = fcntl.ioctl(sock.fileno(), 0x8919, ifreq)[20:24]  # SIOCGIFBRDADDR
                    broadcast = socket.inet_ntoa(brd_bin)
                except OSError:
                    broadcast = str(iface.network.broadcast_address)
                if address in seen:
                    continue
                seen.add(address)
                results.append(
                    LocalInterface(
                        name=name,
                        address=address,
                        network=str(iface.network),
                        broadcast=broadcast,
                    )
                )
            except OSError:
                continue
    finally:
        sock.close()
    return results


def _interfaces_via_lib_private_ip() -> list[LocalInterface]:
    """Prefer the shared Windows/Linux private-IP helper (ipconfig / ip addr)."""
    try:
        from .lib_private_ip import get_all_private_ranges
    except Exception:
        return []
    try:
        ranges = get_all_private_ranges(loopback=False, dhcp_fallback=False, broadcast=False)
    except Exception:
        return []
    if not ranges:
        return []
    results: list[LocalInterface] = []
    seen: set[str] = set()
    for iface in ranges:
        try:
            address = str(iface.ip)
            if address in seen:
                continue
            seen.add(address)
            results.append(
                LocalInterface(
                    name="",
                    address=address,
                    network=str(iface.network),
                    broadcast=str(iface.network.broadcast_address),
                )
            )
        except Exception:
            continue
    return results


def list_local_ipv4_interfaces() -> list[LocalInterface]:
    """Enumerate non-loopback IPv4 interfaces with real netmasks/broadcasts.

    Critical for multi-homed hosts: a bare 255.255.255.255 send only leaves the
    default-route NIC. Each NIC must send its own directed broadcast.

    Windows has no ``ip`` command; use ``lib_private_ip`` (ipconfig) first.
    """
    results: list[LocalInterface] = []
    # Cross-platform helper: Windows ipconfig, Linux `ip -4 addr`.
    results = _interfaces_via_lib_private_ip()
    if results:
        return results
    # Linux-only refinements when the helper is unavailable.
    if sys.platform.startswith("linux"):
        try:
            completed = subprocess.run(
                ["ip", "-4", "-o", "addr", "show"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                results = _parse_ip_addr_output(completed.stdout)
        except (OSError, subprocess.SubprocessError):
            results = []
        if not results:
            results = _interfaces_via_ioctl()
        if results:
            return results
    # Last-resort heuristics (often wrong netmask on multi-NIC hosts).
    seen: set[str] = set()
    fallback: list[LocalInterface] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_DGRAM):
            text = info[4][0]
            if text.startswith("127.") or text in seen:
                continue
            try:
                address = ipaddress.IPv4Address(text)
            except ValueError:
                continue
            network = ipaddress.IPv4Network("{}/24".format(address), strict=False)
            seen.add(text)
            fallback.append(
                LocalInterface(
                    name="",
                    address=str(address),
                    network=str(network),
                    broadcast=str(network.broadcast_address),
                )
            )
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            text = probe.getsockname()[0]
            if not text.startswith("127.") and text not in seen:
                address = ipaddress.IPv4Address(text)
                network = ipaddress.IPv4Network("{}/24".format(address), strict=False)
                fallback.append(
                    LocalInterface(
                        name="",
                        address=str(address),
                        network=str(network),
                        broadcast=str(network.broadcast_address),
                    )
                )
    except OSError:
        pass
    return fallback


def parse_scan_destinations(value: str, port: int = DEFAULT_DISCOVERY_PORT) -> tuple[list[tuple[str, int]], bool]:
    """Expand IPv4 / CIDR / limited-broadcast inputs into discovery targets.

    Returns (destinations, wants_broadcast).
    - ``255.255.255.255`` / empty / ``auto`` → limited + per-NIC directed broadcasts
    - CIDR → directed broadcast of that net + host unicast (capped)
    - single host → unicast only
    """
    destinations: list[tuple[str, int]] = []
    seen: set[str] = set()
    wants_broadcast = False
    raw = (value or "").strip()
    if not raw or raw.lower() in {"auto", "*", "broadcast"}:
        raw = LIMITED_BROADCAST

    def add(host: str) -> None:
        if host not in seen:
            seen.add(host)
            destinations.append((host, port))

    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        if item == LIMITED_BROADCAST or item.lower() in {"auto", "*", "broadcast"}:
            wants_broadcast = True
            add(LIMITED_BROADCAST)
            for iface in list_local_ipv4_interfaces():
                add(iface.broadcast)
                # Also probe the interface address's network gateway-ish .1/.2? No —
                # directed broadcast is enough when bound per-NIC in discover().
            continue
        if "/" in item:
            network = ipaddress.ip_network(item, strict=False)
            if network.version != 4 or network.num_addresses > 65536:
                raise ValueError("IPv4 scan range must contain at most 65536 addresses")
            wants_broadcast = True
            add(str(network.broadcast_address))
            for host in network.hosts():
                add(str(host))
            continue
        address = ipaddress.ip_address(item)
        if address.version != 4:
            raise ValueError("scan address must be IPv4")
        if address.is_multicast:
            raise ValueError("multicast scan addresses are not supported")
        if int(address) & 0xFF == 0xFF and not address.is_loopback:
            wants_broadcast = True
        add(str(address))

    if not destinations:
        raise ValueError("scan address is empty")
    return destinations, wants_broadcast


class DiscoveryResponse(BaseModel):
    service_name: str
    server_public_key: bytes
    fingerprint: bytes
    tcp_port: int
    http_port: int
    protocol_major: int
    protocol_minor: int
    nonce: bytes
    address: Tuple[str, int]

    class Config:
        frozen = True


def verify_response(document: dict, expected_nonce: bytes) -> DiscoveryResponse:
    try:
        public_key = bytes(document["server_public_key"])
        fingerprint = bytes(document["fingerprint"])
        nonce = bytes(document["nonce"])
        signature = bytes(document["signature"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("malformed discovery response") from exc
    if len(public_key) != 32 or len(fingerprint) != 32 or len(signature) != 64:
        raise ValueError("invalid discovery identity fields")
    if nonce != expected_nonce or not 8 <= len(nonce) <= 64:
        raise ValueError("discovery nonce mismatch")
    key = Ed25519PublicKey.from_public_bytes(public_key)
    spki = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if hashlib.sha256(spki).digest() != fingerprint:
        raise ValueError("discovery fingerprint does not match public key")
    unsigned = {
        "service_name": document.get("service_name", ""),
        "server_public_key": public_key,
        "fingerprint": fingerprint,
        "tcp_port": document.get("tcp_port", 0),
        "http_port": document.get("http_port", 0),
        "protocol_major": document.get("protocol_major", 0),
        "protocol_minor": document.get("protocol_minor", 0),
        "nonce": nonce,
        "signature": None,
    }
    try:
        key.verify(signature, SIGNATURE_DOMAIN + encode(unsigned))
    except Exception as exc:
        raise ValueError("invalid discovery signature") from exc
    return DiscoveryResponse(
        service_name=str(unsigned["service_name"]),
        server_public_key=public_key,
        fingerprint=fingerprint,
        tcp_port=int(unsigned["tcp_port"]),
        http_port=int(unsigned["http_port"]),
        protocol_major=int(unsigned["protocol_major"]),
        protocol_minor=int(unsigned["protocol_minor"]),
        nonce=nonce,
        address=("", 0),
    )


def _send_probe(sock: socket.socket, wire: bytes, destination: tuple[str, int]) -> None:
    try:
        sock.sendto(wire, destination)
    except OSError:
        pass


def _collect_responses(
    socks: Iterable[socket.socket],
    nonce: bytes,
    deadline: float,
    seen: set[bytes],
    results: list[DiscoveryResponse],
) -> None:
    sockets = list(socks)
    while time.monotonic() < deadline and sockets:
        remaining = max(0.0, deadline - time.monotonic())
        timeout = min(0.25, remaining) if remaining else 0.0
        if timeout <= 0:
            break
        for sock in list(sockets):
            sock.settimeout(timeout)
            try:
                data, address = sock.recvfrom(1 << 20)
            except socket.timeout:
                continue
            except OSError:
                sockets.remove(sock)
                continue
            try:
                packet = Packet.decode(data)
                if packet.packet_type != DISCOVERY_RESP:
                    continue
                response = verify_response(decode(packet.payload), nonce)
            except (PacketError, BSONError, ValueError):
                continue
            if response.fingerprint in seen:
                continue
            seen.add(response.fingerprint)
            results.append(response.model_copy(update={"address": address}))


def discover(
    destinations: list[tuple[str, int]], timeout: float = 1.5, broadcast: bool = False
) -> list[DiscoveryResponse]:
    """Send UDP DISCOVERY_REQ probes (unicast and/or per-NIC broadcast), never ICMP.

    On multi-homed hosts, limited broadcast (255.255.255.255) only egresses the
    default-route interface. Directed broadcasts are therefore sent from a socket
    bound to each local interface address so every L2 segment is covered.
    """
    nonce = os.urandom(16)
    sequence = random.randrange(0, 65536)
    request = encode({"nonce": nonce, "protocol_major": 1, "protocol_minor": 0})
    wire = Packet(1, 0, DISCOVERY_REQ, sequence, 0, request).encode()
    results: list[DiscoveryResponse] = []
    seen: set[bytes] = set()
    interfaces = list_local_ipv4_interfaces() if broadcast else []
    open_socks: list[socket.socket] = []

    def open_udp(bind_host: Optional[str] = None, enable_broadcast: bool = False) -> Optional[socket.socket]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if enable_broadcast:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if bind_host:
                sock.bind((bind_host, 0))
            open_socks.append(sock)
            return sock
        except OSError:
            return None

    try:
        if broadcast and interfaces:
            # One bound socket per NIC: send limited + that NIC's directed broadcast.
            for iface in interfaces:
                sock = open_udp(iface.address, enable_broadcast=True)
                if sock is None:
                    continue
                _send_probe(sock, wire, (LIMITED_BROADCAST, DEFAULT_DISCOVERY_PORT))
                _send_probe(sock, wire, (iface.broadcast, DEFAULT_DISCOVERY_PORT))
                # Unicast destinations that fall inside this NIC's network.
                try:
                    network = ipaddress.IPv4Network(iface.network, strict=False)
                except ValueError:
                    network = None
                if network is not None:
                    for host, port in destinations:
                        if host in {LIMITED_BROADCAST, iface.broadcast}:
                            continue
                        try:
                            if ipaddress.IPv4Address(host) in network:
                                _send_probe(sock, wire, (host, port))
                        except ValueError:
                            continue
            # Unicast targets outside known NIC nets (still useful for explicit IPs).
            unbound = open_udp(None, enable_broadcast=True)
            if unbound is not None:
                covered = {LIMITED_BROADCAST}
                for iface in interfaces:
                    covered.add(iface.broadcast)
                for host, port in destinations:
                    if host in covered:
                        continue
                    _send_probe(unbound, wire, (host, port))
        else:
            sock = open_udp(None, enable_broadcast=broadcast)
            if sock is not None:
                if broadcast:
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    except OSError:
                        pass
                for destination in destinations:
                    _send_probe(sock, wire, destination)

        # Multi-NIC scans need a bit more time for slower segments.
        effective_timeout = timeout
        if broadcast and len(interfaces) > 1:
            effective_timeout = max(timeout, min(4.0, 1.0 + 0.5 * len(interfaces)))
        deadline = time.monotonic() + effective_timeout
        _collect_responses(open_socks, nonce, deadline, seen, results)
    finally:
        for sock in open_socks:
            try:
                sock.close()
            except OSError:
                pass
    return results
