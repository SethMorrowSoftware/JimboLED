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
from typing import Any, Dict, List, Optional, Set, Tuple

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
    # The address inside the packet is whatever the sender wrote there; the
    # packet's own source address is what actually answered.
    return {"host": src_ip or ip, "name": name or "WLED", "chip": NODE_TYPES.get(t & 0x7F, str(t & 0x7F)),
            "on": bool(t & 0x80), "vid": vid, "seen": time.time()}


MAX_NODES = 256


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
                    if len(self.nodes) > MAX_NODES:
                        # A flood of packets must not grow this without bound.
                        oldest = min(self.nodes, key=lambda h: self.nodes[h]["seen"])
                        self.nodes.pop(oldest, None)
        sock.close()

    def snapshot(self, max_age_s: float = 600.0) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [dict(n) for n in self.nodes.values() if now - n["seen"] <= max_age_s]


MAX_FOLLOW_PROBES = 8


def _ipv4_part(host: str) -> str:
    """``"192.168.1.5"`` or ``"192.168.1.5:8080"`` → ``"192.168.1.5"``; a name → ``""``."""
    ip = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    try:
        return str(ipaddress.IPv4Address(ip))
    except ValueError:
        return ""


def _same_subnet(host: str, others: List[str], prefix: int = 24, local: Optional[List[str]] = None) -> bool:
    """Whether ``host`` is on the same network as one of ``others``: the same
    /24, or the same local network the Pi is on (mesh routers often use a
    /22, and a controller can land in another /24 of it after a power cut)."""
    ip = _ipv4_part(host)
    if not ip:
        return False
    addr = ipaddress.ip_address(ip)
    nets = [ipaddress.ip_network(f"{ip}/{prefix}", strict=False)]
    for cidr in local_subnets() if local is None else local:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if addr in net:
            nets.append(net)
    return any(_ipv4_part(o) and any(ipaddress.ip_address(_ipv4_part(o)) in n for n in nets) for o in others)


