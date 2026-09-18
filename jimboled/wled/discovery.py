"""Find WLED controllers on the local network.

Four strategies are combined and de-duplicated by MAC address:

1. ``avahi-browse`` (present on Raspberry Pi OS) for ``_wled._tcp`` and
   ``_http._tcp`` services whose name starts with ``wled-``.
2. A dependency-free mDNS query (multicast PTR lookup + response parsing).
3. Asking every already-configured controller for the nodes it knows
   (``/json/nodes``).
4. A fast HTTP sweep of the local /24 (``/json/info`` with short timeouts).

Every hit is confirmed by fetching ``/json/info`` so the UI can show name,
version, LED count and whether the device is already configured.
"""
from __future__ import annotations

import ipaddress
import itertools
import logging
import random
import shutil
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set

from .client import WLEDClient, WLEDError

log = logging.getLogger(__name__)

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
SERVICES = ("_wled._tcp.local", "_http._tcp.local")


# --------------------------------------------------------------------- utils
def local_ipv4_addresses() -> List[str]:
    """Best-effort list of this machine's LAN IPv4 addresses."""
    addrs: Set[str] = set()
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                cidr = parts[parts.index("inet") + 1]
                ip = cidr.split("/")[0]
                if not ip.startswith("127."):
                    addrs.add(cidr)
    except (OSError, subprocess.SubprocessError):
        pass
    if not addrs:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))  # no packet is sent for UDP connect
            addrs.add(s.getsockname()[0] + "/24")
            s.close()
        except OSError:
            pass
    return sorted(addrs)


def local_subnets() -> List[str]:
    nets = []
    for cidr in local_ipv4_addresses():
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if net.prefixlen < 22:
            # Huge subnets are impractical to sweep; fall back to the host's /24.
            ip = cidr.split("/")[0]
            net = ipaddress.ip_network(ip + "/24", strict=False)
        nets.append(str(net))
    return nets


# --------------------------------------------------------------- mDNS parse
def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.strip(".").split("."):
        out += struct.pack("B", len(label)) + label.encode("ascii", "ignore")
    return out + b"\x00"


def _read_name(data: bytes, offset: int) -> tuple:
    labels = []
    jumped = False
    end = offset
    guard = 0
    while True:
        guard += 1
        if guard > 128 or offset >= len(data):
            raise ValueError("bad name")
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                end = offset
            break
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack("!H", data[offset:offset + 2])[0] & 0x3FFF
            if not jumped:
                end = offset + 2
            offset = pointer
            jumped = True
            continue
        offset += 1
        labels.append(data[offset:offset + length].decode("utf-8", "ignore"))
        offset += length
    return ".".join(labels), end


def _parse_mdns_response(data: bytes) -> List[Dict[str, Any]]:
    """Return a list of records: {name, type, data}."""
    records = []
    try:
        _id, flags, qd, an, ns, ar = struct.unpack("!6H", data[:12])
        offset = 12
        for _ in range(qd):
            _name, offset = _read_name(data, offset)
            offset += 4
        for _ in range(an + ns + ar):
            name, offset = _read_name(data, offset)
            rtype, rclass, ttl, rdlen = struct.unpack("!HHIH", data[offset:offset + 10])
            offset += 10
            rdata = data[offset:offset + rdlen]
            rec: Dict[str, Any] = {"name": name, "type": rtype}
            if rtype == 12:  # PTR
                rec["data"], _ = _read_name(data, offset)
            elif rtype == 33:  # SRV
                prio, weight, port = struct.unpack("!HHH", rdata[:6])
                target, _ = _read_name(data, offset + 6)
                rec["data"] = {"port": port, "target": target}
            elif rtype == 1:  # A
                rec["data"] = socket.inet_ntoa(rdata[:4])
            elif rtype == 16:  # TXT
                txt = {}
                pos = 0
                while pos < len(rdata):
                    ln = rdata[pos]
                    item = rdata[pos + 1:pos + 1 + ln].decode("utf-8", "ignore")
                    pos += 1 + ln
                    if "=" in item:
                        k, v = item.split("=", 1)
                        txt[k] = v
                rec["data"] = txt
            else:
                rec["data"] = None
            offset += rdlen
            records.append(rec)
    except (struct.error, ValueError, IndexError):
        pass
    return records


