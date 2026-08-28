"""The UDP broadcast responder that lets Jellyfin apps find this server.

Clients broadcast the plaintext string "who is JellyfinServer?" to
255.255.255.255:7359 and expect a small JSON reply. Without this, every client
has to be given the server's address by hand -- which on a television, typed
with a remote, is the difference between a feature people use and one they
give up on.

Runs on its own daemon thread rather than in the asyncio loop: it is a single
blocking recvfrom that must not be able to stall request handling, and a
daemon thread needs no shutdown coordination with uvicorn's lifespan.
"""

import json
import socket
import threading

from loguru import logger

DISCOVERY_PORT = 7359

#: Clients have sent several spellings of this over the years ("who is
#: JellyfinServer?", "Who is Jellyfin Server?"). Both sides are compared with
#: whitespace and punctuation stripped so every spelling matches.
_MAGIC = "whoisjellyfinserver"


def _normalise(message: str) -> str:
    return "".join(c for c in message.lower() if c.isalnum())


class DiscoveryResponder:
    """Answers discovery broadcasts for as long as it is running."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(1.0)
            sock.bind(("0.0.0.0", DISCOVERY_PORT))
        except OSError as exc:
            # Port taken, or no permission to bind. Manual "add server by
            # address" still works, so this is a degraded feature and not a
            # reason to fail startup.
            logger.warning(
                f"Jellyfin discovery disabled: could not bind UDP "
                f"{DISCOVERY_PORT} ({exc}). Clients can still add the server "
                f"by address."
            )
            return

        self._sock = sock
        self._thread = threading.Thread(
            target=self._serve, name="JellyfinDiscovery", daemon=True
        )
        self._thread.start()
        logger.debug(f"Jellyfin discovery responding on UDP {DISCOVERY_PORT}")

    def stop(self) -> None:
        self._stop.set()

        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

            self._sock = None

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                # Socket closed under us by stop(); that is the exit path.
                return
            except Exception as exc:
                logger.debug(f"Jellyfin discovery read failed: {exc}")
                continue

            message = data.decode("utf-8", "ignore")

            if _MAGIC not in _normalise(message):
                continue

            try:
                self._sock.sendto(self._payload(addr[0]).encode(), addr)
            except Exception as exc:
                logger.debug(f"Jellyfin discovery reply to {addr} failed: {exc}")

    @staticmethod
    def _payload(peer: str) -> str:
        from program.services.jellyfin_server import ids
        from program.settings import settings_manager

        settings = settings_manager.settings.jellyfin_server

        address = settings.advertised_url.strip().rstrip("/") or (
            f"http://{_local_address_for(peer)}:8080"
        )

        return json.dumps(
            {
                "Address": address,
                "Id": ids.SERVER_ID.replace("-", ""),
                "Name": settings.server_name,
                "EndpointAddress": None,
            }
        )


def _local_address_for(peer: str) -> str:
    """Which of our addresses this particular client should come back to.

    Asking the routing table rather than taking the first interface: a server
    with a VPN tunnel up has several addresses, and handing a LAN client the
    tunnel address gives it one it cannot reach.
    """

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect((peer, 9))
        address = probe.getsockname()[0]
        probe.close()

        return address
    except Exception:
        return socket.gethostbyname(socket.gethostname())


_responder = DiscoveryResponder()


def start() -> None:
    _responder.start()


def stop() -> None:
    _responder.stop()