# ------------------------------------------------------------------ service
class DiscoveryService:
    def __init__(self, store, devices, *, passive: bool = True):
        self.store = store
        self.devices = devices
        self._lock = threading.Lock()
        self._follow_probed: Dict[str, float] = {}  # host → when follow_moved last probed it
        self._thread: Optional[threading.Thread] = None
        self._status: Dict[str, Any] = {"running": False, "started_at": None, "finished_at": None,
                                        "found": [], "errors": [], "progress": "", "methods": []}
        self.listener = NodeListener()
        self._moved_stop = threading.Event()
        self._started_at = time.time()
        if passive:
            self.listener.start()
            threading.Thread(target=self._follow_loop, name="wled-follow", daemon=True).start()

    def stop(self) -> None:
        self._moved_stop.set()
        self.listener.stop()

    # ------------------------------------------------- controllers that moved
    FOLLOW_EVERY_S = 30.0
    LOST_AFTER_S = 60.0

    def _follow_loop(self) -> None:
        while not self._moved_stop.wait(self.FOLLOW_EVERY_S):
            try:
                self.follow_moved()
            except Exception:  # pragma: no cover - must never stop
                log.exception("following moved controllers failed")

    def _lost_controllers(self) -> Dict[str, Tuple[str, str]]:
        """MAC → (device id, old address) for controllers offline a while.

        Only controllers configured by IPv4 address: one added by name
        (``wled-porch.local``) follows its own address changes anyway.  A
        light from before MACs were remembered is keyed ``"~" + the last six
        hex digits`` its id was made from.
        """
        from .manager import _LEGACY_ID, normalise_mac

        lost: Dict[str, Tuple[str, str]] = {}
        for d in self.store.section("devices") or []:
            if not isinstance(d, dict) or not d.get("enabled", True) or not d.get("id"):
                continue
            if not _ipv4_part(str(d.get("host") or "")):
                continue
            try:
                s = self.devices.summary(d["id"])
            except Exception:
                continue
            # The MAC from config works even before the first successful poll
            # after a restart, which is exactly when a power cut moves things.
            mac = normalise_mac((s.get("info") or {}).get("mac")) or normalise_mac(d.get("mac"))
            if not mac:
                legacy = _LEGACY_ID.match(str(d.get("id") or ""))
                mac = "~" + legacy.group(1) if legacy else ""
            gone_since = max(float(s.get("last_seen") or 0), self._started_at)
            if mac and not s.get("online") and time.time() - gone_since > self.LOST_AFTER_S:
                lost[mac] = (d["id"], str(d["host"]))
        return lost

    def _answered_by_another(self, device_id: Any) -> bool:
        try:
            return bool(self.devices.wrong_device(device_id))
        except Exception:
            return False

    def follow_moved(self, found: Optional[List[Dict[str, Any]]] = None) -> List[str]:
        """Re-point controllers that came back at a new IP address.

        Routers hand out new addresses after a power cut; a controller that
        is suddenly "offline" while a WLED with the *same MAC address* answers
        somewhere else is the same controller, so its address is updated
        rather than leaving a non-technical owner with a dead tile.
        """
        from .manager import normalise_mac

        lost = self._lost_controllers()
        if not lost:
            return []
        # Addresses of lights that are themselves lost don't count as taken:
        # two controllers that swapped addresses are each found at the other's.
        # The same goes for the address of a light that some *other*
        # controller answers at (it is not that light's any more).
        # A disabled light isn't polled, so nothing shows its address is still its own either.
        lost_ids = {did for did, _ in lost.values()}
        known_hosts = {d.get("host") for d in (self.store.section("devices") or [])
                       if isinstance(d, dict) and d.get("enabled", True) and d.get("id") not in lost_ids
                       and not self._answered_by_another(d.get("id"))}
        entries = list(found or [])
        local = local_subnets()
        if found is None:
            candidates = [n["host"] for n in self.listener.snapshot(max_age_s=300)
                          if n["host"] not in known_hosts and _same_subnet(n["host"], [old for _, old in lost.values()], local=local)]
            # Least recently probed first (never-probed, i.e. new, addresses
            # before all others), so every candidate gets its turn.
            candidates.sort(key=lambda h: self._follow_probed.get(h, 0.0))
            self._follow_probed = {h: t for h, t in self._follow_probed.items() if h in candidates}
            for host in candidates[:MAX_FOLLOW_PROBES]:
                self._follow_probed[host] = time.time()
                entry = self.probe(host)
                if entry:
                    entries.append(entry)
        moved = []
        for entry in entries:
            mac = normalise_mac(entry.get("mac"))
            host = entry.get("host")
            if mac and mac not in lost and "~" + mac[-6:] in lost:
                mac = "~" + mac[-6:]  # a light from before MACs were remembered
            if not mac or mac not in lost or not host or host in known_hosts:
                continue
            did, old_host = lost[mac]
            # A WLED reports its own MAC, so this is a hint, not proof: only
            # follow it within the network the controller lived in.
            if not _same_subnet(host, [old_host], local=local):
                log.warning("not following %s to %s: a different network", did, host)
                continue
            lost.pop(mac)

            def mutate(cfg, did=did, host=host):
                for d in cfg.get("devices", []):
                    if d.get("id") == did:
                        d["host"] = host

            self.store.update(mutate, backup_reason="controller-moved")
            log.warning("controller %s moved to %s; following it", did, host)
            moved.append(did)
        return moved

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
                found = [dict(f) for f in self._status["found"]]
            try:
                self.follow_moved(found)
            except Exception:  # pragma: no cover
                log.exception("following moved controllers failed")
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
        from .manager import _LEGACY_ID, is_controller, normalise_mac

        devices = [d for d in (self.store.section("devices") or []) if isinstance(d, dict)]
        by_host = {d.get("host"): d for d in devices}
        known_macs = {}
        for did in self.devices.ids():
            try:
                summary = self.devices.summary(did)
            except Exception:
                continue
            mac = normalise_mac((summary.get("info") or {}).get("mac"))
            if mac:
                known_macs[mac] = did
        for d in devices:
            if isinstance(d, dict) and normalise_mac(d.get("mac")):
                known_macs.setdefault(normalise_mac(d.get("mac")), d.get("id"))
        for entry in snap["found"]:
            emac = normalise_mac(entry.get("mac"))
            did = known_macs.get(emac) if emac else None
            if not did and emac:
                did = next((d.get("id") for d in devices if is_controller(d, emac)), None)
            # By address only when that light's controller can't be told
            # apart: an unrelated WLED that took a light's old address is not
            # "added" (it may be a light that moved, or a new one).
            holder = by_host.get(entry.get("host"))
            unknown = holder is not None and not normalise_mac(holder.get("mac")) and not _LEGACY_ID.match(str(holder.get("id") or ""))
            held = holder is not None and (not emac or unknown or is_controller(holder, emac))
            entry["known"] = bool(did) or held
            entry["device_id"] = did or (holder.get("id") if held else None)
        snap["found"].sort(key=lambda e: (e.get("known", False), str(e.get("name", ""))))
        return snap
