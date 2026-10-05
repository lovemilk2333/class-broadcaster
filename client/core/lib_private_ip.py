import re
import os
import sys
import ipaddress
import subprocess
from typing import List, Optional, Sequence

IPv4_REGEX_COMMON = r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"
IPv4_REGEX_WINDOWS = re.compile(
    r"^\s*IPv4 Address[^:]*:\s*(" + IPv4_REGEX_COMMON + r")"
    r"(?:\s+\(Preferred\))?\s*$",
    re.IGNORECASE,
)
SUBNET_MASK_REGEX_WINDOWS = re.compile(
    r"^\s*Subnet Mask[^:]*:\s*(" + IPv4_REGEX_COMMON + r")\s*$",
    re.IGNORECASE,
)
IPv4_REGEX_LINUX = re.compile(r"\binet\s+(" + IPv4_REGEX_COMMON + r")/(\d{1,2})\b")


def get_all_private_ranges(
    loopback: bool = False,
    dhcp_fallback: bool = False,  # link-local per RFC 3927 / `169.254/16`
    broadcast: bool = False,
    blocks: Optional[Sequence[ipaddress.IPv4Network]] = None,
) -> Optional[List[ipaddress.IPv4Interface]]:
    """Return private interface addresses with their original host and mask.

    Entries are :class:`ipaddress.IPv4Interface` values. For example, a
    Windows pair of ``10.0.192.101`` and ``255.255.0.0`` is returned as
    ``10.0.192.101/16``: ``.ip`` keeps the host address while ``.network``
    exposes the corresponding masked network.
    """

    ips = []  # type: List[ipaddress.IPv4Interface]
    # https://docs.python.org/3/library/sys.html#sys.platform
    platform = sys.platform

    if platform == "win32":
        env = os.environ.copy()
        env["lang"] = "en-US"
        command = "chcp 437 >nul && ipconfig"
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="cp437",
            shell=True,
            env=env,
        )
        output = result.stdout

        # ``ipconfig`` prints the interface address and mask on adjacent
        # lines. Pair them so gateways and DNS addresses are not mistaken for
        # interface addresses, and retain the host part in IPv4Interface.
        patterns = []  # type: List[ipaddress.IPv4Interface]
        current_ip = None  # type: Optional[str]
        for line in output.splitlines():
            address_match = IPv4_REGEX_WINDOWS.match(line)
            if address_match:
                current_ip = address_match.group(1)
                continue
            mask_match = SUBNET_MASK_REGEX_WINDOWS.match(line)
            if mask_match is not None and current_ip is not None:
                try:
                    mask = ipaddress.IPv4Network(
                        "0.0.0.0/{}".format(mask_match.group(1)),
                        strict=False,
                    )
                    patterns.append(
                        ipaddress.ip_interface(
                            "{}/{}".format(current_ip, mask.prefixlen)  # type: ignore
                        )
                    )
                except ValueError:
                    pass
                current_ip = None
    elif platform == "linux":
        result = subprocess.run(
            ["ip", "-4", "addr"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        output = result.stdout

        patterns = [
            ipaddress.ip_interface("{}/{}".format(address, prefix))  # type: ignore
            for address, prefix in IPv4_REGEX_LINUX.findall(output)
        ]
    else:
        raise RuntimeError("unsupported platform `" + platform + "`")

    for ip_str in patterns:
        try:
            ipv4 = (
                ip_str
                if isinstance(ip_str, ipaddress.IPv4Interface)
                else ipaddress.ip_interface(ip_str)
            )
            if (
                ipv4.version != 4
                or not ipv4.ip.is_private
                or (not broadcast and ipv4.ip == ipv4.network.broadcast_address)
                or (not loopback and ipv4.ip.is_loopback)
                or (not dhcp_fallback and ipv4.ip.is_link_local)
                or (blocks is not None and any(ipv4.ip in block for block in blocks))
            ):
                continue
        except ValueError:
            continue

        ips.append(ipv4)

    return ips or None
