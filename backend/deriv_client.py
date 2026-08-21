"""Client Deriv API (nouveau schéma 2026) : REST + OTP + WebSocket.

Deriv a retiré les tokens de session classiques (~15 char) et l'auth via
`authorize` sur le WebSocket. Le flux est désormais :

  1. REST GET  /trading/v1/options/accounts   (Bearer PAT + Deriv-App-ID)
     → liste des comptes du user.
  2. REST POST /trading/v1/options/accounts/{id}/otp
     → renvoie une URL WebSocket contenant un OTP valide 120 s.
  3. WS  wss://api.derivws.com/trading/v1/options/ws/{demo|real}?otp=...
     → connexion pré-authentifiée, plus besoin de `authorize`.

Le format des messages du WebSocket reste identique au v3 classique
(msg_type/req_id/error), avec quelques renommages :
  - `symbol` → `underlying_symbol` dans `proposal`.
  - Contract types Rise/Fall : `CALL/PUT` → `CALLE/PUTE`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("deriv_client")

SubscriptionCallback = Callable[[dict[str, Any]], Awaitable[None]]


class DerivError(Exception):
    """Erreur renvoyée par l'API Deriv (champ `error` dans la réponse)."""

    def __init__(self, code: str, message: str) -> None:
        self.code: str = code
        self.message: str = message
        super().__init__(f"[{code}] {message}")


