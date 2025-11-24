
"""
run_bot.py - модуль запуска торговой системы
Инициализацию всех компонентов системы: логгер, стратегия, позиции, исполнение, агрегация данных.
Управление запуском и остановкой бота с обработкой сигналов (SIGINT/SIGTERM).
Работу в разных режимах: LIVE, DEMO, BACKTEST.
Централизованное логирование событий и ошибок через систему событий (BotLifecycleEvent).
Генерацию финального отчёта по результатам бэктеста.
"""

from __future__ import annotations
import asyncio
import logging
import signal
from dataclasses import dataclass, field
from typing import Optional, Any, List, Dict, cast, Literal, Callable, Tuple
from market_data_utils import ensure_market_schema
from sqlalchemy import create_engine
from datetime import datetime, UTC
from market_history import MarketHistoryManager
from risk_manager import EnhancedRiskManager, RiskLimits
import contextlib
from iqts_standards import (
    get_current_timestamp_ms,
    BotLifecycleEvent,
    BotLifecycleEventHandler,
    AlertCallback,
    BotLifecycleError,
    ComponentInitializationError,
    StrategyInterface,
    PositionManagerInterface,
    ExchangeManagerInterface,
    MarketAggregatorInterface,
    MainBotInterface,
    Candle1m, OrderUpd,
)
import sys
from pathlib import Path
from market_aggregator import MarketAggregatorFactory
from ImprovedQualityTrendSystem import ImprovedQualityTrendSystem
from signal_validator import SignalValidator
from trading_logger import TradingLogger
from exit_system import AdaptiveExitManager
import config as cfg
import os

validator = SignalValidator(strict_mode=False)
exit_manager = AdaptiveExitManager(
    global_timeframe="5m",
    trend_timeframe="1m")

# === Components Container ===
@dataclass
class ComponentsContainer:
    """Bot dependencies container (created at startup)."""
    trade_log: Any
    position_manager: PositionManagerInterface
    exchange_manager: ExchangeManagerInterface
    strategy: StrategyInterface
    market_aggregator: MarketAggregatorInterface
    main_bot: MainBotInterface
    exit_manager: Any
    risk_manager: Optional[Any]
    logger: logging.Logger
    history_manager: Optional[MarketHistoryManager] = None
    async_store: Optional[Any] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

