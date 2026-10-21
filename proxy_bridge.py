"""Локальный HTTP-прокси, который выводит трафик Chrome в VPN.

Зачем: VPN настроен на раздельное туннелирование — python.exe идёт через
туннель, а chrome.exe ходит напрямую с домашнего IP. Авито домашний IP
забанил, поэтому браузер получает 403, хотя с VPN-адреса отдаёт нормальную
страницу или задание proof-of-work.

Схема обхода: Chrome -> 127.0.0.1:8899 (этот файл, python.exe) -> VPN -> Авито.

Запуск отдельно:  python proxy_bridge.py [порт]
Обычно запускается из avito_watch.py автоматически.
"""

from __future__ import annotations

import select
import socket
import sys
import threading
from urllib.parse import urlsplit

DEFAULT_PORT = 8899


def _pipe(a: socket.socket, b: socket.socket) -> None:
    """Переливает байты в обе стороны, пока кто-нибудь не закроет соединение."""
    try:
        while True:
            readable, _, _ = select.select([a, b], [], [], 60)
            if not readable:
                break
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    return
                (b if sock is a else a).sendall(data)
    except OSError:
        pass


def _handle(conn: socket.socket) -> None:
    upstream = None
    try:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = conn.recv(65536)
            if not chunk:
                return
            request += chunk
            if len(request) > 131072:
                return
        head, _, rest = request.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        method, target, _ = lines[0].split(b" ", 2)

        if method == b"CONNECT":
            host, _, port = target.partition(b":")
            upstream = socket.create_connection((host.decode(), int(port or 443)), timeout=25)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            _pipe(conn, upstream)
            return

        parts = urlsplit(target.decode(errors="replace"))
        if not parts.hostname:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return
        upstream = socket.create_connection((parts.hostname, parts.port or 80), timeout=25)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        lines[0] = b" ".join((method, path.encode(), b"HTTP/1.1"))
        upstream.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + rest)
        _pipe(conn, upstream)
    except OSError:
        pass
    finally:
        for sock in (conn, upstream):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def serve(port: int = DEFAULT_PORT) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", port))
    except OSError:
        # порт уже занят — значит прокси уже поднят, второй раз не нужно
        print(f"порт {port} уже занят, используем работающий прокси", flush=True)
        return
    server.listen(64)
    print(f"прокси слушает 127.0.0.1:{port}", flush=True)
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


def start(port: int = DEFAULT_PORT) -> threading.Thread:
    """Поднимает прокси в фоновом потоке (для запуска из watcher'а)."""
    thread = threading.Thread(target=serve, args=(port,), daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT)
