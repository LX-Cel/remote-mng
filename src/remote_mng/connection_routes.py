"""Explicit, bounded connection routes and credential-free failure evidence."""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
import time

import regex

from .errors import RemoteError


def diagnostic(stage, *, route="target", business_input="not_sent", evidence="", actions=None, trace=None):
    return {"stage": stage, "route": route, "observed_at": time.time(),
            "business_input": business_input, "evidence": evidence[-4096:],
            "recovery_actions": actions or (["query_original_resource"] if business_input != "not_sent" else ["inspect_configuration"]),
            "trace": (trace or [])[-64:], "trace_truncated": len(trace or []) > 64,
            "evidence_truncated": len(evidence) > 4096}


def enrich(error, stage, **kwargs):
    error.details.setdefault("diagnostic", diagnostic(stage, **kwargs))
    return error


def validate_routes(cfg):
    jump, proxy = cfg.get("jump"), cfg.get("proxy")
    if jump and proxy:
        raise ValueError("Configure the proxy on the jump endpoint when using a jump")
    if (jump or proxy) and cfg.get("protocol", "ssh") != "ssh":
        raise ValueError("Explicit jump/proxy routes require SSH; configure a separate SSH transfer endpoint for Telnet")
    if jump:
        if not isinstance(jump, dict) or any(jump.get(k) for k in ("jump", "transfer", "login_steps", "login_flow")):
            raise ValueError("Only one standard SSH jump endpoint is supported")
        from .config import Target
        Target.model_validate({**jump, "protocol": "ssh"})
    if proxy:
        if not isinstance(proxy, dict) or set(proxy) - {"type", "host", "port", "username_env", "password_env"}:
            raise ValueError("Invalid proxy fields")
        if proxy.get("type") not in ("http", "socks5") or not isinstance(proxy.get("host"), str) or not proxy["host"]:
            raise ValueError("Proxy requires type and host")
        if not isinstance(proxy.get("port"), int) or not 1 <= proxy["port"] <= 65535:
            raise ValueError("Proxy port must be in [1,65535]")
        if bool(proxy.get("username_env")) != bool(proxy.get("password_env")):
            raise ValueError("Proxy authentication requires both environment references")
        for key in ("username_env", "password_env"):
            if key in proxy and (not isinstance(proxy[key], str) or not regex.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", proxy[key])):
                raise ValueError("Proxy credentials must be environment references")


def validate_login(steps, flow):
    if len(steps) > 64:
        raise ValueError("At most 64 login steps are supported")
    for step in steps:
        if not 0 < float(step.get("timeout", 15)) <= 300:
            raise ValueError("Login step timeout must be in (0,300]")
        if "send_env" in step and (not isinstance(step["send_env"], str) or not regex.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", step["send_env"])):
            raise ValueError("Login credentials must use an environment variable name")
    if not flow:
        return
    if steps:
        raise ValueError("Use login_steps or login_flow, not both")
    if not isinstance(flow, dict) or set(flow) - {"start", "steps", "max_steps", "timeout"}:
        raise ValueError("Invalid login flow")
    nodes = flow.get("steps")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 64:
        raise ValueError("Login flow requires 1-64 nodes")
    if not isinstance(flow.get("max_steps", 32), int) or not 1 <= flow.get("max_steps", 32) <= 128:
        raise ValueError("Login flow traversal limit must be in [1,128]")
    if not 0 < float(flow.get("timeout", 60)) <= 300:
        raise ValueError("Login flow timeout must be in (0,300]")
    ids = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) - {"id", "expect", "send", "send_env", "newline", "next", "branches", "finish", "timeout"}:
            raise ValueError("Invalid login node")
        name = node.get("id")
        if not isinstance(name, str) or not regex.fullmatch(r"[\w.-]{1,64}", name) or name in ids:
            raise ValueError("Login node IDs must be unique")
        ids.add(name)
        if "send" in node and "send_env" in node:
            raise ValueError("Use send OR send_env")
        if "send_env" in node and (not isinstance(node["send_env"], str) or not regex.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", node["send_env"])):
            raise ValueError("Login credentials must use an environment variable name")
        if not 0 < float(node.get("timeout", 15)) <= 300:
            raise ValueError("Login timeout out of range")
        if not node.get("expect") and not node.get("branches"):
            raise ValueError("Each login node must observe an expected prompt")
        branches = node.get("branches", [])
        if branches and (node.get("expect") or node.get("next")):
            raise ValueError("Branch nodes select their own expect and next; do not combine with node expect/next")
        if not isinstance(branches, list) or len(branches) > 16:
            raise ValueError("At most 16 branches are supported")
        for branch in branches:
            if not isinstance(branch, dict) or set(branch) != {"expect", "next"}:
                raise ValueError("A branch requires expect and next")
        for pattern in ([node["expect"]] if node.get("expect") else []) + [b["expect"] for b in branches]:
            if not isinstance(pattern, str) or not 0 < len(pattern) <= 2048:
                raise ValueError("Login patterns must contain 1-2048 characters")
            regex.compile(pattern)
        if node.get("finish") and (node.get("next") or branches or "send" in node or "send_env" in node):
            raise ValueError("A finish node only observes the final prompt")
    if flow.get("start") not in ids:
        raise ValueError("Login start node is missing")
    for node in nodes:
        edges = [node["next"]] if node.get("next") else []
        edges += [b["next"] for b in node.get("branches", [])]
        if any(edge not in ids for edge in edges) or (not edges and not node.get("finish")):
            raise ValueError("Login edges must refer to nodes and paths must end at finish nodes")


async def proxy_socket(proxy, host, port, timeout):
    """Return an owned nonblocking TCP socket after an HTTP/SOCKS handshake."""
    loop = asyncio.get_running_loop()
    sock = None
    async def exact(count):
        data = b""
        while len(data) < count:
            part = await loop.sock_recv(sock, count - len(data))
            if not part:
                raise RemoteError("proxy_failed", "Proxy closed during negotiation")
            data += part
        return data
    try:
        async with asyncio.timeout(timeout):
            addresses = await loop.getaddrinfo(proxy["host"], proxy["port"], type=socket.SOCK_STREAM)
            for family, kind, proto, _, addr in addresses:
                sock = socket.socket(family, kind, proto)
                sock.setblocking(False)
                try:
                    await loop.sock_connect(sock, addr)
                    break
                except OSError:
                    sock.close()
                    sock = None
            if sock is None:
                raise RemoteError("proxy_unreachable", "Proxy TCP connection failed")
            user = password = None
            if proxy.get("username_env"):
                from .transports import _secret
                user = _secret(proxy, "username_env")
                password = _secret(proxy, "password_env")
            if proxy["type"] == "http":
                if any(c in host for c in "\r\n\0"):
                    raise RemoteError("invalid_target", "Invalid HTTP CONNECT target")
                authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
                request = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
                if user is not None:
                    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
                    request += f"Proxy-Authorization: Basic {auth}\r\n"
                await loop.sock_sendall(sock, (request + "\r\n").encode("ascii"))
                response = b""
                while not response.endswith(b"\r\n\r\n"):
                    response += await exact(1)
                    if len(response) > 16384:
                        raise RemoteError("proxy_failed", "Proxy response exceeds header limit")
                parts = response.split(b"\r\n", 1)[0].split()
                if len(parts) < 2 or parts[1] != b"200":
                    raise RemoteError("proxy_authentication_failed" if len(parts) > 1 and parts[1] == b"407" else "proxy_failed", "HTTP CONNECT was rejected")
            else:
                await loop.sock_sendall(sock, b"\x05\x01" + (b"\x02" if user is not None else b"\x00"))
                method = await exact(2)
                if method != (b"\x05\x02" if user is not None else b"\x05\x00"):
                    raise RemoteError("proxy_authentication_failed", "SOCKS5 authentication method was rejected")
                if user is not None:
                    ub, pb = user.encode(), password.encode()
                    if not 1 <= len(ub) <= 255 or not 1 <= len(pb) <= 255:
                        raise RemoteError("invalid_config", "SOCKS5 credential length is invalid")
                    await loop.sock_sendall(sock, b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
                    if await exact(2) != b"\x01\x00":
                        raise RemoteError("proxy_authentication_failed", "SOCKS5 credentials were rejected")
                try:
                    address = ipaddress.ip_address(host)
                    encoded = (b"\x01" if address.version == 4 else b"\x04") + address.packed
                except ValueError:
                    hb = host.encode("idna")
                    if not 1 <= len(hb) <= 255:
                        raise RemoteError("invalid_target", "SOCKS5 hostname length is invalid")
                    encoded = b"\x03" + bytes([len(hb)]) + hb
                await loop.sock_sendall(sock, b"\x05\x01\x00" + encoded + port.to_bytes(2, "big"))
                reply = await exact(4)
                if reply[:3] != b"\x05\x00\x00":
                    raise RemoteError("proxy_failed", "SOCKS5 target connection was rejected")
                count = {1: 4, 4: 16}.get(reply[3])
                if reply[3] == 3:
                    count = (await exact(1))[0]
                if count is None:
                    raise RemoteError("proxy_failed", "SOCKS5 response is invalid")
                await exact(count + 2)
            return sock
    except BaseException:
        if sock:
            sock.close()
        raise
