"""
PositionManager.py - единый владелец состояния позиций и PnL
TradeSignal → OrderReq, ведет учет исполнения
"""

from __future__ import annotations
from typing import Optional, List, cast
from decimal import Decimal, InvalidOperation
from typing import Dict, Any
import logging
from dataclasses import dataclass, field
from typing import Literal
from sqlalchemy.engine import Engine, create_engine
import threading
from collections import deque
from iqts_standards import (
     OrderReq, OrderUpd, PositionSnapshot, PositionEvent,
    TradeSignalIQTS, PriceFeed, EventHandler,
    get_current_timestamp_ms, create_correlation_id,
    ExchangeManagerInterface
)
import asyncio
from risk_manager import Direction
from exit_system import AdaptiveExitManager
from config import STRATEGY_PARAMS

# === Исключения ===
logger = logging.getLogger(__name__)
class PositionManagerError(Exception):
    """Базовая ошибка PositionManager"""
    pass


class InvalidSignalError(PositionManagerError):
    """Некорректный торговый сигнал"""
    pass


class InsufficientFundsError(PositionManagerError):
    """Недостаточно средств"""
    pass


class PositionNotFoundError(PositionManagerError):
    """Позиция не найдена"""
    pass


class InvalidOrderSizeError(PositionManagerError):
    """Некорректный размер ордера"""
    pass


# === Внутренние типы ===

@dataclass
class SymbolMeta:
    """Метаданные символа для торговли"""
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal
    price_precision: int
    quantity_precision: int
    leverage_max: int = 20
    leverage_default: int = 10


@dataclass
class PendingOrder:
    """Ожидающий ордер"""
    client_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    type: str
    qty: Decimal
    price: Optional[Decimal]
    correlation_id: str
    created_at: int = field(default_factory=get_current_timestamp_ms)
    stop_price: Optional[Decimal] = None
    reduce_only: bool = False
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class PMStats:
    """Статистика PositionManager"""
    signals_processed: int = 0
    orders_created: int = 0
    positions_opened: int = 0
    positions_closed: int = 0
    fills_processed: int = 0
    duplicate_signals: int = 0
    invalid_signals: int = 0
    total_realized_pnl: Decimal = Decimal('0')
    last_signal_ts: Optional[int] = None