class DerivClient:
    """Client WebSocket Deriv (v1/options), sûr pour usage concurrent asyncio."""

    def __init__(
        self,
        app_id: str,
        rest_base_url: str = "https://api.derivws.com",
        request_timeout: float = 30.0,
        max_reconnect_delay: float = 60.0,
        preferred_account_type: str = "demo",
    ) -> None:
        if not app_id:
            raise ValueError("app_id (Deriv-App-ID) requis")
        self._app_id: str = app_id
        self._rest_base_url: str = rest_base_url.rstrip("/")
        self._request_timeout: float = request_timeout
        self._max_reconnect_delay: float = max_reconnect_delay
        self._preferred_account_type: str = preferred_account_type

        self._pat_token: Optional[str] = None
        self._account_info: Optional[dict[str, Any]] = None

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._req_id: int = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._subscriptions: dict[str, SubscriptionCallback] = {}

        self._reader_task: Optional[asyncio.Task[None]] = None
        self._lock: asyncio.Lock = asyncio.Lock()

        self._connected_event: asyncio.Event = asyncio.Event()
        self._closing: bool = False

    # ------------------------------------------------------------------
    # Propriétés
    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._connected_event.is_set()

    @property
    def account_info(self) -> dict[str, Any]:
        """Info du compte actif (loginid/balance/currency), disponible après connect()."""
        if self._account_info is None:
            raise RuntimeError("Non connecté")
        return self._account_info

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------
    async def connect(self, pat_token: str) -> None:
        """Découverte du compte + OTP + ouverture WebSocket."""
        self._closing = False
        self._pat_token = pat_token
        self._account_info = await self._discover_account()
        await self._open_ws()

    async def close(self) -> None:
        """Ferme proprement la connexion."""
        self._closing = True
        self._connected_event.clear()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionClosed(None, None))
        self._pending.clear()

    # ------------------------------------------------------------------
    # REST : découverte du compte + génération OTP
    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        if not self._pat_token:
            raise RuntimeError("pat_token manquant")
        return {
            "Authorization": f"Bearer {self._pat_token}",
            "Deriv-App-ID": self._app_id,
            "Accept": "application/json",
        }

    async def _discover_account(self) -> dict[str, Any]:
        """Liste les comptes du user et retient celui du type préféré."""
        url = f"{self._rest_base_url}/trading/v1/options/accounts"
        async with httpx.AsyncClient(timeout=self._request_timeout) as http:
            try:
                r = await http.get(url, headers=self._headers())
            except httpx.HTTPError as exc:
                raise DerivError("NetworkError", f"REST accounts injoignable : {exc}") from exc

        if r.status_code == 401:
            detail = r.text[:200] or "Token PAT invalide ou Deriv-App-ID erroné"
            raise DerivError("Unauthorized", detail)
        if r.status_code >= 400:
            raise DerivError(f"HTTP{r.status_code}", r.text[:300])

        try:
            payload = r.json()
        except ValueError as exc:
            raise DerivError("BadResponse", "Réponse JSON invalide") from exc

        accounts = payload.get("data") or []
        if not accounts:
            raise DerivError("NoAccount", "Aucun compte options disponible")

        pref = self._preferred_account_type
        picked = next((a for a in accounts if a.get("account_type") == pref), accounts[0])

        try:
            return {
                "loginid": picked["account_id"],
                "account_id": picked["account_id"],
                "balance": float(picked.get("balance", 0.0)),
                "currency": picked.get("currency", "USD"),
                "account_type": picked.get("account_type", "demo"),
                "status": picked.get("status"),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise DerivError("BadResponse", f"Format inattendu : {exc}") from exc

    async def _fetch_otp_url(self) -> str:
        """Retourne une URL WebSocket pré-authentifiée (OTP valide 120 s)."""
        account_id = self.account_info["account_id"]
        url = f"{self._rest_base_url}/trading/v1/options/accounts/{account_id}/otp"
        async with httpx.AsyncClient(timeout=self._request_timeout) as http:
            try:
                r = await http.post(url, headers=self._headers())
            except httpx.HTTPError as exc:
                raise DerivError("NetworkError", f"REST OTP injoignable : {exc}") from exc

        if r.status_code >= 400:
            raise DerivError(f"HTTP{r.status_code}", r.text[:300])
        try:
            data = r.json()["data"]
            ws_url = data["url"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DerivError("BadResponse", f"OTP: format inattendu : {exc}") from exc
        if not isinstance(ws_url, str) or not ws_url.startswith("wss://"):
            raise DerivError("BadResponse", "URL WebSocket manquante")
        return ws_url

    # ------------------------------------------------------------------
    # WebSocket : open / reader / reconnect
    # ------------------------------------------------------------------
    async def _open_ws(self) -> None:
        async with self._lock:
            if self.is_connected:
                return
            ws_url = await self._fetch_otp_url()
            logger.info("WS Deriv : ouverture (OTP embarqué)")
            self._ws = await websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
                max_size=2**22,
                open_timeout=15,
            )
            self._connected_event.set()
            self._reader_task = asyncio.create_task(self._reader_loop())
            logger.info("WS Deriv : connexion établie")

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        continue
                except ValueError:
                    logger.warning("Message non-JSON ignoré")
                    continue
                self._dispatch(message)
        except ConnectionClosed as exc:
            logger.warning("WS Deriv fermée : %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Erreur inattendue dans reader_loop")
        finally:
            self._connected_event.clear()
            if not self._closing:
                asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        """Reconnexion avec backoff : régénère un OTP frais à chaque tentative."""
        delay = 1.0
        while not self._closing:
            try:
                await self._open_ws()
                await self._resubscribe()
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Reconnexion échouée (%s), retry dans %.1fs", exc, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def _resubscribe(self) -> None:
        """Point d'extension : ré-abonnements post-reconnexion (ex: ticks)."""
        return

    # ------------------------------------------------------------------
    # Envoi / réception avec corrélation req_id
    # ------------------------------------------------------------------
    def _dispatch(self, message: dict[str, Any]) -> None:
        req_id = message.get("req_id")
        if isinstance(req_id, int) and req_id in self._pending:
            future = self._pending.pop(req_id)
            if not future.done():
                future.set_result(message)
            return
        msg_type = message.get("msg_type")
        if isinstance(msg_type, str) and msg_type in self._subscriptions:
            callback = self._subscriptions[msg_type]
            asyncio.create_task(self._safe_callback(callback, message))
            return
        logger.debug("Message non routé : %s", message.get("msg_type"))

    @staticmethod
    async def _safe_callback(
        callback: SubscriptionCallback, message: dict[str, Any]
    ) -> None:
        try:
            await callback(message)
        except Exception:  # noqa: BLE001
            logger.exception("Erreur dans callback de subscription")

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Envoie une requête et attend la réponse corrélée par req_id."""
        if self._ws is None or not self._connected_event.is_set():
            raise ConnectionClosed(None, None)

        self._req_id += 1
        req_id = self._req_id
        payload = {**payload, "req_id": req_id}

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = future

        try:
            await self._ws.send(json.dumps(payload, separators=(",", ":")))
        except Exception:
            self._pending.pop(req_id, None)
            raise

        try:
            response = await asyncio.wait_for(future, timeout=self._request_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise
        return self._raise_on_error(response)

    @staticmethod
    def _raise_on_error(response: dict[str, Any]) -> dict[str, Any]:
        error = response.get("error")
        if isinstance(error, dict):
            raise DerivError(
                code=str(error.get("code", "UnknownError")),
                message=str(error.get("message", "Erreur inconnue")),
            )
        return response

    # ------------------------------------------------------------------
    # Abonnements
    # ------------------------------------------------------------------
    def on_subscription(self, msg_type: str, callback: SubscriptionCallback) -> None:
        self._subscriptions[msg_type] = callback

    def clear_subscription(self, msg_type: str) -> None:
        self._subscriptions.pop(msg_type, None)

    async def forget_all(self, *types: str) -> dict[str, Any]:
        return await self.send({"forget_all": list(types)})

    # ------------------------------------------------------------------
    # Méthodes métier
    # ------------------------------------------------------------------
    async def proposal(
        self,
        contract_type: str,
        symbol: str,
        amount: float,
        duration: int,
        duration_unit: str = "t",
        basis: str = "stake",
        currency: str = "USD",
        barrier: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Demande une cotation (prix) pour un contrat donné."""
        payload: dict[str, Any] = {
            "proposal": 1,
            "contract_type": contract_type,
            "underlying_symbol": symbol,
            "amount": amount,
            "basis": basis,
            "currency": currency,
            "duration": duration,
            "duration_unit": duration_unit,
        }
        if barrier is not None:
            payload["barrier"] = barrier
        if extra:
            payload.update(extra)
        response = await self.send(payload)
        return response["proposal"]

    async def buy(self, proposal_id: str, price: float) -> dict[str, Any]:
        response = await self.send({"buy": proposal_id, "price": price})
        return response["buy"]

    async def buy_proposal(
        self,
        contract_type: str,
        symbol: str,
        amount: float,
        duration: int,
        duration_unit: str = "t",
        basis: str = "stake",
        currency: str = "USD",
        barrier: Optional[str] = None,
        max_price: Optional[float] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        prop = await self.proposal(
            contract_type=contract_type,
            symbol=symbol,
            amount=amount,
            duration=duration,
            duration_unit=duration_unit,
            basis=basis,
            currency=currency,
            barrier=barrier,
            extra=extra,
        )
        ask_price = float(prop["ask_price"])
        price_cap = max_price if max_price is not None else ask_price
        if ask_price > price_cap:
            raise DerivError(
                code="PriceMoved",
                message=f"Prix {ask_price} > plafond {price_cap}",
            )
        return await self.buy(proposal_id=str(prop["id"]), price=ask_price)

    async def sell(self, contract_id: int, price: float = 0.0) -> dict[str, Any]:
        response = await self.send({"sell": contract_id, "price": price})
        return response["sell"]

    async def proposal_open_contract(
        self, contract_id: int, subscribe: bool = True
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "proposal_open_contract": 1,
            "contract_id": contract_id,
        }
        if subscribe:
            payload["subscribe"] = 1
        response = await self.send(payload)
        return response["proposal_open_contract"]