# === Bot Lifecycle Manager ===
class BotLifecycleManager:
    """
    Bot lifecycle manager:
      - start(): assembly and bootstrap
      - monitoring (optional)
      - stop(): graceful shutdown
      - Event model for component state tracking
    """

    def __init__(self,
                 config: Dict[str, Any],
                 *,
                 event_handlers: Optional[List[BotLifecycleEventHandler]] = None,
                 shutdown_timeout_seconds: float = 30.0) -> None:
        """Prepares internal state: shutdown_event, components container, etc."""
        self.config = config
        self.shutdown_timeout = shutdown_timeout_seconds
        self._shutdown_event = asyncio.Event()
        self._components: Optional[ComponentsContainer] = None
        self._event_handlers = event_handlers or []
        self._is_running = False
        self._monitoring_task: Optional[asyncio.Task] = None
        self._main_loop_task: Optional[asyncio.Task] = None
        self._trading_task: Optional[asyncio.Task] = None
        self.logger = logging.getLogger(__name__)
        self._stopping = False
        self._stop_lock = asyncio.Lock()

    # ---------- Event system ----------
    def add_event_handler(self, handler: BotLifecycleEventHandler) -> None:
        """Add lifecycle event handler"""
        self._event_handlers.append(handler)

    def remove_event_handler(self, handler: BotLifecycleEventHandler) -> None:
        """Remove lifecycle event handler"""
        if handler in self._event_handlers:
            self._event_handlers.remove(handler)

    def _emit_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Internal method to emit event to all subscribers"""
        event: BotLifecycleEvent = {
            "event_type": event_type,
            "timestamp_ms": get_current_timestamp_ms(),
            "data": data
        }
        for handler in self._event_handlers:
            try:
                handler(event)
            except Exception as e:
                logging.error(f"Error in lifecycle event handler: {e}")

    async def _create_history_manager(self, market_data_utils: Any, logger: logging.Logger) -> MarketHistoryManager:
        """Create and initialize MarketHistoryManager."""
        try:
            # Создаем асинхронный engine, если его нет в market_data_utils
            if not hasattr(market_data_utils, 'aengine') or market_data_utils.aengine is None:
                from sqlalchemy.ext.asyncio import create_async_engine
                market_db_dsn = self.config.get("market_db_dsn", "sqlite+aiosqlite:///data/market_data.sqlite")

                # Конвертируем sync DSN в async DSN если нужно
                if isinstance(market_db_dsn, str) and market_db_dsn.startswith("sqlite:///"):
                    market_db_dsn = market_db_dsn.replace("sqlite:///", "sqlite+aiosqlite:///")

                market_data_utils.aengine = create_async_engine(market_db_dsn, future=True, echo=False)

            history_manager = MarketHistoryManager(
                engine=market_data_utils.aengine,
                market_data_utils=market_data_utils,
                logger=logger  # ✅ Используем переданный logger, а не создаем новый
            )

            logger.info(f"MarketHistoryManager created at {history_manager.created_at.isoformat()}")
            return history_manager

        except Exception as e:
            error_msg = f"Failed to create MarketHistoryManager: {e}"
            logger.error(error_msg)
            raise ComponentInitializationError(error_msg)

    async def wait_for_shutdown(self) -> None:
        """
        Блокирует выполнение до запроса остановки (Ctrl+C/SIGTERM или вызов stop()).
        Безопасно вызывать из нескольких мест — ожидание завершится, когда событие установлено.
        """
        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            # Если таск отменили извне, фиксируем shutdown и пробрасываем исключение.
            self._shutdown_event.set()
            raise

    async def stop(self) -> None:
        async with self._stop_lock:
            if not self._is_running or self._stopping:
                return
            self._stopping = True
            current_task = asyncio.current_task()

        try:
            self._emit_event("LIFECYCLE_STOPPING", {})
            self._shutdown_event.set()

            # --- Cancel background tasks safely ---
            tasks_to_wait: List[asyncio.Task] = []

            # Cancel trading task if it's not this very task and still alive
            if self._trading_task and not self._trading_task.done():
                if self._trading_task is not current_task:
                    self._trading_task.cancel()
                    tasks_to_wait.append(self._trading_task)

            # Cancel main loop if it's not this very task and still alive
            if self._main_loop_task and not self._main_loop_task.done():
                if self._main_loop_task is not current_task:
                    self._main_loop_task.cancel()
                    tasks_to_wait.append(self._main_loop_task)

            # Cancel monitoring task if alive
            if self._monitoring_task and not self._monitoring_task.done():
                self._monitoring_task.cancel()
                tasks_to_wait.append(self._monitoring_task)

            # --- Await tasks completion with timeout ---
            if tasks_to_wait:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks_to_wait, return_exceptions=True),
                        timeout=self.shutdown_timeout
                    )
                except asyncio.TimeoutError:
                    self._emit_event("SHUTDOWN_TIMEOUT_WARNING", {"timeout": self.shutdown_timeout})
                    # Ensure tasks are cancelled, then swallow any exceptions from them
                    for task in tasks_to_wait:
                        task.cancel()
                    await asyncio.gather(*tasks_to_wait, return_exceptions=True)

            # --- Cleanup components ---
            await self._cleanup()
            self._emit_event("LIFECYCLE_STOPPED", {})

        except asyncio.CancelledError:
            # Do not swallow cancellation: propagate it further
            self._emit_event("LIFECYCLE_STOP_CANCELLED", {})
            raise
        except Exception as e:
            self._emit_event("LIFECYCLE_STOP_FAILED", {"error": str(e)})
            raise BotLifecycleError(f"Failed to stop bot: {e}") from e
        finally:
            # Reset state regardless of outcome
            self._is_running = False
            self._stopping = False
            self._trading_task = None
            self._main_loop_task = None
            self._monitoring_task = None

    # ---------- Main loops ----------
    async def _run_main_loop(self) -> None:
        """Главный цикл жизненного цикла. Задача мониторинга"""
        logger = logging.getLogger(__name__)
        try:
            execution_mode = self.config.get("execution_mode", "DEMO")
            backtest_cfg = self.config.get("backtest", {})
            auto_shutdown = bool(backtest_cfg.get("auto_shutdown", False))

            logger.info(f"Main loop started in {execution_mode} mode, auto_shutdown={auto_shutdown}")

            iteration = 0

            while not self._shutdown_event.is_set():
                try:
                    iteration += 1

                    if self._components:
                        await self._check_components_health()

                    if execution_mode == "BACKTEST" and auto_shutdown:
                        if self._components and self._components.market_aggregator:
                            backtest_completed = getattr(
                                self._components.market_aggregator,
                                "backtest_completed",
                                False
                            )

                            if backtest_completed:
                                logger.info("Backtest completed, initiating auto-shutdown...")

                                self._emit_event("BACKTEST_COMPLETED", {
                                    "auto_shutdown": True,
                                    "execution_mode": execution_mode
                                })

                                await asyncio.sleep(2.0)
                                await self.stop()
                                return

                    try:
                        await asyncio.wait_for(self._shutdown_event.wait(), timeout=5.0)
                        break
                    except asyncio.TimeoutError:
                        continue

                except Exception as e:
                    logger.error(f"Error in main loop iteration: {e}")
                    self._emit_event("MAIN_LOOP_ITERATION_ERROR", {"error": str(e)})
                    await asyncio.sleep(5.0)

        except asyncio.CancelledError:
            logger.info("Main loop cancelled")
            raise
        except Exception as e:
            logger.exception("Fatal error in main loop: %s", e)
            self._emit_event("MAIN_LOOP_ERROR", {"error": str(e)})
            try:
                await self.stop()
            except Exception:
                pass
            self._shutdown_event.set()

    async def _run_main_bot_monitoring(self) -> None:
        """Periodic monitoring (timer-based)."""
        try:
            while not self._shutdown_event.is_set():
                if self._components:
                    try:
                        # Основная статистика
                        if self._components.main_bot:
                            stats = self._components.main_bot.get_stats()
                            self._emit_event("MONITORING_STATS", {"stats": stats})

                            health = self._components.main_bot.get_component_health()
                            unhealthy = [
                                k for k, v in health.items()
                                if isinstance(v, str) and v.lower() not in ("healthy", "connected")
                            ]
                            if unhealthy:
                                self._emit_event("COMPONENTS_UNHEALTHY", {"unhealthy": unhealthy, "health": health})

                        # Мониторинг history_manager
                        if self._components.history_manager:
                            history_uptime = (
                                    datetime.now(UTC) - self._components.history_manager.created_at
                            ).total_seconds()

                            buffer_stats = {}
                            if hasattr(self._components.history_manager, 'get_buffer_stats'):
                                buffer_stats = self._components.history_manager.get_buffer_stats()

                            self._emit_event("HISTORY_MANAGER_STATUS", {
                                "uptime_seconds": history_uptime,
                                "buffers": buffer_stats,
                                "created_at": self._components.history_manager.created_at.isoformat()
                            })

                    except Exception as e:
                        self._emit_event("MONITORING_ERROR", {"error": str(e)})

                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=60.0)
                    break
                except asyncio.TimeoutError:
                    continue

        except Exception as e:
            self._emit_event("MONITORING_CRITICAL_ERROR", {"error": str(e)})
            self._shutdown_event.set()

    # ---------- Component management ----------
    async def _create_components(self) -> ComponentsContainer:
        """Creation and initialization of all bot components with shared strategy (+ DI risk/exit managers)"""
        try:
            logger = self._create_logger()
            trade_log = await self._create_trade_log(logger)
            async_store = await self._create_async_store() if self.config.get("use_async_store") else None

            # --- MarketDataUtils ---
            from market_data_utils import MarketDataUtils
            if not hasattr(trade_log, 'market_engine') or trade_log.market_engine is None:
                logger.error("TradingLogger.market_engine is None - cannot create MarketDataUtils")
                raise ComponentInitializationError("TradingLogger.market_engine not initialized")

            market_data_utils = MarketDataUtils(
                market_engine=trade_log.market_engine,
                logger=logger
            )
            logger.info("MarketDataUtils created successfully")

            # --- History Manager ---
            history_manager = await self._create_history_manager(
                market_data_utils=market_data_utils,
                logger=logger
            )

            # --- Strategy (singleton) ---
            strategy = await self._create_strategy(logger)

            # --- Risk Manager (DI) ---
            risk_manager = None
            if EnhancedRiskManager:
                limits_cfg = self.config.get("risk_limits", {})
                limits = RiskLimits(
                    max_portfolio_risk=float(limits_cfg.get("max_portfolio_risk", 0.02)),
                    max_daily_loss=float(limits_cfg.get("max_daily_loss", 0.05)),
                    max_position_value_pct=float(limits_cfg.get("max_position_value_pct", 0.30)),
                    stop_loss_atr_multiplier=float(limits_cfg.get("stop_loss_atr_multiplier", 2.0)),
                    take_profit_atr_multiplier=float(limits_cfg.get("take_profit_atr_multiplier", 3.0)),
                    atr_periods=int(limits_cfg.get("atr_periods", 14))
                )
                risk_manager = EnhancedRiskManager(limits)
                logger.info("✅ EnhancedRiskManager created via DI")
            else:
                logger.warning("RiskManager not available (import failed), DI skipped")

            # --- Exit Manager (DI) ---
            exit_manager = await self._create_exit_manager(logger)

            # --- SignalValidator (DI) ---
            validator = SignalValidator(
                strict_mode=self.config.get("validation", {}).get("strict_mode", False),
                logger=logger
            )
            logger.info("✅ SignalValidator created (DI)")

            # --- Exchange Manager (нужен до PositionManager для связки) ---
            exchange_manager = await self._create_exchange_manager(trade_log, logger)

            # --- PositionManager with DI ---
            position_manager = await self._create_position_manager(
                trade_log=trade_log,
                logger=logger,
                signal_validator=validator  # ✅ Передаём validator через DI
            )

            # Внедрение зависимостей, если не переданы через конструктор
            if hasattr(position_manager, 'risk_manager') and not position_manager.risk_manager and risk_manager:
                position_manager.risk_manager = risk_manager
                logger.info("🔗 Injected risk_manager into PositionManager")

            if hasattr(position_manager, 'exit_manager') and not position_manager.exit_manager and exit_manager:
                position_manager.exit_manager = exit_manager
                logger.info("🔗 Injected exit_manager into PositionManager")

            # Связка execution engine
            position_manager.execution_engine = exchange_manager
            logger.info("✅ execution_engine linked to PositionManager")

            # --- Market Aggregator ---
            market_aggregator = await self._create_market_aggregator(
                logger=logger,
                trade_log=trade_log
            )

            # --- Main Bot (передаём strategy, PM, EM, exit_manager, risk_manager) ---
            main_bot = await self._create_main_bot(
                market_aggregator=market_aggregator,
                strategy=strategy,
                position_manager=position_manager,
                exchange_manager=exchange_manager,
                exit_manager=exit_manager,
                risk_manager=risk_manager,
                trade_log=trade_log,
                market_data_utils=market_data_utils,
                logger=logger,
                validator=validator,
            )

            return ComponentsContainer(
                trade_log=trade_log,
                position_manager=position_manager,
                exchange_manager=exchange_manager,
                strategy=strategy,
                market_aggregator=market_aggregator,
                main_bot=main_bot,
                exit_manager=exit_manager,
                risk_manager=risk_manager,
                logger=logger,
                history_manager=history_manager,
                async_store=async_store,
            )

        except Exception as e:
            raise ComponentInitializationError(f"Failed to create components: {e}") from e

    def _create_logger(self) -> logging.Logger:
        """
        Централизованная настройка логирования для всей системы.
        Настраивает корневой логгер - все дочерние логгеры автоматически унаследуют конфигурацию.
        """
        # ✅ Настраиваем КОРНЕВОЙ логгер (root logger)
        root_logger = logging.getLogger()

        # Очищаем существующие handlers (идемпотентность)
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        # Устанавливаем уровень
        root_logger.setLevel(self.config.get("log_level", "INFO"))

        # Формат сообщений
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        # ✅ Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        # ✅ File handler (если включен)
        try:
            log_file = cfg.LOGGING_CONFIG.get("file_path", "trading_bot.log")
            if log_file:
                file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
                file_handler.setFormatter(formatter)
                root_logger.addHandler(file_handler)
                self.logger.info(f"✅ File logging enabled: {log_file}")
        except Exception as e:
            print(f"⚠️ Failed to setup file logging: {e}")

        # ✅ Возвращаем именованный логгер для BotLifecycleManager
        logger = logging.getLogger("TradingBot")
        logger.info("Logging system initialized (centralized configuration)")

        return logger

    async def _create_trade_log(self, logger: logging.Logger) -> TradingLogger:
        """Create trade logging system."""
        try:
            market_dsn = self.config.get("market_db_dsn", cfg.MARKET_DB_DSN)
            trades_dsn = self.config.get("trading_db_dsn", cfg.TRADING_DB_DSN)

            def dsn_to_path(dsn: str) -> str:
                return dsn.replace("sqlite:///", "") if isinstance(dsn, str) and dsn.startswith("sqlite:///") else dsn

            market_db_path = dsn_to_path(market_dsn)
            trades_db_path = dsn_to_path(trades_dsn)

            self._ensure_database_structure(market_db_path, trades_db_path)

            db_cfg = self.config.get("database", {})
            trade_log = TradingLogger(
                market_db_path=market_db_path,
                trades_db_path=trades_db_path,
                on_alert=self._create_alert_callback(),
                pool_size=int(db_cfg.get("pool_size", 4)),
                enable_async=bool(self.config.get("enable_async_logging", True)),
                logger_instance=logger
            )

            async def on_candle_ready(symbol: str, candle: Dict[str, Any], recent: List[Dict[str, Any]]) -> None:
                """
                Обработчик готовой свечи - ТОЛЬКО логирование.
                Вся торговая логика находится в EnhancedTradingBot.on_candle_ready()
                """
                try:
                    # Сохраняем последнюю свечу для истории
                    if hasattr(trade_log, '_last_candle'):
                        trade_log._last_candle[symbol] = dict(candle)
                    
                    timeframe = candle.get('_timeframe', '?')
                    logger.debug(
                        f"Candle logged: {symbol} {timeframe} @ {candle['ts']} "
                        f"O:{float(candle['open']):.2f} "
                        f"H:{float(candle['high']):.2f} "
                        f"L:{float(candle['low']):.2f} "
                        f"C:{float(candle['close']):.2f}"
                    )
                except Exception as err:
                    logger.error(f"Error logging candle for {symbol}: {err}")

            async def on_market_event(event: Dict[str, Any]) -> None:
                """Обработчик рыночных событий"""
                try:
                    event_type = event.get("event_type")
                    if event_type:
                        logger.debug(f"Market event: {event_type}")
                except Exception as err:
                    logger.error(f"Error processing market event: {err}")

            def on_connection_state_change(state: Dict[str, Any]) -> None:
                """Обработчик изменения состояния соединения"""
                try:
                    status = state.get("status", "unknown")
                    logger.info(f"Market connection state: {status}")

                    # Эмитим событие для BotLifecycleManager
                    if hasattr(self, '_emit_event'):
                        if status == "connected":
                            self._emit_event("MARKET_CONNECTED", {"status": status})
                        elif status == "disconnected":
                            self._emit_event("MARKET_DISCONNECTED", {"status": status})
                        elif status == "error":
                            error_msg = state.get("error_message", "unknown error")
                            self._emit_event("MARKET_CONNECTION_ERROR", {
                                "status": status,
                                "error": error_msg
                            })
                except Exception as err:
                    logger.error(f"Error processing connection state: {err}")

            # Присваиваем методы экземпляру TradingLogger
            trade_log.on_candle_ready = on_candle_ready
            trade_log.on_market_event = on_market_event
            trade_log.on_connection_state_change = on_connection_state_change
            trade_log._last_candle = {}  # Для хранения последних свечей

            if getattr(trade_log, "enable_async", False) and callable(getattr(trade_log, "start_async", None)):
                await trade_log.start_async()

            logger.info("✅ TradingLogger created successfully with strategy integration")
            return trade_log

        except Exception as e:
            logger.error(f"Failed to create TradingLogger: {e}", exc_info=True)
            raise ComponentInitializationError(f"TradingLogger creation failed: {e}") from e

    def _ensure_database_structure(self, market_db_path: str, trading_db_path: str) -> None:
        """✅ УПРОЩЕНО: Обеспечивает структуру БД через существующие методы."""
        try:

            for db_path in [market_db_path, trading_db_path]:
                if db_path:
                    db_dir = os.path.dirname(db_path)
                    if db_dir:
                        os.makedirs(db_dir, exist_ok=True)
                        logging.info(f"✅ Directory ensured: {db_dir}")

            try:


                market_engine = create_engine(f"sqlite:///{market_db_path}")
                ensure_market_schema(market_engine)
                logging.info(f"✅ Market database schema ensured: {market_db_path}")
                market_engine.dispose()

            except ImportError as e:
                logging.error(f"❌ Failed to import market_data_utils: {e}")
                raise
            except Exception as e:
                logging.error(f"❌ Failed to ensure market schema: {e}")
                raise

            logging.info(f"✅ Database structures ensured")

        except Exception as e:
            logging.error(f"❌ Failed to ensure database structures: {e}")
            raise

    def _create_alert_callback(self) -> AlertCallback:
        """Create callback for critical notifications"""

        def alert_handler(level: str, data: Dict[str, Any]) -> None:
            try:
                if level == "error":
                    self._emit_event("CRITICAL_ERROR", data)
                elif level == "warning":
                    self._emit_event("WARNING", data)
                else:
                    self._emit_event("ALERT", {"level": level, "data": data})
            except Exception as e:
                logging.error(f"Alert handler error: {e}")

        return alert_handler

    async def _create_async_store(self) -> Any:
        """Create async storage (optional)"""
        return None

    async def _create_strategy(self, logger: logging.Logger):
        """Создание стратегии с проверкой совместимости интерфейсов"""
        logger.info("Creating ImprovedQualityTrendSystem")
        system_cfg = self.config.get("trading_system", {})
        strategy_obj = ImprovedQualityTrendSystem(
            config=system_cfg,
            data_provider=None
        )

        # ✅ ПРОВЕРКА СОВМЕСТИМОСТИ
        required_methods = ['analyze_and_trade', 'generate_signal', 'get_system_status',
                            'update_performance', 'get_performance_report']

        for method in required_methods:
            if not hasattr(strategy_obj, method):
                logger.error(f"❌ Strategy missing required method: {method}")
                raise ComponentInitializationError(f"Strategy missing {method}")
            else:
                logger.info(f"✅ Strategy has method: {method}")

        strategy_iface = cast(StrategyInterface, strategy_obj)
        logger.info("✅ ImprovedQualityTrendSystem created and interface validated")
        return strategy_iface

    async def _create_position_manager(
        self,
        trade_log: Any,
        logger: logging.Logger,
        signal_validator: Optional[Any] = None
    ) -> PositionManagerInterface:
        """
        Create PositionManager with Dependency Injection.

        Args:
            trade_log: TradingLogger instance
            logger: Logger instance
            signal_validator: Optional SignalValidator for DI

        Returns:
            PositionManagerInterface instance
        """
        logger.info("Creating PositionManager with DI")

        symbols = self.config.get("symbols", [])
        symbols_meta: Dict[str, Dict[str, Any]] = {}

        for s in symbols:
            meta = cfg.get_symbol_config(s)
            symbols_meta[s] = meta

        from position_manager import PositionManager

        execution_mode = cast(
            Literal["LIVE", "DEMO", "BACKTEST"],
            self.config.get("execution_mode", "DEMO")
        )

        pm = PositionManager(
            symbols_meta=symbols_meta,
            db_dsn=self.config.get("trading_db_dsn"),
            trade_log=trade_log,
            price_feed=None,
            execution_mode=execution_mode,
            db_engine=None,
            signal_validator=signal_validator  # ✅ DI для SignalValidator
        )
        # Привяжем exchange_manager позже, после его создания
        logger.info("PositionManager created successfully with SignalValidator DI")
        return cast(PositionManagerInterface, pm)

    async def _create_exchange_manager(self, trade_log: Any, logger: logging.Logger) -> ExchangeManagerInterface:
        """Create ExchangeManager"""
        logger.info("Creating ExchangeManager")

        # 1) Определяем режим и складываем конфиги по приоритету:
        #    локальный self.config["exchange"] перекрывает cfg.EXECUTION_MODES[mode]
        mode = self.config.get("execution_mode", cfg.EXECUTION_MODE)
        exec_cfg_mode = cfg.EXECUTION_MODES.get(mode, {}) or {}
        exec_cfg_local = self.config.get("exchange", {}) or {}

        base_url = exec_cfg_local.get("base_url") or exec_cfg_mode.get("base_url")
        ws_url = exec_cfg_local.get("ws_url") or exec_cfg_mode.get("ws_url")
        timeout_seconds = int(exec_cfg_local.get("timeout_seconds", exec_cfg_mode.get("timeout_seconds", 30)))

        if not base_url:
            raise ComponentInitializationError(f"Missing base_url for execution mode: {mode}")

        # 2) Явные флаги режима
        demo_mode = bool(exec_cfg_local.get("demo_mode", exec_cfg_mode.get("demo_mode", mode == "DEMO")))
        is_testnet = bool(exec_cfg_local.get("testnet", exec_cfg_mode.get("testnet", False)))

        # 3) Безопасный on_order_update (одно определение)
        def on_order_update(fill: OrderUpd) -> None:
            try:
                pm = getattr(self._components, "position_manager", None)
                if pm is None:
                    logger.warning("Order update received before position_manager is ready: %s", fill)
                    return
                pm.update_on_fill(fill)
            except Exception as e:
                logger.error("on_order_update error: %s", e)

        symbols_meta = self.config.get("symbols_meta")

        if not symbols_meta:
            # Создаём базовые метаданные для основных символов
            symbols_meta = {
                "ETHUSDT": {
                    "tick_size": 0.01,
                    "step_size": 0.001,
                    "min_notional": 5.0,
                    "price_precision": 2,
                    "quantity_precision": 3
                }

            }
            logger.warning(
                "⚠️ symbols_meta not in config, using defaults for ETHUSDT, BTCUSDT"
            )

        from exchange_manager import ExchangeManager

        em = ExchangeManager(
            base_url=base_url,
            on_order_update=on_order_update,
            trade_log=trade_log,
            demo_mode=demo_mode,
            is_testnet=is_testnet,
            logger_instance=logger,
            metrics=None,
            event_handlers=None,
            ws_url=ws_url,
            execution_mode=mode,
            timeout_seconds=timeout_seconds,
            symbols_meta=symbols_meta  # ✅ ПЕРЕДАТЬ
        )

        logger.info("ExchangeManager created successfully")
        return cast(ExchangeManagerInterface, em)

    async def _create_exit_manager(self, logger: logging.Logger) -> Any:
        """✅ ДОБАВЛЕНО: Create AdaptiveExitManager"""
        logger.info("Creating AdaptiveExitManager")

        try:
            from exit_system import AdaptiveExitManager

            strategy_config = self.config.get("strategy", {})
            quality_detector_config = strategy_config.get("quality_detector", {})

            exit_manager = AdaptiveExitManager(
                global_timeframe=cast(Literal["1m", "5m", "15m", "1h"],
                                     quality_detector_config.get("global_timeframe", "5m")),
                trend_timeframe=cast(Literal["1m", "5m", "15m", "1h"],
                                    quality_detector_config.get("trend_timeframe", "1m")),
            )

            logger.info("AdaptiveExitManager created successfully")
            return exit_manager

        except ImportError as e:
            logger.error(f"Failed to import AdaptiveExitManager: {e}")
            raise ComponentInitializationError(f"AdaptiveExitManager is required: {e}")
        except Exception as e:
            logger.error(f"Failed to create AdaptiveExitManager: {e}")
            raise ComponentInitializationError(f"AdaptiveExitManager creation failed: {e}")

    async def _create_market_aggregator(
            self,
            logger: logging.Logger,
            trade_log: Any
    ) -> MarketAggregatorInterface:
        """Create market data aggregator"""
        try:
            # Создаем агрегатор
            market_aggregator = MarketAggregatorFactory.create_market_aggregator(
                execution_mode=self.config["execution_mode"],
                config=self.config,
                on_candle_ready=trade_log.on_candle_ready,
                on_connection_state_change=trade_log.on_connection_state_change,
                event_handlers=[trade_log.on_market_event],
                logger_instance=logger,
                trading_logger=trade_log
            )

            logger.info("Market aggregator created successfully")
            return market_aggregator

        except Exception as e:
            logger.error(f"Failed to create market aggregator: {e}")
            raise ComponentInitializationError(f"Market aggregator creation failed: {e}")

    async def start(self) -> None:
        """Assembles dependencies, loads history, calls main_bot.bootstrap(), starts aggregator and monitoring."""
        if self._is_running:
            raise BotLifecycleError("Bot is already running")

        try:
            self._emit_event("LIFECYCLE_STARTING", {"config": self.config})

            self._components = await self._create_components()
            self._emit_event("COMPONENTS_CREATED", {"components": list(self._components.__dict__.keys())})

            # Загрузка исторических данных перед разогревом бота
            if self._components.history_manager:
                symbols = self.config.get("symbols", [])
                days_back = self.config.get("history_days_back", 1)

                self.logger.info(f"Loading {days_back} days of history for {symbols}...")
                try:
                    if self.config.get("execution_mode") == "BACKTEST":
                        self.logger.info("BACKTEST: пропускаем загрузку истории с Binance")
                        history_results = {
                            "loaded": True,
                            "backtest_skip": True,
                            "symbols": symbols,
                            "source": "local_database"
                        }
                        self._emit_event("HISTORY_LOADED", {"results": history_results})
                    else:
                        self.logger.info("LIVE/DEMO mode → loading recent history from Binance")
                        history_results = await asyncio.wait_for(
                            self._components.history_manager.load_history(
                                symbols=self.config["symbols"],
                                days=1
                            ),
                            timeout=120.0
                        )

                    # === Правильная обработка результата загрузки истории ===
                    if isinstance(history_results, dict):
                        if history_results.get("backtest_skip") or history_results.get("source") == "local_db_skip":
                            self.logger.info(
                                "BACKTEST: загрузка истории с Binance пропущена — используются данные из локальной БД")
                        elif "loaded" in history_results and not history_results.get("loaded", True):
                            self.logger.warning(
                                f"Загрузка истории не удалась: {history_results.get('error', 'unknown error')}")
                        else:
                            # Это нормальный результат от Binance — можно логировать по символам
                            for symbol, counts in history_results.items():
                                if isinstance(counts, dict):
                                    self.logger.info(
                                        f"History loaded for {symbol}: "
                                        f"1m={counts.get('1m', 0)}, 5m={counts.get('5m', 0)} candles"
                                    )
                    else:
                        self.logger.warning(f"Неожиданный формат history_results: {type(history_results)}")

                    self._emit_event("HISTORY_LOADED", {"results": history_results})
                    # ✅ Устанавливаем флаг готовности истории в агрегаторе
                    if hasattr(self._components.market_aggregator, 'set_history_ready'):
                        self._components.market_aggregator.set_history_ready()

                except asyncio.TimeoutError:
                    error_msg = "History loading timeout exceeded (120s)"
                    self.logger.error(error_msg)
                    self._emit_event("HISTORY_LOAD_TIMEOUT", {"timeout": 120.0})
                    raise BotLifecycleError(error_msg)
                except Exception as e:
                    error_msg = f"Failed to load history: {e}"
                    self.logger.error(error_msg)
                    self._emit_event("HISTORY_LOAD_FAILED", {"error": str(e)})
                    raise BotLifecycleError(error_msg)
            else:
                self.logger.warning("MarketHistoryManager not available, skipping history load")

            # ✅ НОВОЕ: Запускаем первый анализ ML модели на последней исторической свече
            if self._components.history_manager and self._components.strategy:
                symbols = self.config.get("symbols", [])
                for symbol in symbols:
                    try:
                        self.logger.info(f"🔍 Triggering initial ML analysis for {symbol}...")

                        # Получаем последнюю свечу 5m из БД с индикаторами
                        market_data_utils = getattr(self._components.history_manager, 'market_data_utils', None)
                        if market_data_utils:
                            last_candles_5m = await market_data_utils.read_candles_5m(symbol, last_n=100)
                            last_candles_1m = await market_data_utils.read_candles_1m(symbol, last_n=200)

                            if last_candles_5m and last_candles_1m:
                                # Проверяем, что у последней свечи есть индикаторы
                                last_5m = last_candles_5m[-1]
                                required_fields = ['cmo_14', 'adx_14', 'cusum_1m_recent']

                                if all(last_5m.get(field) is not None for field in required_fields):
                                    # Формируем market_data для стратегии
                                    import pandas as pd
                                    market_data = {
                                        '5m': pd.DataFrame(last_candles_5m),
                                        '1m': pd.DataFrame(last_candles_1m)
                                    }

                                    # ✅ ИСПРАВЛЕНО: generate_signal - ASYNC метод, используем await
                                    self.logger.info(
                                        f"🚀 Calling strategy.generate_signal with historical data for {symbol}")
                                    signal = await self._components.strategy.generate_signal(market_data)

                                    if signal:
                                        # ✅ ИСПРАВЛЕНО: Используем правильные ключи словаря
                                        direction = signal.get('direction', 0)
                                        confidence = signal.get('confidence', 0.0)
                                        entry_price = signal.get('entry_price', 0.0)

                                        self.logger.info(
                                            f"✅ Initial signal from history: {symbol} "
                                            f"dir={direction} "
                                            f"conf={confidence:.2f} "
                                            f"entry={entry_price:.2f}"
                                        )

                                        # Обрабатываем сигнал через main_bot
                                        if hasattr(self._components.main_bot, 'core'):
                                            core_bot = self._components.main_bot.core
                                            if hasattr(core_bot, '_process_trade_signal'):
                                                await core_bot._process_trade_signal(signal)
                                                self.logger.info(f"✅ Initial signal processed for {symbol}")
                                    else:
                                        self.logger.info(f"ℹ️ No signal from initial analysis for {symbol}")
                                else:
                                    missing = [f for f in required_fields if last_5m.get(f) is None]
                                    self.logger.warning(
                                        f"⚠️ Last 5m candle for {symbol} missing indicators: {missing}"
                                    )
                            else:
                                self.logger.warning(f"⚠️ No historical candles found for {symbol}")
                        else:
                            self.logger.warning("market_data_utils not available for initial analysis")

                    except Exception as e:
                        self.logger.error(f"❌ Initial analysis failed for {symbol}: {e}", exc_info=True)
                        continue

            symbols = self.config.get("symbols", [])
            EXECUTION_MODE = self.config.get("execution_mode", "DEMO")
            history_window = self.config.get("history_window", 50)

            # ✅ КРИТИЧНОЕ ИСПРАВЛЕНИЕ: Разделяем BACKTEST и LIVE/DEMO режимы
            if EXECUTION_MODE == "BACKTEST":
                self.logger.info(f"🚀 Starting BACKTEST mode for {symbols}")

                # ✅ ШАГ 1: Инициализируем торговый бот
                start_method = getattr(self._components.main_bot, "start", None)
                if callable(start_method):
                    result = start_method()
                    if asyncio.iscoroutine(result):
                        await result
                    self.logger.info("✅ EnhancedTradingBot initialized and ready for backtest")

                # Обновить _original_on_candle_ready в BacktestMarketAggregatorFixed
                if hasattr(self._components.market_aggregator, '_original_on_candle_ready'):
                    # Получаем правильный chained callback из main_bot
                    if hasattr(self._components.main_bot, 'core'):
                        core_adapter = self._components.main_bot  # MainBotAdapter

                        # Создаём правильный callback
                        async def backtest_chained_callback(symbol, candle, recent):
                            # 1. trade_log
                            if self._components.trade_log and hasattr(self._components.trade_log, "on_candle_ready"):
                                try:
                                    result = self._components.trade_log.on_candle_ready(symbol, candle, recent)
                                    if asyncio.iscoroutine(result):
                                        await result
                                except Exception as e:
                                    self.logger.error(f"Error in trade_log callback: {e}")

                            # 2. main_bot adapter
                            try:
                                await core_adapter.handle_candle_ready(symbol, candle, recent)
                            except Exception as e:
                                self.logger.error(f"Error in MainBotAdapter callback: {e}", exc_info=True)

                        # ✅ ЗАМЕНЯЕМ callback ДО start_async()
                        self._components.market_aggregator._original_on_candle_ready = backtest_chained_callback
                        self.logger.info(
                            "✅ Backtest callback chain updated: trade_log → MainBotAdapter → EnhancedTradingBot")

                # ✅ ШАГ 2: Запускаем воспроизведение свечей
                self.logger.info("⏳ Starting backtest replay (blocking mode)...")
                await self._components.market_aggregator.start_async(symbols, history_window=history_window)

                # ✅ ШАГ 3: Бэктест завершён (start_async уже завершился = все свечи обработаны)
                self.logger.info("✅ Backtest replay completed (blocking mode)")
                self.logger.info("🏁 Backtest finished, skipping main_bot monitoring")
                self._emit_event("BACKTEST_COMPLETED", {"execution_mode": EXECUTION_MODE})

                # Завершаем метод start() для BACKTEST
                return

            # ✅ Для LIVE/DEMO режимов — обычный запуск
            else:
                self.logger.info(f"Starting MarketAggregator in {EXECUTION_MODE} mode for symbols: {symbols}")
                await self._components.market_aggregator.start_async(symbols, history_window=history_window)

                # Запускаем main_bot.start()
                start_method = getattr(self._components.main_bot, "start", None)
                if callable(start_method):
                    result = start_method()
                    if asyncio.iscoroutine(result):
                        await result
                    self.logger.info("Main bot started")
                else:
                    self.logger.error("Main bot does not implement start(); trading loop will not run")

                # Устанавливаем обработчики сигналов для graceful shutdown
                self._setup_signal_handlers()

                # Запускаем задачи мониторинга и контроля lifecycle
                self._main_loop_task = asyncio.create_task(self._run_main_loop())
                self._monitoring_task = asyncio.create_task(self._run_main_bot_monitoring())

                self._is_running = True
                self._emit_event("LIFECYCLE_STARTED", {})

        except Exception as e:
            self._emit_event("LIFECYCLE_START_FAILED", {"error": str(e)})
            await self._cleanup()
            raise BotLifecycleError(f"Failed to start bot: {e}") from e

    async def _create_main_bot(self,
                               market_aggregator: MarketAggregatorInterface,
                               strategy: StrategyInterface,  # ⭐ Получаем созданную стратегию
                               position_manager: PositionManagerInterface,
                               exchange_manager: ExchangeManagerInterface,
                               exit_manager: Any,
                               risk_manager: Optional[Any],
                               trade_log: Any,
                               market_data_utils: Any,
                               logger: logging.Logger,
                               validator: Any) -> MainBotInterface:
        """
        Создаём главный бот с переданной стратегией для цикла торговли.
        ✅ ОБНОВЛЕНО: Интеграция с PositionManager для правильного управления позициями.
        """
        logger.info("Creating MainBot with provided trading strategy and PositionManager")

        # --- Импорты и подготовка окружения ---
        proj_dir = str(Path(__file__).resolve().parent)
        if proj_dir not in sys.path:
            sys.path.insert(0, proj_dir)

        try:
            import pandas as pd
        except Exception as e:
            logger.error(f"`pandas` is required for DataProvider: {e}")
            raise ComponentInitializationError(f"pandas not available: {e}")

        try:
            from sqlalchemy import create_engine, text
        except Exception as e:
            raise ComponentInitializationError(f"SQLAlchemy not available: {e}")

        # --- Импорт вашего бота ---
        try:
            from trade_bot import EnhancedTradingBot, DataProvider as TBDataProvider, \
                ExecutionEngine as TBExecutionEngine
        except ModuleNotFoundError as e:
            raise ComponentInitializationError(f"trade_bot.EnhancedTradingBot not found: {e}")

        # --- Импорт стандартов ---
        try:
            from iqts_standards import create_correlation_id, get_current_timestamp_ms
        except ImportError as e:
            raise ComponentInitializationError(f"iqts_standards not available: {e}")

        # ================================================================
        # DataProviderFromDB - без изменений
        # ================================================================

        class DataProviderFromDB(TBDataProvider):
            """
            Провайдер данных из SQLite.
            ✅ УПРОЩЕНО: Работает только с БД (без in-memory буфера).
            Данные в порядке ASC (от старых к новым), как при обучении модели.
            """

            def __init__(self, market_data_utils: Any, logger: logging.Logger):
                self.utils = market_data_utils
                self.logger = logger
                # ✅ ДОБАВИТЬ: Кэш загруженных данных
                self._cache: Dict[Tuple[str, str], Tuple[pd.DataFrame, int]] = {}
                self._cache_ttl_ms = 1000  # 1 секунда кэша
                # ✅ Add semaphore to limit parallel DB requests
                self._db_semaphore = asyncio.Semaphore(20)  # Max 20 parallel DB requests
                self.logger.info("✅ DataProviderFromDB initialized with DB semaphore (max=20)")

            async def _load_from_db(self, symbol: str, timeframe: str, limit: int = 1000) -> Optional[pd.DataFrame]:
                """
                Загрузить исторические данные из БД.
                ✅ ИСПРАВЛЕНО: В BACKTEST режиме загружаем данные ДО current_time_ms (не "последние N от конца БД")
                """
                try:
                    # ✅ Получаем текущее время (в BACKTEST это simulated_time)
                    from iqts_standards import get_current_timestamp_ms
                    current_time_ms = get_current_timestamp_ms()

                    cache_key = (symbol, timeframe)
                    if cache_key in self._cache:
                        cached_df, cached_ts = self._cache[cache_key]
                        cache_age = current_time_ms - cached_ts

                        if cache_age < self._cache_ttl_ms:
                            self.logger.debug(
                                f"📦 Cache HIT for {symbol} {timeframe} (age: {cache_age}ms)"
                            )
                            return cached_df.copy()

                    # ✅ Определяем параметры запроса
                    if timeframe == '1m':
                        actual_limit = min(limit, 500)
                        read_method = self.utils.read_candles_1m
                    elif timeframe == '5m':
                        actual_limit = min(limit, 200)
                        read_method = self.utils.read_candles_5m
                    else:
                        self.logger.warning(f"Unsupported timeframe for DB load: {timeframe}")
                        return None

                    self.logger.debug(
                        f"📊 Calling {read_method.__name__} for {symbol} {timeframe} "
                        f"(last_n={actual_limit}, end_ts={current_time_ms})"
                    )

                    # ✅ ИСПРАВЛЕНИЕ: Используем last_n + end_ts для фильтрации по времени в BACKTEST
                    # ✅ Wrap DB calls with semaphore to limit parallelism
                    async with self._db_semaphore:
                        data = await read_method(
                            symbol=symbol,
                            last_n=actual_limit,
                            end_ts=current_time_ms  # ← КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Ограничиваем до текущего времени бэктеста
                        )

                    # ✅ Валидация данных
                    if not data:
                        self.logger.warning(f"No data returned from DB for {symbol} {timeframe}")
                        return None

                    if not isinstance(data, list):
                        self.logger.error(f"Invalid data type: {type(data)} (expected list)")
                        return None

                    if len(data) == 0:
                        self.logger.warning(f"Empty data list for {symbol} {timeframe}")
                        return None

                    # ✅ Конвертируем в DataFrame
                    try:
                        df = pd.DataFrame(data)
                    except ValueError as ve:
                        self.logger.error(
                            f"DataFrame creation failed: {ve}, data sample: {data[:2] if len(data) > 0 else 'empty'}"
                        )
                        return None

                    # ✅ Создаем timestamp из ts для ML-модели
                    if 'ts' in df.columns:
                        df['timestamp'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
                        df = df.set_index('timestamp')

                    # ✅ Сохраняем в кэш
                    self._cache[cache_key] = (df, current_time_ms)

                    # ✅ Очистка старого кэша
                    if len(self._cache) > 10:
                        expired = [k for k, (_, ts) in self._cache.items()
                                   if current_time_ms - ts > self._cache_ttl_ms * 2]
                        for k in expired:
                            del self._cache[k]

                    self.logger.info(
                        f"✅ Loaded {len(df)} rows from DB for {symbol} {timeframe} "
                        f"(last_n={actual_limit}, end_ts={current_time_ms})"
                    )
                    return df

                except Exception as e:
                    self.logger.error(f"Error loading from DB: {symbol} {timeframe}: {e}", exc_info=True)
                    return None

            async def get_market_data(self, symbol: str, timeframes: List[str]) -> Dict[str, pd.DataFrame]:
                result = {}

                for tf in timeframes:
                    try:
                        db_df = await self._load_from_db(symbol, tf, limit=1000)

                        if db_df is None or db_df.empty:
                            self.logger.warning(f"No data available for {symbol} {tf}")
                            continue

                        # ✅ ДОБАВИТЬ ДИАГНОСТИКУ
                        if tf == '5m':
                            self.logger.info(f"📊 5m DataFrame shape: {db_df.shape}")
                            self.logger.info(f"📋 5m columns ({len(db_df.columns)}): {db_df.columns.tolist()}")

                            # Проверка последней строки
                            last_row = db_df.iloc[-1]
                            self.logger.info(f"🔍 Last row ts: {last_row.get('ts')}")

                            # Проверка наличия 22 фич ML-модели
                            required_features = [
                                'cmo_14', 'volume', 'trend_acceleration_ema7', 'regime_volatility',
                                'bb_width', 'adx_14', 'plus_di_14', 'minus_di_14', 'atr_14_normalized',
                                'volume_ratio_ema3', 'candle_relative_body', 'upper_shadow_ratio',
                                'lower_shadow_ratio', 'price_vs_vwap', 'bb_position', 'cusum_1m_recent',
                                'cusum_1m_quality_score', 'cusum_1m_trend_aligned', 'cusum_1m_price_move',
                                'is_trend_pattern_1m', 'body_to_range_ratio_1m', 'close_position_in_range_1m'
                            ]

                            missing = [f for f in required_features if f not in db_df.columns]
                            if missing:
                                self.logger.error(f"❌ Missing ML features: {missing}")

                            # Проверка NULL в последней строке
                            null_features = [f for f in required_features
                                             if f in db_df.columns and pd.isna(last_row.get(f))]
                            if null_features:
                                self.logger.error(f"❌ NULL values in last row: {null_features}")

                        result[tf] = db_df
                        self.logger.info(f"📊 market_data ready for {symbol} {tf}: {len(db_df)} rows")

                    except Exception as e:
                        self.logger.error(f"Error getting market data for {symbol} {tf}: {e}")

                return result

            async def get_current_price(self, symbol: str) -> float:
                """
                Получить текущую цену из последней свечи в БД.
                ✅ iloc[-1] - последняя свеча (самая новая в ASC порядке)
                """
                try:
                    db_df = await self._load_from_db(symbol, '1m', limit=1)

                    if db_df is not None and not db_df.empty:
                        # ✅ iloc[-1] корректно для ASC порядка (последняя = новейшая)
                        return float(db_df['close'].iloc[-1])

                    self.logger.error(f"Cannot get current price for {symbol}")
                    return 0.0

                except Exception as e:
                    self.logger.error(f"Error getting current price for {symbol}: {e}")
                    return 0.0

        # ================================================================
        # ExecutionEngineFromExchangeManager - ✅ ОБНОВЛЕНО
        # ================================================================

        class ExecutionEngineFromExchangeManager(TBExecutionEngine):
            """
            ✅ ОБНОВЛЕНО: Интеграция с PositionManager для правильного управления позициями.

            Flow:
                TradeSignalIQTS → TradeSignal (intent-based) → PositionManager → OrderReq → ExchangeManager
            """

            def __init__(self, em: ExchangeManagerInterface, position_manager: Any, logger: logging.Logger):
                self.em = em
                self.position_manager = position_manager  # ✅ Сохраняем ссылку на PM
                self.logger = logger
                self.logger.info("ExecutionEngine created with PositionManager integration")

            async def place_order(self, trade_signal: Dict) -> Dict:
                """
                ✅ ОБНОВЛЕНО: Интеграция с PositionManager для правильного управления позициями.

                Flow:
                    1. Конвертация TradeSignalIQTS → TradeSignal (intent-based)
                    2. PositionManager.handle_signal() → OrderReq (с client_order_id, qty)
                    3. ExchangeManager.place_order(OrderReq) → Исполнение на бирже

                Args:
                    trade_signal: Сигнал от ImprovedQualityTrendSystem

                Returns:
                    Dict с success, position_id, order_id
                """
                try:
                    # ✅ ШАГ 1: Конвертация TradeSignalIQTS → TradeSignal
                    direction = trade_signal.get('direction')

                    if direction is None:
                        return {
                            "success": False,
                            "error": "Missing direction in signal",
                            "position_id": None
                        }

                    # Приводим к int
                    try:
                        direction_int = int(direction)
                    except (ValueError, TypeError) as e:
                        return {
                            "success": False,
                            "error": f"Invalid direction type: {direction}",
                            "position_id": None
                        }

                    # Определяем intent
                    from iqts_standards import Direction
                    if direction_int == Direction.BUY:
                        intent = "LONG_OPEN"
                    elif direction_int == Direction.SELL:
                        intent = "SHORT_OPEN"
                    else:
                        return {
                            "success": False,
                            "error": f"Invalid direction value: {direction_int} (FLAT not supported)",
                            "position_id": None
                        }

                    # Формируем TradeSignal для PositionManager
                    symbol = trade_signal.get('symbol', 'ETHUSDT')
                    entry_price = trade_signal.get('entry_price', 0.0)

                    if entry_price <= 0:
                        return {
                            "success": False,
                            "error": f"Invalid entry_price: {entry_price}",
                            "position_id": None
                        }

                    pm_signal = {
                        'symbol': symbol,
                        'intent': intent,
                        'decision_price': entry_price,
                        'correlation_id': trade_signal.get('client_order_id') or create_correlation_id(),
                        'confidence': trade_signal.get('confidence', 0.0),
                        'metadata': trade_signal.get('metadata', {}),
                        'risk_context': {
                            'decision_price': entry_price
                        }
                    }

                    self.logger.info(
                        f"🔄 Converted signal: {intent} @ {entry_price:.2f} "
                        f"(correlation_id={pm_signal['correlation_id'][:16]}...)"
                    )

                    # ✅ ШАГ 2: Проверяем наличие PositionManager
                    if not self.position_manager:
                        self.logger.warning(
                            "⚠️ PositionManager not available, falling back to direct ExchangeManager call"
                        )

                        # Fallback: прямой вызов ExchangeManager
                        meth = getattr(self.em, "place_order", None)
                        if callable(meth):
                            res = meth(trade_signal)
                            if asyncio.iscoroutine(res):
                                res = await res
                            if not isinstance(res, dict):
                                res = {"success": bool(res)}

                            # Гарантированно возвращаем position_id
                            if "position_id" not in res:
                                res["position_id"] = (
                                        res.get("client_order_id") or
                                        res.get("orderId") or
                                        res.get("order_id") or
                                        res.get("id") or
                                        f"pos_{symbol}_{int(get_current_timestamp_ms())}"
                                )
                            return res

                        return {
                            "success": False,
                            "error": "No place_order method in ExchangeManager",
                            "position_id": None
                        }

                    # ✅ ШАГ 3: PositionManager обрабатывает сигнал
                    self.logger.info("📊 Delegating to PositionManager.handle_signal()")

                    order_req = self.position_manager.handle_signal(pm_signal)

                    if not order_req:
                        return {
                            "success": False,
                            "error": "PositionManager rejected signal (duplicate/invalid/max positions)",
                            "position_id": None
                        }

                    self.logger.info(
                        f"✅ PositionManager created OrderReq: "
                        f"client_order_id={order_req['client_order_id']}, "
                        f"qty={float(order_req['qty']):.4f}, "
                        f"side={order_req['side']}, "
                        f"type={order_req['type']}"
                    )

                    # ✅ ШАГ 4: Отправляем OrderReq на биржу через ExchangeManager
                    meth = getattr(self.em, "place_order", None)

                    if not callable(meth):
                        return {
                            "success": False,
                            "error": "ExchangeManager.place_order not available",
                            "position_id": None
                        }

                    # ExchangeManager.place_order принимает OrderReq
                    exchange_result = meth(order_req)
                    if asyncio.iscoroutine(exchange_result):
                        exchange_result = await exchange_result

                    if not isinstance(exchange_result, dict):
                        exchange_result = {"success": bool(exchange_result)}

                    # ✅ ШАГ 5: Формируем результат
                    success = exchange_result.get("status") in ["NEW", "FILLED", "WORKING"] or exchange_result.get(
                        "success", False)

                    result = {
                        "success": success,
                        "position_id": f"{symbol}_{order_req['client_order_id']}",
                        "order_id": order_req['client_order_id'],
                        "client_order_id": order_req['client_order_id'],
                        "exchange_order_id": exchange_result.get("orderId") or exchange_result.get("exchange_order_id"),
                        "symbol": symbol,
                        "side": order_req['side'],
                        "qty": float(order_req['qty']),
                        "status": exchange_result.get("status", "UNKNOWN"),
                        "message": f"Order sent via PositionManager: {order_req['client_order_id']}"
                    }

                    if not success:
                        result["error"] = exchange_result.get("error_message") or exchange_result.get(
                            "error") or "Unknown error"
                        self.logger.error(
                            f"❌ Exchange rejected order: {result['error']} "
                            f"(status={exchange_result.get('status')})"
                        )
                    else:
                        self.logger.info(
                            f"✅ Order accepted by exchange: {order_req['client_order_id']} "
                            f"(status={result['status']})"
                        )

                    return result

                except Exception as err:
                    self.logger.error(f"❌ place_order failed: {err}", exc_info=True)
                    return {
                        "success": False,
                        "error": str(err),
                        "position_id": None,
                        "order_id": None
                    }

            async def close_position(self, position_id: str) -> Dict:
                """Закрыть позицию через ExchangeManager"""
                try:
                    meth = getattr(self.em, "close_position", None)
                    if callable(meth):
                        res = meth(position_id)
                        if asyncio.iscoroutine(res):
                            res = await res
                        if isinstance(res, dict):
                            return res
                        return {"success": bool(res)}
                    return {"success": False, "error": "no close_position method"}
                except Exception as err:
                    self.logger.error(f"close_position failed: {err}", exc_info=True)
                    return {"success": False, "error": str(err)}

            async def get_account_info(self) -> Dict:
                """Получить информацию об аккаунте"""
                try:
                    meth = getattr(self.em, "get_account_info", None)
                    if callable(meth):
                        res = meth()
                        if asyncio.iscoroutine(res):
                            res = await res
                        if isinstance(res, dict):
                            return res
                    return {}
                except Exception as err:
                    self.logger.error(f"get_account_info failed: {err}", exc_info=True)
                    return {}

        # ================================================================
        # Создание компонентов
        # ================================================================

        data_provider = DataProviderFromDB(market_data_utils, logger)
        logger.info("✅ DataProviderFromDB created")

        # ✅ ВАЖНО: Передаем position_manager в ExecutionEngine
        execution_engine = ExecutionEngineFromExchangeManager(
            em=exchange_manager,
            position_manager=position_manager,  # ✅ Интеграция с PM
            logger=logger
        )
        logger.info("✅ ExecutionEngine created with PositionManager integration")
        exchange_manager.validator = validator
        # BEGIN REPLACE: создание core_bot с DI risk_manager и exit_manager
        core_bot = EnhancedTradingBot(
            config=self.config,
            data_provider=data_provider,
            execution_engine=execution_engine,
            trading_system=cast(ImprovedQualityTrendSystem, strategy),
            risk_manager=risk_manager,
            validator=validator,
        )
        logger.info("✅ EnhancedTradingBot created with RiskManager DI")
        # END REPLACE
        logger.info("✅ EnhancedTradingBot created")

        # ================================================================
        # MainBotAdapter - без изменений
        # ================================================================

        class MainBotAdapter(MainBotInterface):
            """Адаптер для EnhancedTradingBot"""

            def __init__(self, core: EnhancedTradingBot, logger: logging.Logger):
                self.core = core
                self.logger = logger
                self._handler: Optional[Callable] = None
                self._start_task: Optional[asyncio.Task] = None
                self._stats = {
                    "signals_processed": 0,
                    "candles_processed": 0,
                    "events_processed": 0,
                    "last_candle_ts": None
                }
                self._active_analysis_tasks: Dict[str, asyncio.Task] = {}
                # ✅ Task cleanup configuration
                self._task_cleanup_interval = 60  # Cleanup every 60s
                self._task_max_age = 300  # Max task age 5 minutes
                self._cleanup_task: Optional[asyncio.Task] = None
                self._task_creation_times: Dict[str, float] = {}  # Track task creation times

            async def main_trading_loop(self) -> None:
                """Пустой цикл - работаем в event-driven режиме"""
                self.logger.info("MainBotAdapter: event-driven mode (no polling loop)")
                while self.core.is_running:
                    await asyncio.sleep(60)

            async def start(self) -> None:
                """Запуск бота"""
                await self.core.start()
                self._start_task = asyncio.create_task(self.main_trading_loop())

            async def stop(self) -> None:
                """Остановка бота"""
                if self._cleanup_task:
                    self._cleanup_task.cancel()
                if self._start_task:
                    self._start_task.cancel()
                await self.core.shutdown()

            async def bootstrap(self) -> None:
                """Инициализация"""
                # Start cleanup task
                try:
                    self._cleanup_task = asyncio.create_task(self._cleanup_stale_tasks())
                    self.logger.info("MainBotAdapter bootstrap completed with cleanup task")
                except Exception as e:
                    self.logger.error(f"Failed to start cleanup task: {e}")
                    self.logger.info("MainBotAdapter bootstrap completed without cleanup task")

            def get_stats(self) -> Dict:
                """Статистика"""
                return {
                    **self._stats,
                    "bot_status": self.core.get_status()
                }

            def get_component_health(self) -> Dict:
                """Здоровье компонентов"""
                return {
                    "is_running": self.core.is_running,
                    "active_positions": len(self.core.active_positions)
                }

            def add_event_handler(self, handler: Callable) -> None:
                """Регистрация обработчика событий"""
                self._handler = handler

            async def _cleanup_stale_tasks(self) -> None:
                """Периодическая очистка зависших задач"""
                while True:
                    try:
                        await asyncio.sleep(self._task_cleanup_interval)

                        try:
                            loop = asyncio.get_running_loop()
                        except RuntimeError:
                            loop = asyncio.get_event_loop()
                        current_time = loop.time()
                        stale_tasks = []

                        for symbol, task in list(self._active_analysis_tasks.items()):
                            if task.done():
                                stale_tasks.append(symbol)
                            # Check if task is too old (stuck)
                            elif symbol in self._task_creation_times:
                                age = current_time - self._task_creation_times[symbol]
                                if age > self._task_max_age:
                                    self.logger.warning(f"⚠️ Cancelling stale task for {symbol} (age={age:.1f}s)")
                                    task.cancel()
                                    stale_tasks.append(symbol)

                        # Cleanup
                        for symbol in stale_tasks:
                            if symbol in self._active_analysis_tasks:
                                del self._active_analysis_tasks[symbol]
                            if symbol in self._task_creation_times:
                                del self._task_creation_times[symbol]

                        if stale_tasks:
                            self.logger.info(f"🧹 Cleaned up {len(stale_tasks)} stale tasks: {stale_tasks}")

                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        self.logger.error(f"Error in task cleanup: {e}")

            async def handle_candle_ready(self, symbol: str, candle: Candle1m, recent_stack: List[Candle1m]) -> None:
                """
                Обработка готовой свечи - делегирование в EnhancedTradingBot.
                
                MainBotAdapter - это только адаптер интерфейса, вся логика в core bot.
                """
                try:
                    self.logger.debug(
                        f"🎯 MainBotAdapter.handle_candle_ready: {symbol} @ {candle.get('ts')} "
                        f"(_timeframe={candle.get('_timeframe', 'unknown')})"
                    )
                    
                    # Обновление статистики
                    self._stats["candles_processed"] += 1
                    self._stats["last_candle_ts"] = candle.get('ts')
                    self._stats["events_processed"] += 1
                    
                    # ✅ Делегирование в EnhancedTradingBot
                    await self.core.on_candle_ready(symbol, candle, recent_stack)
                    
                except Exception as e:
                    self.logger.error(f"Error in handle_candle_ready: {e}", exc_info=True)
        # ================================================================
        # Возвращаем адаптер
        # ================================================================

        adapter = MainBotAdapter(core_bot, logger)
        # Сохраняем ссылку на адаптер для доступа к _active_analysis_tasks
        core_bot._adapter = adapter

        # ✅ CRITICAL FIX: Chain callbacks to include MainBotAdapter
        if hasattr(market_aggregator, 'on_candle_ready'):
            original_on_candle_ready = market_aggregator.on_candle_ready

            async def chained_on_candle_ready(symbol, candle, recent):
                # 1. Логируем в trade_log (только сохранение данных)
                if trade_log and hasattr(trade_log, "on_candle_ready"):
                    try:
                        result = trade_log.on_candle_ready(symbol, candle, recent)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        logger.error(f"Error in trade_log callback: {e}")

                # 2. Передаём в адаптер (который вызовет EnhancedTradingBot.on_candle_ready)
                try:
                    await adapter.handle_candle_ready(symbol, candle, recent)
                except Exception as e:
                    logger.error(f"Error in MainBotAdapter callback: {e}", exc_info=True)

            market_aggregator.on_candle_ready = chained_on_candle_ready
            logger.info("✅ Chained callbacks: trade_log → MainBotAdapter → EnhancedTradingBot")

        logger.info("✅ MainBotAdapter created and subscribed")
        return cast(MainBotInterface, adapter)

    async def _check_components_health(self):
        """Проверка состояния компонентов"""
        if not self._components or not hasattr(self._components, 'main_bot'):
            return

        try:
            health = self._components.main_bot.get_component_health()

            ok_statuses = {"healthy", "connected"}
            issues = []

            if isinstance(health, dict):
                for component_name, status in health.items():
                    if component_name == "components":
                        continue

                    status_norm = str(status).lower()
                    if status_norm not in ok_statuses:
                        issues.append(f"{component_name}: {status}")

            if issues:
                self._emit_event("COMPONENTS_HEALTH_ISSUES", {"issues": issues})

        except Exception as e:
            self.logger.error(f"Health check error: {e}")

    def _setup_signal_handlers(self) -> None:
        """Setup SIGINT/SIGTERM handlers for proper shutdown."""

        def signal_handler(signum: int, frame) -> None:
            self._emit_event("SIGNAL_RECEIVED", {"signal": signum})
            asyncio.create_task(self.stop())

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    async def _cleanup(self) -> None:
        """Stops aggregator/AsyncStore, closes resources."""
        if not self._components:
            return
        try:
            self._emit_event("CLEANUP_STARTED", {})

            # ✅ УПРОЩЕННАЯ ВЕРСИЯ: Используем безопасные вызовы
            cleanup_tasks = []

            # Останавливаем агрегатор
            if hasattr(self._components.market_aggregator, 'stop'):
                cleanup_tasks.append(self._safe_call(self._components.market_aggregator.stop))

            # Отключаем user stream
            if hasattr(self._components.exchange_manager, 'disconnect_user_stream'):
                cleanup_tasks.append(self._safe_call(self._components.exchange_manager.disconnect_user_stream))

            # Закрываем history_manager
            if self._components.history_manager and hasattr(self._components.history_manager, 'close'):
                cleanup_tasks.append(self._safe_call(self._components.history_manager.close))

            # Останавливаем trade_log
            if hasattr(self._components.trade_log, 'stop_async'):
                cleanup_tasks.append(self._safe_call(self._components.trade_log.stop_async))
            if hasattr(self._components.trade_log, 'close'):
                cleanup_tasks.append(self._safe_call(self._components.trade_log.close))

            # Останавливаем main_bot
            if self._components.main_bot and hasattr(self._components.main_bot, "stop"):
                cleanup_tasks.append(self._safe_call(self._components.main_bot.stop))

            # Выполняем все cleanup задачи
            if cleanup_tasks:
                await asyncio.gather(*cleanup_tasks, return_exceptions=True)

            # Отменяем задачу основного бота
            if self._main_loop_task:
                try:
                    if not self._main_loop_task.done():
                        self._main_loop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._main_loop_task
                finally:
                    self._main_loop_task = None

            self._emit_event("CLEANUP_COMPLETED", {})

        except Exception as e:
            self._emit_event("CLEANUP_ERROR", {"error": str(e)})
            self.logger.error(f"Cleanup failed: {e}", exc_info=True)

    async def _safe_call(self, method):
        """Безопасный вызов метода (синхронного или асинхронного)"""
        try:
            if asyncio.iscoroutinefunction(method):
                return await method()
            elif callable(method):
                result = method()
                if asyncio.iscoroutine(result):
                    return await result
                return result
        except Exception as e:
            self.logger.warning(f"Safe call failed: {e}")
            return None

    @property
    def is_running(self) -> bool:
        """Check if bot is running"""
        return self._is_running

    @property
    def components(self) -> Optional[ComponentsContainer]:
        """Get components container"""
        return self._components


# === Entry Point ===
async def main():
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "backtest":
        await run_backtest_mode()
        return

    errors = cfg.validate_config()
    if errors:
        raise RuntimeError(f"Config errors: {errors}")

    # ✅ Auto-detect BACKTEST mode from config
    if cfg.EXECUTION_MODE == "BACKTEST":
        print("🔍 Detected EXECUTION_MODE='BACKTEST' in config, using run_backtest_mode()")
        await run_backtest_mode()
        return

    # For DEMO/LIVE modes, trading_logger not needed upfront
    runtime_cfg = cfg.build_runtime_config(trading_logger=None)

    def event_handler(event: BotLifecycleEvent) -> None:
        event_type = event['event_type']
        data = event.get('data', {})

        # ✅ ДОБАВЛЕНО: Логируем в основной логгер
        logger = logging.getLogger("TradingBot")

        if event_type == "LIFECYCLE_STARTING":
            logger.info("🚀 LIFECYCLE_STARTING - Starting bot lifecycle")
        elif event_type == "COMPONENTS_CREATED":
            components = data.get('components', [])
            logger.info(f"✅ COMPONENTS_CREATED - Components: {components}")
        elif event_type == "HISTORY_LOADED":
            results = data.get('results', {})
            logger.info(f"📊 HISTORY_LOADED - Results: {results}")
        elif event_type == "MAIN_BOT_BOOTSTRAPPED":
            logger.info("🔥 MAIN_BOT_BOOTSTRAPPED - Main bot warmed up")
        elif event_type == "LIFECYCLE_STARTED":
            logger.info("🎉 LIFECYCLE_STARTED - Bot successfully started!")
        elif event_type == "CRITICAL_ERROR":
            logger.error(f"🚨 CRITICAL_ERROR: {data}")
        elif event_type == "WARNING":
            logger.warning(f"⚠️ WARNING: {data}")
        elif event_type == "MONITORING_STATS":
            stats = data.get('stats', {})
            logger.info(f"📊 MONITORING_STATS: {stats}")
        elif event_type == "BACKTEST_COMPLETED":
            logger.info("🏁 BACKTEST_COMPLETED")

        # Сохраняем оригинальный вывод в консоль
        if event_type == "CRITICAL_ERROR":
            print(f"🚨 CRITICAL ERROR: {data}")
        elif event_type == "WARNING":
            print(f"⚠️ WARNING: {data}")
        elif event_type == "LIFECYCLE_STARTED":
            print("✅ Bot started successfully!")
        elif event_type == "LIFECYCLE_STOPPED":
            print("🛑 Bot stopped")
        elif event_type == "MONITORING_STATS":
            stats = data.get('stats', {})
            print(f"📊 Stats: {stats}")
        elif event_type == "BACKTEST_COMPLETED":
            print("🏁 Backtest completed!")

    bot_manager = BotLifecycleManager(
        runtime_cfg,
        event_handlers=[event_handler],
        shutdown_timeout_seconds=45.0
    )

    try:
        print("🚀 Starting trading bot...")
        await bot_manager.start()
        await bot_manager.wait_for_shutdown()

    except KeyboardInterrupt:
        print("\nℹ️  Received interrupt signal")
    except Exception as e:
        print(f"❌ Bot error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("🔄 Shutting down...")
        await bot_manager.stop()
        print("✅ Shutdown complete")

async def run_backtest_mode():
    """Запуск бота в режиме BACKTEST c автозавершением и финальным отчётом."""
    from backtest_setup import build_backtest_config

    runtime_cfg = await build_backtest_config()

    errors = cfg.validate_config()
    if errors:
        raise RuntimeError(f"Config errors: {errors}")

    runtime_cfg.setdefault("execution_mode", "BACKTEST")
    runtime_cfg.setdefault("backtest", {})
    runtime_cfg["backtest"].setdefault("auto_shutdown", True)

    bot_manager: Optional[BotLifecycleManager] = None

    def backtest_event_handler(event: BotLifecycleEvent) -> None:
        nonlocal bot_manager

        event_type = event.get("event_type")
        data = event.get("data", {}) or {}

        if event_type == "LIFECYCLE_STARTED":
            print("✅ Backtest started successfully!")

        elif event_type == "BACKTEST_COMPLETED":
            print("🏁 Backtest completed! Generating final report...")

            if not bot_manager or not bot_manager.components:
                print("⚠️  Components are not available for reporting.")
                return

            comps = bot_manager.components
            trade_log = getattr(comps, "trade_log", None)
            main_bot = getattr(comps, "main_bot", None)

            try:
                print("\n" + "=" * 60)
                print("📊 BACKTEST RESULTS")
                print("=" * 60)

                trading_stats = {}
                if trade_log and hasattr(trade_log, "get_trading_stats"):
                    try:
                        trading_stats = trade_log.get_trading_stats() or {}
                    except Exception as err:
                        print(f"⚠️  trading_stats unavailable: {err}")

                bot_stats = {}
                if main_bot and hasattr(main_bot, "get_stats"):
                    try:
                        bot_stats = main_bot.get_stats() or {}
                    except Exception as err:
                        print(f"⚠️  main_bot stats unavailable: {err}")

                total_trades = trading_stats.get("total_trades", 0) or 0
                win_rate = trading_stats.get("win_rate_percent", 0.0) or 0.0
                total_pnl = trading_stats.get("total_pnl_usdt", 0.0) or 0.0
                avg_pnl = trading_stats.get("avg_pnl_usdt", 0.0) or 0.0
                signals = bot_stats.get("signals_generated", 0) or 0
                events = bot_stats.get("events_processed", 0) or 0

                print(f"📈 Total trades: {int(total_trades)}")
                print(f"🎯 Win rate: {float(win_rate):.2f}%")
                print(f"💰 Total PnL: {float(total_pnl):.2f} USDT")
                print(f"📊 Avg PnL: {float(avg_pnl):.2f} USDT")
                print(f"⚡ Signals: {int(signals)}")
                print(f"📋 Events: {int(events)}")
                print("=" * 60)

                try:
                    if trade_log and hasattr(trade_log, "get_all_symbols_stats"):
                        all_symbols_stats = trade_log.get_all_symbols_stats() or {}
                    else:
                        all_symbols_stats = {}

                    if all_symbols_stats:
                        print("\n📊 СВОДНЫЙ ОТЧЁТ ПО СИМВОЛАМ:")
                        print(
                            f"{'Symbol':<10} {'Trades':<7} {'WinRate':<9} {'NetPnL%':<9} {'AvgPnL%':<9} {'MaxWin%':<9} {'MaxLoss%':<9}")
                        print("-" * 65)

                        total_trades_all = 0
                        total_net_pnl_pct = 0.0

                        for symbol, stats in all_symbols_stats.items():
                            total = int(stats.get('total_trades', 0) or 0)
                            if total <= 0:
                                continue

                            winrate = float(stats.get('win_rate_percent', 0.0) or 0.0)

                            trade_records = []
                            if hasattr(trade_log, "get_trade_history"):
                                try:
                                    trade_records = trade_log.get_trade_history(symbol) or []
                                except Exception:
                                    trade_records = []

                            pnl_percentages = []
                            for tr in trade_records:
                                if isinstance(tr, dict):
                                    val = tr.get('net_pnl_percent') or tr.get('realized_pnl_pct')
                                else:
                                    val = getattr(tr, 'net_pnl_percent', None) or getattr(tr, 'realized_pnl_pct', None)

                                if val is not None:
                                    try:
                                        pnl_percentages.append(float(val))
                                    except Exception:
                                        pass

                            if pnl_percentages:
                                net_pnl_pct = sum(pnl_percentages)
                                avg_pnl_pct = net_pnl_pct / len(pnl_percentages)
                                max_win_pct = max(pnl_percentages)
                                max_loss_pct = min(pnl_percentages)
                            else:
                                net_pnl_pct = avg_pnl_pct = max_win_pct = max_loss_pct = 0.0

                            print(
                                f"{symbol:<10} {total:<7} {winrate:<9.2f} {net_pnl_pct:<9.2f} {avg_pnl_pct:<9.2f} {max_win_pct:<9.2f} {max_loss_pct:<9.2f}")

                            total_trades_all += total
                            total_net_pnl_pct += net_pnl_pct

                        print("-" * 65)
                        print(f"Общая чистая прибыль: {total_net_pnl_pct:.2f}% по {total_trades_all} сделкам.")
                    else:
                        print("\n📊 Нет данных для отчёта по символам")

                except Exception as err:
                    print(f"\n❌ Ошибка при генерации отчёта по символам: {err}")

            except Exception as err:
                print(f"❌ Error generating final report: {err}")

        elif event_type == "LIFECYCLE_STOPPED":
            print("🛑 Bot shutdown completed")

        elif event_type == "SIGNAL_PROCESSED":
            symbol = data.get('symbol', 'N/A')
            intent = data.get('intent', 'N/A')
            if intent in {"LONG_OPEN", "SHORT_OPEN", "LONG_CLOSE", "SHORT_CLOSE"}:
                print(f"📈 Signal: {symbol} {intent}")

        elif event_type in {"CRITICAL_ERROR", "WARNING"}:
            print(f"⚠️  {event_type}: {data}")

    bot_manager = BotLifecycleManager(
        runtime_cfg,
        event_handlers=[backtest_event_handler],
        shutdown_timeout_seconds=45.0
    )

    try:
        print("🚀 Starting backtest...")
        await bot_manager.start()
        await bot_manager.wait_for_shutdown()
    except KeyboardInterrupt:
        print("\nℹ️  Received interrupt signal")
    except Exception as e:
        print(f"❌ Bot error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("🔄 Shutting down...")
        await bot_manager.stop()
        print("✅ Shutdown complete")


if __name__ == "__main__":


    os.makedirs("data", exist_ok=True)
    asyncio.run(main())