class PositionManager:
    """
    Единый владелец состояния позиций и PnL.
    Преобразует TradeSignal → OrderReq, ведет учет исполнения.
    """

    def __init__(self,
                 symbols_meta: Dict[str, Dict[str, Any]],
                 db_dsn: str,
                 trade_log: Any,
                 *,
                 price_feed: Optional[PriceFeed] = None,
                 execution_mode: Literal["LIVE", "DEMO", "BACKTEST"] = "DEMO",
                 db_engine: Optional[Engine] = None,
                 signal_validator: Optional[Any] = None,
                 exit_manager: Optional[AdaptiveExitManager] = None):

        self.exchange_manager: Optional[ExchangeManagerInterface] = None
        self.exit_manager: Optional[AdaptiveExitManager] = None
        # Dependency Injection: SignalValidator
        self.signal_validator = signal_validator

        # Основные параметры
        self.symbols_meta = self._parse_symbols_meta(symbols_meta)
        self.db_dsn = db_dsn
        self.trade_log = trade_log
        self.price_feed = price_feed
        self.execution_mode = execution_mode
        self.logger = logger
        self._position_ids: Dict[str, int] = {}
        self._init_position_ids_cache()
        # Database
        self.engine = db_engine or create_engine(db_dsn)

        # Event system
        self._event_handlers: List[EventHandler] = []
        self._active_stop_orders: Dict[str, Dict[str, Any]] = {}
        # Внутреннее состояние
        self._positions: Dict[str, PositionSnapshot] = {}
        self._pending_orders: Dict[str, PendingOrder] = {}
        self._processed_correlations: deque = deque(maxlen=5000)

        # Статистика
        self._stats = PMStats()

        # Конфигурация
        self._default_balance = Decimal('10000')  # Для DEMO/BACKTEST
        self._position_size_percent = Decimal('20')  # 20% от баланса
        self._max_positions = 5
        self._order_counter = 0
        self._lock = threading.RLock()
        self.logger.info(f"PositionManager initialized: mode={execution_mode}, symbols={len(self.symbols_meta)}")

    def set_exchange_manager(self, em: ExchangeManagerInterface) -> None:
        """Установить ссылку на ExchangeManager для работы со стоп-ордерами."""
        self.exchange_manager = em
        self.logger.info("ExchangeManager linked to PositionManager")

    def _generate_unique_order_id(self, symbol: str, prefix: str = "entry") -> str:
        """Генерация гарантированно уникального order ID"""
        with self._lock:
            self._order_counter += 1
            timestamp = get_current_timestamp_ms()
            return f"{prefix}_{symbol}_{timestamp}_{self._order_counter}"

    def _parse_symbols_meta(self, symbols_meta: Dict[str, Dict[str, Any]]) -> Dict[str, SymbolMeta]:
        """Парсинг метаданных символов в типизированные объекты"""
        parsed = {}
        for symbol, meta in symbols_meta.items():
            try:
                parsed[symbol] = SymbolMeta(
                    symbol=symbol,
                    tick_size=Decimal(str(meta.get("tick_size", "0.01"))),
                    step_size=Decimal(str(meta.get("step_size", "0.001"))),
                    min_notional=Decimal(str(meta.get("min_notional", "5.0"))),
                    price_precision=int(meta.get("price_precision", 2)),
                    quantity_precision=int(meta.get("quantity_precision", 3)),
                    leverage_max=int(meta.get("leverage_max", 20)),
                    leverage_default=int(meta.get("leverage_default", 10))
                )
            except Exception as e:
                self.logger.error(f"Error parsing meta for {symbol}: {e}")
                # Используем значения по умолчанию
                parsed[symbol] = SymbolMeta(
                    symbol=symbol,
                    tick_size=Decimal("0.01"),
                    step_size=Decimal("0.001"),
                    min_notional=Decimal("5.0"),
                    price_precision=2,
                    quantity_precision=3
                )
        return parsed

    # === Event System ===

    def add_event_handler(self, handler: EventHandler) -> None:
        """Добавить обработчик событий позиций"""
        if handler not in self._event_handlers:
            self._event_handlers.append(handler)
            self.logger.debug(f"Added position event handler: {handler}")

    def remove_event_handler(self, handler: EventHandler) -> None:
        """Удалить обработчик событий позиций"""
        if handler in self._event_handlers:
            self._event_handlers.remove(handler)
            self.logger.debug(f"Removed position event handler: {handler}")

    def _emit_event(self, event: PositionEvent) -> None:
        """Внутренний метод эмиссии события всем подписчикам"""
        for handler in self._event_handlers:
            try:
                handler(event)
            except Exception as e:
                self.logger.error(f"Error in position event handler: {e}")

    # === Главный интерфейс ===

    def handle_signal(self, signal: TradeSignalIQTS) -> Optional[OrderReq]:
        """Преобразовать сигнал в OrderReq и сохранить в БД."""
        try:
            self._validate_signal(signal)
            
            # Проверка целостности risk_context (если есть validation_hash)
            if not self._verify_risk_context(signal):
                self._stats.invalid_signals += 1
                self.logger.error(
                    f"Signal rejected due to risk_context tampering: {signal.get('symbol')}"
                )
                return None

            # Проверка дедупликации
            correlation_id = signal.get("correlation_id")
            if correlation_id and correlation_id in self._processed_correlations:
                self._stats.duplicate_signals += 1
                self.logger.debug(f"Duplicate signal ignored: {correlation_id}")
                return None

            self._stats.signals_processed += 1
            self._stats.last_signal_ts = get_current_timestamp_ms()

            # Получаем текущую позицию
            symbol = signal["symbol"]
            current_position = self.get_position(symbol)

            # Обрабатываем по типу сигнала
            order_req = None
            intent = signal["intent"]
            position_id = None

            if intent in ["LONG_OPEN", "SHORT_OPEN"]:
                order_req = self._handle_open_signal(signal, current_position)

            elif intent in ["LONG_CLOSE", "SHORT_CLOSE"]:
                order_req = self._handle_close_signal(signal, current_position)
                # ✅ ИСПРАВЛЕНИЕ: Получаем position_id для закрытия
                if symbol in self._position_ids:
                    position_id = self._position_ids[symbol]

            elif intent == "WAIT":
                order_req = self._handle_wait_signal(signal, current_position)
                # ✅ ИСПРАВЛЕНИЕ: Получаем position_id для стоп-ордера
                if symbol in self._position_ids:
                    position_id = self._position_ids[symbol]

            elif intent == "HOLD":
                pass
            else:
                raise InvalidSignalError(f"Unknown signal intent: {intent}")

            if correlation_id:
                self._processed_correlations.append(correlation_id)

            if order_req:
                if hasattr(self, 'trade_log') and self.trade_log:
                    try:
                        # ✅ Теперь position_id передаётся правильно
                        success = self.trade_log.create_order_from_req(order_req, position_id=position_id)
                        if success:
                            self._stats.orders_created += 1
                            self.logger.info(
                                f"Created and persisted order: {intent} for {symbol} "
                                f"(position_id={position_id})"  # ✅ Логируем для проверки
                            )
                        else:
                            self.logger.error(f"Failed to persist order: {order_req['client_order_id']}")
                            return None
                    except Exception as e:
                        self.logger.error(f"Failed to persist order: {e}")
                        return None
                else:
                    self.logger.warning("No trade_log available for order persistence")

                # Эмитим событие
                self._emit_event(PositionEvent(
                    event_type="ORDER_CREATED_FROM_SIGNAL",
                    symbol=symbol,
                    timestamp_ms=get_current_timestamp_ms(),
                    correlation_id=correlation_id,
                    position_data={
                        "signal_intent": intent,
                        "order_type": order_req["type"],
                        "order_side": order_req["side"],
                        "qty": float(order_req["qty"]),
                        "position_id": position_id
                    }
                ))

            return order_req

        except InvalidSignalError as e:
            self._stats.invalid_signals += 1
            self.logger.warning(f"Invalid signal: {e}")
            return None
        except Exception as e:
            self._stats.invalid_signals += 1
            self.logger.error(f"Error handling signal: {e}")
            return None

    def _handle_open_signal(self, signal: TradeSignalIQTS, current_position: PositionSnapshot) -> Optional[OrderReq]:
        """Обработка сигнала открытия позиции"""
        from typing import cast, Literal

        symbol = signal["symbol"]
        intent = signal["intent"]

        # Проверяем, что нет противоположной позиции
        if current_position["status"] != "FLAT":
            current_side = current_position.get("side")
            signal_side = "LONG" if intent == "LONG_OPEN" else "SHORT"

            if current_side == signal_side:
                self.logger.debug(f"Position already open in same direction: {symbol} {current_side}")
                return None
            else:
                # Есть противоположная позиция - нужно сначала закрыть
                self.logger.warning(f"Cannot open {signal_side}, opposite position exists: {symbol} {current_side}")
                return None

        side: Literal["BUY", "SELL"] = cast(
            Literal["BUY", "SELL"],
            "BUY" if intent == "LONG_OPEN" else "SELL"
        )
        return self.build_entry_order(signal, side)

    def _handle_close_signal(self, signal: TradeSignalIQTS, current_position: PositionSnapshot) -> Optional[OrderReq]:
        """Обработка сигнала закрытия позиции"""
        symbol = signal["symbol"]

        # Проверяем наличие позиции
        if current_position["status"] == "FLAT":
            self.logger.debug(f"No position to close: {symbol}")
            return None

        return self.build_exit_order(signal, current_position, "SIGNAL_EXIT")

    def _handle_wait_signal(
            self,
            signal: TradeSignalIQTS,
            current_position: PositionSnapshot
    ) -> Optional[OrderReq]:
        """
        Обработка WAIT сигнала с вычислением trailing stop.

        ✅ UPDATED: Uses AdaptiveExitManager.calculate_trailing_stop() (v2.1+)
        """
        # ✅ ИСПРАВЛЕНИЕ: Инициализируем symbol ДО try блока
        symbol = signal.get("symbol", "UNKNOWN")

        try:
            # ✅ Теперь symbol всегда определён
            if not symbol or symbol == "UNKNOWN":
                self.logger.error("❌ Missing or invalid symbol in signal")
                return None

            if current_position["status"] == "FLAT":
                return None

            position_side = current_position["side"]
            if not position_side:
                self.logger.error(f"Position side is None for {symbol}")
                return None

            trailing_request = signal.get("metadata", {}).get("trailing_update_request")
            if not trailing_request:
                return None

            # ═══════════════════════════════════════════════════════════
            # ✅ ПРОВЕРКА: exit_manager должен быть установлен
            # ═══════════════════════════════════════════════════════════

            if not hasattr(self, 'exit_manager') or not self.exit_manager:
                self.logger.error(
                    f"❌ CRITICAL: exit_manager not set for PositionManager! "
                    f"Cannot calculate trailing stop for {symbol}. "
                    f"Skipping WAIT signal processing."
                )
                return None

            # ═══════════════════════════════════════════════════════════
            # ✅ ВЫЧИСЛЕНИЕ TRAILING STOP через AdaptiveExitManager
            # ═══════════════════════════════════════════════════════════

            current_price = float(signal["decision_price"])
            entry_price = trailing_request.get("entry_price")
            max_pnl_percent = trailing_request.get("max_pnl_percent")
            current_stop = self._get_current_stop_price(symbol)

            self.logger.debug(
                f"Calculating trailing stop via AdaptiveExitManager for {symbol}:\n"
                f"  current_price: {current_price}\n"
                f"  entry_price: {entry_price}\n"
                f"  max_pnl_percent: {max_pnl_percent}\n"
                f"  current_stop: {current_stop}\n"
                f"  side: {position_side}"
            )

            # Вызываем метод exit_manager
            result = self.exit_manager.calculate_trailing_stop(
                current_price=current_price,
                entry_price=entry_price,
                side=position_side,
                max_pnl_percent=max_pnl_percent,
                current_stop_price=current_stop,
                symbol=symbol
            )

            # ═══════════════════════════════════════════════════════════
            # ✅ ОБРАБОТКА РЕЗУЛЬТАТА
            # ═══════════════════════════════════════════════════════════

            if not result or not isinstance(result, dict):
                self.logger.warning(
                    f"⚠️ exit_manager.calculate_trailing_stop returned invalid result: {result}"
                )
                return None

            new_stop_price_float = result.get("new_stop_price")

            if new_stop_price_float is None:
                self.logger.debug(
                    f"No trailing stop update needed for {symbol} "
                    f"(reason: {result.get('reason', 'unknown')})"
                )
                return None

            # ═══════════════════════════════════════════════════════════
            # ✅ СОЗДАНИЕ ОРДЕРА НА ОБНОВЛЕНИЕ СТОПА
            # ═══════════════════════════════════════════════════════════

            # Валидация новой цены стопа
            if new_stop_price_float <= 0:
                self.logger.error(
                    f"❌ Invalid trailing stop price: {new_stop_price_float}"
                )
                return None

            # Квантуем цену
            new_stop_price = self.quantize_price(symbol, Decimal(str(new_stop_price_float)))

            # Проверяем что цена изменилась
            if current_stop and abs(float(new_stop_price) - current_stop) < 0.00000001:
                self.logger.debug(
                    f"Trailing stop price unchanged for {symbol}: {current_stop}"
                )
                return None

            # Генерируем ID для нового стоп-ордера
            client_order_id = self._generate_unique_order_id(symbol, "trail_stop")

            # Определяем сторону стопа
            stop_side: Literal["BUY", "SELL"] = "SELL" if position_side == "LONG" else "BUY"

            # Создаём OrderReq
            order_req = OrderReq(
                client_order_id=client_order_id,
                symbol=symbol,
                side=stop_side,
                type="STOP_MARKET",
                qty=current_position["qty"],
                price=None,
                stop_price=new_stop_price,
                time_in_force="GTC",
                reduce_only=True,
                correlation_id=signal.get("correlation_id", create_correlation_id()),
                metadata={
                    "reason": "trailing_stop_update",
                    "previous_stop": current_stop,
                    "new_stop": float(new_stop_price),
                    "max_pnl_percent": max_pnl_percent,
                    "entry_price": entry_price
                }
            )

            self.logger.info(
                f"✅ Trailing stop update for {symbol}: "
                f"{current_stop} → {float(new_stop_price)} "
                f"(distance: {result.get('stop_distance_pct', 0):.2f}%)"
            )

            return order_req

        except KeyError as e:
            # ✅ symbol теперь всегда определён
            self.logger.error(
                f"❌ Missing required field in signal for {symbol}: {e}",
                exc_info=True
            )
            return None

        except Exception as e:
            # ✅ symbol теперь всегда определён
            self.logger.error(
                f"❌ Error handling WAIT signal for {symbol}: {e}",
                exc_info=True
            )
            return None

    async def create_initial_stop(
            self,
            symbol: str,
            *,
            stop_loss_pct: Optional[float] = None
    ) -> Optional[OrderReq]:
        """
        Создать и ОТПРАВИТЬ начальный стоп-лосс ордер для открытой позиции.

        ✅ ИСПРАВЛЕНО:
        - Валидация ответа ExchangeManager на None
        - Безопасный fallback для ack_status

        Args:
            symbol: Торговый символ
            stop_loss_pct: Процент стоп-лосса

        Returns:
            OrderReq если стоп успешно создан, иначе None
        """
        try:
            self.logger.info(
                f"🟢 create_initial_stop CALLED: "
                f"symbol={symbol} stop_loss_pct={stop_loss_pct}"
            )

            # === ШАГ 1: Проверка ExchangeManager ===
            if not self.exchange_manager:
                self.logger.error("❌ CRITICAL: ExchangeManager not set!")
                return None

            # === ШАГ 2: Получаем позицию ===
            position = self.get_position(symbol)

            if position["status"] != "OPEN":
                self.logger.warning(
                    f"Cannot create stop: position not OPEN for {symbol}"
                )
                return None

            position_side = position.get("side")
            entry_price = position.get("avg_entry_price")

            if not entry_price:
                self.logger.error(f"No entry price for {symbol}")
                return None

            # === ШАГ 3: Определяем stop_loss_pct ===
            if stop_loss_pct is None:
                try:
                    strategy_config = STRATEGY_PARAMS.get("CornEMA", {})
                    stop_loss_pct = float(
                        strategy_config.get("entry_stoploss_pct", 0.30)
                    )
                    self.logger.info(
                        f"Using entry_stoploss_pct from config: {stop_loss_pct}%"
                    )
                except Exception as e:
                    self.logger.error(f"Error loading stop_loss_pct: {e}")
                    stop_loss_pct = 0.30

            # === ШАГ 4: Рассчитываем цену стопа ===
            try:
                entry_price_dec = Decimal(str(entry_price))
                pct_factor = Decimal(str(stop_loss_pct)) / Decimal('100')

                if position_side == "LONG":
                    multiplier = Decimal('1') - pct_factor
                elif position_side == "SHORT":
                    multiplier = Decimal('1') + pct_factor
                else:
                    self.logger.error(f"Invalid position side: {position_side}")
                    return None

                stop_price_decimal = entry_price_dec * multiplier
            except ValueError as e:
                self.logger.error(f"Decimal conversion failed: {e}")
                return None

            # === ШАГ 5: Квантуем цену ===
            stop_price_decimal = self.quantize_price(symbol, stop_price_decimal)
            self.logger.info(
                f"Calculated stop price: {float(stop_price_decimal):.8f} "
                f"(entry={float(entry_price):.8f}, loss={stop_loss_pct}%)"
            )

            # === ШАГ 6: Генерируем ID ===
            client_order_id = self._generate_unique_order_id(symbol, "auto_stop")
            stop_side: Literal["BUY", "SELL"] = (
                "SELL" if position_side == "LONG" else "BUY"
            )
            correlation_id = (
                f"initial_stop_{symbol}_{get_current_timestamp_ms()}"
            )

            # === ШАГ 7: Формируем OrderReq ===
            order_req = OrderReq(
                client_order_id=client_order_id,
                symbol=symbol,
                side=stop_side,
                type="STOP_MARKET",
                qty=position["qty"],
                price=None,
                stop_price=stop_price_decimal,
                time_in_force="GTC",
                reduce_only=True,
                correlation_id=correlation_id,
                metadata={
                    "reason": "initial_stop",
                    "entry_price": float(entry_price),
                    "stop_loss_pct": stop_loss_pct,
                    "position_side": position_side
                }
            )

            # === ШАГ 8: Отправляем с await ===
            self.logger.warning(
                f"🔍 Sending initial stop to ExchangeManager..."
            )

            ack = self.exchange_manager.place_order(order_req)

            # ✅ ШАГ 9: ИСПРАВЛЕНО - Валидация ответа ExchangeManager
            if not ack or not isinstance(ack, dict):
                self.logger.error(
                    f"❌ ExchangeManager returned invalid response:\n"
                    f"  Type: {type(ack).__name__}\n"
                    f"  Value: {ack}\n"
                    f"  Symbol: {symbol}\n"
                    f"  Order ID: {client_order_id}"
                )
                self._remove_active_stop_tracking(symbol)
                return None

            self.logger.warning(f"🔍 ExchangeManager response: {ack}")

            # ✅ Безопасное извлечение статуса с fallback
            ack_status = ack.get("status", "ERROR")

            # === ШАГ 10: Обработка успешного ответа ===
            if ack_status in ["NEW", "WORKING", "FILLED"]:
                # Регистрируем ордер
                pending_order = PendingOrder(
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=stop_side,
                    type="STOP_MARKET",
                    qty=position["qty"],
                    price=None,
                    stop_price=stop_price_decimal,
                    correlation_id=correlation_id,
                    reduce_only=True,
                    metadata={
                        "is_trailing_stop": False,
                        "reason": "initial_stop",
                        "position_side": position_side,
                        "stop_loss_pct": stop_loss_pct
                    }
                )

                self._pending_orders[client_order_id] = pending_order

                self._update_active_stop_tracking(symbol, {
                    "client_order_id": client_order_id,
                    "stop_price": float(stop_price_decimal),
                    "side": stop_side,
                    "position_side": position_side,
                    "correlation_id": correlation_id,
                    "created_at": get_current_timestamp_ms(),
                    "reason": "initial_stop"
                })

                state = self._get_or_create_state(symbol)
                state["last_trailing_update_ts"] = get_current_timestamp_ms()

                self.logger.warning(
                    f"{'=' * 80}\n"
                    f"✅ INITIAL STOP CREATED SUCCESSFULLY\n"
                    f"  Symbol: {symbol}\n"
                    f"  Position: {position_side} @ {float(entry_price):.8f}\n"
                    f"  Stop Price: {float(stop_price_decimal):.8f}\n"
                    f"  Distance: {stop_loss_pct}%\n"
                    f"  Client Order ID: {client_order_id}\n"
                    f"  Status: {ack_status}\n"
                    f"{'=' * 80}"
                )

                return order_req

            else:
                # Ордер отклонён
                error_msg = (
                        ack.get("error_message") or
                        ack.get("error") or
                        "Unknown error"
                )

                self.logger.error(
                    f"❌ INITIAL STOP REJECTED:\n"
                    f"  Symbol: {symbol}\n"
                    f"  Status: {ack_status}\n"
                    f"  Error: {error_msg}"
                )

                self._remove_active_stop_tracking(symbol)
                return None

        except Exception as e:
            self.logger.error(
                f"❌ EXCEPTION in create_initial_stop: {e}",
                exc_info=True
            )
            return None

    def on_stop_triggered(self, symbol: str, execution_price: float) -> None:
        """
        Обработчик срабатывания стопа от MainBot/ExchangeManager.

        Делегирует исполнение в ExchangeManager для закрытия позиции.

        Args:
            symbol: Торговый символ
            execution_price: Цена исполнения стопа (stop_price)

        ОТВЕТСТВЕННОСТИ:
        - Проверить что ExchangeManager доступен
        - Делегировать исполнение стопа в EM
        - Логировать событие
        """
        try:
            self.logger.info(
                f"🔴 on_stop_triggered called: {symbol} @ {execution_price:.8f}"
            )

            # === ПРОВЕРКА 1: ExchangeManager установлен ===
            if not self.exchange_manager:
                self.logger.error(
                    f"❌ CRITICAL: ExchangeManager not set for PositionManager! "
                    f"Cannot execute stop for {symbol}"
                )
                return

            # === ПРОВЕРКА 2: Метод существует ===
            if not hasattr(self.exchange_manager, 'check_stops_on_price_update'):
                self.logger.error(
                    f"❌ ExchangeManager doesn't have check_stops_on_price_update method"
                )
                return

            # === ПРОВЕРКА 3: Позиция открыта ===
            position = self.get_position(symbol)
            if position["status"] != "OPEN":
                self.logger.warning(
                    f"⚠️ on_stop_triggered called for {symbol} but position is {position['status']}"
                )
                return

            position_side = position.get("side")
            entry_price = position.get("avg_entry_price")

            self.logger.info(
                f"  Position: {position_side} @ {float(entry_price) if entry_price else 'N/A'}"
            )

            # === ДЕЛЕГИРОВАНИЕ в ExchangeManager ===
            self.logger.info(
                f"Delegating to ExchangeManager.check_stops_on_price_update("
                f"symbol={symbol}, current_price={execution_price})"
            )

            self.exchange_manager.check_stops_on_price_update(
                symbol=symbol,
                current_price=execution_price
            )

            self.logger.info(
                f"✅ Stop execution delegated to ExchangeManager for {symbol}"
            )

        except Exception as e:
            self.logger.error(
                f"❌ Error in on_stop_triggered for {symbol}: {e}",
                exc_info=True
            )

    async def _cancel_stops_for_symbol(self, symbol: str) -> None:
        """
        ✅ ASYNC: Отменить все активные стоп-ордера для символа.
        """
        try:
            # 1. Удаляем из внутреннего отслеживания
            stops_to_cancel = self._active_stop_orders.get(symbol, [])

            if not stops_to_cancel:
                self.logger.debug(f"No active stops to cancel for {symbol}")
                return

            # 2. Отменяем на бирже
            for order_id in stops_to_cancel:
                try:
                    self.logger.info(f"Cancelling stop order {order_id} for {symbol}")

                    cancel_method = self.exchange_manager.cancel_order

                    # ✅ Проверяем async/sync
                    if asyncio.iscoroutinefunction(cancel_method):
                        result =  cancel_method(order_id)
                    else:
                        result = cancel_method(order_id)

                    if not isinstance(result, dict):
                        self.logger.warning(f"Invalid cancel result: {result}")
                        continue

                    status = result.get("status", "UNKNOWN")

                    if status == "CANCELED":
                        self.logger.info(f"✅ Stop {order_id} cancelled successfully")
                    else:
                        self.logger.warning(
                            f"⚠️ Stop {order_id} cancel status: {status}"
                        )

                except Exception as e:
                    self.logger.error(
                        f"❌ Failed to cancel stop {order_id}: {e}",
                        exc_info=True
                    )

            # 3. Очищаем внутренний трекинг
            self._remove_active_stop_tracking(symbol)

        except Exception as e:
            self.logger.error(
                f"❌ Error in _cancel_stops_for_symbol for {symbol}: {e}",
                exc_info=True
            )
    def _validate_stop_update(self, stop_update: Dict[str, Any],
                              position: PositionSnapshot,
                              signal: TradeSignalIQTS) -> Dict[str, Any]:
        """Валидация данных stop_update"""
        try:
            # Проверяем обязательные поля
            if "new_stop_price" not in stop_update:
                return {"valid": False, "error": "Missing new_stop_price"}

            new_stop_price = stop_update["new_stop_price"]

            # Проверяем тип и значение цены
            try:
                price_value = float(new_stop_price)
                if price_value <= 0:
                    return {"valid": False, "error": "new_stop_price must be positive"}
            except (ValueError, TypeError):
                return {"valid": False, "error": "new_stop_price must be a number"}

            # Проверяем разумность цены относительно текущей рыночной цены
            decision_price = float(signal.get("decision_price", 0))
            if decision_price > 0:
                price_diff_pct = abs(price_value - decision_price) / decision_price * 100

                # Стоп не должен быть слишком далеко от рыночной цены
                max_stop_distance = 10.0  # 10% максимум
                if price_diff_pct > max_stop_distance:
                    return {"valid": False, "error": f"Stop price too far from market price: {price_diff_pct:.2f}%"}

            # Проверяем направление стопа относительно позиции
            position_side = position.get("side")
            if position_side == "LONG" and decision_price > 0:
                # Для лонга стоп должен быть ниже рыночной цены
                if price_value >= decision_price:
                    return {"valid": False, "error": "LONG stop must be below market price"}
            elif position_side == "SHORT" and decision_price > 0:
                # Для шорта стоп должен быть выше рыночной цены
                if price_value <= decision_price:
                    return {"valid": False, "error": "SHORT stop must be above market price"}

            return {"valid": True, "error": None}

        except Exception as e:
            return {"valid": False, "error": f"Validation error: {e}"}



    def _is_stop_update_beneficial(self, position: PositionSnapshot,
                                   current_stop: Optional[float],
                                   new_stop: float) -> bool:
        """Проверить, выгодно ли обновление стопа"""
        try:
            if current_stop is None:
                return True  # Если стопа нет, любой стоп лучше

            position_side = position.get("side")

            if position_side == "LONG":
                # Для лонга новый стоп должен быть выше текущего
                return new_stop > current_stop
            elif position_side == "SHORT":
                # Для шорта новый стоп должен быть ниже текущего
                return new_stop < current_stop
            else:
                self.logger.warning(f"Unknown position side: {position_side}")
                return False

        except Exception as e:
            self.logger.error(f"Error checking stop update benefit: {e}")
            return False

    def update_on_fill(self, fill: OrderUpd) -> None:
        """
        Единственная точка обновления позиции по факту исполнения.
        Обрабатывает FILLED ордера и обновляет состояние позиций.
        """
        try:
            if fill["status"] != "FILLED":
                return

            # Обновляем ордер в БД
            if hasattr(self, 'trade_log') and self.trade_log:
                try:
                    self.trade_log.update_order_on_upd(fill)
                except Exception as e:
                    self.logger.error(f"Failed to update order in DB: {e}")

            symbol = fill["symbol"]
            filled_qty = fill["filled_qty"]
            avg_price = fill.get("avg_price")
            commission = fill.get("commission", Decimal('0'))
            client_order_id = fill["client_order_id"]

            if not avg_price or filled_qty <= 0:
                self.logger.warning(f"Invalid fill data: {fill}")
                return

            self.logger.debug(
                f"Processing fill: {symbol} {fill['side']} {float(filled_qty)} @ {float(avg_price)} "
                f"reduce_only={fill.get('reduce_only', False)} client_order_id={client_order_id}"
            )

            # ✅ ИЗВЛЕКАЕМ метаданные ДО удаления из pending_orders
            order_type = None
            is_stop_order = False
            is_trailing_stop = False

            if client_order_id in self._pending_orders:
                order = self._pending_orders[client_order_id]
                order_type = order.type

                if order.type in ["STOP_MARKET", "STOP"]:
                    is_stop_order = True

                    # ✅ Многоуровневая проверка trailing stop
                    # ПРИОРИТЕТ 1: client_order_id (самое надежное)
                    if "trail_stop" in str(order.client_order_id):
                        is_trailing_stop = True
                        self.logger.debug(f"Trailing stop detected: client_order_id={order.client_order_id}")

                    # ПРИОРИТЕТ 2: metadata (если доступно)
                    elif hasattr(order, 'metadata') and order.metadata:
                        if order.metadata.get("is_trailing_stop"):
                            is_trailing_stop = True
                            self.logger.debug(f"Trailing stop detected: metadata flag")

                    # ПРИОРИТЕТ 3: correlation_id (fallback)
                    elif "trail" in str(order.correlation_id):
                        is_trailing_stop = True
                        self.logger.debug(f"Trailing stop detected: correlation_id={order.correlation_id}")

                    # Если ничего не нашли - это обычный stop loss
                    if not is_trailing_stop:
                        self.logger.debug(f"Regular stop loss detected for {symbol}")

                    # Удаляем из tracking
                    self._remove_active_stop_tracking(symbol)
                    self.logger.info(
                        f"STOP ORDER FILLED: {symbol} {fill['side']} "
                        f"type={order.type} is_trailing={is_trailing_stop} "
                        f"client_order_id={client_order_id}"
                    )

            # Получаем текущую позицию
            current_position = self.get_position(symbol)

            self.logger.debug(
                f"Current position before fill: {symbol} "
                f"status={current_position['status']} "
                f"side={current_position.get('side')} "
                f"qty={float(current_position.get('qty', 0))}"
            )

            # Определяем, что вход или выход
            is_reduce_only = fill.get("reduce_only", False)

            # ✅ ВЫЗОВ _process_exit_fill С ПРАВИЛЬНЫМИ ПАРАМЕТРАМИ
            if is_reduce_only or current_position["status"] != "FLAT":
                # Это выход из позиции
                self._process_exit_fill(
                    symbol,
                    fill,
                    current_position,
                    order_type=order_type,  # Передаём тип ордера
                    is_trailing_stop=is_trailing_stop  # Передаём флаг trailing stop
                )
            else:
                # Это вход в новую позицию
                self._process_entry_fill(symbol, fill)

            # ✅ УДАЛЯЕМ из pending orders ПОСЛЕ обработки
            if client_order_id in self._pending_orders:
                del self._pending_orders[client_order_id]

            self._stats.fills_processed += 1

            # Эмитим события
            if is_stop_order:
                self._emit_event(PositionEvent(
                    event_type="STOP_ORDER_FILLED",
                    symbol=symbol,
                    timestamp_ms=get_current_timestamp_ms(),
                    correlation_id=fill.get("trade_id"),
                    position_data={
                        "client_order_id": client_order_id,
                        "side": fill["side"],
                        "qty": float(filled_qty),
                        "price": float(avg_price),
                        "stop_price": float(avg_price),
                        "commission": float(commission),
                        "reason": "trailing_stop" if is_trailing_stop else "stop_loss"
                    }
                ))

            # Стандартное событие обработки fill
            self._emit_event(PositionEvent(
                event_type="FILL_PROCESSED",
                symbol=symbol,
                timestamp_ms=get_current_timestamp_ms(),
                correlation_id=fill.get("trade_id"),
                position_data={
                    "client_order_id": client_order_id,
                    "side": fill["side"],
                    "qty": float(filled_qty),
                    "price": float(avg_price),
                    "commission": float(commission),
                    "is_stop_order": is_stop_order,
                    "is_trailing_stop": is_trailing_stop
                }
            ))

        except Exception as e:
            self.logger.error(f"Error processing fill: {e}")

    def is_on_cooldown(self, symbol: str) -> bool:
        """
        Проверить, находится ли символ в cooldown.

        Returns:
            True если любое направление (LONG или SHORT) в cooldown
        """
        try:
            state = self._get_or_create_state(symbol)

            # Проверяем оба направления
            cooldown_long = state.get("cooldown_counter_LONG", 0)
            cooldown_short = state.get("cooldown_counter_SHORT", 0)

            is_cooling = (cooldown_long > 0 or cooldown_short > 0)

            if is_cooling:
                self.logger.debug(
                    f"Cooldown active for {symbol}: LONG={cooldown_long}, SHORT={cooldown_short}"
                )

            return is_cooling

        except Exception as e:
            self.logger.error(f"Error checking cooldown for {symbol}: {e}")
            return False  # Безопасный fallback

    def _get_or_create_state(self, symbol: str) -> Dict[str, Any]:
        """
        Получить или создать внутреннее состояние для символа.
        Состояние хранит данные для trailing stop, cooldown, и т.д.
        """
        if not hasattr(self, '_symbol_states'):
            self._symbol_states = {}

        if symbol not in self._symbol_states:
            # Создаём начальное состояние
            self._symbol_states[symbol] = {
                "position": None,
                "entry_price": None,
                "entry_stop_loss_price": None,

                # Trailing stop поля
                "max_pnl_percent": 0.0,
                "is_trailing_active": False,
                "trailing_update_count": 0,
                "candles_since_entry": 0,
                "last_trailing_update_pnl": 0.0,

                #  Timestamp-based trailing tracking
                "last_trailing_update_ts": 0,  # Timestamp последнего обновления trailing stop (мс)
                "last_candle_ts": 0,  # Timestamp последней обработанной свечи (мс)

                # Cooldown поля
                "cooldown_counter": 0,
                "last_decremented_bar": None
            }

            self.logger.debug(f"Created initial state for {symbol}")

        return self._symbol_states[symbol]

    def update_peak_pnl(self, symbol: str, current_price: float,
                        candle_ts: Optional[int] = None) -> None:
        """
        Обновляет пик профита и активирует trailing stop при достижении порога.
        Также отслеживает количество свечей с момента входа.

        """
        position = self.get_position(symbol)
        if position["status"] == "FLAT" or not position["avg_entry_price"]:
            return

        current_price_dec = Decimal(str(current_price))
        entry_price = position["avg_entry_price"]
        side = position["side"]

        # Расчёт текущего PnL
        if side == "LONG":
            current_pnl = (current_price_dec - entry_price) / entry_price * Decimal('100')
        else:
            current_pnl = (entry_price - current_price_dec) / entry_price * Decimal('100')

        current_pnl_float = float(current_pnl)

        # Получаем состояние
        state = self._get_or_create_state(symbol)
        old_max = state["max_pnl_percent"]

        # ✅ НОВОЕ: Обновляем timestamp текущей свечи
        if candle_ts:
            state["last_candle_ts"] = candle_ts
            state["current_candle_number"] = candle_ts

        # НОВОЕ: Инкрементируем счетчик свечей
        state["candles_since_entry"] += 1

        # Обновляем пик
        if current_pnl_float > old_max:
            state["max_pnl_percent"] = current_pnl_float
            # НОВОЕ: Обновляем номер свечи последнего обновления при новом пике
            state["last_trailing_update_candle"] = state["current_candle_number"]
            self.logger.debug(f"New peak PnL for {symbol}: {old_max:.2f}% → {current_pnl_float:.2f}%")

        # Проверяем активацию trailing
        if not state["is_trailing_active"]:
            trailing_cfg = self._get_trailing_config(symbol)
            min_profit = trailing_cfg.get("min_profit_percent", 0.5)
            delay_candles = trailing_cfg.get("activation_delay_candles", 3)

            if (state["candles_since_entry"] >= delay_candles and
                    current_pnl_float >= min_profit):
                state["is_trailing_active"] = True
                state["trailing_update_count"] = 0
                state["last_trailing_update_pnl"] = current_pnl_float
                self.logger.info(
                    f"Trailing activated for {symbol} at PnL={current_pnl_float:.2f}%"
                )

    def _process_entry_fill(self, symbol: str, fill: OrderUpd) -> None:
        """
        Обработка исполнения ордера входа.
        Создаёт новую позицию и сохраняет её в БД.
        """
        try:
            from typing import cast, Literal

            filled_qty = fill["filled_qty"]
            avg_price_raw = fill.get("avg_price")
            side = "LONG" if fill["side"] == "BUY" else "SHORT"
            commission_raw = fill.get("commission", Decimal('0'))

            if not avg_price_raw or filled_qty <= 0:
                self.logger.warning(f"Invalid fill data in entry: {fill}")
                return

            # Преобразуем в нужные типы для логирования и расчётов
            avg_price_float = float(avg_price_raw)
            commission_float = float(commission_raw)

            # ✅ ДОБАВЛЕНО: Конвертируем в Decimal для внутреннего использования
            filled_qty_decimal = Decimal(str(filled_qty)) if not isinstance(filled_qty, Decimal) else filled_qty
            avg_price_decimal = Decimal(str(avg_price_raw)) if not isinstance(avg_price_raw, Decimal) else avg_price_raw
            commission_decimal = Decimal(str(commission_raw)) if not isinstance(commission_raw,
                                                                                Decimal) else commission_raw

            self.logger.warning(
                f"🟢 _process_entry_fill CALLED:\n"
                f"  symbol: {symbol}\n"
                f"  filled_qty: {filled_qty}\n"
                f"  avg_price: {avg_price_float:.8f}\n"
                f"  commission: {commission_float:.6f}\n"
                f"  side: {side}"
            )

            #  Создаём новую позицию с правильными типами
            position = PositionSnapshot(
                symbol=symbol,
                status="OPEN",
                side=cast(Literal["LONG", "SHORT"], side),  # ← Явное приведение типа
                qty=filled_qty_decimal,  # ← Decimal
                avg_entry_price=avg_price_decimal,  # ← Decimal
                market_price=avg_price_float,
                realized_pnl_usdt=Decimal('0'),
                unrealized_pnl_usdt=Decimal('0'),
                created_ts=get_current_timestamp_ms(),
                updated_ts=get_current_timestamp_ms(),
                correlation_id=fill.get("client_order_id"),
                fee_total_usdt=commission_decimal  # ← ИСПРАВЛЕНО: Decimal вместо float
            )

            # Сохраняем в памяти
            self._positions[symbol] = position
            self._stats.positions_opened += 1

            # Сохраняем в БД
            self._save_position_to_db(position, is_new=True)

            # Логирование
            self.logger.info(
                f"Position opened: {symbol} {side} {float(filled_qty_decimal)} @ {avg_price_float:.4f} "
                f"entry_fee={commission_float:.6f} USDT"
            )

            # Эмит события
            self._emit_event(PositionEvent(
                event_type="POSITION_OPENED",
                symbol=symbol,
                timestamp_ms=get_current_timestamp_ms(),
                correlation_id=fill.get("trade_id"),
                position_data={
                    "side": side,
                    "qty": float(filled_qty_decimal),
                    "entry_price": avg_price_float,
                    "entry_fee": commission_float,
                    "correlation_id": fill.get("client_order_id")
                }
            ))

        except Exception as e:
            self.logger.error(f"Error processing entry fill for {symbol}: {e}")

    def _process_exit_fill(
            self,
            symbol: str,
            fill: OrderUpd,
            current_position: PositionSnapshot,
            order_type: Optional[str] = None,
            is_trailing_stop: bool = False
    ) -> None:
        """
        Обработка исполнения ордера выхода.

        ✅ ИСПРАВЛЕНО:
        - Инициализация pnl_decimal
        - Обработка неизвестного direction
        """
        filled_qty = fill["filled_qty"]
        avg_price_raw = fill.get("avg_price")
        commission_raw = fill.get("commission", Decimal('0'))

        if not avg_price_raw or filled_qty <= 0:
            self.logger.warning(f"Invalid fill data in exit: {fill}")
            return

        # Преобразуем Decimal → float для логирования
        avg_price_float = float(avg_price_raw)
        commission_float = float(commission_raw)

        self.logger.warning(
            f"🟣 _process_exit_fill CALLED:\n"
            f"  symbol: {symbol}\n"
            f"  filled_qty: {filled_qty}\n"
            f"  avg_price: {avg_price_float:.8f}\n"
            f"  commission: {commission_float:.6f}\n"
            f"  position_side: {current_position.get('side')}\n"
            f"  entry_price: {current_position.get('avg_entry_price')}\n"
            f"  order_type: {order_type}"
        )

        if current_position["status"] == "FLAT":
            self.logger.warning(f"Trying to exit flat position: {symbol}")
            return

        position_qty = current_position["qty"]
        position_side = current_position["side"]
        entry_price_raw = current_position["avg_entry_price"]

        # Защита от None
        if not entry_price_raw:
            self.logger.error(f"Missing entry_price for {symbol}, cannot calculate PnL")
            return
        entry_price_float = float(entry_price_raw)

        if not position_side:
            self.logger.error(f"Position side is None for {symbol}, cannot calculate PnL")
            return

        # Convert all values to Decimal for calculation
        entry_price_decimal = Decimal(str(entry_price_float))
        avg_price_decimal = Decimal(str(avg_price_float))
        filled_qty_decimal = Decimal(str(filled_qty)) if not isinstance(filled_qty, Decimal) else filled_qty

        # Convert position_side to Direction for comparison
        position_direction = Direction.BUY if position_side == "LONG" else Direction.SELL

        # ═══════════════════════════════════════════════════════════
        # ✅ ИСПРАВЛЕНИЕ: Инициализация pnl_decimal + обработка else
        # ═══════════════════════════════════════════════════════════

        # Рассчитываем PnL (используем Decimal для точности)
        if position_direction == Direction.BUY:
            pnl_decimal = (avg_price_decimal - entry_price_decimal) * filled_qty_decimal
            self.logger.debug(
                f"PnL calculation for LONG: ({avg_price_float:.8f} - {entry_price_float:.8f}) "
                f"* {float(filled_qty_decimal):.4f} = {float(pnl_decimal):.2f}"
            )

        elif position_direction == Direction.SELL:
            pnl_decimal = (entry_price_decimal - avg_price_decimal) * filled_qty_decimal
            self.logger.debug(
                f"PnL calculation for SHORT: ({entry_price_float:.8f} - {avg_price_float:.8f}) "
                f"* {float(filled_qty_decimal):.4f} = {float(pnl_decimal):.2f}"
            )

        else:
            # ✅ FALLBACK: Неизвестный direction
            self.logger.error(
                f"❌ Unknown position direction for {symbol}: {position_direction} "
                f"(position_side={position_side})"
            )
            pnl_decimal = Decimal('0')  # ✅ Безопасное значение по умолчанию
            # Можно также вернуться досрочно:
            # return

        # Ensure both fees are Decimal
        existing_entry_fee_raw = current_position.get("fee_total_usdt")
        if existing_entry_fee_raw is None:
            existing_entry_fee_decimal = Decimal('0')
        else:
            existing_entry_fee_decimal = Decimal(str(existing_entry_fee_raw))

        exit_fee_decimal = Decimal(str(commission_float))
        total_fees_decimal = existing_entry_fee_decimal + exit_fee_decimal

        # ✅ Теперь pnl_decimal всегда определён
        realized_pnl_decimal = pnl_decimal - total_fees_decimal

        remaining_qty = position_qty - filled_qty_decimal

        client_order_id = fill.get("client_order_id", "")

        # ═══════════════════════════════════════════════════════════
        # ОПРЕДЕЛЕНИЕ ПРИЧИНЫ ВЫХОДА
        # ═══════════════════════════════════════════════════════════

        # ПРИОРИТЕТ 1: Явный флаг is_trailing_stop
        if is_trailing_stop:
            exit_reason = "TRAILING_STOP"
            self.logger.debug(f"Exit reason: TRAILING_STOP (explicit flag)")

        # ПРИОРИТЕТ 2: Проверка order_type для STOP ордеров
        elif order_type in ["STOP_MARKET", "STOP"]:
            exit_reason = "STOP_LOSS"

            # Дополнительная проверка по client_order_id
            if "trail" in str(client_order_id).lower():
                exit_reason = "TRAILING_STOP"
                self.logger.debug(f"Exit reason: TRAILING_STOP (detected in client_order_id)")
            elif "auto_stop" in str(client_order_id).lower():
                exit_reason = "STOP_LOSS"
                self.logger.debug(f"Exit reason: STOP_LOSS (auto_stop in client_order_id)")

        # ПРИОРИТЕТ 3: Market/Limit выход по сигналу
        else:
            exit_reason = "SIGNAL_EXIT"
            self.logger.debug(f"Exit reason: SIGNAL_EXIT (order_type={order_type})")

        # ═══════════════════════════════════════════════════════════
        # ЗАКРЫТИЕ ПОЗИЦИИ
        # ═══════════════════════════════════════════════════════════

        # Полностью закрываем позицию?
        if remaining_qty <= Decimal('0.001'):
            updated_position = PositionSnapshot(
                symbol=symbol,
                status="FLAT",
                side=None,
                qty=Decimal('0'),
                avg_entry_price=Decimal('0'),
                realized_pnl_usdt=current_position.get("realized_pnl_usdt", Decimal('0')) + realized_pnl_decimal,
                unrealized_pnl_usdt=Decimal('0'),
                created_ts=current_position["created_ts"],
                updated_ts=get_current_timestamp_ms(),
                fee_total_usdt=total_fees_decimal
            )

            self._positions[symbol] = updated_position
            self._stats.positions_closed += 1
            self._stats.total_realized_pnl += realized_pnl_decimal

            # Расчёт процентов
            position_size_usdt = entry_price_decimal * filled_qty_decimal
            pnl_percent = (
                (float(realized_pnl_decimal) / float(position_size_usdt) * 100)
                if position_size_usdt > 0
                else 0.0
            )

            price_change_pct = (
                ((avg_price_float - entry_price_float) / entry_price_float * 100)
                if position_side == "LONG"
                else ((entry_price_float - avg_price_float) / entry_price_float * 100)
            )

            # ═══════════════════════════════════════════════════════════
            # ОБНОВЛЕНИЕ БД
            # ═══════════════════════════════════════════════════════════

            # Загрузка position_id из БД
            position_id = None
            if hasattr(self, '_position_ids') and hasattr(self, 'trade_log') and self.trade_log:
                position_id = self._position_ids.get(symbol)

                if not position_id:
                    try:
                        open_positions = self.trade_log.get_open_positions_db(symbol)
                        if open_positions:
                            position_id = open_positions[0].id
                            self.logger.warning(
                                f"position_id not in cache for {symbol}, loaded from DB: {position_id}"
                            )
                    except Exception as e:
                        self.logger.error(f"Failed to load position_id from DB for {symbol}: {e}")

                if position_id:
                    self.trade_log.close_position(
                        position_id=position_id,
                        exit_price=avg_price_raw,
                        exit_reason=exit_reason
                    )
                    if symbol in self._position_ids:
                        del self._position_ids[symbol]

                    # ✅ ИСПРАВЛЕНИЕ: Проверяем async/sync
                    cancel_method = self._cancel_stops_for_symbol
                    if asyncio.iscoroutinefunction(cancel_method):
                        # Создаём задачу для async метода
                        asyncio.create_task(cancel_method(symbol))
                    else:
                        # Синхронный вызов
                        cancel_method(symbol)
                else:
                    self.logger.error(
                        f"Cannot close position in DB: position_id not found for {symbol}"
                    )

            # ═══════════════════════════════════════════════════════════
            # ЛОГИРОВАНИЕ И СОБЫТИЯ
            # ═══════════════════════════════════════════════════════════

            self.logger.info(
                f"✅ Position closed: {symbol} {position_side} "
                f"PnL={pnl_percent:.2f}% ({float(realized_pnl_decimal):.2f} USDT) "
                f"price_change={price_change_pct:.2f}% "
                f"entry={entry_price_float:.4f} exit={avg_price_float:.4f} "
                f"entry_fee={float(existing_entry_fee_decimal):.2f} "
                f"exit_fee={commission_float:.2f} "
                f"total_fee={float(total_fees_decimal):.2f} USDT "
                f"reason={exit_reason}"
            )

            # Эмит события
            self._emit_event(PositionEvent(
                event_type="POSITION_CLOSED",
                symbol=symbol,
                timestamp_ms=get_current_timestamp_ms(),
                correlation_id=fill.get("trade_id"),
                position_data={
                    "side": position_side,
                    "qty": float(filled_qty_decimal),
                    "entry_price": entry_price_float,
                    "exit_price": avg_price_float,
                    "pnl_usdt": float(realized_pnl_decimal),
                    "pnl_percent": pnl_percent,
                    "price_change_percent": price_change_pct,
                    "entry_fee": float(existing_entry_fee_decimal),
                    "exit_fee": commission_float,
                    "total_fee": float(total_fees_decimal),
                    "exit_reason": exit_reason,
                    "trigger_stop_cooldown": (exit_reason == "STOP_LOSS"),
                    "total_realized_pnl": float(self._stats.total_realized_pnl)
                }
            ))

        else:
            # ═══════════════════════════════════════════════════════════
            # ЧАСТИЧНОЕ ЗАКРЫТИЕ ПОЗИЦИИ
            # ═══════════════════════════════════════════════════════════

            self.logger.info(
                f"Partial exit: {symbol} {position_side} "
                f"filled={float(filled_qty_decimal):.4f}, "
                f"remaining={float(remaining_qty):.4f}"
            )

            # Обновляем позицию
            updated_position = PositionSnapshot(
                symbol=symbol,
                status="OPEN",
                side=position_side,
                qty=remaining_qty,
                avg_entry_price=entry_price_raw,
                realized_pnl_usdt=current_position.get("realized_pnl_usdt", Decimal('0')) + realized_pnl_decimal,
                unrealized_pnl_usdt=current_position.get("unrealized_pnl_usdt", Decimal('0')),
                created_ts=current_position["created_ts"],
                updated_ts=get_current_timestamp_ms(),
                fee_total_usdt=total_fees_decimal
            )

            self._positions[symbol] = updated_position

    # === Вспомогательные методы ===

    def _validate_signal(self, signal: TradeSignalIQTS) -> None:
        """
        Валидация торгового сигнала.
        
        Использует внедрённый SignalValidator если доступен,
        иначе выполняет базовую валидацию.
        """
        # Используем внедрённый validator если доступен
        if self.signal_validator and hasattr(self.signal_validator, 'validate_signal'):
            try:
                validation_result = self.signal_validator.validate_signal(signal)
                if not validation_result.get('valid', False):
                    errors = validation_result.get('errors', ['Unknown validation error'])
                    raise InvalidSignalError(f"Signal validation failed: {', '.join(errors)}")
                # Успешная валидация через SignalValidator
                return
            except AttributeError:
                # Fallback если метод недоступен
                self.logger.warning("SignalValidator.validate_signal() not available, using basic validation")
        
        # Базовая валидация (backward compatibility)
        required_fields = ["symbol", "intent", "decision_price"]
        for field in required_fields:
            if field not in signal:
                raise InvalidSignalError(f"Missing required field: {field}")

        symbol = signal["symbol"]
        if symbol not in self.symbols_meta:
            raise InvalidSignalError(f"Unknown symbol: {symbol}")

        if signal["decision_price"] <= 0:
            raise InvalidSignalError("Invalid decision_price")

    def _compute_risk_context_hash(self, risk_context: Dict[str, Any]) -> str:
        """
        Вычисление SHA256 хеша от risk_context для проверки целостности.

        Алгоритм:
        1. Создаёт копию risk_context без поля validation_hash
        2. Сериализует в JSON с сортировкой ключей
        3. Вычисляет SHA256 от JSON строки
        4. Возвращает hex строку (64 символа)

        Args:
            risk_context: Словарь с параметрами риска

        Returns:
            Hex строка SHA256 хеша или пустая строка при ошибке

        Example:
            >>> risk_ctx = {
            ...     "position_size": 0.5,
            ...     "initial_stop_loss": 3200.0,
            ...     "take_profit": 3400.0
            ... }
            >>> hash_val = self._compute_risk_context_hash(risk_ctx)
            >>> len(hash_val)
            64
        """
        import json
        import hashlib

        try:
            # ✅ ШАГ 1: Создаём копию без validation_hash
            ctx_copy = {
                k: v
                for k, v in risk_context.items()
                if k != "validation_hash"
            }

            # ✅ ШАГ 2: Сериализуем в JSON с сортировкой ключей
            # sort_keys=True гарантирует один и тот же порядок
            # default=str конвертирует нестандартные типы (Decimal, datetime)
            canonical = json.dumps(
                ctx_copy,
                sort_keys=True,
                default=str,
                ensure_ascii=False,
                separators=(',', ':')  # Убираем пробелы для консистентности
            )

            self.logger.debug(
                f"Computing hash for risk_context: {canonical[:100]}..."
            )

            # ✅ ШАГ 3: Вычисляем SHA256
            hash_bytes = hashlib.sha256(canonical.encode('utf-8')).digest()

            # ✅ ШАГ 4: Возвращаем hex строку
            hash_hex = hash_bytes.hex()

            self.logger.debug(f"Computed hash: {hash_hex[:16]}...{hash_hex[-8:]}")

            return hash_hex

        except Exception as e:
            self.logger.error(
                f"❌ Error computing risk_context hash: {e}",
                exc_info=True
            )
            return ""

    def _verify_risk_context(self, signal: TradeSignalIQTS) -> bool:
        """
        Проверка целостности risk_context через validation_hash.
        Поддерживает извлечение validation_hash из:
        - signal.metadata.validation_hash (TradeSignal стандарт)
        - signal.validation_hash (TradeSignalIQTS формат)
        - risk_context.validation_hash (legacy)
        """
        try:
            risk_context = signal.get("risk_context")
            if not risk_context:
                self.logger.debug("No risk_context in signal, skipping verification")
                return True
            # ✅ Извлекаем validation_hash из всех возможных мест
            stored_hash = (
                    signal.get("metadata", {}).get("validation_hash") or
                    signal.get("validation_hash") or  # type: ignore[typeddict-item]
                    (risk_context.get("validation_hash") if isinstance(risk_context, dict) else None)
            )

            if not stored_hash:
                self.logger.debug("No validation_hash found, skipping verification")
                return True

            # ✅ Вычисляем хеш от текущего risk_context
            computed_hash = self._compute_risk_context_hash(risk_context)

            if not computed_hash:
                self.logger.warning("Failed to compute hash, skipping verification")
                return True  # Не блокируем если не смогли вычислить

            # ✅ Сравниваем хеши
            if stored_hash != computed_hash:
                self.logger.error(
                    f"❌ SECURITY ALERT: Risk context validation FAILED!\n"
                    f"  Symbol: {signal.get('symbol', 'UNKNOWN')}\n"
                    f"  Stored hash:   {stored_hash}\n"
                    f"  Computed hash: {computed_hash}\n"
                    f"  Risk context may have been tampered with!"
                )
                return False

            self.logger.debug(
                f"✅ Risk context validated successfully "
                f"(hash: {stored_hash[:8]}...{stored_hash[-8:]})"
            )
            return True

        except Exception as e:
            self.logger.error(
                f"❌ Error verifying risk_context: {e}",
                exc_info=True
            )
            return False  # При ошибке считаем невалидным для безопасности

    def _save_position_to_db(self, position: PositionSnapshot, is_new: bool) -> None:
        """
        УПРОЩЕННОЕ сохранение позиции в БД - с поддержкой fee_total_usdt.

        ✅ ИСПРАВЛЕНО:
        - Правильная типизация словарей
        - Убраны literal()
        """
        try:
            if not hasattr(self, 'trade_log') or not self.trade_log:
                self.logger.warning("No trade_log available for position saving")
                return

            symbol = position["symbol"]

            if is_new:
                # ═══════════════════════════════════════════════════════════
                # СОЗДАНИЕ НОВОЙ ПОЗИЦИИ
                # ═══════════════════════════════════════════════════════════

                if not position.get("side") or not position.get("qty") or not position.get("avg_entry_price"):
                    self.logger.error(f"Missing required fields for new position: {symbol}")
                    return

                # ✅ Типизация: Dict[str, Any]
                position_record: Dict[str, Any] = {
                    "symbol": symbol,
                    "side": position["side"],
                    "status": position.get("status", "OPEN"),
                    "entry_ts": position.get("created_ts") or get_current_timestamp_ms(),
                    "entry_price": position["avg_entry_price"],
                    "qty": position["qty"],
                    "position_usdt": position["qty"] * position["avg_entry_price"],
                    "leverage": Decimal('1.0'),
                    "reason_entry": "SIGNAL",
                    "correlation_id": position.get("correlation_id"),
                    "fee_total_usdt": position.get("fee_total_usdt", Decimal('0')),
                    "exit_ts": None,
                    "exit_price": None,
                    "realized_pnl_usdt": position.get("realized_pnl_usdt", Decimal('0')),
                    "realized_pnl_pct": None,
                    "reason_exit": None
                }

                normalized_record = self.trade_log._normalize_params(position_record)
                position_id = self.trade_log.create_position(normalized_record)

                if position_id:
                    if not hasattr(self, '_position_ids'):
                        self._position_ids: Dict[str, int] = {}
                    self._position_ids[symbol] = position_id
                    self.logger.info(
                        f"✅ Created position in DB: {symbol} {position['side']} "
                        f"id={position_id} entry_fee={float(position.get('fee_total_usdt', 0)):.6f}"
                    )

            else:
                # ═══════════════════════════════════════════════════════════
                # ОБНОВЛЕНИЕ СУЩЕСТВУЮЩЕЙ ПОЗИЦИИ
                # ═══════════════════════════════════════════════════════════

                position_id: Optional[int] = None

                # Пытаемся получить position_id из кэша
                if hasattr(self, '_position_ids') and symbol in self._position_ids:
                    position_id = self._position_ids[symbol]
                else:
                    # Загружаем из БД
                    open_positions = self.trade_log.get_open_positions_db(symbol)
                    if open_positions:
                        position_id = open_positions[0].get("id")
                        if not hasattr(self, '_position_ids'):
                            self._position_ids: Dict[str, int] = {}
                        if position_id:
                            self._position_ids[symbol] = position_id

                if not position_id:
                    self.logger.error(
                        f"❌ Cannot update position - no position_id found for {symbol}"
                    )
                    return

                # ✅ ИСПРАВЛЕНИЕ: Явная типизация Dict[str, Any]
                updates: Dict[str, Any] = {
                    "updated_ts": get_current_timestamp_ms()
                }

                # Добавляем только изменённые поля
                status = position.get("status")
                if status:
                    updates["status"] = str(status)  # ✅ Явное приведение к str

                qty = position.get("qty")
                if qty is not None:
                    updates["qty"] = qty  # ✅ Decimal остаётся Decimal

                realized_pnl = position.get("realized_pnl_usdt")
                if realized_pnl is not None:
                    updates["realized_pnl_usdt"] = realized_pnl  # ✅ Decimal

                fee_total = position.get("fee_total_usdt")
                if fee_total is not None:
                    updates["fee_total_usdt"] = fee_total  # ✅ Decimal

                # Нормализуем параметры для SQLAlchemy
                normalized_updates = self.trade_log._normalize_params(updates)

                success = self.trade_log.update_position(position_id, normalized_updates)

                if success:
                    self.logger.info(
                        f"✅ Updated position in DB: {symbol} id={position_id}"
                    )
                else:
                    self.logger.warning(
                        f"⚠️ Failed to update position in DB: {symbol} id={position_id}"
                    )

        except Exception as e:
            self.logger.error(
                f"❌ Error in _save_position_to_db for {position.get('symbol', 'unknown')}: {e}",
                exc_info=True
            )

            # Записываем ошибку в trade_log
            if hasattr(self, 'trade_log') and self.trade_log:
                try:
                    self.trade_log.record_error({
                        "error_type": "position_save_failed",
                        "symbol": position.get("symbol"),
                        "is_new": is_new,
                        "error": str(e)
                    })
                except Exception as log_error:
                    self.logger.error(
                        f"Failed to record error to trade_log: {log_error}"
                    )

    def _init_position_ids_cache(self) -> None:
        """Инициализирует кеш ID позиций из существующих открытых позиций."""
        try:
            if not hasattr(self, '_position_ids'):
                self._position_ids = {}

            # Загружаем все открытые позиции
            open_positions = self.trade_log.get_open_positions_db()

            for pos in open_positions:
                symbol = pos.get("symbol")
                position_id = pos.get("id")
                if symbol and position_id:
                    self._position_ids[symbol] = position_id

            if self._position_ids:
                self.logger.info(f"Loaded {len(self._position_ids)} position IDs from DB")

        except Exception as e:
            self.logger.error(f"Error initializing position IDs cache: {e}")

    # === Публичные методы ===

    def get_position(self, symbol: str) -> PositionSnapshot:
        """Вернуть актуальный снимок по символу"""
        if symbol not in self._positions:
            # Возвращаем пустую позицию
            return PositionSnapshot(
                symbol=symbol,
                status="FLAT",
                side=None,
                qty=Decimal('0'),
                avg_entry_price=Decimal('0'),
                market_price=None,
                realized_pnl_usdt=Decimal('0'),
                unrealized_pnl_usdt=Decimal('0'),
                created_ts=get_current_timestamp_ms(),
                updated_ts=get_current_timestamp_ms()
            )

        return self._positions[symbol].copy()

    def get_open_positions_snapshot(self) -> Dict[str, PositionSnapshot]:
        """Вернуть все открытые позиции (in-memory снапшот)"""
        open_positions = {}
        for symbol, position in self._positions.items():
            if position["status"] != "FLAT":
                open_positions[symbol] = position.copy()
        return open_positions

    def get_stats(self) -> Dict[str, Any]:
        """Счётчики и статистика работы"""
        open_positions_count = len(self.get_open_positions_snapshot())

        return {
            "signals_processed": self._stats.signals_processed,
            "orders_created": self._stats.orders_created,
            "positions_opened": self._stats.positions_opened,
            "positions_closed": self._stats.positions_closed,
            "fills_processed": self._stats.fills_processed,
            "duplicate_signals": self._stats.duplicate_signals,
            "invalid_signals": self._stats.invalid_signals,
            "open_positions_count": open_positions_count,
            "pending_orders_count": len(self._pending_orders),
            "total_realized_pnl": float(self._stats.total_realized_pnl),
            "processed_correlations_count": len(self._processed_correlations),
            "execution_mode": self.execution_mode,
            "last_signal_ts": self._stats.last_signal_ts,
            "symbols_count": len(self.symbols_meta)
        }

    def reset_for_backtest(self) -> None:
        """Полный сброс состояния перед прогоном бэктеста"""
        try:
            self.logger.info("Resetting PositionManager for backtest...")

            # 1. In-memory очистка
            self._positions.clear()
            self._pending_orders.clear()
            self._processed_correlations.clear()
            self._active_stop_orders.clear()
            self._order_counter = 0
            self._position_ids.clear()

            # ✅ НОВОЕ: Очистка symbol states
            if hasattr(self, '_symbol_states'):
                self._symbol_states.clear()

            self._stats = PMStats()

            # 2. БД очистка через TradingLogger
            if hasattr(self, 'trade_log') and self.trade_log:
                self.trade_log.clear_trading_tables_for_backtest()
            else:
                self.logger.warning("TradingLogger not available, skipping DB cleanup")

            self.logger.info("PositionManager reset completed")

        except Exception as e:
            self.logger.error(f"Error in reset_for_backtest: {e}")
            raise

    # === Расчетные методы ===

    def quantize_qty(self, symbol: str, qty: Decimal) -> Decimal:
        """Квантовать количество согласно биржевым требованиям"""
        if symbol not in self.symbols_meta:
            raise PositionManagerError(f"Symbol {symbol} not found in metadata")

        meta = self.symbols_meta[symbol]
        step_size = meta.step_size

        # Округляем вниз до ближайшего step_size
        quantized = (qty // step_size) * step_size
        return quantized.quantize(Decimal('0.' + '0' * meta.quantity_precision))

    def quantize_price(self, symbol: str, price: Decimal) -> Decimal:
        """Квантовать цену согласно tick size"""
        if symbol not in self.symbols_meta:
            raise PositionManagerError(f"Symbol {symbol} not found in metadata")

        meta = self.symbols_meta[symbol]
        tick_size = meta.tick_size

        # Округляем до ближайшего tick_size
        quantized = round(price / tick_size) * tick_size
        return quantized.quantize(Decimal('0.' + '0' * meta.price_precision))

    def is_min_notional_met(self, symbol: str, qty: Decimal, price: Decimal) -> bool:
        """Проверить соответствие минимальному объёму сделки"""
        if symbol not in self.symbols_meta:
            return False

        meta = self.symbols_meta[symbol]
        notional = qty * price
        return notional >= meta.min_notional

    def build_entry_order(self, signal: TradeSignalIQTS, side: Literal["BUY", "SELL"]
    ) -> Optional[OrderReq]:
        """
        Построить ордер входа в позицию.
        Размер позиции ДОЛЖЕН быть в risk_context['position_size'].
        Если его нет - сигнал отклоняется.
        """
        symbol = signal.get("symbol", "UNKNOWN")

        try:
            if not symbol or symbol == "UNKNOWN":
                self.logger.error("❌ Missing or invalid symbol in signal")
                return None

            # ════════════════════════════════════════════════════════════
            # ШАГ 1: ИЗВЛЕЧЕНИЕ position_size из risk_context
            # ════════════════════════════════════════════════════════════

            risk_context = signal.get("risk_context", {})
            qty = None

            if risk_context and "position_size" in risk_context:
                qty_raw = risk_context["position_size"]

                if qty_raw and qty_raw > 0:
                    try:
                        qty = Decimal(str(qty_raw))
                        self.logger.info(
                            f"✅ Using position_size from risk_context: "
                            f"{float(qty):.4f} for {symbol}"
                        )
                    except (ValueError, TypeError, InvalidOperation) as e:
                        self.logger.error(
                            f"❌ Failed to convert position_size to Decimal: {e}"
                        )
                        qty = None

            # ✅ ИСПРАВЛЕНИЕ: Если position_size не найден - отклоняем сигнал
            if qty is None or qty <= 0:
                self.logger.error(
                    f"❌ position_size not provided or invalid for {symbol}!\n"
                    f"  risk_context: {risk_context}\n"
                    f"  position_size: {qty}\n"
                    f"  Signal REJECTED - position_size is mandatory."
                )
                raise InvalidOrderSizeError(
                    f"position_size not provided in risk_context for {symbol}"
                )

            # ════════════════════════════════════════════════════════════
            # ШАГ 2: ИЗВЛЕЧЕНИЕ decision_price
            # ════════════════════════════════════════════════════════════

            decision_price_raw = signal.get("decision_price")

            if not decision_price_raw or decision_price_raw <= 0:
                self.logger.error(
                    f"❌ Invalid decision_price for {symbol}: {decision_price_raw}"
                )
                raise InvalidSignalError(
                    f"Missing or invalid decision_price: {decision_price_raw}"
                )

            try:
                decision_price = Decimal(str(decision_price_raw))
            except (ValueError, TypeError, InvalidOperation) as e:
                self.logger.error(
                    f"❌ Failed to convert decision_price to Decimal: {e}"
                )
                raise InvalidSignalError(
                    f"Invalid decision_price format: {decision_price_raw}"
                ) from e

            # ════════════════════════════════════════════════════════════
            # ШАГ 3: СОЗДАНИЕ ORDERREQ
            # ════════════════════════════════════════════════════════════

            # Определяем тип ордера
            order_type: Literal["MARKET", "LIMIT"] = "MARKET"
            price = None

            if order_type == "LIMIT":
                price = self.quantize_price(symbol, decision_price)

            # Генерируем ID
            client_order_id = self._generate_unique_order_id(symbol, "entry")

            # Генерируем correlation_id если отсутствует
            correlation_id = signal.get("correlation_id")
            if not correlation_id:
                from iqts_standards import create_correlation_id
                correlation_id = create_correlation_id()

            order_req = OrderReq(
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                type=order_type,
                qty=qty,
                price=price,
                time_in_force="GTC" if order_type == "LIMIT" else None,
                reduce_only=False,
                correlation_id=correlation_id,
            )

            # ════════════════════════════════════════════════════════════
            # ШАГ 4: РЕГИСТРАЦИЯ В PENDING ORDERS
            # ════════════════════════════════════════════════════════════

            pending_order = PendingOrder(
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                type=order_type,
                qty=qty,
                price=price,
                correlation_id=correlation_id,
                reduce_only=False,
                metadata={
                    "signal_intent": signal.get("intent"),
                    "decision_price": float(decision_price),
                    "created_at": get_current_timestamp_ms(),
                    "stops_precomputed": signal.get("stops_precomputed", False)
                }
            )

            self._pending_orders[client_order_id] = pending_order

            self.logger.info(
                f"✅ Entry order built: {symbol} {side} "
                f"qty={float(qty):.4f} @ {order_type} "
                f"price={float(decision_price):.8f} "
                f"(client_order_id={client_order_id})"
            )

            return order_req

        except InvalidOrderSizeError:
            # Пробрасываем как есть
            raise

        except InvalidSignalError:
            # Пробрасываем как есть
            raise

        except KeyError as e:
            self.logger.error(
                f"❌ Missing required field in signal for {symbol}: {e}",
                exc_info=True
            )
            raise InvalidSignalError(
                f"Missing required field in signal: {e}"
            ) from e

        except Exception as e:
            self.logger.error(
                f"❌ Unexpected error building entry order for {symbol}: {e}",
                exc_info=True
            )
            raise InvalidSignalError(
                f"Unexpected error building entry order: {e}"
            ) from e

    def build_exit_order(self, signal: TradeSignalIQTS, position: PositionSnapshot,
                         reason: str) -> Optional[OrderReq]:
        """Построить ордер выхода из позиции"""
        try:
            from typing import cast, Literal

            symbol = signal["symbol"]

            if position["status"] == "FLAT":
                raise PositionNotFoundError(f"No position to exit for {symbol}")

            # Определяем сторону выхода (противоположную позиции)
            position_side = position["side"]

            # ✅ ИСПРАВЛЕНО: Явное приведение к Literal типу
            exit_side: Literal["BUY", "SELL"] = cast(
                Literal["BUY", "SELL"],
                "SELL" if position_side == "LONG" else "BUY"
            )

            # Количество - вся позиция
            qty = position["qty"]

            # Явное приведение типа ордера
            order_type: Literal["MARKET", "LIMIT", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"] = cast(
                Literal["MARKET", "LIMIT", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"],
                "MARKET"
            )

            # Создаем OrderReq
            client_order_id = self._generate_unique_order_id(symbol, "exit")

            order_req = OrderReq(
                client_order_id=client_order_id,
                symbol=symbol,
                side=exit_side,
                type=order_type,
                qty=qty,
                price=None,
                time_in_force=None,
                reduce_only=True,
                correlation_id=signal.get("correlation_id", create_correlation_id())
            )

            # Сохраняем в pending orders
            pending_order = PendingOrder(
                client_order_id=client_order_id,
                symbol=symbol,
                side=exit_side,
                type=order_type,
                qty=qty,
                price=None,
                correlation_id=order_req["correlation_id"],
                reduce_only=True,
                metadata=None # для exit ордеров metadata не нужен
            )
            self._pending_orders[client_order_id] = pending_order

            return order_req

        except Exception as e:
            raise InvalidSignalError(f"Error building exit order: {e}")


    def build_stop_order(self, signal: TradeSignalIQTS, position: PositionSnapshot,
                         new_stop_price: Decimal,
                         is_trailing: bool = False) -> Optional[OrderReq]:  # ✅ ДОБАВЛЕН параметр
        """
        Построить стоп-ордер (трейлинг-обновление или initial stop).

        Args:
            signal: Торговый сигнал
            position: Текущая позиция
            new_stop_price: Новая цена стопа
            is_trailing: True если это trailing stop, False если initial stop

        Returns:
            OrderReq или None
        """
        try:
            symbol = signal["symbol"]

            if position["status"] == "FLAT":
                raise PositionNotFoundError(f"No position for stop order: {symbol}")

            # Противоположная стороне позиции
            position_side = position["side"]
            stop_side = "SELL" if position_side == "LONG" else "BUY"

            # Кол-во — вся позиция
            qty = position["qty"]

            # Ключевой момент: для STOP_* используем stop_price, price = None
            stop_price = new_stop_price

            # ✅ ИСПОЛЬЗУЕМ ПАРАМЕТР is_trailing
            tag = "trail_stop" if is_trailing else "auto_stop"

            # Диагностическое логирование
            self.logger.warning(
                f"🟢 build_stop_order: symbol={symbol} is_trailing={is_trailing} tag={tag}"
            )

            client_order_id = self._generate_unique_order_id(symbol, tag)

            self.logger.warning(
                f"✅ Generated client_order_id: {client_order_id}"
            )

            # ✅ Разные correlation_id для разных типов стопов
            correlation_id = (
                f"trail_{symbol}_{get_current_timestamp_ms()}"
                if is_trailing
                else f"auto_stop_{symbol}_{get_current_timestamp_ms()}"
            )

            order_req = OrderReq(
                client_order_id=client_order_id,
                symbol=symbol,
                side=cast(Literal["BUY", "SELL"], stop_side),
                type="STOP_MARKET",
                qty=qty,
                price=None,
                stop_price=stop_price,
                time_in_force="GTC",
                reduce_only=True,
                correlation_id=correlation_id,
                metadata={
                    "reason": "trailing_stop_update" if is_trailing else "initial_stop",
                    "position_side": position_side,
                    "is_trailing_stop": is_trailing,  # ✅ Явный флаг
                    **(signal.get("metadata") or {})
                }
            )

            # Регистрируем в pending, как и остальные ордера
            pending_order = PendingOrder(
                client_order_id=client_order_id,
                symbol=symbol,
                side=cast(Literal["BUY", "SELL"], stop_side),
                type="STOP_MARKET",
                qty=qty,
                price=None,
                stop_price=stop_price,
                correlation_id=order_req["correlation_id"],
                reduce_only=True,
                metadata={"is_trailing_stop": is_trailing,
                    "reason": "trailing_stop_update" if is_trailing else "initial_stop",
                    "position_side": position_side}
            )
            self._pending_orders[client_order_id] = pending_order

            # Обновляем отслеживание активных стопов
            self._update_active_stop_tracking(symbol, {
                "client_order_id": client_order_id,
                "stop_price": float(stop_price),
                "side": stop_side,
                "position_side": position_side,  # ✅ ДОБАВЛЕНО
                "correlation_id": order_req["correlation_id"],
                "created_at": get_current_timestamp_ms(),
                "is_trailing": is_trailing  # ✅ ДОБАВЛЕНО
            })

            self.logger.info(
                f"{'Trailing' if is_trailing else 'Initial'} stop created: "
                f"{symbol} {position_side} stop_price={float(stop_price):.8f} "
                f"client_order_id={client_order_id}"
            )

            return order_req

        except Exception as e:
            self.logger.error(f"Error building stop order: {e}")
            return None

    def _update_active_stop_tracking(self, symbol: str, stop_info: Dict[str, Any]) -> None:
        """Обновить отслеживание активных стоп-ордеров"""
        try:
            # Если уже есть стоп для символа, отменяем предыдущий
            if symbol in self._active_stop_orders:
                old_stop = self._active_stop_orders[symbol]
                self.logger.debug(
                    f"Replacing existing stop for {symbol}: {old_stop.get('stop_price')} -> {stop_info.get('stop_price')}")

            self._active_stop_orders[symbol] = stop_info
            self.logger.debug(f"Updated stop tracking for {symbol}: {stop_info.get('stop_price')}")

        except Exception as e:
            self.logger.error(f"Error updating stop tracking for {symbol}: {e}")

    def _remove_active_stop_tracking(self, symbol: str) -> None:
        """Удалить отслеживание стоп-ордера"""
        try:
            if symbol in self._active_stop_orders:
                removed_stop = self._active_stop_orders.pop(symbol)
                self.logger.debug(f"Removed stop tracking for {symbol}: {removed_stop.get('stop_price')}")
        except Exception as e:
            self.logger.error(f"Error removing stop tracking for {symbol}: {e}")

    def get_active_stops(self) -> Dict[str, Dict[str, Any]]:
        """Получить информацию о всех активных стоп-ордерах"""
        return self._active_stop_orders.copy()

    def _get_trailing_config(self, symbol: str) -> Dict[str, Any]:
        """Получить конфигурацию trailing stop из config.py"""
        try:
            from config import get_trailing_stop_config
            return get_trailing_stop_config(symbol)
        except Exception as e:
            self.logger.error(f"Error getting trailing config for {symbol}: {e}")
            return {
                "enabled": False,
                "trailing_percent": 1.5,
                "min_profit_percent": 0.5,
                "activation_delay_candles": 3,
                "max_updates_per_position": 20,
                "price_change_threshold_percent": 0.1
            }

    def _get_current_stop_price(self, symbol: str) -> Optional[float]:
        """
        Получить цену текущего активного стоп-ордера.

        Source of truth: ExchangeManager._active_orders
        Fallback: PositionManager._active_stop_orders (кэш)

        Returns:
            float: Цена стопа или None если стоп отсутствует
        """
        try:
            # ✅ ПРИОРИТЕТ 1: ExchangeManager (источник истины)
            if hasattr(self, 'exchange_manager') and self.exchange_manager:
                if hasattr(self.exchange_manager, 'get_active_orders'):
                    try:
                        active_orders = self.exchange_manager.get_active_orders(symbol)
                        for order in active_orders:
                            if order["type"] in ["STOP_MARKET", "STOP"] and order.get("stop_price"):
                                stop_price = order["stop_price"]
                                self.logger.debug(
                                    f"Current stop_price from ExchangeManager for {symbol}: {stop_price}"
                                )
                                return stop_price
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to get active orders from ExchangeManager for {symbol}: {e}"
                        )

            # ✅ ПРИОРИТЕТ 2: Fallback на кэш PM (если EM недоступен)
            if hasattr(self, '_active_stop_orders') and symbol in self._active_stop_orders:
                stop_price = self._active_stop_orders[symbol].get("stop_price")
                if stop_price:
                    self.logger.debug(
                        f"Using cached stop_price for {symbol}: {stop_price} "
                        "(ExchangeManager unavailable)"
                    )
                    return stop_price

            # Стоп отсутствует
            self.logger.debug(f"No active stop found for {symbol}")
            return None

        except Exception as e:
            self.logger.error(f"Error getting current stop price for {symbol}: {e}")
            return None

