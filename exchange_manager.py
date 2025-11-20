"""
ExchangeManager v2 - универсальный менеджер исполнения ордеров
Поддерживает режимы LIVE/DEMO/BACKTEST с единым интерфейсом
"""

from __future__ import annotations
from typing import Dict, Any, Optional, Callable, List, Literal, Set
from decimal import Decimal
import time
from datetime import datetime, timezone
import asyncio
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field

from iqts_standards import (
    OrderReq, OrderUpd, ExchangeEvent, PriceFeed,
    ExchangeEventHandler, get_current_timestamp_ms, OrderType
)

logger = logging.getLogger(__name__)



# === Исключения ===

class ExchangeManagerError(Exception):
    """Базовая ошибка ExchangeManager"""
    pass


class InvalidOrderError(ExchangeManagerError):
    """Некорректный ордер"""
    pass


class ConnectionError(ExchangeManagerError):
    """Ошибка соединения с биржей"""
    pass


class ExchangeApiError(ExchangeManagerError):
    """Ошибка API биржи"""

    def __init__(self, message: str, error_code: Optional[str] = None):
        super().__init__(message)
        self.error_code = error_code


# === Внутренние типы ===

@dataclass
class ActiveOrder:
    """Активный ордер в системе"""
    client_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    type: Literal["MARKET", "LIMIT", "STOP_MARKET", "STOP", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"]
    qty: Decimal
    price: Optional[Decimal]
    stop_price: Optional[Decimal] = None
    filled_qty: Decimal = Decimal('0')
    status: str = "NEW"
    correlation_id: Optional[str] = None
    timestamp_ms: int = field(default_factory=get_current_timestamp_ms)
    reduce_only: bool = False
    exchange_order_id: Optional[str] = None
    trigger_price: Optional[Decimal] = None  # Цена триггера для STOP ордеров


@dataclass
class ConnectionState:
    """Состояние соединения"""
    status: Literal["connected", "disconnected", "connecting", "error"] = "disconnected"
    last_heartbeat: Optional[int] = None
    reconnect_count: int = 0
    error_message: Optional[str] = None
    connected_at: Optional[int] = None


class ExchangeManager:
    """
    Универсальный менеджер исполнения ордеров для LIVE/DEMO/BACKTEST режимов.

    Ответственности:
    - Принимает OrderReq → выполняет → эмитит OrderUpd
    - Поддерживает подключение к бирже (LIVE)
    - Эмулирует исполнение (DEMO/BACKTEST)
    - Транспортное сопровождение STOP ордеров
    """

    def __init__(self,
                 base_url: str,
                 on_order_update: Callable[[OrderUpd], None],
                 trade_log: Optional[Any] = None,
                 *,
                 demo_mode: bool = True,
                 is_testnet: bool = False,
                 logger_instance: Optional[logging.Logger] = None,
                 metrics: Optional[Any] = None,
                 event_handlers: Optional[List[ExchangeEventHandler]] = None,
                 ws_url: Optional[str] = None,
                 execution_mode: str = "DEMO",
                 timeout_seconds: Optional[int] = None,
                 symbols_meta: Optional[Dict[str, Dict[str, Any]]] = None
                 ):

        # Основные параметры
        self.base_url = base_url
        self.on_order_update = on_order_update
        self.trade_log = trade_log
        self.demo_mode = demo_mode
        self.is_testnet = is_testnet
        self.logger = logger_instance or logger
        self.metrics = metrics
        self.execution_mode = execution_mode
        self._lock = threading.RLock()  # ✅ Потокобезопасность
        self.symbols_meta = symbols_meta or self._get_default_symbols_meta()

        self.logger.info(
            f"ExchangeManager initialized with {len(self.symbols_meta)} symbols"
        )
        # >>> ЗАРЕЗЕРВИРОВАНО ДЛЯ LIVE РЕЖИМА (пока не используется в DEMO/BACKTEST)
        # Эти параметры будут задействованы при реализации WebSocket подключения
        self.ws_url = ws_url  # WebSocket URL для user data stream
        self.timeout_seconds = int(timeout_seconds) if timeout_seconds is not None else None

        # Event system
        self._event_handlers: List[ExchangeEventHandler] = event_handlers or []

        # Состояние соединения
        self._connection_state = ConnectionState()

        # Режим работы
        self._is_backtest_mode = (execution_mode == "BACKTEST")

        # Флаг _use_sync_stop_check определяет, используется ли внешняя синхронная проверка
        # В BACKTEST/DEMO стопы проверяются извне (из MainBot), поэтому внутренний монитор не нужен
        self._use_sync_stop_check = self._is_backtest_mode or (execution_mode == "DEMO")

        self.logger.warning(
            f"🔧 ExchangeManager __init__: execution_mode={execution_mode} "
            f"_is_backtest_mode={self._is_backtest_mode} "
            f"_use_sync_stop_check={self._use_sync_stop_check}"
        )

        # Активные ордера
        self._active_orders: Dict[str, ActiveOrder] = {}
        self._orders_by_symbol: Dict[str, Set[str]] = defaultdict(set)

        # Price feed для DEMO режима
        self._price_feed: Optional[PriceFeed] = None

        # Статистика
        self._stats = {
            "orders_sent": 0,
            "orders_filled": 0,
            "orders_rejected": 0,
            "orders_canceled": 0,
            "reconnects_count": 0,
            "total_latency_ms": 0,
            "latency_samples": 0,
            "active_stops": 0,
            "last_order_ts": None
        }

        # === Инициализация компонентов по режиму ===
        self._stop_monitor_active = False
        self._stop_monitor_thread: Optional[threading.Thread] = None

        if self.demo_mode:
            self._demo_latency_ms = 50  # Эмуляция задержки
            self._demo_slippage_pct = 0.001  # 0.1% слипажа для MARKET
            self._demo_stop_slippage_pct = 0.0001  # 0.01% для STOP

            # Запускаем фоновый монитор ТОЛЬКО если НЕ используется синхронная проверка
            # В BACKTEST и DEMO мы полагаемся на check_stops_on_price_update → монитор не нужен
            if not self._use_sync_stop_check:
                self._ensure_stop_monitor_running()
        else:
            # LIVE режим
            self._ws_connection = None
            self._listen_key: Optional[str] = None
            self._keepalive_task: Optional[asyncio.Task] = None

        self.logger.info(f"ExchangeManager initialized: demo_mode={demo_mode}, testnet={is_testnet}")

    def get_account_info(self) -> Dict:
        """
        Получает информацию об аккаунте.
        В режиме DEMO/BACKTEST возвращает заглушку.
        В режиме LIVE должен быть переопределен или реализован через API брокера.
        """
        self.logger.debug(f"ExchangeManager get_account_info called in {self.execution_mode} mode")

        if self.execution_mode == "LIVE":
            # Реализовать настоящий запрос к API биржи для получения данных о счете
            self.logger.debug("get_account_info is not implemented for LIVE mode yet")
            return {"success": False, "error": "Method not implemented", "mode": self.execution_mode}
        else:
            return {
                "success": True,
                "mode": self.execution_mode,
                "demo": self.demo_mode,
                "testnet": self.is_testnet,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "account_id": "DEMO_ACCOUNT_001",
                "balances": [

                    {"asset": "ETH", "free": 10.0, "locked": 0.0}
                ],
                "total_balance_usdt": 1000.0  # Примерная общая стоимость
            }

    def _get_default_symbols_meta(self) -> Dict[str, Dict[str, Any]]:
        """
        Дефолтные метаданные символов (используется если symbols_meta не передан).
        ✅ Этот метод вызывается ТОЛЬКО из __init__ если symbols_meta=None
        """
        self.logger.warning(
            "⚠️ symbols_meta not provided to ExchangeManager, using defaults. "
            "For production, provide actual symbol metadata from exchange."
        )

        return {
            "ETHUSDT": {
                "tick_size": 0.01,
                "step_size": 0.001,
                "min_notional": 5.0,
                "price_precision": 2,
                "quantity_precision": 3
            },
            "BTCUSDT": {
                "tick_size": 0.1,
                "step_size": 0.00001,
                "min_notional": 5.0,
                "price_precision": 1,
                "quantity_precision": 5
            },
            "BNBUSDT": {
                "tick_size": 0.01,
                "step_size": 0.01,
                "min_notional": 5.0,
                "price_precision": 2,
                "quantity_precision": 2
            }
        }
    # === Event System ===

    def add_event_handler(self, handler: ExchangeEventHandler) -> None:
        """Добавить обработчик событий биржи."""
        if handler not in self._event_handlers:
            self._event_handlers.append(handler)
            self.logger.debug(f"Added event handler: {handler}")

    def remove_event_handler(self, handler: ExchangeEventHandler) -> None:
        """Удалить обработчик событий биржи."""
        if handler in self._event_handlers:
            self._event_handlers.remove(handler)
            self.logger.debug(f"Removed event handler: {handler}")

    def _emit_event(self, event: ExchangeEvent) -> None:
        """Внутренний метод эмиссии события всем подписчикам."""
        for handler in self._event_handlers:
            try:
                handler(event)
            except Exception as e:
                self.logger.error(f"Error in event handler: {e}")

    # === Основной интерфейс ===

    def place_order(self, order_req: OrderReq) -> Dict[str, Any]:
        """
        Размещение ордера с полной валидацией инвариантов.

        ✅ ИСПРАВЛЕНИЯ v3.0 (2025-11-20):
        1. Валидация SL/TP ценового инварианта для LONG/SHORT
        2. Проверка min_notional в BACKTEST
        3. Добавление validation_hash в OrderUpd
        4. Добавление correlation_id в OrderUpd
        5. Округление commission до биржевой точности

        BACKTEST режим:
          - MARKET/LIMIT: мгновенное исполнение с валидацией
          - STOP/TAKE_PROFIT: регистрация через _place_order_demo()

        LIVE/DEMO режим:
          - Отправка на биржу через API

        Args:
            order_req: Запрос на размещение ордера (OrderReq)

        Returns:
            Dict с полями:
            - status: "NEW" | "FILLED" | "REJECTED"
            - client_order_id: ID ордера
            - exchange_order_id: ID биржи (если исполнен)
            - error: сообщение об ошибке (если REJECTED)

        Raises:
            ValueError: Если order_req невалиден
        """
        try:
            # ═══════════════════════════════════════════════════════════
            # ШАГ 1: НОРМАЛИЗАЦИЯ И ВАЛИДАЦИЯ
            # ═══════════════════════════════════════════════════════════

            # Нормализация типа ордера
            otype = str(order_req.get("type", "")).upper()
            is_stop_family = otype in (
                "STOP", "STOP_MARKET",
                "TAKE_PROFIT", "TAKE_PROFIT_MARKET"
            )

            # Нормализация stop_price для стоп-ордеров
            if is_stop_family:
                if order_req.get("stop_price") is None and order_req.get("price") is not None:
                    order_req = dict(order_req)
                    order_req["stop_price"] = order_req["price"]
                    order_req["price"] = None

            # Базовая валидация
            self._validate_order_req(order_req)

            # Извлекаем параметры
            symbol = order_req["symbol"]
            client_order_id = order_req["client_order_id"]
            side = order_req["side"]
            qty = order_req["qty"]

            # ═══════════════════════════════════════════════════════════
            # ШАГ 2: ОБРАБОТКА STOP-ОРДЕРОВ (регистрация, не исполнение)
            # ═══════════════════════════════════════════════════════════

            if is_stop_family:
                self.logger.debug(
                    f"Registering {otype} order: {symbol} {side} "
                    f"qty={float(qty):.4f} stop_price={order_req.get('stop_price')}"
                )

                ack = self._place_order_demo(order_req)
                self._stats["orders_sent"] += 1
                return ack

            # ═══════════════════════════════════════════════════════════
            # ШАГ 3: ОБРАБОТКА MARKET/LIMIT ОРДЕРОВ В BACKTEST
            # ═══════════════════════════════════════════════════════════

            self._stats["orders_sent"] += 1

            # Определяем время исполнения
            fill_ts = get_current_timestamp_ms()
            if order_req.get("metadata") and order_req["metadata"].get("candle_ts"):
                fill_ts = int(order_req["metadata"]["candle_ts"])

            # ═══════════════════════════════════════════════════════════
            # ШАГ 4: ОПРЕДЕЛЕНИЕ ЦЕНЫ ИСПОЛНЕНИЯ
            # ═══════════════════════════════════════════════════════════

            fill_price = None

            # Приоритет 1: Цена из ордера (LIMIT)
            if order_req.get("price"):
                fill_price = order_req["price"]

            # Приоритет 2: Текущая рыночная цена (MARKET)
            elif self._price_feed:
                price = self._price_feed(symbol)
                if price:
                    fill_price = Decimal(str(price))

            if not fill_price:
                self._stats["orders_rejected"] += 1
                self.logger.error(
                    f"❌ No price available for order execution: {client_order_id}"
                )
                return {
                    "status": "REJECTED",
                    "error": "no_price_available",
                    "error_message": "Cannot determine execution price",
                    "client_order_id": client_order_id
                }

            # ═══════════════════════════════════════════════════════════
            # ✅ ИСПРАВЛЕНИЕ #1: ВАЛИДАЦИЯ SL/TP ЦЕНОВОГО ИНВАРИАНТА
            # ═══════════════════════════════════════════════════════════

            metadata = order_req.get("metadata", {})
            risk_context = metadata.get("risk_context")

            if risk_context:
                initial_stop_loss = risk_context.get("initial_stop_loss")
                take_profit = risk_context.get("take_profit")

                # Валидация для LONG позиций (BUY)
                if side == "BUY" and initial_stop_loss and take_profit:
                    # Инвариант: SL < Entry < TP
                    if not (initial_stop_loss < float(fill_price) < take_profit):
                        self._stats["orders_rejected"] += 1
                        self.logger.error(
                            f"❌ BACKTEST INVARIANT VIOLATION (LONG):\n"
                            f"  Symbol: {symbol}\n"
                            f"  Order ID: {client_order_id}\n"
                            f"  Expected: SL < Entry < TP\n"
                            f"  Actual: {initial_stop_loss:.2f} < {float(fill_price):.2f} < {take_profit:.2f}\n"
                            f"  Violation: {initial_stop_loss >= float(fill_price) or float(fill_price) >= take_profit}\n"
                            f"  REJECTING ORDER"
                        )
                        return {
                            "status": "REJECTED",
                            "error": "price_invariant_violation",
                            "error_message": (
                                f"LONG invariant violated: "
                                f"SL({initial_stop_loss:.2f}) >= Entry({float(fill_price):.2f}) "
                                f"or Entry >= TP({take_profit:.2f})"
                            ),
                            "client_order_id": client_order_id,
                            "metadata": {
                                "expected_range": f"{initial_stop_loss:.2f} < {float(fill_price):.2f} < {take_profit:.2f}",
                                "side": side
                            }
                        }

                # Валидация для SHORT позиций (SELL)
                elif side == "SELL" and initial_stop_loss and take_profit:
                    # Инвариант: TP < Entry < SL
                    if not (take_profit < float(fill_price) < initial_stop_loss):
                        self._stats["orders_rejected"] += 1
                        self.logger.error(
                            f"❌ BACKTEST INVARIANT VIOLATION (SHORT):\n"
                            f"  Symbol: {symbol}\n"
                            f"  Order ID: {client_order_id}\n"
                            f"  Expected: TP < Entry < SL\n"
                            f"  Actual: {take_profit:.2f} < {float(fill_price):.2f} < {initial_stop_loss:.2f}\n"
                            f"  Violation: {take_profit >= float(fill_price) or float(fill_price) >= initial_stop_loss}\n"
                            f"  REJECTING ORDER"
                        )
                        return {
                            "status": "REJECTED",
                            "error": "price_invariant_violation",
                            "error_message": (
                                f"SHORT invariant violated: "
                                f"TP({take_profit:.2f}) >= Entry({float(fill_price):.2f}) "
                                f"or Entry >= SL({initial_stop_loss:.2f})"
                            ),
                            "client_order_id": client_order_id,
                            "metadata": {
                                "expected_range": f"{take_profit:.2f} < {float(fill_price):.2f} < {initial_stop_loss:.2f}",
                                "side": side
                            }
                        }

            # ═══════════════════════════════════════════════════════════
            # ✅ ИСПРАВЛЕНИЕ #4: ПРОВЕРКА MIN_NOTIONAL
            # ═══════════════════════════════════════════════════════════

            notional = float(fill_price) * float(qty)

            symbol_info = self.symbols_meta.get(symbol)
            if symbol_info:
                min_notional = symbol_info.get("min_notional", 0)

                if min_notional > 0 and notional < min_notional:
                    self._stats["orders_rejected"] += 1
                    self.logger.error(
                        f"❌ BACKTEST: Order notional too small:\n"
                        f"  Symbol: {symbol}\n"
                        f"  Order ID: {client_order_id}\n"
                        f"  Notional: {notional:.2f} USDT\n"
                        f"  Min required: {min_notional:.2f} USDT\n"
                        f"  Deficit: {min_notional - notional:.2f} USDT\n"
                        f"  REJECTING ORDER"
                    )
                    return {
                        "status": "REJECTED",
                        "error": "min_notional_violation",
                        "error_message": (
                            f"Notional {notional:.2f} USDT < "
                            f"min_notional {min_notional:.2f} USDT"
                        ),
                        "client_order_id": client_order_id,
                        "metadata": {
                            "notional": notional,
                            "min_notional": min_notional,
                            "deficit": min_notional - notional
                        }
                    }

            # ═══════════════════════════════════════════════════════════
            # ✅ ИСПРАВЛЕНИЕ #8: КОМИССИЯ С ОКРУГЛЕНИЕМ
            # ═══════════════════════════════════════════════════════════

            # Вычисляем комиссию
            commission_raw = qty * fill_price * Decimal('0.0004')

            # Округляем до 6 знаков (стандарт Binance)
            from decimal import ROUND_DOWN
            commission = commission_raw.quantize(
                Decimal('0.000001'),
                rounding=ROUND_DOWN
            )

            # ═══════════════════════════════════════════════════════════
            # ✅ ИСПРАВЛЕНИЕ #2: ВЫЧИСЛЕНИЕ VALIDATION_HASH
            # ═══════════════════════════════════════════════════════════

            validation_hash = ""
            if risk_context:
                validation_hash = self._compute_validation_hash(risk_context)

            # ═══════════════════════════════════════════════════════════
            # ✅ ИСПРАВЛЕНИЕ #4: ДОБАВЛЕНИЕ CORRELATION_ID
            # ═══════════════════════════════════════════════════════════

            correlation_id = order_req.get("correlation_id")
            if not correlation_id:
                from iqts_standards import create_correlation_id
                correlation_id = create_correlation_id()

            # ═══════════════════════════════════════════════════════════
            # ШАГ 5: СОЗДАНИЕ ORDER UPDATE
            # ═══════════════════════════════════════════════════════════

            order_update = OrderUpd(
                client_order_id=client_order_id,
                exchange_order_id=f"bt_{fill_ts}",
                symbol=symbol,
                side=side,
                type=otype,
                status="FILLED",
                qty=qty,
                price=order_req.get("price"),
                filled_qty=qty,
                avg_price=fill_price,
                commission=commission,  # ✅ Округлённая
                commission_asset="USDT",
                ts_ms_exchange=fill_ts,
                timestamp_ms=fill_ts,
                trade_id=correlation_id,
                correlation_id=correlation_id,  # ✅ Добавлено
                validation_hash=validation_hash,  # ✅ Добавлено
                reduce_only=order_req.get("reduce_only", False),
                metadata={
                    "execution_mode": "BACKTEST",
                    "fill_price": float(fill_price),
                    "notional": notional,
                    "commission_raw": float(commission_raw),
                    "commission_rounded": float(commission),
                    "risk_context": risk_context
                }
            )

            # ═══════════════════════════════════════════════════════════
            # ШАГ 6: ВЫЗОВ CALLBACK
            # ═══════════════════════════════════════════════════════════

            try:
                self.logger.debug(
                    f"🔵 BACKTEST: Calling on_order_update for {client_order_id} "
                    f"(type={otype}, status=FILLED, "
                    f"validation_hash={validation_hash[:8] if validation_hash else 'none'}...)"
                )

                if self.on_order_update:
                    self.on_order_update(order_update)
                else:
                    self.logger.warning(
                        f"⚠️ No on_order_update callback registered for {client_order_id}"
                    )

            except Exception as cb_err:
                self.logger.error(
                    f"❌ Callback error for {client_order_id}: {cb_err}",
                    exc_info=True
                )

            # ═══════════════════════════════════════════════════════════
            # ШАГ 7: ВОЗВРАТ ACK
            # ═══════════════════════════════════════════════════════════

            self._stats["orders_filled"] += 1

            self.logger.info(
                f"✅ BACKTEST order filled: {symbol} {side} "
                f"qty={float(qty):.4f} @ {float(fill_price):.8f} "
                f"(commission={float(commission):.6f} USDT, "
                f"notional={notional:.2f} USDT)"
            )

            return {
                "status": "FILLED",
                "client_order_id": client_order_id,
                "exchange_order_id": f"bt_{fill_ts}",
                "symbol": symbol,
                "side": side,
                "filled_qty": float(qty),
                "avg_price": float(fill_price),
                "commission": float(commission),
                "timestamp_ms": fill_ts,
                "correlation_id": correlation_id,
                "validation_hash": validation_hash
            }

        except Exception as e:
            self._stats["orders_rejected"] += 1
            self.logger.error(
                f"❌ Error placing order: {e}",
                exc_info=True
            )
            return {
                "status": "REJECTED",
                "error": "execution_error",
                "error_message": str(e),
                "client_order_id": order_req.get("client_order_id", "unknown")
            }

    def _compute_validation_hash(self, risk_context: Dict[str, Any]) -> str:
        """
        Вычисление validation_hash для OrderUpd.

        ✅ ДОБАВЛЕНО: Поддержка валидации риск-событий

        Алгоритм соответствует PositionManager._compute_risk_context_hash()
        для консистентной проверки на стороне потребителей.

        Args:
            risk_context: Словарь с параметрами риска

        Returns:
            SHA256 hex строка (64 символа) или пустая строка при ошибке
        """
        import json
        import hashlib

        try:
            # Создаём копию без validation_hash
            ctx_copy = {
                k: v
                for k, v in risk_context.items()
                if k != "validation_hash"
            }

            # Сериализуем с сортировкой ключей
            canonical = json.dumps(
                ctx_copy,
                sort_keys=True,
                default=str,
                ensure_ascii=False,
                separators=(',', ':')
            )

            # Вычисляем SHA256
            hash_bytes = hashlib.sha256(canonical.encode('utf-8')).digest()
            hash_hex = hash_bytes.hex()

            self.logger.debug(
                f"Computed validation_hash: {hash_hex[:8]}...{hash_hex[-8:]}"
            )

            return hash_hex

        except Exception as e:
            self.logger.error(
                f"❌ Error computing validation_hash: {e}",
                exc_info=True
            )
            return ""

    def cancel_order(self, client_order_id: str) -> Dict[str, Any]:
        """Отмена активного ордера."""
        try:
            if client_order_id not in self._active_orders:
                return {
                    "client_order_id": client_order_id,
                    "status": "REJECTED",
                    "timestamp_ms": get_current_timestamp_ms(),
                    "error_message": f"Order {client_order_id} not found"
                }

            if self.demo_mode:
                return self._cancel_order_demo(client_order_id)
            else:
                return self._cancel_order_live(client_order_id)

        except Exception as e:
            self.logger.error(f"Error canceling order {client_order_id}: {e}")
            return {
                "client_order_id": client_order_id,
                "status": "REJECTED",
                "timestamp_ms": get_current_timestamp_ms(),
                "error_message": str(e)
            }

    def _cancel_order_demo(self, client_order_id: str) -> Dict[str, Any]:
        """Отмена ордера в DEMO режиме."""
        try:
            order = self._active_orders.get(client_order_id)
            if not order:
                return {
                    "client_order_id": client_order_id,
                    "status": "REJECTED",
                    "timestamp_ms": get_current_timestamp_ms(),
                    "error_message": f"Order {client_order_id} not found"
                }

            # Отправляем update о cancel
            self._send_order_update(OrderUpd(
                client_order_id=client_order_id,
                exchange_order_id=order.exchange_order_id,
                symbol=order.symbol,
                side=order.side,
                status="CANCELED",
                filled_qty=Decimal('0'),
                avg_price=None,
                commission=None,
                ts_ms_exchange=get_current_timestamp_ms(),
                trade_id=order.correlation_id
            ))

            # Удаляем из активных
            self._remove_active_order(client_order_id)
            self._stats["orders_canceled"] += 1

            self.logger.info(f"Order canceled: {client_order_id}")

            return {
                "client_order_id": client_order_id,
                "status": "CANCELED",
                "timestamp_ms": get_current_timestamp_ms()
            }

        except Exception as e:
            self.logger.error(f"Error canceling demo order {client_order_id}: {e}")
            return {
                "client_order_id": client_order_id,
                "status": "REJECTED",
                "timestamp_ms": get_current_timestamp_ms(),
                "error_message": str(e)
            }

    # === DEMO/BACKTEST режим ===

    def _place_order_demo(self, req: OrderReq) -> Dict[str, Any]:
        """
        Размещение ордера в DEMO режиме (и для регистрации STOP в бэктесте).

        ИЗМЕНЕНИЯ:
        - Устанавливается trigger_price = stop_price для STOP ордеров
        - Улучшена обработка trailing updates
        """

        otype_str = str(req["type"]).upper()
        is_stop_family = otype_str in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET")

        # ✅ Обновляем ТОЛЬКО trailing стопы
        if is_stop_family and req.get("correlation_id"):
            corr_id = str(req.get("correlation_id", ""))

            # Проверяем маркеры trailing update
            is_trailing_update = any(marker in corr_id for marker in ["trail", "update", "trailing"])

            if is_trailing_update:
                try:
                    sp = req.get("stop_price")
                    if sp is not None:
                        self.update_stop_order(
                            symbol=req["symbol"],
                            new_stop_price=sp,
                            correlation_id=corr_id
                        )
                        # ✅ Возвращаем успех сразу
                        return {
                            "client_order_id": req["client_order_id"],
                            "status": "REPLACED",
                            "timestamp_ms": get_current_timestamp_ms()
                        }
                except InvalidOrderError as e:
                    # ✅ НЕ создаём дубликат! Возвращаем ошибку.
                    self.logger.warning(f"Cannot update trailing stop: {e}")
                    return {
                        "client_order_id": req["client_order_id"],
                        "status": "REJECTED",
                        "error_message": str(e),
                        "timestamp_ms": get_current_timestamp_ms()
                    }

        # ✅ Приведение к правильному типу
        from typing import cast
        otype: OrderType = cast(OrderType, otype_str)

        # ✅ ИСПРАВЛЕНО: Устанавливаем trigger_price при создании STOP
        stop_price_value = req.get("stop_price") if is_stop_family else None

        order = ActiveOrder(
            client_order_id=req["client_order_id"],
            symbol=req["symbol"],
            side=req["side"],
            type=otype,
            qty=req["qty"],
            price=req.get("price"),
            stop_price=stop_price_value,
            trigger_price=stop_price_value,  # ✅ НОВОЕ: Копируем stop_price в trigger_price
            correlation_id=req.get("correlation_id"),
            reduce_only=req.get("reduce_only", False),
            exchange_order_id=f"demo_{get_current_timestamp_ms()}"
        )

        # Регистрируем
        self._active_orders[order.client_order_id] = order
        self._orders_by_symbol[order.symbol].add(order.client_order_id)

        # Если это STOP/TP — запускаем монитор (если не используется sync check)
        if is_stop_family and not self._use_sync_stop_check:
            if not self._stop_monitor_active:
                self._stop_monitor_active = True

                def _monitor():
                    self.logger.debug("Stop monitor started")
                    try:
                        while self._stop_monitor_active:
                            for oid in list(self._active_orders.keys()):
                                o = self._active_orders.get(oid)
                                if not o:
                                    continue
                                if o.type in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
                                    if self._check_stop_trigger(o):
                                        o.type = "MARKET"
                                        o.stop_price = None
                                        self._demo_fill_order(o.client_order_id)
                            time.sleep(0.05)
                    except Exception as err:
                        self.logger.error(f"Error in stop monitor: {err}")
                    finally:
                        self.logger.debug("Stop monitor stopped")

                self._stop_monitor_thread = threading.Thread(target=_monitor, daemon=True)
                self._stop_monitor_thread.start()

            # Отправляем рабочий статус
            self._demo_send_working_update(order)
            return {
                "client_order_id": req["client_order_id"],
                "status": "NEW",
                "timestamp_ms": get_current_timestamp_ms()
            }

        # MARKET/LIMIT
        if order.type == "MARKET":
            threading.Timer(self._demo_latency_ms / 1000, self._demo_fill_order, args=[order.client_order_id]).start()
        elif order.type == "LIMIT":
            self._demo_send_working_update(order)

        return {
            "client_order_id": req["client_order_id"],
            "status": "NEW",
            "timestamp_ms": get_current_timestamp_ms()
        }

    def _demo_send_working_update(self, order: ActiveOrder) -> None:
        """Отправка статуса WORKING для DEMO ордера."""
        order.status = "WORKING"
        self._send_order_update(OrderUpd(
            client_order_id=order.client_order_id,
            exchange_order_id=order.exchange_order_id,
            symbol=order.symbol,
            side=order.side,
            status="WORKING",
            filled_qty=Decimal('0'),
            avg_price=None,
            commission=None,
            ts_ms_exchange=get_current_timestamp_ms(),
            trade_id=order.correlation_id,
        ))

    def _calculate_commission(
            self,
            price: Decimal,
            qty: Decimal,
            is_maker: bool = False
    ) -> Decimal:
        """
        Расчёт комиссии для сделки.

        Binance Futures стандартные ставки:
        - Maker: 0.02% (0.0002)
        - Taker: 0.04% (0.0004)

        Args:
            price: Цена исполнения
            qty: Количество
            is_maker: True для LIMIT ордеров (maker), False для MARKET (taker)

        Returns:
            Комиссия в USDT
        """
        # Binance Futures стандартные ставки
        maker_fee_rate = Decimal('0.0002')  # 0.02%
        taker_fee_rate = Decimal('0.0004')  # 0.04%

        fee_rate = maker_fee_rate if is_maker else taker_fee_rate

        # Комиссия = цена * количество * ставка
        commission = price * qty * fee_rate

        self.logger.debug(
            f"Commission calculation:\n"
            f"  Price: {float(price):.8f}\n"
            f"  Qty: {float(qty)}\n"
            f"  Position size: {float(price * qty):.2f} USDT\n"
            f"  Fee type: {'MAKER' if is_maker else 'TAKER'}\n"
            f"  Fee rate: {float(fee_rate):.6f} ({float(fee_rate * 100):.4f}%)\n"
            f"  Commission: {float(commission):.6f} USDT"
        )

        return commission

    def _demo_fill_order(self, client_order_id: str) -> None:
        """
        Эмуляция исполнения ордера в DEMO режиме.

        ИЗМЕНЕНИЯ:
        - Унифицирована логика для STOP ордеров
        - STOP всегда исполняется по trigger_price (без slippage в BACKTEST)
        - MARKET ордера учитывают slippage
        """
        order = self._active_orders.get(client_order_id)
        if not order:
            return

        try:
            # Получаем текущую цену
            current_price = None
            if self._price_feed:
                current_price = self._price_feed(order.symbol)

            if not current_price:
                if order.price:
                    current_price = float(order.price)
                else:
                    self._demo_reject_order(order, "No price available")
                    return

            # ===== ОПРЕДЕЛЕНИЕ ЦЕНЫ ИСПОЛНЕНИЯ =====
            fill_price = None
            slippage = 0.0

            # СЛУЧАЙ 1: Бывший STOP ордер (trigger_price установлен)
            if order.trigger_price is not None:
                fill_price = float(order.trigger_price)

                # В BACKTEST - без slippage, в DEMO - с минимальным slippage
                if not self._is_backtest_mode:
                    slippage = fill_price * self._demo_stop_slippage_pct  # 0.01%
                    if order.side == "BUY":
                        fill_price += slippage
                    else:
                        fill_price -= slippage

                self.logger.info(
                    f"{'BACKTEST' if self._is_backtest_mode else 'DEMO'}: "
                    f"STOP filled at trigger_price: {order.symbol} "
                    f"trigger={order.trigger_price} fill={fill_price:.8f} slippage={slippage:.8f}"
                )

            # СЛУЧАЙ 2: MARKET ордер
            elif order.type == "MARKET":
                fill_price = current_price
                slippage = current_price * self._demo_slippage_pct  # 0.1%
                if order.side == "BUY":
                    fill_price += slippage
                else:
                    fill_price -= slippage

                self.logger.info(
                    f"DEMO: MARKET order filled at {fill_price:.8f} "
                    f"(current={current_price:.8f}, slippage={slippage:.8f})"
                )

            # СЛУЧАЙ 3: LIMIT ордер
            elif order.type in ["LIMIT", "STOP_LIMIT", "TAKE_PROFIT_LIMIT"]:
                if order.price is not None:
                    fill_price = float(order.price)
                else:
                    fill_price = current_price
                slippage = 0.0  # LIMIT исполняется по заявленной цене

                self.logger.info(
                    f"DEMO: LIMIT order filled at {fill_price:.8f} (order.price={order.price})"
                )

            # СЛУЧАЙ 4: Fallback (не должен срабатывать)
            else:
                if order.price is not None:
                    fill_price = float(order.price)
                else:
                    fill_price = current_price
                slippage = 0.0

                self.logger.warning(
                    f"Using fallback price logic for {order.symbol} type={order.type}"
                )

            # ✅ Защита от None
            if fill_price is None:
                fill_price = current_price
                self.logger.error(
                    f"No fill_price determined for {order.symbol}, using current_price"
                )

            # ===== РАСЧЕТ КОМИССИИ =====
            commission = self._calculate_commission(
                price=Decimal(str(fill_price)),
                qty=order.qty,
                is_maker=(order.type == "LIMIT")
            )

            # ===== ЛОГИРОВАНИЕ =====
            self.logger.info(
                f"🔵 SENDING FILL: {order.symbol} {order.type}\n"
                f"  trigger_price: {order.trigger_price}\n"
                f"  order.price: {order.price}\n"
                f"  current_price: {current_price:.8f}\n"
                f"  fill_price: {fill_price:.8f}\n"
                f"  slippage: {slippage:.8f}\n"
                f"  commission: {float(commission):.6f}"
            )

            # ===== ОТПРАВКА ОБНОВЛЕНИЯ =====
            self._send_order_update(OrderUpd(
                client_order_id=order.client_order_id,
                exchange_order_id=order.exchange_order_id,
                symbol=order.symbol,
                side=order.side,
                status="FILLED",
                price=order.price,
                filled_qty=order.qty,
                avg_price=Decimal(str(fill_price)),
                commission=commission,
                ts_ms_exchange=get_current_timestamp_ms(),
                trade_id=order.correlation_id,
                reduce_only=order.reduce_only  # ✅ ДОБАВЛЕНО
            ))

            # ✅ Удаляем ПОСЛЕ отправки
            self._remove_active_order(client_order_id)
            self._stats["orders_filled"] += 1

        except Exception as e:
            self.logger.error(f"Error filling demo order {client_order_id}: {e}")
            self._demo_reject_order(order, str(e))

    def _demo_reject_order(self, order: ActiveOrder, reason: str) -> None:
        """Отклонение ордера в DEMO режиме."""
        self._send_order_update(OrderUpd(
            client_order_id=order.client_order_id,
            exchange_order_id=order.exchange_order_id,
            symbol=order.symbol,
            side=order.side,
            status="REJECTED",
            filled_qty=Decimal('0'),
            avg_price=None,
            commission=None,
            ts_ms_exchange=get_current_timestamp_ms(),
            trade_id=order.correlation_id
        ))

        self._remove_active_order(order.client_order_id)
        self._stats["orders_rejected"] += 1

    # === STOP мониторинг ===

    def check_stops_on_price_update(self, symbol: str, current_price: float) -> None:
        """
        Синхронная принудительная проверка STOP ордеров по заданной цене.
        Работает во всех режимах: BACKTEST, DEMO, LIVE.
        Вызывается из MainBot при закрытии свечи.

        Args:
            symbol: Торговый символ
            current_price: Текущая рыночная цена (для проверки триггера)
        """
        self.logger.warning(
            f"🔍 check_stops_on_price_update CALLED: "
            f"symbol={symbol} current_price={current_price:.8f}"
        )

        # ✅ ИСПРАВЛЕНО: Безопасная итерация по копии списка
        for order_id in list(self._active_orders.keys()):
            order = self._active_orders.get(order_id)
            if not order or order.symbol != symbol:
                continue
            if order.type not in ["STOP", "STOP_MARKET"]:
                continue

            if self._check_stop_trigger_with_price(order, current_price):
                self.logger.info(f"✅ STOP triggered by sync check for {symbol}")

                stop_price = order.stop_price
                if not stop_price:
                    self.logger.error(f"STOP order has no stop_price: {order_id}")
                    # ✅ КРИТИЧНО: Удаляем битый ордер!
                    self._remove_active_order(order_id)
                    break

                # ✅ КРИТИЧНО: Удаляем СНАЧАЛА (защита от повторного срабатывания)
                self._remove_active_order(order_id)

                # Затем исполняем
                try:
                    self._trigger_stop_order(order, execution_price=float(stop_price))
                except Exception as e:
                    self.logger.error(
                        f"Error triggering stop {order_id}: {e}. "
                        f"Order already removed, won't retry."
                    )

                break

    def _check_stop_trigger_with_price(self, order: ActiveOrder, current_price: float) -> bool:
        """Проверка триггера STOP с явно переданной ценой."""
        if not order.stop_price:
            return False

        stop_price = float(order.stop_price)
        tolerance = 0.0001

        is_closing_long = (order.side == "SELL" and order.reduce_only)
        is_closing_short = (order.side == "BUY" and order.reduce_only)

        triggered = False

        if order.type in ["STOP", "STOP_MARKET"]:
            if is_closing_long:
                triggered = current_price <= stop_price * (1 + tolerance)
            elif is_closing_short:
                triggered = current_price >= stop_price * (1 - tolerance)
            else:
                if order.side == "BUY":
                    triggered = current_price >= stop_price * (1 - tolerance)
                else:
                    triggered = current_price <= stop_price * (1 + tolerance)

        elif order.type in ["TAKE_PROFIT", "TAKE_PROFIT_MARKET"]:
            if is_closing_long:
                triggered = current_price >= stop_price * (1 - tolerance)
            elif is_closing_short:
                triggered = current_price <= stop_price * (1 + tolerance)
            else:
                if order.side == "BUY":
                    triggered = current_price <= stop_price * (1 + tolerance)
                else:
                    triggered = current_price >= stop_price * (1 - tolerance)

        return triggered

    def _ensure_stop_monitor_running(self) -> None:
        """Обеспечить работу монитора STOP ордеров."""
        if not self._stop_monitor_active and self.demo_mode:
            self._stop_monitor_active = True
            self._stop_monitor_thread = threading.Thread(target=self._stop_monitor_loop, daemon=True)
            self._stop_monitor_thread.start()
            self.logger.debug("STOP monitor started")

    def _stop_monitor_loop(self) -> None:
        """Основной цикл мониторинга STOP ордеров."""
        while self._stop_monitor_active:
            try:
                # Создаем копию списка для безопасной итерации
                active_order_ids = list(self._active_orders.keys())

                for order_id in active_order_ids:
                    order = self._active_orders.get(order_id)
                    if not order:
                        continue

                    if order.type not in ["STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"]:
                        continue

                    if self._check_stop_trigger(order):
                        self._remove_active_order(order_id)

                        # Затем исполняем
                        if order.stop_price:
                            try:
                                self._trigger_stop_order(order, execution_price=float(order.stop_price))
                            except Exception as e:
                                self.logger.error(f"Error triggering stop {order_id}: {e}")
                        else:
                            self.logger.error(f"Stop order {order_id} has no stop_price")

                # Спим 100мс
                time.sleep(0.1)

            except Exception as e:
                self.logger.error(f"Error in stop monitor: {e}")
                time.sleep(1)

    def _check_stop_trigger(self, order: ActiveOrder) -> bool:
        """Проверить, сработал ли STOP ордер."""
        # Валидация основных условий
        if not self._price_feed or not callable(self._price_feed):
            self.logger.debug(f"Stop check skipped: price_feed not available for {order.client_order_id}")
            return False

        if not order.stop_price:
            self.logger.debug(f"Stop check skipped: no stop_price for {order.client_order_id}")
            return False

        # Получение текущей цены с обработкой ошибок
        try:
            current_price = self._price_feed(order.symbol)
        except Exception as e:
            self.logger.error(f"Error calling price_feed for {order.symbol}: {e}")
            return False

        if not current_price:
            self.logger.debug(f"Stop check skipped: no current price for {order.symbol}")
            return False

        # Конвертация цен с обработкой ошибок
        try:
            stop_price = float(order.stop_price)
            current_price_float = float(current_price)
        except (ValueError, TypeError) as e:
            self.logger.error(f"Error converting prices for {order.symbol}: {e}")
            return False

        # Tolerance для избежания проблем с точностью float (0.01%)
        tolerance = 0.0001

        # Периодическое логирование для мониторинга
        if not hasattr(self, '_stop_check_counter'):
            self._stop_check_counter = {}

        order_id = order.client_order_id
        self._stop_check_counter[order_id] = self._stop_check_counter.get(order_id, 0) + 1

        if self._stop_check_counter[order_id] % 10 == 0:
            self.logger.debug(
                f"Monitoring {order.type} {order.side} reduce_only={order.reduce_only}: {order.symbol} "
                f"current={current_price_float:.8f} stop={stop_price:.8f}"
            )

        # ✅ ИСПРАВЛЕНО: Определяем направление позиции по reduce_only + side
        is_closing_long = (order.side == "SELL" and order.reduce_only)
        is_closing_short = (order.side == "BUY" and order.reduce_only)

        triggered = False

        if order.type in ["STOP", "STOP_MARKET"]:
            if is_closing_long:
                # Закрываем LONG когда цена падает НИЖЕ stop_price
                triggered = current_price_float <= stop_price * (1 + tolerance)

            elif is_closing_short:
                # Закрываем SHORT когда цена растет ВЫШЕ stop_price
                triggered = current_price_float >= stop_price * (1 - tolerance)

            else:
                # Открывающий STOP ордер (не reduce_only)
                if order.side == "BUY":
                    # Стоп на покупку срабатывает, когда цена поднялась выше stop_price
                    triggered = current_price_float >= stop_price * (1 - tolerance)
                else:  # SELL
                    # Стоп на продажу срабатывает, когда цена опустилась ниже stop_price
                    triggered = current_price_float <= stop_price * (1 + tolerance)

        elif order.type in ["TAKE_PROFIT", "TAKE_PROFIT_MARKET"]:
            if is_closing_long:
                # Тейк-профит для LONG срабатывает когда цена растет ВЫШЕ target
                triggered = current_price_float >= stop_price * (1 - tolerance)

            elif is_closing_short:
                # Тейк-профит для SHORT срабатывает когда цена падает НИЖЕ target
                triggered = current_price_float <= stop_price * (1 + tolerance)

            else:
                # Открывающий TAKE_PROFIT (редкий случай)
                if order.side == "BUY":
                    triggered = current_price_float <= stop_price * (1 + tolerance)
                else:  # SELL
                    triggered = current_price_float >= stop_price * (1 - tolerance)

        # Логирование при срабатывании
        if triggered:
            position_direction = "LONG" if is_closing_long else ("SHORT" if is_closing_short else "OPEN")
            self.logger.info(
                f"STOP TRIGGERED: {order.type} closing {position_direction} {order.symbol} "
                f"current_price={current_price_float:.8f} stop_price={stop_price:.8f} "
                f"order_id={order_id}"
            )
            # Очистка счётчика при срабатывании
            if order_id in self._stop_check_counter:
                del self._stop_check_counter[order_id]

        return triggered


    def _trigger_stop_order(self, order: ActiveOrder, execution_price: float) -> None:
        """
        Исполнение стоп-ордера с ПРЯМЫМ вызовом callback.
        - Прямой вызов self.on_order_update() гарантирует отправку fill
        - Комиссия: 0.04% taker fee
        """
        # Захватываем идентификаторы РАНЬШЕ try, чтобы использовать в except
        client_order_id = getattr(order, "client_order_id", None)
        symbol = getattr(order, "symbol", "?")

        try:
            self.logger.info(f"🔴 _trigger_stop_order: {symbol} {order.side} @ {execution_price:.8f}")

            # === Цена/кол-во как Decimal ===
            fill_price = Decimal(str(execution_price))
            filled_qty = order.qty if isinstance(order.qty, Decimal) else Decimal(str(order.qty))

            # === Комиссия (0.04%) ===
            commission = (fill_price * filled_qty * Decimal("0.0004"))

            self.logger.debug(
                "  Execution details:\n"
                f"    client_order_id: {client_order_id}\n"
                f"    fill_price: {float(fill_price):.8f}\n"
                f"    qty: {float(filled_qty)}\n"
                f"    commission: {float(commission):.6f}\n"
                f"    reduce_only: True"
            )

            # Готовим OrderUpd (приводим типы к Decimal где нужно)
            avg_price = fill_price
            price = order.price if isinstance(order.price, Decimal) else (
                Decimal(str(order.price)) if order.price is not None else None)

            fill = OrderUpd(
                client_order_id=client_order_id,
                exchange_order_id=order.exchange_order_id or f"stop_{get_current_timestamp_ms()}",
                symbol=symbol,
                side=order.side,
                status="FILLED",
                qty=filled_qty,  # если в модели ожидается qty: Decimal
                price=price,  # исходная лимит-цена ордера (если была)
                filled_qty=filled_qty,  # Decimal (исправлено)
                avg_price=avg_price,  # Decimal
                commission=commission,  # Decimal
                reduce_only=True,
                trade_id=f"stop_{symbol}_{get_current_timestamp_ms()}",
                correlation_id=order.correlation_id,
                ts_ms_exchange=get_current_timestamp_ms(),
            )

            # Прямой вызов callback
            self.logger.info(f"🔵 Calling on_order_update for STOP fill: {client_order_id}")
            try:
                self.on_order_update(fill)
                self.logger.info(f"✅ Callback executed successfully for STOP {client_order_id}")
            except Exception as callback_error:
                self.logger.error(f"❌ Callback error for {client_order_id}: {callback_error}", exc_info=True)
                # не прерываем — продолжим удаление ордера

            # Удаляем из активных
            self._remove_active_order(client_order_id)
            self._stats["orders_filled"] += 1
            self.logger.info(f"✅ STOP order fully executed: {symbol} {order.side} @ {float(fill_price):.8f}")

        except Exception as e:
            self.logger.error(f"❌ Error in _trigger_stop_order for {symbol}: {e}", exc_info=True)
            # Защищённо используем client_order_id в except
            if client_order_id and client_order_id in self._active_orders:
                self._remove_active_order(client_order_id)

    def update_stop_order(self, symbol: str, new_stop_price: Decimal, correlation_id: str) -> None:
        """
        Обновить активный STOP ордер для символа.

        Ищет существующий STOP по symbol + type (не по correlation_id,
        т.к. каждый trailing update создаёт новый correlation_id).
        """
        # Ищем активный STOP ордер для символа
        for order in self._active_orders.values():
            if (order.symbol == symbol and
                    order.type in ["STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"]):
                old_price = order.stop_price
                order.stop_price = new_stop_price

                # Обновляем correlation_id для отслеживания
                order.correlation_id = correlation_id

                self.logger.info(
                    f"✅ Updated STOP order for {symbol}: "
                    f"{float(old_price):.8f} → {float(new_stop_price):.8f} "
                    f"(client_order_id={order.client_order_id})"
                )
                return

        # Не найден - это ошибка (должен быть создан initial stop)
        raise InvalidOrderError(
            f"No active STOP order found for {symbol}. "
            f"Ensure initial stop was created on position open."
        )

    # === LIVE режим (заглушки) ===

    def _place_order_live(self, req: OrderReq) -> Dict[str, Any]:  # ✅ Было: OrderAck
        """Размещение ордера через API биржи."""
        self.logger.info(
            f"Placing LIVE order via {self.base_url} "
            f"(timeout={self.timeout_seconds})"
        )
        # Реализация для реальной биржи
        self.logger.warning("LIVE mode not implemented, falling back to DEMO")
        return self._place_order_demo(req)

    def _cancel_order_live(self, client_order_id: str) -> Dict[str, Any]:  # ✅ Было: OrderAck
        """Отмена ордера через API биржи."""
        self.logger.info(
            f"Cancelling LIVE order via {self.base_url} "
            f"(timeout={self.timeout_seconds})"
        )
        # Реализация для реальной биржи
        self.logger.warning("LIVE mode not implemented, falling back to DEMO")
        return self._cancel_order_demo(client_order_id)

    def connect_user_stream(self) -> None:
        """Подключение к user-data stream."""
        if self.demo_mode:
            self.logger.info("DEMO mode: user stream connection skipped")
            self._connection_state.status = "connected"
            self._connection_state.connected_at = get_current_timestamp_ms()
        else:
            # >>> ДОБАВИТЬ: логирование ws_url / timeout
            self.logger.info(
                f"Connecting to LIVE user stream: ws_url={self.ws_url}, timeout={self.timeout_seconds}"
            )
            #  здесь будет реальная реализация подключения к ws_url
            self.logger.warning("LIVE user stream not implemented")
            raise ConnectionError("LIVE user stream not implemented")

    def disconnect_user_stream(self) -> None:
        """Отключение от user-data stream."""
        if self.demo_mode:
            self._stop_monitor_active = False
            if self._stop_monitor_thread:
                self._stop_monitor_thread.join(timeout=1)
            self._connection_state.status = "disconnected"
            self.logger.info("DEMO mode: user stream disconnected")
        else:
            #  Реализация для реальной биржи
            self.logger.warning("LIVE user stream disconnect not implemented")

    # === Вспомогательные методы ===

    def set_price_feed_callback(self, cb: PriceFeed) -> None:
        """Источник цен для DEMO/STOP мониторинга."""
        self._price_feed = cb

    from typing import Literal

    OrderTypeLiteral = Literal["MARKET", "LIMIT", "STOP_MARKET", "STOP", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"]

    def _validate_order_req(self, req: OrderReq) -> None:
        """Валидация OrderReq перед отправкой."""

        # 1) Обязательные поля (ключи — литерально, без циклов по строкам)
        if not req.get("client_order_id"):
            raise InvalidOrderError("Missing required field: client_order_id")
        if not req.get("symbol"):
            raise InvalidOrderError("Missing required field: symbol")
        if req.get("side") is None:
            raise InvalidOrderError("Missing required field: side")
        if req.get("type") is None:
            raise InvalidOrderError("Missing required field: type")
        if req.get("qty") is None:
            raise InvalidOrderError("Missing required field: qty")

        # 2) Значения полей
        # qty > 0 (в проекте qty — Decimal)
        qty: Decimal = req["qty"]
        if qty <= Decimal("0"):
            raise InvalidOrderError("Quantity must be positive")

        # side ∈ {"BUY","SELL"}
        side = str(req["side"]).upper()
        if side not in ("BUY", "SELL"):
            raise InvalidOrderError(f"Invalid side: {req['side']}")

        # type ∈ допустимом списке
        otype = str(req["type"]).upper()
        valid_types: tuple[OrderType, ...] = (
            "MARKET", "LIMIT", "STOP_MARKET", "STOP", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"
        )
        if otype not in valid_types:
            raise InvalidOrderError(f"Invalid order type: {req['type']}")

        # 3) Специфические проверки
        # LIMIT требует price
        if otype == "LIMIT" and req.get("price") is None:
            raise InvalidOrderError("LIMIT orders require price")

        # STOP/TAKE_PROFIT требуют stop_price
        if otype in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET") and req.get("stop_price") is None:
            raise InvalidOrderError(f"{otype} orders require stop_price")

        # Дополнительные мягкие проверки (по желанию можно закомментировать)
        if req.get("price") is not None:
            price: Decimal = req["price"]  # в проекте price — Decimal|None
            if price <= Decimal("0"):
                raise InvalidOrderError("Price must be positive")

        if req.get("stop_price") is not None:
            sp: Decimal = req["stop_price"]
            if sp <= Decimal("0"):
                raise InvalidOrderError("stop_price must be positive")

        # reduce_only — булево, если присутствует
        if "reduce_only" in req and req["reduce_only"] is not None and not isinstance(req["reduce_only"], bool):
            raise InvalidOrderError("reduce_only must be a boolean if specified")

    def _send_order_update(self, update: OrderUpd) -> None:
        """Отправка обновления ордера через callback."""
        try:
            self.on_order_update(update)

            # Эмитим событие
            self._emit_event(ExchangeEvent(
                event_type="ORDER_UPDATE_RECEIVED",
                timestamp_ms=get_current_timestamp_ms(),
                data={
                    "client_order_id": update["client_order_id"],
                    "status": update["status"],
                    "filled_qty": float(update.get("filled_qty", 0))
                }
            ))

        except Exception as e:
            self.logger.error(f"Error in order update callback: {e}")

    def _remove_active_order(self, client_order_id: str) -> None:
        """Удаление ордера из активных."""
        order = self._active_orders.pop(client_order_id, None)
        if order:
            self._orders_by_symbol[order.symbol].discard(client_order_id)
            if order.type in ["STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"]:
                self._stats["active_stops"] = max(0, self._stats["active_stops"] - 1)

            # ✅ НОВОЕ: Очистка счетчика логирования стопов
            if hasattr(self, '_stop_check_counter') and client_order_id in self._stop_check_counter:
                del self._stop_check_counter[client_order_id]
                self.logger.debug(f"Cleared stop check counter for {client_order_id}")

    # === Публичные методы диагностики ===

    def get_connection_state(self) -> Dict[str, Any]:  # ✅ Изменили тип возврата
        """
        Для DEMO считаем соединение логически 'CONNECTED', чтобы health-check не валился.
        Для LIVE можно вернуть реальный state.
        """
        if self.demo_mode:
            return {
                "status": "CONNECTED",
                "last_heartbeat": get_current_timestamp_ms(),
                "reconnect_count": 0,
                "error_message": None,
                "connected_at": self._stats.get("last_order_ts") or get_current_timestamp_ms(),
                "last_error_at": None,
            }

        # LIVE режим — конвертируем dataclass в словарь
        from dataclasses import asdict
        return asdict(self._connection_state)

    def get_stats(self) -> Dict[str, Any]:
        """Счётчики и статистика работы."""
        avg_latency = 0.0
        if self._stats["latency_samples"] > 0:
            avg_latency = self._stats["total_latency_ms"] / self._stats["latency_samples"]
        state = self.get_connection_state()
        return {
            **self._stats,
            "avg_latency_ms": round(avg_latency, 2),
            "active_orders_count": len(self._active_orders),
            "connection_state": state["status"].lower(),
            "demo_mode": self.demo_mode,
            "uptime_seconds": self._get_uptime_seconds()
        }

    def _get_uptime_seconds(self) -> int:
        """Время работы в секундах."""
        if self._connection_state.connected_at:
            return int((get_current_timestamp_ms() - self._connection_state.connected_at) / 1000)
        return 0

    def reset_for_backtest(self) -> None:
        """Очистить внутренние очереди/мониторы/кэши перед прогоном истории."""
        # Останавливаем мониторы
        self._stop_monitor_active = False
        if self._stop_monitor_thread:
            self._stop_monitor_thread.join(timeout=1)

        # Очищаем состояние
        self._active_orders.clear()
        self._orders_by_symbol.clear()

        # Сбрасываем статистику
        self._stats = {
            "orders_sent": 0,
            "orders_filled": 0,
            "orders_rejected": 0,
            "orders_canceled": 0,
            "reconnects_count": 0,
            "total_latency_ms": 0,
            "latency_samples": 0,
            "active_stops": 0,
            "last_order_ts": None
        }

        # Сбрасываем соединение
        self._connection_state = ConnectionState()

        self.logger.info("ExchangeManager reset for backtest")

    def get_active_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Получить список активных ордеров.

        ИЗМЕНЕНИЯ:
        - Добавлено поле reduce_only в возвращаемый словарь
        """
        orders = []
        for order in self._active_orders.values():
            if symbol is None or order.symbol == symbol:
                orders.append({
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "side": order.side,
                    "type": order.type,
                    "qty": float(order.qty),
                    "price": float(order.price) if order.price else None,
                    "stop_price": float(order.stop_price) if order.stop_price else None,
                    "status": order.status,
                    "filled_qty": float(order.filled_qty),
                    "correlation_id": order.correlation_id,
                    "reduce_only": order.reduce_only  # ✅ ДОБАВЛЕНО
                })
        return orders
