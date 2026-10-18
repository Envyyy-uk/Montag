"""Tailscale: адреса й ім'я ноутбука в tailnet, сертифікат HTTPS, тип з'єднання з телефоном.

`tailscale cert` видає справжній сертифікат Let's Encrypt для <машина>.<tailnet>.ts.net —
iPhone довіряє йому без жодних налаштувань, тож PWA і Web Push працюють.
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

_cache: dict[str, tuple[float, dict]] = {}


def _cli() -> str | None:
    return shutil.which("tailscale") or (r"C:\Program Files\Tailscale\tailscale.exe"
                                         if Path(r"C:\Program Files\Tailscale\tailscale.exe").exists() else None)


def status(max_age: float = 10.0) -> dict | None:
    """`tailscale status --json` (кеш 10 с). None — Tailscale не встановлено або не запущено."""
    hit = _cache.get("status")
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    cli = _cli()
    if not cli:
        return None
    try:
        out = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=10)
        data = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        data = None
    if data is not None:
        _cache["status"] = (time.monotonic(), data)
    return data


def self_ip(st: dict | None = None) -> str | None:
    st = st if st is not None else status()
    if not st:
        return None
    for ip in (st.get("Self") or {}).get("TailscaleIPs") or []:
        if ipaddress.ip_address(ip).version == 4:
            return ip
    return None


def dns_name(st: dict | None = None) -> str | None:
    st = st if st is not None else status()
    name = ((st or {}).get("Self") or {}).get("DNSName") or ""
    return name.rstrip(".") or None


def make_cert(cert_file: Path, key_file: Path) -> str:
    """Сертифікат для <машина>.<tailnet>.ts.net (потрібні увімкнені HTTPS-сертифікати в адмінці Tailscale)."""
    cli, name = _cli(), dns_name()
    if not cli or not name:
        raise RuntimeError("Tailscale не запущено або в tailnet не ввімкнено MagicDNS")
    cert_file.parent.mkdir(parents=True, exist_ok=True)
    out = subprocess.run([cli, "cert", "--cert-file", str(cert_file), "--key-file", str(key_file), name],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "tailscale cert не вдався")
    return name


@dataclass
class Peer:
    ip: str
    name: str
    direct: bool          # пряме з'єднання, без ретранслятора DERP
    lan: bool             # пряме і через приватну (домашню) мережу
    endpoint: str = ""


def _host(addr: str) -> str:
    if addr.startswith("["):
        return addr[1:addr.index("]")]
    return addr.rsplit(":", 1)[0]


def peer_for(ip: str, st: dict | None = None) -> Peer | None:
    """Хто з tailnet підключився з цієї адреси і як: напряму вдома, напряму через інтернет чи через ретранслятор."""
    st = st if st is not None else status()
    if not st:
        return None
    for p in (st.get("Peer") or {}).values():
        if ip in (p.get("TailscaleIPs") or []):
            cur = p.get("CurAddr") or ""
            direct = bool(cur)  # CurAddr порожній — трафік іде через ретранслятор DERP
            lan = False
            if cur:
                try:
                    lan = ipaddress.ip_address(_host(cur)).is_private
                except ValueError:
                    lan = False
            return Peer(ip, (p.get("DNSName") or p.get("HostName") or "").rstrip("."), direct, direct and lan, cur)
    return None