def mdns_query(services=SERVICES, timeout: float = 2.5) -> List[Dict[str, Any]]:
    """Dependency-free mDNS browse.  Returns [{name, host, port, ip, txt}]."""
    query = struct.pack("!6H", random.randint(1, 65535), 0, len(services), 0, 0, 0)
    for svc in services:
        query += _encode_name(svc) + struct.pack("!HH", 12, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.settimeout(0.3)
    found: Dict[str, Dict[str, Any]] = {}
    try:
        sock.bind(("", 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.sendto(query, (MDNS_ADDR, MDNS_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            recs = _parse_mdns_response(data)
            ptrs = [r for r in recs if r["type"] == 12 and r["name"] in services]
            srvs = {r["name"]: r["data"] for r in recs if r["type"] == 33}
            a_recs = {r["name"]: r["data"] for r in recs if r["type"] == 1}
            txts = {r["name"]: r["data"] for r in recs if r["type"] == 16}
            for ptr in ptrs:
                instance = ptr["data"]
                short = instance.split(".")[0].lower()
                if ptr["name"].startswith("_http") and not short.startswith("wled"):
                    continue
                srv = srvs.get(instance) or {}
                target = srv.get("target", "")
                ip = a_recs.get(target) or addr[0]
                found[instance] = {"name": instance.split(".")[0], "host": target, "port": srv.get("port", 80),
                                   "ip": ip, "txt": txts.get(instance, {})}
    except OSError as exc:
        log.debug("mDNS query failed: %s", exc)
    finally:
        sock.close()
    return list(found.values())


def avahi_browse(timeout: float = 4.0) -> List[Dict[str, Any]]:
    """Use avahi-browse when available (robust on Raspberry Pi OS)."""
    if not shutil.which("avahi-browse"):
        return []
    results: Dict[str, Dict[str, Any]] = {}
    for svc in ("_wled._tcp", "_http._tcp"):
        try:
            proc = subprocess.run(["avahi-browse", "-rptk", svc], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("avahi-browse failed: %s", exc)
            continue
        for line in proc.stdout.splitlines():
            if not line.startswith("="):
                continue
            parts = line.split(";")
            if len(parts) < 9:
                continue
            _, _iface, proto, name, _stype, _domain, host, ip, port = parts[:9]
            if proto != "IPv4":
                continue
            name = name.replace("\\032", " ").replace("\\.", ".")
            if svc == "_http._tcp" and not name.lower().startswith("wled"):
                continue
            results[ip] = {"name": name, "host": host, "port": int(port or 80), "ip": ip, "txt": {}}
    return list(results.values())


def zeroconf_browse(timeout: float = 3.0) -> List[Dict[str, Any]]:
    """Use python-zeroconf if it happens to be installed."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []
    found: Dict[str, Dict[str, Any]] = {}
    zc = Zeroconf()

    class _Listener:
        def add_service(self, zeroconf, stype, name):
            info = zeroconf.get_service_info(stype, name, timeout=1500)
            if not info:
                return
            short = name.split(".")[0]
            if stype.startswith("_http") and not short.lower().startswith("wled"):
                return
            ips = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
            ips = [ip for ip in ips if ":" not in ip]
            if ips:
                found[name] = {"name": short, "host": info.server, "port": info.port or 80, "ip": ips[0], "txt": {}}

        update_service = add_service

        def remove_service(self, *a):
            pass

    try:
        browsers = [ServiceBrowser(zc, s + ".", _Listener()) for s in SERVICES]
        time.sleep(timeout)
        for b in browsers:
            b.cancel()
    except Exception as exc:  # pragma: no cover
        log.debug("zeroconf browse failed: %s", exc)
    finally:
        zc.close()
    return list(found.values())


# ------------------------------------------------------------ UDP 65506
NODE_PORT = 65506
NODE_TYPES = {82: "ESP8266", 32: "ESP32", 33: "ESP32-S2", 34: "ESP32-S3", 35: "ESP32-C3", 37: "ESP32-C2",
              38: "ESP32-H2", 39: "ESP32-C6", 40: "ESP32-C61", 41: "ESP32-C5", 42: "ESP32-P4"}


def parse_node_packet(data: bytes, src_ip: str = "") -> Optional[Dict[str, Any]]:
    """Decode WLED's 44-byte "sysinfo" broadcast (sent every 30 s on UDP 65506)."""
    if len(data) < 40 or data[0] != 255 or data[1] != 1:
        return None
    ip = ".".join(str(b) for b in data[2:6])
    name = data[6:38].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    t = data[38]
    vid = struct.unpack("<I", data[40:44])[0] if len(data) >= 44 else 0
    return {"host": ip if ip != "0.0.0.0" else src_ip, "name": name or "WLED", "chip": NODE_TYPES.get(t & 0x7F, str(t & 0x7F)),
            "on": bool(t & 0x80), "vid": vid, "seen": time.time()}


class NodeListener:
    """Always-on passive listener: learns about every WLED on the LAN for free."""

    def __init__(self):
        self._lock = threading.Lock()
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="wled-nodes", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("", NODE_PORT))
        except OSError as exc:
            self.error = f"UDP {NODE_PORT} unavailable: {exc}"
            log.info("passive WLED node listener disabled (%s)", exc)
            return
        sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data, (src, _port) = sock.recvfrom(128)
            except socket.timeout:
                continue
            except OSError:
                break
            node = parse_node_packet(data, src)
            if node:
                with self._lock:
                    self.nodes[node["host"]] = node
        sock.close()

    def snapshot(self, max_age_s: float = 600.0) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [dict(n) for n in self.nodes.values() if now - n["seen"] <= max_age_s]


# ------------------------------------------------------------------ service
class DiscoveryService:
    def __init__(self, store, devices, *, passive: bool = True):
        self.store = store
        self.devices = devices
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._status: Dict[str, Any] = {"running": False, "started_at": None, "finished_at": None,
                                        "found": [], "errors": [], "progress": "", "methods": []}
        self.listener = NodeListener()
        if passive:
            self.listener.start()

    def passive_nodes(self) -> List[Dict[str, Any]]:
        """Controllers heard on UDP 65506 that are not configured yet."""
        known_hosts = {d.get("host") for d in (self.store.section("devices") or [])}
        return [n for n in self.listener.snapshot() if n["host"] not in known_hosts]

    @property
    def is_running(self) -> bool:
        with self._lock:
            return bool(self._status["running"])

    def status(self) -> Dict[str, Any]:
        with self._lock:
            snap = dict(self._status)
            snap["found"] = [dict(f) for f in self._status["found"]]
            snap["errors"] = list(self._status["errors"])
        snap["passive"] = self.passive_nodes()
        snap["passive_error"] = self.listener.error
        return self._annotate(snap)

    def start(self, methods: Optional[List[str]] = None, subnet: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if self._status["running"]:
                return self.status()
            methods = methods or ["mdns", "nodes", "subnet"]
            self._status = {"running": True, "started_at": time.time(), "finished_at": None,
                            "found": [], "errors": [], "progress": "starting", "methods": methods}
            self._thread = threading.Thread(target=self._run, args=(methods, subnet), name="wled-discovery", daemon=True)
            self._thread.start()
        return self.status()

    def _set_progress(self, text: str) -> None:
        with self._lock:
            self._status["progress"] = text

    def _add(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            for existing in self._status["found"]:
                if (existing.get("mac") and existing.get("mac") == entry.get("mac")) or existing.get("host") == entry.get("host"):
                    existing.update({k: v for k, v in entry.items() if v})
                    existing["sources"] = sorted(set(existing.get("sources", [])) | set(entry.get("sources", [])))
                    return
            self._status["found"].append(entry)

    def _run(self, methods: List[str], subnet: Optional[str]) -> None:
        candidates: Dict[str, Set[str]] = {}  # host -> sources

        def add_candidate(host: str, source: str) -> None:
            if host:
                candidates.setdefault(host, set()).add(source)

        try:
            if "mdns" in methods:
                self._set_progress("Listening for mDNS announcements")
                for hit in avahi_browse():
                    add_candidate(hit["ip"], "mdns")
                for hit in mdns_query():
                    add_candidate(hit["ip"], "mdns")
                if not any("mdns" in s for s in candidates.values()):
                    for hit in zeroconf_browse():
                        add_candidate(hit["ip"], "mdns")
            if "nodes" in methods:
                self._set_progress("Asking known controllers about their neighbours")
                for node in self.listener.snapshot():
                    add_candidate(node["host"], "broadcast")
                for did in self.devices.ids():
                    try:
                        for node in self.devices.client_for(did).get_nodes():
                            add_candidate(str(node.get("ip") or ""), "nodes")
                    except WLEDError:
                        continue
            # Confirm what we have so far before the slow sweep.
            self._set_progress("Checking discovered devices")
            self._confirm_many(candidates)
            if "subnet" in methods:
                nets = [subnet] if subnet else local_subnets()
                for net in nets:
                    self._set_progress(f"Scanning {net}")
                    self._sweep(net, set(candidates))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("discovery failed")
            with self._lock:
                self._status["errors"].append(str(exc))
        finally:
            with self._lock:
                self._status["running"] = False
                self._status["finished_at"] = time.time()
                self._status["progress"] = "done"

    def _confirm_many(self, candidates: Dict[str, Set[str]]) -> None:
        if not candidates:
            return
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(self.probe, host): host for host in candidates}
            for fut in as_completed(futs):
                host = futs[fut]
                try:
                    entry = fut.result()
                except Exception:
                    continue
                if entry:
                    entry["sources"] = sorted(candidates[host])
                    self._add(entry)

    def _sweep(self, net: str, skip: Set[str]) -> None:
        try:
            network = ipaddress.ip_network(net, strict=False)
        except ValueError:
            with self._lock:
                self._status["errors"].append(f"'{net}' is not a valid network (try 192.168.1.0/24)")
            return
        if network.num_addresses > 1024:
            with self._lock:
                self._status["errors"].append(f"{net} is too large to scan (max /22 = 1024 addresses); scanning its first /22")
            network = ipaddress.ip_network(f"{network.network_address}/22", strict=False)
        hosts = [str(h) for h in itertools.islice(network.hosts(), 1024) if str(h) not in skip]
        with ThreadPoolExecutor(max_workers=32) as pool:
            futs = {pool.submit(self.probe, h, 0.7): h for h in hosts}
            done = 0
            for fut in as_completed(futs):
                done += 1
                if done % 32 == 0:
                    self._set_progress(f"Scanning {net} ({done}/{len(hosts)})")
                try:
                    entry = fut.result()
                except Exception:
                    continue
                if entry:
                    entry["sources"] = ["scan"]
                    self._add(entry)

    @staticmethod
    def probe(host: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
        """Return a device descriptor if ``host`` answers like WLED."""
        try:
            client = WLEDClient(host, timeout=timeout, connect_timeout=min(timeout, 1.0))
        except WLEDError:
            return None
        try:
            info = client.get_info()
        except WLEDError:
            return None
        finally:
            # A /24 sweep probes 254 addresses and nearly all of them fail:
            # every one used to leave its session behind.
            client.close()
        if not isinstance(info, dict) or ("ver" not in info and "leds" not in info):
            return None
        leds = info.get("leds") or {}
        return {
            "host": host,
            "name": info.get("name") or host,
            "mac": info.get("mac"),
            "ver": info.get("ver"),
            "led_count": leds.get("count"),
            "arch": info.get("arch"),
            "ip": info.get("ip") or host,
        }

    def _annotate(self, snap: Dict[str, Any]) -> Dict[str, Any]:
        devices = self.store.section("devices") or []
        known_hosts = {d.get("host") for d in devices}
        known_macs = {}
        for did in self.devices.ids():
            try:
                summary = self.devices.summary(did)
            except Exception:
                continue
            mac = (summary.get("info") or {}).get("mac")
            if mac:
                known_macs[mac] = did
        for entry in snap["found"]:
            entry["known"] = entry.get("host") in known_hosts or entry.get("mac") in known_macs
            entry["device_id"] = known_macs.get(entry.get("mac"))
        snap["found"].sort(key=lambda e: (e.get("known", False), str(e.get("name", ""))))
        return snap
