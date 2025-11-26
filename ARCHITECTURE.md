
Дата: 2025-11-18  
Автор: pwm777  
Версия: 2.0 (объединение structure_bot.txt + архитектурные улучшения)

---

## 2. High-Level Overview

```
                 +-------------------+
                 |  Market Aggregator|
                 | (LIVE/DEMO/BACKTEST)
                 +---------+---------+
                           |
                     Candle Events
                           v
+------------------+   +------------------+
|  MarketHistory   |   |  MarketDataUtils |
| (historical load)|   | (indicators, ML features,
+--------+----------+  |  CUSUM, ATR etc.)|
         |              +--------+-------+
         | Warmup/Backfill        |
         v                        v
                +-------------------------------+
                |  Strategy (ImprovedQuality    |
                |  Trend System / Confirmator)  |
                |  + RiskManager (DI)           |
                +------+------------------------+
                       | DetectorSignal
                       v
                +-------------------------------+
                | EnhancedRiskManager           |
                | calculate_risk_context()      |
                +------+------------------------+
                       | RiskContext
                       v
                +-------------------------------+
                | EnhancedTradingBot            |
                | + SignalValidator (DI)        |
                | + ExitManager (DI)            |
                +------+------------------------+
                       | Intent + RiskContext
                       v
                +-------------------------------+
                | PositionManager (DI risk/exit)|
                | Technical order construction  |
                +------+------------------------+
                       | OrderReq
                       v
                +-------------------------------+
                | ExchangeManager               |
                | Place/Modify/Cancel           |
                +---------------+---------------+
                                |
                         Fills / Order Updates
                                v
                        +--------------+
                        | TradingLogger|
                        |  Audit, PnL  |
                        +--------------+
```

---

## 3. Bounded Contexts

| Контекст | Ответственность | Входы | Выходы |
|----------|-----------------|-------|--------|
| Market Data | Сбор / агрегация / прогрев | Биржа / БД | Свечи с индикаторами |
| Strategy | Анализ + генерация направленных сигналов | Свечи, индикаторы | DetectorSignal, TradeSignalIQTS |
| Risk Management | Консолидация размера, SL, TP | DetectorSignal, цена, ATR | RiskContext |
| Execution | Жизненный цикл позиции (открытие/обновление) | Intent + RiskContext | OrderReq / Position state |
| Exit Decision | Управление выходами и защитой прибыли | Позиция + рынок | ExitDecision |
| Audit & Logging | Трассировка, статистика, история | Позиции, ордера | История, отчёты |
| Validation | Единый контроль качества сигналов | Сырые сигналы | ValidationResult |

---


# Trade_Bot Structure (v2.0)
  
## 1. БАЗОВЫЕ МОДУЛИ СИСТЕМЫ:
    +-- iqts_standards.py (стандарты и интерфейсы)
    +-- run_bot.py (главный координатор)
    +-- trade_bot.py (исполнение)
    +-- ImprovedQualityTrendSystem.py (стратегия)
    +-- iqts_detectors.py (детекторы)
    +-- multi_timeframe_confirmator.py (анализатор)
    +-- market_aggregator.py (агрегатор данных)
    L-- market_data_utils.py (утилиты данных)
	
## 2. ДОПОЛНЕННЫЕ МОДУЛИ:
    +-- EnhancedRiskManager (риск-менеджер) - risk_manager.py
    +-- AdaptiveExitManager (менеджер выхода) - exit_system.py
    +-- TradingLogger (логгер торговли) - trading_logger.py
    +-- MLGlobalDetector (ML детектор) - ml_global_detector.py
    +-- PerformanceTracker (трекер производительности) - performance_tracker.py
    +-- BacktestEngine (движок бэктеста) - backtest_engine.py
    +-- MarketHistoryManager (менеджер исторических данных) - market_history.py
    +-- ExchangeManager (менеджер биржи) - exchange_manager.py
	+-- MLLabelingTool (офлайн разметчик и генерация snapshot'ов) - ml_labeling_tool_v3.py
    +-- MLGlobalModelTrainer (офлайн обучение ML-модели глобального тренда) - ml_train_global_v2.py
	+--	backtest_setup.py — умный генератор конфигурации для режима BACKTEST
	L-- SignalValidator (Глобальный валидатор) - signal_validator.py

## 3. Общая архитектура системы
BotLifecycleManager (run_bot.py) - главный координатор
    +-- ImprovedQualityTrendSystem (стратегия) - ImprovedQualityTrendSystem.py
    ¦   L-- ThreeLevelHierarchicalConfirmator (анализатор) - multi_timeframe_confirmator.py
    ¦       +-- MLGlobalTrendDetector (глобальный детектор) - iqts_detectors.py
    ¦       L-- RoleBasedOnlineTrendDetector (трендовый детектор) - iqts_detectors.py
    +-- EnhancedTradingBot (исполнение) - trade_bot.py
    ¦   +-- PositionTracker (трекинг позиций)
    ¦   +-- AdaptiveExitManager (управление выходом) - exit_system.py 
    +-- PositionManager (менеджер позиций)
    +-- ExchangeManager (менеджер биржи)
    +-- MarketAggregator (агрегатор данных) - market_aggregator.py
    +-- MarketDataUtils (утилиты данных) - market_data_utils.py
    L-- MarketHistoryManager (менеджер исторических данных)

## 5. Модуль: run_bot.py
textrun_bot.py — точка входа и оркестратор всей торговой системы
+-- Главные обязанности:
¦   +-- Сборка всех компонентов через DI
¦   +-- Управление жизненным циклом (start → wait → graceful stop)
¦   +-- Единая система событий BotLifecycleEvent
¦   +-- Поддержка трёх режимов: LIVE / DEMO / BACKTEST
¦   +-- Централизованный обработчик сигналов завершения (SIGINT/SIGTERM)
¦   L-- Генерация финального отчёта в режиме BACKTEST
¦
+-- Ключевые классы и структуры
¦
+-- ComponentsContainer (dataclass)
¦   +-- trade_log: TradingLogger
¦   +-- position_manager: PositionManagerInterface
¦   +-- exchange_manager: ExchangeManagerInterface
¦   +-- strategy: StrategyInterface (ImprovedQualityTrendSystem)
¦   +-- market_aggregator: MarketAggregatorInterface
¦   +-- main_bot: MainBotInterface (MainBotAdapter → EnhancedTradingBot)
¦   +-- exit_manager: AdaptiveExitManager
¦   +-- risk_manager: Optional[EnhancedRiskManager]
¦   +-- logger: logging.Logger
¦   +-- history_manager: Optional[MarketHistoryManager]
¦   +-- async_store: Optional[Any]
¦   L-- created_at: datetime
¦
L-- BotLifecycleManager
    +-- Конструктор:
    ¦     config: Dict,
    ¦     event_handlers: List[BotLifecycleEventHandler] = [],
    ¦     shutdown_timeout_seconds: float = 30.0
    ¦
    +-- Состояние:
    ¦   +-- _shutdown_event: asyncio.Event
    ¦   +-- _components: Optional[ComponentsContainer]
    ¦   +-- _is_running / _stopping флаги
    ¦   +-- _main_loop_task, _trading_task, _monitoring_task
    ¦   L-- _stop_lock: asyncio.Lock() (защита от двойного stop)
    ¦
    +-- Публичные методы:
    ¦   +-- async start() → None
    ¦   ¦     • создаёт все компоненты (_create_components)
    ¦   ¦     • подписывает on_candle_ready → TradingLogger + MainBotAdapter
    ¦   ¦     • запускает три фоновые задачи:
    ¦   ¦         – _run_main_loop()           (health-check + backtest auto-shutdown)
    ¦   ¦         – _run_main_bot_monitoring()  (статистика каждые 60 сек)
    ¦   ¦         – market_aggregator.start()  (основной поток данных)
    ¦   ¦     • эмитит события LIFECYCLE_STARTING → COMPONENTS_CREATED → …
    ¦   ¦     L-- помечает _is_running = True
    ¦   ¦
    ¦   +-- async stop() → None
    ¦   ¦     • безопасно отменяет все фоновые задачи
    ¦   ¦     • ждёт завершения с таймаутом
    ¦   ¦     • вызывает _cleanup() (stop агрегатора, закрытие БД, etc.)
    ¦   ¦     L-- эмитит LIFECYCLE_STOPPED
    ¦   ¦
    ¦   +-- async wait_for_shutdown() → None
    ¦   ¦     • блокирует до установки _shutdown_event
    ¦   ¦     L-- корректно обрабатывает CancelledError
    ¦   ¦
    ¦   +-- add_event_handler / remove_event_handler
    ¦   L-- _emit_event(event_type, data) → рассылает всем подписчикам
    ¦
    +-- Создание компонентов (приватные фабрики):
    ¦   +-- _create_logger() → logging.Logger
    ¦   +-- _create_trade_log() → TradingLogger
    ¦   +-- _create_async_store() → Any (опционально)
    ¦   +-- MarketDataUtils (из trade_log.market_engine)
    ¦   +-- _create_history_manager() → MarketHistoryManager (async engine)
    ¦   +-- _create_strategy() → ImprovedQualityTrendSystem (singleton)
    ¦   +-- _create_risk_manager() → EnhancedRiskManager (по конфигу)
    ¦   +-- _create_exit_manager() → AdaptiveExitManager (по конфигу)
    ¦   +-- _create_position_manager() → PositionManagerImpl
    ¦   +-- _create_exchange_manager() → ExchangeManager (Binance)
    ¦   +-- _create_market_aggregator() → MarketAggregator (через Factory)
    ¦   L-- _create_main_bot_adapter() → MainBotAdapter
    ¦         • оборачивает EnhancedTradingBot
    ¦         • реализует MainBotInterface
    ¦         • подписывает on_candle_ready цепочкой:
    ¦               1. TradingLogger.on_candle_ready()
    ¦               2. EnhancedTradingBot.on_candle_ready()
    ¦
    +-- Сигналы завершения:
    ¦   L-- _setup_signal_handlers() → ловит SIGINT/SIGTERM → asyncio.create_task(stop())
    ¦
    +-- Очистка ресурсов:
    ¦   L-- _cleanup() → graceful остановка всех компонентов (stop(), close(), disconnect_user_stream и т.д.)
    ¦
    L-- Свойства:
        +-- is_running: bool
        L-- components: Optional[ComponentsContainer]
		
Дополнительные функции в модуле
text+-- async def main() → None
¦     • валидация config
¦     • автоопределение BACKTEST по cfg.EXECUTION_MODE
¦     • создание BotLifecycleManager с event_handler (логи + консоль)
¦     • запуск → ожидание shutdown → graceful stop
¦
L-- async def run_backtest_mode() → None
      • отдельный event_handler с красивым финальным отчётом
      • сбор статистики из TradingLogger и MainBot
      • вывод таблицы по символам + итоговый PnL
============================================================================
## 5. Модуль: trade_bot.py

Абстрактные интерфейсы:
trade_bot.py
+-- DataProvider (ABC)
¦   +-- @abstractmethod async get_market_data(symbol: str, timeframes: List[str]) → Dict[str, pd.DataFrame]
¦   L-- @abstractmethod async get_current_price(symbol: str) → float
¦
L-- ExecutionEngine (ABC)
    +-- @abstractmethod async place_order(trade_signal: TradeSignalIQTS) → Dict
    +-- @abstractmethod async close_position(position_id: str) → Dict
    L-- @abstractmethod async get_account_info() → Dict

Основные классы:
+-- EnhancedTradingBot
¦   +-- Конструктор:
¦   ¦     config: Dict,
¦   ¦     data_provider: DataProvider,
¦   ¦     execution_engine: ExecutionEngine,
¦   ¦     trading_system: Optional[ImprovedQualityTrendSystem] = None,
¦   ¦     risk_manager: Optional[EnhancedRiskManager] = None,
¦   ¦     exit_manager: Optional[AdaptiveExitManager] = None,
¦   ¦     validator: Optional[SignalValidator] = None
¦   ¦
¦   +-- Инициализация:
¦   ¦   +-- _setup_logging() → logging.Logger
¦   ¦   +-- _setup_monitoring() → None
¦   ¦   L-- _validate_connections() → None (проверка data_provider + execution_engine)
¦   ¦
¦   +-- Компоненты системы (DI-ready):
¦   ¦   +-- trading_system          → ImprovedQualityTrendSystem (внедрённый или созданный)
¦   ¦   +-- exit_manager            → AdaptiveExitManager (внедрённый или созданный по конфигу)
¦   ¦   +-- risk_manager            → EnhancedRiskManager (опционально)
¦   ¦   +-- validator               → SignalValidator (по умолчанию strict_mode=False)
¦   ¦   +-- monitoring_system       → EnhancedMonitoringSystem (telegram/email алерты)
¦   ¦   L-- position_tracker        → PositionTracker (встроенный)
¦   ¦
¦   +-- Управление данными:
¦   ¦   +-- _get_market_data() → Optional[Dict[str, pd.DataFrame]]
¦   ¦   L-- _parse_timeframe(tf: str) → int (секунды)
¦   ¦
¦   +-- Главная точка входа:
¦   ¦   L-- async on_candle_ready(symbol, candle: Dict, recent_candles: List[Dict]) → None
¦   ¦       • определяет timeframe
¦   ¦       • проверяет наличие активной позиции через PositionTracker
¦   ¦       • если позиция есть → только _manage_existing_positions()
¦   ¦       • если позиции нет → только на 5m генерирует сигнал
¦   ¦
¦   +-- Обработка сигналов:
¦   ¦   +-- _process_trade_signal(signal: TradeSignalIQTS) → None
¦   ¦   ¦     • валидация через SignalValidator
¦   ¦   ¦     • конвертация в intent-based сигнал
¦   ¦   ¦     • вызов execution_engine.place_order()
¦   ¦   ¦     L-- уведомление через monitoring_system
¦   ¦   L-- _convert_iqts_signal_to_trade_signal() → Optional[Dict] (direction → intent)
¦   ¦
¦   +-- Управление открытыми позициями:
¦   ¦   +-- _manage_existing_positions(market_data) → None
¦   ¦   ¦     • вызов AdaptiveExitManager.update_position_stops()
¦   ¦   ¦     • при should_exit → execution_engine.close_position()
¦   ¦   ¦     L-- обновление трейлинг-стопов
¦   ¦   +-- _update_position_stop_loss(position_id, new_sl) → None
¦   ¦   L-- _handle_position_closed(position_id, close_price) → None
¦   ¦
¦   +-- Уведомления и логирование:
¦   ¦   +-- _send_trade_notification(trade_signal, execution_result) → None
¦   ¦   +-- _send_position_closed_notification(position_id, trade_result) → None
¦   ¦   L-- _log_system_status() → None
¦   ¦
¦   +-- Валидация данных:
¦   ¦   +-- _basic_validate_market_data(market_data) → bool (OHLCV, NaN, геометрия свечей)
¦   ¦   L-- локальные проверки цены и свечей в on_candle_ready()
¦   ¦
¦   +-- Аварийные процедуры:
¦   ¦   +-- async _emergency_shutdown() → None (закрывает все позиции)
¦   ¦   L-- async shutdown() → None (полная грациозная остановка, отменяет задачи)
¦   ¦
¦   +-- Публичные методы:
¦   ¦   +-- async start() → None
¦   ¦   L-- get_status() → Dict (is_running, позиции, статистика)
¦   ¦
¦   L-- Свойства / состояние:
¦       • is_running: bool
¦       • active_positions: Dict (исторически использовался, сейчас дублирует PositionTracker)
¦       • position_tracker: PositionTracker
¦       • trading_system, exit_manager, monitoring_system
¦
L-- PositionTracker
    +-- Конструктор: max_history: int = 1000
    +-- Методы управления:
    ¦   +-- add_position(position_id: str, position_data: Dict) → None
    ¦   +-- get_position(position_id) → Optional[Dict]
    ¦   +-- get_all_positions() → Dict[str, Dict]
    ¦   +-- has_active_position(symbol: str) → bool
    ¦   L-- close_position(position_id, close_price, realized_pnl) → None
    ¦
    +-- Расчёт PnL:
    ¦   +-- update_position_pnl(position_id, current_price) → None
    ¦   +-- calculate_realized_pnl(position_id, close_price) → float (с учётом комиссий)
    ¦   L-- get_total_unrealized_pnl() → float
    ¦
    L-- История закрытых позиций:
        L-- get_closed_positions(limit: int = 100) → List[Dict] (deque с maxlen)
======================================================================================
		
## 6. Модуль: ImprovedQualityTrendSystem.py
Типы данных:
ImprovedQualityTrendSystem.py — главная торговая стратегия v3.0 (финальная архитектура)
Типы данных:
ImprovedQualityTrendSystem.py
+-- RegimeType = Literal[
¦     "strong_uptrend", "weak_uptrend",
¦     "strong_downtrend", "weak_downtrend",
¦     "sideways", "uncertain"
¦   ]
+-- VolumeProfileType = Literal["high", "normal", "low"]
¦
L-- MarketRegime (dataclass)
    +-- regime: RegimeType
    +-- confidence: float [0.0–1.0]
    +-- volatility_level: float
    +-- trend_strength: float
    L-- volume_profile: VolumeProfileType

Основной класс:
L-- ImprovedQualityTrendSystem (реализует TradingSystemInterface)
    +-- Конструктор:
    ¦     config: Dict[str, Any]
    ¦     data_provider: Optional[Any] = None
    ¦
    +-- Компоненты системы (DI-ready):
    ¦ +-- three_level_confirmator: ThreeLevelHierarchicalConfirmator
    ¦ ¦       (global_timeframe="5m", trend_timeframe="1m")
    ¦ +-- risk_manager: EnhancedRiskManager (инициализируется из config.risk_limits)
    ¦ L-- _cached_global_signal: Dict[str, Dict] — кэш сильных 5m сигналов при разногласиях
    ¦
    +-- Инициализация:
    ¦ +-- _initialize_risk_manager() → EnhancedRiskManager
    ¦ +-- _initialize_performance_tracker() → Dict (total_trades, winning_trades, total_pnl)
    ¦ +-- _setup_logging() → отдельный логгер без дублирования
    ¦ L-- чтение quality_config → global/trend таймфреймы, пороги
    ¦
    +-- Фильтры качества:
    ¦ +-- _apply_quality_filters(signal: DetectorSignal) → DetectorSignal | None
    ¦ ¦     • min_confidence, min_volume_ratio, volatility_bounds
    ¦ +-- _adaptive_volume_filter(df_1m) → bool
    ¦ L-- _adaptive_volatility_filter(atr_current, atr_ema) → bool
    ¦
    +-- Проверки условий:
    ¦ +-- _check_trading_conditions(symbol) → bool
    ¦ ¦     • торговые часы, max_daily_trades, blacklist
    ¦ +-- _is_trading_session_now() → bool (по trading_hours из config)
    ¦ L-- _validate_market_data_quality(market_data) → bool (NaN, объём, геометрия)
    ¦
    +-- Анализ рынка:
    ¦ +-- _update_market_regime(market_data) → None
    ¦ ¦     обновляет self.current_regime
    ¦ L-- _calculate_atr(df, period=14) → float
    ¦
    +-- Основные методы интерфейса:
    ¦
    ¦ +-- async analyze_and_trade(
    ¦ ¦       market_data: Dict[Timeframe, pd.DataFrame]
    ¦ ¦   ) → Optional[TradeSignalIQTS]
    ¦ ¦     Полный цикл анализа:
    ¦ │     1. validate → check_conditions → confirmator.analyze()
    ¦ │     2. apply_quality_filters()
    ¦ │     3. risk_manager.calculate_risk_context()
    ¦ │     4. формирование TradeSignalIQTS с:
    ¦ │         • stops_precomputed=True
    ¦ │         • risk_context (position_size, SL, TP, ATR и т.д.)
    ¦ │         • validation_hash
    ¦ │     5. запись в performance_tracker
    ¦ │     L. возврат сигнала в PositionManager
    ¦ │
    ¦ +-- async generate_signal(
    ¦ ¦       symbol: str,
    ¦       market_data: Dict[Timeframe, pd.DataFrame]
    ¦   ) → Optional[Dict]
    ¦ │     Упрощённый путь (для PositionManager):
    ¦ │     • Вызов three_level_confirmator.analyze()
    ¦ │     • При ok=True → немедленный сигнал
    ¦ │     • При direction_disagreement → кэшируем сильный 5m сигнал
    ¦ │     • При последующих вызовах → check_cached_global_signal()
    ¦ │     L. Возвращает минимальный сигнал (direction, confidence, entry_price)
    ¦ │
    ¦ +-- async check_cached_global_signal(
    ¦       symbol: str,
    ¦       market_data: Dict[Timeframe, pd.DataFrame]
    ¦   ) → Optional[Dict]
    ¦ │     Логика отложенного входа:
    ¦ │     • TTL кэша: 5 минут (300 000 ms)
    ¦ │     • Проверка согласованности текущего 1m тренда с кэшированным 5m
    ¦ │     • При согласии → генерация delayed_signal с текущей ценой
    ¦ │     • confidence = max(cached_conf, current_trend_conf)
    ¦ │     L. Очистка кэша после успешного входа
    ¦ │
    ¦ +-- update_performance(result: TradeResult) → None
    ¦ +-- get_system_status() → SystemStatus
    ¦ L-- get_performance_report() → Dict (total, daily, by_regime)
    ¦
    +-- Состояние системы:
    ¦ +-- current_regime: Optional[MarketRegime]
    ¦ +-- trades_today: int
    ¦ +-- daily_stats: Dict (trades, win_rate, pnl)
    ¦ +-- performance_tracker: Dict
    ¦ +-- account_balance: float (для симуляции)
    ¦ L-- _daily_stats_lock: threading.Lock() — потокобезопасность
    ¦
    L-- Управление жизненным циклом:
        L-- async shutdown() → None
              • финальный performance_report в лог
              • очистка кэша и блокировок

Ключевые особенности реализации:
+-- Упрощённая архитектура: прямая связь с ThreeLevelHierarchicalConfirmator
+-- Два независимых потока:
      • analyze_and_trade → полный TradeSignalIQTS с risk_context
      • generate_signal + check_cached_global_signal → отложенные входы
+-- Интеллектуальное кэширование сильных 5m сигналов при разногласиях
+-- Автоматические отложенные входы без пропуска тренда
+-- Адаптивные фильтры объёма и волатильности (EMA-сглаживание)
+-- Полная интеграция с EnhancedRiskManager → точные стопы и размеры
+-- Потокобезопасность статистики и кэша
L-- Типовая безопасность: Literal, TypedDict, dataclass

Поток данных:
+-- Основной (полный):
      on_candle_ready → analyze_and_trade()
          ↓
      ThreeLevelHierarchicalConfirmator → ok=True → EnhancedRiskManager → TradeSignalIQTS
          ↓
      PositionManager.handle_signal()
+-- Упрощённый (отложенный вход):
      generate_signal() → direction_disagreement → _cached_global_signal
          ↓ (следующие 5 минут)
      check_cached_global_signal() → согласование → delayed_signal → вход по рынку
          ↓
      PositionManager.handle_signal()

Логика кэширования:
+-- Сохраняется только при confidence ≥ 0.60 и чётком направлении
+-- TTL: 300 000 мс (5 минут)
+-- Автоматическая очистка при:
      • успешном входе
      • смене направления
      • истечении TTL
+-- Проверка согласованности с текущим 1m трендом перед выдачей delayed_signal
L-- confidence берётся как максимум из кэша и текущего анализа

Интеграция в систему:
BotLifecycleManager → ImprovedQualityTrendSystem
    +-- analyze_and_trade() → полный цикл с risk_context
    +-- generate_signal() + check_cached_global_signal() → отложенные входы
    L-- Полная совместимость с PositionManager и EnhancedTradingBot (DI)

===========================================================================

## 7. МОДУЛЬ: ml_global_detector.py

ml_global_detector.py — ML-детектор глобального разворота на 5m (LightGBM)
+-- Назначение:
    +-- Основной источник глобального тренда в ThreeLevelHierarchicalConfirmator
    +-- Поддержка двух форматов моделей:
        • Современный пакетный (joblib): окно lookback × base_features + scaler + policy
        • Legacy (raw Booster): одно-бартовый вход (обратная совместимость)
    +-- Классы предсказания:
        0 → FLAT
        1 → BUY reversal
        2 → SELL reversal
    +-- Полная интеграция с decision_policy (tau/delta/cooldown)
    L-- Используется в MLGlobalTrendDetector как основной движок

Основной класс:
L-- MLGlobalDetector (наследует Detector из iqts_standards)
    +-- Конструктор:
    ¦     timeframe: Timeframe = "5m"
    ¦     model_path: str = "models/ml_global_5m_lgbm.joblib"
    ¦     use_fallback: bool = False (не используется напрямую — управляется выше)
    ¦     name: Optional[str]
    ¦     use_scaler: Optional[bool] = None (автоопределение из модели)
    ¦
    +-- Загрузка модели (умная):
    ¦     • joblib.load() → dict или Booster
    ¦     • Пакетный формат:
    ¦         {
    ¦   "model": lgb.Booster,
    ¦   "scaler": StandardScaler,
    ¦   "base_feature_names": [...],
    ¦   "lookback": 12,
    ¦   "required_warmup": 50,
    ¦   "metadata": { "decision_policy": {...}, "scaler_used": True }
    ¦ }
    ¦     • Legacy формат: только Booster → lookback=1, без скейлера
    ¦
    +-- Атрибуты после загрузки:
    ¦ +-- model: lgb.Booster
    ¦ +-- scaler: Optional[StandardScaler]
    ¦ +-- base_feature_names: List[str] (из модели)
    ¦ +-- lookback: int (из модели, минимум 1)
    ¦ +-- feature_names: List[str] — оконные имена: f"{feat}_t{i}"
    ¦ +-- decision_policy: Optional[Dict] — tau, delta, cooldown, bars_per_day
    ¦ +-- min_confidence: float = 0.53 (из метаданных)
    ¦ +-- required_warmup: int
    ¦ L-- model_metadata: Dict (версия, формат, scaler_used и т.д.)
    ¦
    +-- Формирование вектора признаков:
    ¦ L-- _prepare_features(df_5m: pd.DataFrame) → np.ndarray
    ¦       • Берёт последние lookback свечей
    ¦       • Для каждого base_feature формирует окно t0..t-(lookback-1)
    ¦       • Возвращает плоский вектор (lookback × len(base_features))
    ¦       • Применяет scaler (если есть)
    ¦
    +-- Основной метод:
    ¦ L-- async analyze(data: Dict[Timeframe, pd.DataFrame]) → DetectorSignal
    ¦       1. Проверка наличия 5m данных и warmup
    ¦       2. _prepare_features() → X
    ¦       3. model.predict(X) → [prob_flat, prob_buy, prob_sell]
    ¦       4. Определение класса: argmax
    ¦       5. Confidence = максимальная вероятность
    ¦       6. Применение decision_policy (если есть):
    ¦            • cooldown: блокировка сигнала после предыдущего
    ¦            • tau/delta: фильтрация слабых движений
    ¦       7. Формирование DetectorSignal:
    ¦            • ok = confidence >= min_confidence
    ¦            • direction = 1 (BUY), -1 (SELL), 0 (FLAT)
    ¦            • reason = "ml_global_buy" / "ml_global_sell" / "ml_flat"
    ¦            • metadata: probas, feature_importance (опционально)
    ¦
    +-- Вспомогательные методы:
    ¦ +-- _generate_windowed_feature_names() → List[str]
    ¦ +-- get_required_bars() → { "5m": max(warmup, lookback) }
    ¦ +-- get_status() → Dict (модель загружена, lookback, scaler, policy)
    ¦ L-- reset_state() → очистка cooldown и истории
    ¦
    L-- Обработка ошибок:
          • При любой ошибке → raise → выше MLGlobalTrendDetector активирует CUSUM fallback
Поток работы

MLGlobalTrendDetector.analyze()
    ↓
MLGlobalDetector._prepare_features() → X (lookback × features)
    ↓
model.predict(X) → [0.12, 0.78, 0.10]
    ↓
class=1 (BUY), confidence=0.78
    ↓
применить decision_policy (cooldown=6 баров и т.д.)
    ↓
DetectorSignal(ok=True, direction=1, confidence=0.78, reason="ml_global_buy")
===========================================================================
	
## 8. Модуль: iqts_detectors.py
iqts_detectors.py — объединённый модуль всех детекторов сигналов (v3.0)
+-- Назначение:
    +-- Единое место для всех детекторов, используемых в ThreeLevelHierarchicalConfirmator
    +-- Поддержка ML + fallback на CUSUM
    +-- Чистый, типобезопасный, асинхронный интерфейс
    L-- Заменяет старые разрозненные модули детекторов

Типы данных:
iqts_detectors.py
+-- DetectorSignal (из iqts_standards)
    {
      "ok": bool,
      "direction": int | Literal["BUY","SELL","FLAT"],
      "confidence": float,
      "reason": str,
      "metadata": dict
    }

Основные классы:

L-- MLGlobalTrendDetector (наследует Detector)
    +-- Глобальный тренд-детектор на 5m
    +-- Конструктор:
    ¦     timeframe: Timeframe = "5m"
    ¦     model_path: str = "models/ml_global_5m_lgbm.joblib"
    ¦     use_fallback: bool = True
    ¦     cusum_config: Optional[CusumConfig] = CUSUM_CONFIG_5M
    ¦
    +-- Компоненты:
    ¦ +-- ml_detector: MLGlobalDetector (из ml_global_detector.py)
    ¦ L-- fallback_detector: GlobalTrendDetector (CUSUM-based)
    ¦
    +-- Поведение:
    ¦     • При старте пытается загрузить LGBM модель
    ¦     • При ошибке → активирует CUSUM fallback
    ¦     • using_fallback = True только после активации
    ¦     • Логирует все переходы: "ML model loaded", "Activating CUSUM fallback"
    ¦
    L-- async analyze(data: Dict[Timeframe, pd.DataFrame]) → DetectorSignal
          • Сначала пытается ML
          • При ошибке → fallback
          • Возвращает нормализованный DetectorSignal

L-- RoleBasedOnlineTrendDetector (наследует Detector)
    +-- Локальный тренд-детектор на 1m (CUSUM + роль)
    +-- Использует CUSUM_CONFIG_1M по умолчанию
    +-- Векторный расчёт CUSUM (быстрый pandas)
    +-- Выдаёт:
          • direction: 1 / -1 / 0
          • confidence: на основе cusum_zscore
          • reason: "cusum_long", "cusum_short", "no_signal"
    L-- Полностью потокобезопасен (без состояния между вызовами)

L-- GlobalTrendDetector (CUSUM fallback для 5m)
    +-- Простой онлайн CUSUM на основе скользящей истории цен
    +-- Динамический порог: max(0.01% цены, std)
    +-- Состояние:
    ¦     • price_history: List[float]
    ¦     • cusum_pos / cusum_neg: float
    ¦     • cusum_threshold: float (по умолчанию 4.0)
    +-- Методы:
    ¦     • analyze(current_price) → DetectorSignal
    ¦     • reset_state()
    ¦     L-- get_status()
    +-- Используется как резервный при падении ML

L-- MLGlobalDetector (внутренний класс, не экспортируется)
    +-- Загрузка и инференс LightGBM модели
    +-- Преобразование 5m фич в X для predict_proba
    +-- Возвращает confidence и direction
    L-- Обёрнут в MLGlobalTrendDetector для fallback-логики

==================================================================
		
## 9. Модуль: multi_timeframe_confirmator.py
multi_timeframe_confirmator.py — ядро подтверждения сигналов в иерархической системе
+-- Назначение:
    +-- Заменяет всю старую цепочку (HierarchicalQualityTrendSystem → ThreeLevel…)
    +-- Единый 3-уровневый конфирматор: Глобальный тренд (5m) → Локальный тренд (1m)
    +-- Используется напрямую в ImprovedQualityTrendSystem (упрощённая архитектура v3.0)
    +-- Только фильтрация и согласование — НЕ меняет confidence дочерних детекторов
    L-- Возвращает стандартизированный DetectorSignal для дальнейшей обработки

Типы данных:
multi_timeframe_confirmator.py
+-- DetectorSignal (из iqts_standards)
    {
      ok: bool
      direction: "BUY" | "SELL" | "FLAT"
      confidence: float [0.0-1.0]
      reason: str
      metadata: dict

Основной класс:
L-- ThreeLevelHierarchicalConfirmator (наследует Detector)
    +-- Конструктор:
    ¦     global_timeframe: Timeframe = "5m"
    ¦     trend_timeframe: Timeframe = "1m"
    ¦     name: str = "ThreeLevelHierarchicalConfirmator"
    ¦
    +-- Дочерние детекторы (DI-ready):
    ¦ +-- global_detector: MLGlobalTrendDetector
    ¦ ¦       • model_path="models/ml_global_5m_lgbm.joblib"
    ¦ ¦       • use_fallback=True
    ¦ ¦       • timeframe = global_timeframe
    ¦ L-- trend_detector: RoleBasedOnlineTrendDetector
    ¦         • timeframe = trend_timeframe
    ¦         • role="trend"
    ¦
    +-- Пороги уверенности:
    ¦     min_global_confidence = 0.60
    ¦     min_trend_confidence  = 0.55
    ¦     direction_agreement_required = True
    ¦
    +-- Состояние:
    ¦ +-- _last_signal: Optional[DetectorSignal]
    ¦ +-- last_confirmed_direction: Optional[int]
    ¦ +-- confirmation_count: int
    ¦ +-- global_signal_history: List[Dict] (max_history_length)
    ¦ L-- trend_signal_history: List[Dict]
    ¦
    +-- Основной метод:
    ¦ L-- async analyze(data: Dict[Timeframe, pd.DataFrame]) → DetectorSignal
    ¦       1. validate_market_data()
    ¦       2. Параллельный вызов:
    ¦            • global_detector.analyze(data)
    ¦            • trend_detector.analyze(data)
    ¦       3. Пороговые гейты (ok + confidence)
    ¦       4. Приоритетная логика согласования:
    ¦            • Противоположные направления → direction_disagreement → FLAT
    ¦            • Слабые сигналы → weak_signals → FLAT
    ¦            • Нет данных → insufficient_data → FLAT
    ¦       5. Агрегация confidence (взвешенная сумма)
    ¦       6. Формирование итогового DetectorSignal
    ¦       7. Обновление истории и счётчиков
    ¦
    +-- Логика согласования (приоритеты):
    ¦ 1. Оба сигнала сильные и согласны → ok=True, direction=BUY/SELL
    ¦ 2. Направления противоположны → reason="direction_disagreement"
    ¦ 3. Один/оба слабые → reason="weak_signals"
    ¦ 4. Недостаточно данных → reason="insufficient_data"
    ¦
    +-- Агрегация уверенности:
    ¦ L-- _calculate_combined_confidence(global_conf, trend_conf) → float
    ¦       • Взвешенная сумма с коэффициентами из config.weights
    ¦       • Без дополнительного штрафа за волатильность
    ¦
    +-- История сигналов:
    ¦ +-- _update_global_history(signal)
    ¦ L-- _update_trend_history(signal)
    ¦       • Хранит последние N сигналов для анализа качества
    ¦
    +-- Диагностика:
    ¦ +-- get_last_signal() → Optional[DetectorSignal]
    ¦ +-- get_system_status() → Dict
    ¦       • таймфреймы, счётчики, статусы дочерних детекторов
    ¦       • confidence_weights, параметры порогов
    ¦ L-- reset_state() → полная очистка истории и счётчиков
    ¦
    L-- Внутренние вспомогательные методы:
          • _validate_input_data()
          • _check_direction_agreement()
          • _set_last_signal()
          • _update_signal_history()
========================================================
		
## 10. Модуль: market_aggregator.py
Абстрактный базовый класс:
market_aggregator.py
+-- BaseMarketAggregator (ABC)
¦   +-- Конструктор: logger_instance
¦   +-- Состояние:
¦   ¦   +-- _is_running: bool
¦   ¦   +-- _main_lock: threading.RLock
¦   ¦   +-- _connection_state: NetConnState
¦   ¦   L-- _stats: Dict[str, Any]
¦   +-- Фоновые задачи:
¦   ¦   +-- _running_tasks: Dict[str, asyncio.Task]
¦   ¦   L-- _create_or_cancel_task() > asyncio.Task
¦   +-- Утилиты:
¦   ¦   +-- _convert_to_decimal() > Decimal
¦   ¦   +-- _convert_to_float() > float
¦   ¦   +-- _cancel_all_tasks() > None
¦   ¦   L-- _candle_dict_to_candle1m() > Candle1m
¦   +-- Управление жизненным циклом:
¦   ¦   +-- stop() > None
¦   ¦   +-- shutdown() > None
¦   ¦   L-- get_stats() > Dict[str, Any]
¦   L-- Абстрактные методы:
¦       +-- _get_mode() > str
¦       L-- async start_async() > None
¦
+-- Вспомогательные функции:
¦   +-- bucket_ts_with_phase() > int
¦   L-- finalize_cutoff() > int
¦
+-- LiveMarketAggregator (BaseMarketAggregator)
¦   +-- Конструктор: db_dsn, on_candle_ready, on_connection_state_change, interval_ms, logger_instance, trading_logger
¦   +-- Состояние:
¦   ¦   +-- _symbol_buffers: Dict[str, deque]
¦   ¦   +-- _active_symbols: List[str]
¦   ¦   L-- _market_data_utils: MarketDataUtils
¦   +-- Основные методы:
¦   ¦   +-- _get_mode() > str
¦   ¦   +-- async start_async() > None
¦   ¦   L-- async wait_for_completion() > None
¦   L-- Интерфейс:
¦       +-- add_event_handler() > None
¦       +-- fetch_recent() > List[Candle1m]
¦       L-- get_connection_state() > NetConnState
¦
+-- DemoMarketAggregatorPhased (BaseMarketAggregator)
¦   +-- Конструктор: config, on_candle_ready, on_connection_state_change, logger_instance, trading_logger
¦   +-- Константы:
¦   ¦   +-- ONE_M_MS: int = 60_000
¦   ¦   L-- FIVE_M_MS: int = 300_000
¦   +-- Состояние:
¦   ¦   +-- _active_symbols: List[str]
¦   ¦   +-- _symbol_buffers_1m: Dict[str, deque]
¦   ¦   +-- _symbol_buffers_5m: Dict[str, deque]
¦   ¦   +-- _last_historical_ts: Dict[str, int]
¦   ¦   +-- _last_historical_ts_5m: Dict[str, int]
¦   ¦   +-- _websocket: Optional[WebSocket]
¦   ¦   +-- _ws_task: Optional[asyncio.Task]
¦   ¦   L-- _market_data_utils: MarketDataUtils
¦   +-- WebSocket управление:
¦   ¦   +-- async _connect_ws() > None
¦   ¦   +-- async _ws_loop() > None
¦   ¦   L-- async _schedule_reconnect() > None
¦   +-- Обработка данных:
¦   ¦   +-- _on_kline_1m() > None
¦   ¦   +-- _on_kline_5m() > None
¦   ¦   +-- _kline_to_candle1m() > Optional[Candle1m]
¦   ¦   +-- _candle_to_dict() > Dict[str, Any]
¦   ¦   +-- _on_candle_ready_1m() > None
¦   ¦   L-- async _on_candle_ready_5m() > None
¦   L-- Интерфейс:
¦       +-- add_event_handler() > None
¦       +-- fetch_recent() > List[Candle1m]
¦       L-- get_buffer_history() > List[Candle1m]
¦
+-- BacktestMarketAggregatorFixed (BaseMarketAggregator)
¦   +-- Конструктор: trading_logger, on_candle_ready, symbols, virtual_clock_start_ms, virtual_clock_end_ms, interval_ms, logger
¦   +-- Состояние:
¦   ¦   +-- _symbol_buffers: Dict[str, deque]
¦   ¦   +-- _engine: Engine
¦   ¦   L-- _stats: Dict[str, Any]
¦   +-- Воспроизведение данных:
¦   ¦   +-- async _replay_loop() > None
¦   ¦   L-- async start_async() > None
¦   L-- Интерфейс:
¦       +-- async wait_for_completion() > None
¦       +-- fetch_recent() > List[Candle1m]
¦       L-- get_buffer_history() > List[Candle1m]
¦
L-- MarketAggregatorFactory
    +-- Статические методы:
    ¦   +-- validate_config() > List[str]
    ¦   +-- _create_live_aggregator() > MarketAggregatorInterface
    ¦   +-- _create_demo_aggregator() > MarketAggregatorInterface
    ¦   L-- _create_backtest_aggregator() > MarketAggregatorInterface
    L-- Основной метод:
        L-- create_market_aggregator() > MarketAggregatorInterface
==================================================================
## 11. Модуль: market_data_utils.py
market_data_utils.py — единый расчётный движок индикаторов и DAO-слой для candles_1m / candles_5m
+-- Назначение:
¦   +-- Обеспечение 100% идентичных индикаторов во всех режимах (LIVE / DEMO / BACKTEST)
¦   +-- Автоматическая миграция схемы БД (добавление новых колонок)
¦   +-- Bulk и incremental расчёт CUSUM(1m) + ML-фич 5m
¦   L-- Поставка готовых данных для ImprovedQualityTrendSystem и LightGBM-модели
¦
+-- Ключевые структуры данных
¦
+-- CusumConfig
¦   +-- normalize_window, eps, h, z_to_conf
¦   L-- CUSUM_CONFIG_1M и CUSUM_CONFIG_5M — раздельные настройки по ТФ
¦
+-- IndicatorConfig
¦   +-- ema_periods = [3,7,9,15,30]
¦   +-- cmo_period = 14, adx_period = 14, atr_period = 14
¦   +-- macd = (12,26,9), bb_period = 20, vwap_period = 96
¦   L-- используется при генерации DDL и при расчётах
¦
L-- CalculationMetrics
    +-- symbol, started_at → completed_at
    +-- rows_processed, indicators_count, errors_count, duration_ms
    L-- complete() → фиксирует метрики
¦
+-- Схема и миграции БД
¦
L-- ensure_market_schema(engine, logger=None)
    • Читает FEATURE_NAME_MAP из iqts_standards
    • Генерирует DDL для candles_1m и candles_5m с динамическими колонками
    • Создаёт таблицы CREATE TABLE IF NOT EXISTS
    • Добавляет недостающие колонки через ALTER TABLE (idempotent)
    • Создаёт индексы: idx_{table}_symbol_ts, idx_{table}_finalized
    L-- Полностью автоматическая поддержка новых фич без ручного ALTER
¦
+-- DAO-методы (работа с БД)
¦
+-- upsert_candles_1m(engine, candles: List[Dict]) → None
¦   • INSERT OR REPLACE по PRIMARY KEY (symbol, ts)
¦   • Автоматическое приведение всех полей к правильным типам
¦
+-- upsert_candles_5m(engine, candles: List[Dict]) → None
¦   • Аналогично 1m, но с ML-фичами
¦
+-- get_last_n_candles(engine, symbol, timeframe, n=500) → List[Dict]
¦   L-- Используется для warmup и проверки
¦
+-- mark_candle_finalized(engine, symbol, ts, tf="1m") → None
¦
+-- Расчёт CUSUM (1m и 5m)
¦
L-- compute_cusum_1m_incremental(df: pd.DataFrame) → pd.DataFrame
    • Δclose → rolling mean/std → z-score
    • k_t = h × σ_t
    • CUSUM+ / CUSUM– с динамическим порогом
    • cusum_state: 1 (up), -1 (down), 0 (neutral)
    • cusum_zscore, cusum_confidence, cusum_reason
    L-- Полностью векторизованный pandas (быстро даже на 100k+ строк)
¦
+-- Расчёт ML-фич 5m (основной пайплайн)
¦
L-- compute_5m_features_bulk(bars_5m: List[Dict]) → List[Dict]
    • Принимает список словарей 5-минутных свечей (уже с 1m-данными внутри)
    • Вычисляет ВСЕ признаки из FEATURE_NAME_MAP["5m"]:
    ¦   +-- EMA(3,7,9,15,30), CMO(14), ADX(14), ATR(14)
    ¦   +-- MACD histogram, Bollinger width, VWAP(96)
    ¦   +-- price_change_5, trend_momentum_z (Z-score от price_change_5)
    ¦   +-- trend_acceleration_ema7 = ΔEMA(7)
    ¦   +-- volume_ratio_ema3 = Volume / EMA(Volume,3)
    ¦   +-- candle_relative_body, upper_shadow_ratio, lower_shadow_ratio
    ¦   +-- price_vs_vwap = (Close - VWAP)/VWAP
    ¦   +-- 1m-based features:
    ¦         – cusum_1m_recent, cusum_1m_quality_score
    ¦         – is_trend_pattern_1m, body_to_range_ratio_1m, close_position_in_range_1m
    L-- Возвращает список словарей — готово для upsert_candles_5m()
¦
+-- Вспомогательные векторизованные функции
¦   +-- _ema_series(values, period)
¦   +-- _macd_series(fast, slow, signal)
¦   +-- _bb_width_series(close, period=20)
¦   +-- _vwap_series(bars_5m, period=96)
¦   +-- _z_score_series(values, window=20)
¦   +-- _trend_acceleration_series(ema7)
¦   +-- _volume_ratio_ema3_series(volume)
¦   +-- _candle_body_ratios(open, high, low, close)
¦   +-- _price_vs_vwap_series(close, vwap)
¦   L-- _pattern_features_1m() → (is_trend_pattern, body_ratio, close_pos)
¦
L-- warmup_5m_indicators(symbol, engine) → None
    • Загружает последние N свечей 1m
    • Агрегирует в 5m
    • Вызывает compute_5m_features_bulk()
    • Записывает в БД
    L-- Вызывается при старте бота (гарантирует актуальные фичи)
====================================================================

## 12. Модуль: market_history.py
Типы данных:
market_history.py — полноценный менеджер загрузки и разогрева исторических данных Binance Futures
+-- Назначение:
    +-- Надёжная загрузка 1m и 5m свечей с Binance (с обработкой rate-limit и дыр)
    +-- Автоматический расчёт всех индикаторов (CUSUM 1m + ML-features 5m)
    +-- "Умный" прогрев: продолжение с последнего обработанного бара
    +-- Интерактивный CLI-режим для ручного запуска/догрузки данных
    L-- 100% совместимость с market_data_utils.py и схемой БД
+
+-- Ключевые классы
+
+-- BinanceDataFetcher
    +-- Асинхронный HTTP-клиент только для одной страницы klines
    +-- fetch_candles(symbol, interval, start_time, end_time, limit=1000)
          • Экспоненциальный backoff при 429
          • 3 попытки, таймаут 30 сек
          • НЕ делает пагинацию — только одну страницу
    +-- _process_raw_candles() → преобразует сырой ответ Binance в Candle1m-совместимый dict
    L-- Логирование: debug при пустой странице, warning при rate-limit
+
+-- IndicatorWarmupManager
    +-- warmup_1m_indicators(symbol, candles_1m) → пересчёт CUSUM через MarketDataUtils
    +-- warmup_5m_indicators(symbol, candles_5m) → compute_5m_features_bulk()
    L-- warmup_config: минимальное количество баров (по умолчанию 28)
+
L-- MarketHistoryManager (главный оркестратор)
    +-- Конструктор:
          async_engine: AsyncEngine
          market_data_utils: MarketDataUtils
          logger
    +
    +-- Основной метод:
          L-- interactive_load(days_back=90, symbols=None)
                • Запрашивает символы у пользователя (или берёт из config)
                • Для каждого символа:
                    – Проверяет, сколько уже есть данных
                    – Догружает недостающие 1m свечи с Binance
                    – Проверяет целостность (нет дыр > 1 минуты)
                    – При дыре — перезагружает весь проблемный интервал
                    – Записывает в candles_1m
                    – Пересчитывает CUSUM 1m (если нужно)
                    – Догружает/пересчитывает 5m фичи с продолжением
                • tqdm-прогресс-бар с красивым описанием этапов
                • Финальный отчёт: сколько свечей обработано, пересчитано и т.д.
    +
    +-- Внутренние методы:
          +-- _download_interval_strict(symbol, interval, start, end)
                • Делает пагинацию строго по 1000 свечей
                • Проверяет непрерывность ts (допускается разрыв ≤ 1 минута)
                • При большой дыре — логирует ошибку и перезагружает весь интервал
          +-- _find_last_processed_5m_candle(symbol) → ts или None
                • Ищет последнюю 5m свечу с заполненными ML-фичами
                • Позволяет возобновлять расчёт, а не пересчитывать всё заново
          L-- _check_existing_data_interactive() → словарь с текущим состоянием данных
+
+-- CLI entry point
      L-- async def main() → запуск interactive_load() с настройка sync + async engine
	
Интеграция в общий поток данных:

MarketHistoryManager > Binance API
    v
Загрузка исторических данных > MarketDataUtils
    v
Разогрев индикаторов > Торговая стратегия

Процесс загрузки истории:
BotLifecycleManager.start()
    > _create_history_manager() > MarketHistoryManager
    > load_history() 
        > BinanceDataFetcher.fetch_candles()
        > IndicatorWarmupManager.warmup_*_indicators()
        > MarketDataUtils.upsert_candles_*m()
        > MarketDataUtils.compute_*_features_bulk()

Особенности реализации:
+-- Интеллектуальная проверка существующих данных
+-- Обработка пропусков в данных (gap detection)
+-- Продолжение расчета с последней обработанной свечи
+-- Интерактивный режим с прогресс-баром
L-- Поддержка как 1m, так и 5m таймфреймов


Интеграция в общий поток данных:

MarketAggregator > MarketDataUtils > База данных
    v
Candle события > Индикаторы > Торговая стратегия

DemoMarketAggregatorPhased (режим DEMO)
    > WebSocket Binance Futures
    > Обработка kline 1m/5m
    > MarketDataUtils.upsert_candles_1m/5m()
    > Расчет индикаторов (CUSUM, ML features)
    > Колбэк on_candle_ready

MarketDataUtils (вычислительное ядро)
    > Асинхронные операции с БД
    > Векторные расчеты индикаторов
    > Анти look-ahead нормализация
    > Кэширование и метрики

BacktestMarketAggregatorFixed (режим BACKTEST)
    > Чтение исторических данных из БД
    > Воспроизведение с виртуальным временем
    > Инкрементальный расчет индикаторов
    > Эмуляция рыночных событий

		
BotLifecycleManager.start()
    > _create_components()
        > _create_strategy() > ImprovedQualityTrendSystem
        > _create_main_bot() > EnhancedTradingBot
            > DataProviderFromDB
            > ExecutionEngineFromExchangeManager  
            > MainBotAdapter

EnhancedTradingBot.start()
    > trading_system.analyze_and_trade() > ImprovedQualityTrendSystem.analyze_and_trade()
        > three_level_confirmator.analyze() > ThreeLevelHierarchicalConfirmator.analyze()
            > global_detector.analyze() > MLGlobalTrendDetector.analyze()
            > trend_detector.analyze() > RoleBasedOnlineTrendDetector.analyze()

ThreeLevelHierarchicalConfirmator.analyze()
    > ML-модель (через MLGlobalDetector) или CUSUM fallback (GlobalTrendDetector)
    > CUSUM анализ (RoleBasedOnlineTrendDetector)

EnhancedTradingBot._process_trade_signal()
    > execution_engine.place_order() > ExecutionEngineFromExchangeManager.place_order()
    > position_tracker.add_position() > PositionTracker.add_position()
================================================================	

## 13. Модуль: iqts_standards.py
Типы данных и литералы:
iqts_standards.py
+-- Timeframe = Literal["1m", "5m", "15m", "1h"]
+-- DirectionLiteral = Literal[1, -1, 0]
+-- MarketRegimeLiteral = Literal["strong_uptrend", "weak_uptrend", ...]
+-- ReasonCode = Literal["hierarchical_confirmed", "trend_confirmed", ...]
+-- SignalIntent = Literal["LONG_OPEN", "LONG_CLOSE", ...]
+-- ExecutionMode = Literal["LIVE", "DEMO", "BACKTEST"]
L-- ConnectionStatus = Literal["connected", "disconnected", ...]

Основные TypedDict:
+-- DetectorMetadata
¦   +-- z_score: float
¦   +-- cusum_pos: float
¦   +-- vola_flag: bool
¦   +-- regime: MarketRegimeLiteral
¦   L-- extra: Dict[str, Any]
¦
+-- DetectorSignal
¦   +-- ok: bool
¦   +-- direction: DirectionLiteral
¦   +-- confidence: float
¦   +-- reason: ReasonCode
¦   L-- metadata: DetectorMetadata
¦
+-- TradeSignalIQTS
¦   +-- direction: DirectionLiteral
¦   +-- entry_price: float
¦   +-- position_size: float
¦   +-- stop_loss: float
¦   +-- take_profit: float
¦   +-- confidence: float
¦   +-- regime: MarketRegimeLiteral
¦   L-- metadata: DetectorMetadata
¦
+-- SystemStatus
¦   +-- current_regime: MarketRegimeLiteral
¦   +-- regime_confidence: float
¦   +-- trades_today: int
¦   +-- max_daily_trades: int
¦   +-- total_trades: int
¦   +-- win_rate: float
¦   +-- total_pnl: float
¦   L-- current_parameters: Dict[str, Any]
¦
+-- Candle1m
¦   +-- symbol: str
¦   +-- ts: int
¦   +-- open: Decimal, high: Decimal, low: Decimal, close: Decimal
¦   +-- volume: Decimal
¦   +-- ema3, ema7, ema9, ema15, ema30: Optional[Decimal]
¦   +-- cmo14, adx14, plus_di14, minus_di14, atr14: Optional[Decimal]
¦   L-- cusum, cusum_state, cusum_zscore, ...: Optional[Decimal]
¦
L-- Конфигурации:
    +-- RiskConfig
    +-- QualityDetectorConfig
    +-- MonitoringConfig
    L-- TradingSystemConfig

Протоколы (интерфейсы):
+-- DetectorInterface (Protocol)
¦   +-- get_required_bars() > Dict[Timeframe, int]
¦   +-- async analyze() > DetectorSignal
¦   L-- validate_data() > bool
¦
+-- RiskManagerInterface (Protocol)
¦   +-- calculate_position_size() > float
¦   +-- calculate_dynamic_stops() > tuple[float, float]
¦   +-- update_daily_pnl() > None
¦   L-- should_close_all_positions() > bool
¦
+-- TradingSystemInterface (Protocol)
¦   +-- async analyze_and_trade() > Optional[TradeSignalIQTS]
¦   +-- get_system_status() > SystemStatus
¦   L-- update_performance() > None
¦
+-- StrategyInterface (Protocol)
¦   +-- generate_signal() > Optional[StrategySignal]
¦   L-- get_required_history() > int
¦
+-- PositionManagerInterface (Protocol)
¦   +-- handle_signal() > Optional[OrderReq]
¦   +-- update_on_fill() > None
¦   +-- get_open_positions_snapshot() > Dict[str, PositionSnapshot]
¦   L-- get_stats() > Dict[str, Any]
¦
+-- ExchangeManagerInterface (Protocol)
¦   +-- async place_order() > Dict[str, Any]
¦   +-- async cancel_order() > Dict[str, Any]
¦   +-- disconnect_user_stream() > None
¦   +-- check_stops_on_price_update() > None
¦   L-- get_active_orders() > List[Dict[str, Any]]
¦
+-- MarketAggregatorInterface (Protocol)
¦   +-- async start_async() > None
¦   +-- async wait_for_completion() > None
¦   +-- stop() > None
¦   +-- shutdown() > None
¦   +-- add_event_handler() > None
¦   +-- get_stats() > Dict[str, Any]
¦   +-- get_connection_state() > NetConnState
¦   +-- fetch_recent() > List[Candle1m]
¦   L-- async fetch_candles() > List[Dict[str, Any]]
¦
L-- MainBotInterface (Protocol)
    +-- async bootstrap() > None
    +-- handle_candle_ready() > None
    +-- get_stats() > Dict[str, Any]
    +-- get_component_health() > Dict[str, Any]
    L-- add_event_handler() > None

Базовые классы:
+-- Detector (ABC)
¦   +-- Конструктор: name
¦   +-- Атрибуты:
¦   ¦   +-- name: str
¦   ¦   +-- logger: logging.Logger
¦   ¦   +-- _last_signal: Optional[DetectorSignal]
¦   ¦   L-- _created_at: datetime
¦   +-- Абстрактные методы:
¦   ¦   +-- get_required_bars() > Dict[Timeframe, int]
¦   ¦   L-- async analyze() > DetectorSignal
¦   +-- Конкретные методы:
¦   ¦   +-- validate_data() > bool
¦   ¦   +-- get_status() > Dict[str, Any]
¦   ¦   L-- _set_last_signal() > None
¦   L-- Наследники:
¦       +-- RoleBasedOnlineTrendDetector
¦       +-- MLGlobalTrendDetector
¦       L-- GlobalTrendDetector
¦
+-- Direction (Enum)
¦   +-- BUY = "BUY"
¦   +-- SELL = "SELL"
¦   L-- FLAT = "FLAT"
¦
L-- SignalOut (dataclass)
    +-- signal: int (1, -1, 0)
    +-- strength: float
    +-- reason: ReasonCode
    +-- z: float
    +-- cusum_pos: float
    +-- cusum_neg: float
    L-- vola_flag: bool

Утилиты и функции:
+-- Нормализация сигналов:
¦   +-- normalize_signal() > DetectorSignal
¦   +-- normalize_direction() > DirectionLiteral
¦   L-- normalize_trading_hours() > tuple[int, int]
¦
+-- Валидация данных:
¦   +-- validate_market_data() > bool
¦   +-- validate_system_status() > SystemStatus
¦   L-- safe_nested_getattr() > Any
¦
+-- Работа с причинами:
¦   +-- map_reason() > ReasonCode
¦   +-- get_reason_category() > str
¦   L-- is_successful_reason() > bool
¦
+-- Управление временем:
¦   +-- get_current_timestamp_ms() > int
¦   +-- set_simulated_time() > None
¦   +-- clear_simulated_time() > None
¦   +-- is_simulated_time_enabled() > bool
¦   L-- create_correlation_id() > str
¦
L-- Константы:
    +-- FEATURE_NAME_MAP (маппинг фич для БД)
    +-- REQUIRED_OHLCV_COLUMNS
    +-- DEFAULT_TRADING_HOURS
    L-- INVALID_DATA

Исключения:
+-- BotLifecycleError
L-- ComponentInitializationError

Интеграция в систему:

Все модули системы импортируют типы и интерфейсы из iqts_standards.py:

ImprovedQualityTrendSystem > DetectorSignal, TradeSignalIQTS
iqts_detectors > Detector, ReasonCode
multi_timeframe_confirmator > DetectorInterface
trade_bot.py > TradingSystemInterface, SystemStatus
run_bot.py > все Protocol интерфейсы
market_aggregator.py > MarketAggregatorInterface, Candle1m

Особенности:
+-- Единый источник истины для всех типов данных
+-- Runtime-checkable Protocol для слабой связности
+-- Полная типовая безопасность с mypy/pyright
+-- Поддержка симуляции времени для бэктестов
L-- Совместимость с существующими модулями
=============================================================

## 14. Модуль: exchange_manager.py

Типы данных:
exchange_manager.py — универсальный транспортный слой исполнения ордеров
+-- Роль в системе:
    +-- Единая точка отправки OrderReq → получение OrderUpd
    +-- Поддержка трёх режимов: LIVE / DEMO / BACKTEST без изменения интерфейса
    +-- Эмуляция исполнения в DEMO/BACKTEST (MARKET = мгновенно, STOP = по цене триггера)
    +-- Точное отслеживание активных стоп-ордеров (включая reduce_only)
    +-- Источник истины для статуса ордеров и connection_state
    L-- Интеграция с PositionManager и AdaptiveExitManager через callback
+
+-- Основной класс
+
L-- ExchangeManager
    +-- Конструктор:
          base_url: str (не используется в DEMO/BACKTEST)
          on_order_update: Callable[[OrderUpd], None] ← главный callback в бот
          trade_log: Optional[TradingLogger]
          demo_mode: bool = True
          execution_mode: "LIVE"|"DEMO"|"BACKTEST"
          symbols_meta: Dict (для округления цены/qty)
    +
    +-- Внутреннее состояние:
          _active_orders: Dict[client_order_id, ActiveOrder]          → все активные ордера
          _orders_by_symbol: defaultdict[set]                        → быстрый поиск по символу
          _connection_state: ConnectionState                           → статус соединения
          _stats: Dict                                                → счётчики (orders_sent, active_stops и т.д.)
          _lock: threading.RLock                                      → потокобезопасность
    +
    +-- Публичные методы исполнения
          L-- async place_order(order_req: OrderReq) → str (client_order_id)
                • Валидация полей
                • Округление price/qty по symbols_meta
                • Создание ActiveOrder
                • В DEMO/BACKTEST:
                    – MARKET → мгновенный FILL
                    – LIMIT → NEW (не исполняется)
                    – STOP_MARKET / TAKE_PROFIT_MARKET → ждёт триггера
                • В LIVE → будет HTTP + WS (пока заглушка)
                • Эмит OrderUpd("NEW") → "FILLED"/"PARTIALLY_FILLED"
    +
    +-- Управление стоп-ордерами
          L-- async check_stop_orders(current_prices: Dict[str, float]) → None
                • Вызывается извне (в DEMO/BACKTEST — каждый тик из MainBot)
                • Проверяет все STOP/TAKE_PROFIT по текущей цене
                • При триггере → мгновенный FILL по текущей цене
                • reduce_only ордера уменьшают позицию (логика в PositionManager)
    +
    +-- Обработка обновлений
          L-- handle_order_update(update: OrderUpd) → None
                • Обновляет ActiveOrder (status, filled_qty)
                • Удаляет при FULL FILL / CANCELED
                • Эмитит ExchangeEvent("ORDER_UPDATE_RECEIVED")
                • Вызывает внешний callback on_order_update
    +
    +-- Диагностика и мониторинг
          +-- get_active_orders(symbol?) → List[Dict] (с reduce_only)
          +-- get_connection_state() → Dict (в DEMO всегда "CONNECTED")
          +-- get_stats() → Dict (включая avg_latency, uptime)
          L-- reset_for_backtest() → полная очистка перед новым прогоном
    +
    L-- Система событий
          +-- add_event_handler(handler: ExchangeEventHandler)
          L-- _emit_event(event: ExchangeEvent)
+
+-- Внутренние типы
¦   +-- ActiveOrder (dataclass) — полное состояние ордера
¦   +-- ConnectionState (dataclass) — статус соединения
¦   L-- ExchangeEvent — тип события (ORDER_PLACED, ORDER_UPDATE_RECEIVED и т.д.)

Ключевые особенности:
+-- Поддержка трех режимов: LIVE/DEMO/BACKTEST
+-- Унифицированный интерфейс для всех типов ордеров
+-- Потокобезопасность через threading.RLock
+-- Интеллектуальный STOP мониторинг с синхронной проверкой
+-- Реалистичная эмуляция комиссий и slippage
+-- Event система для отслеживания состояния
L-- Полная интеграция с iqts_standards

Интеграция в систему:

BotLifecycleManager > _create_exchange_manager() > ExchangeManager
    v
EnhancedTradingBot > ExecutionEngineFromExchangeManager > ExchangeManager
    v
PositionManager > ExchangeManager (для исполнения ордеров)

+-- Flow исполнения ордера (ОБНОВЛЕНО):
¦   +-- 1. TradeSignalIQTS > TradeSignal (intent-based)
¦   +-- 2. PositionManager.handle_signal() > OrderReq
¦   +-- 3. ExchangeManager.place_order(OrderReq)
¦   +-- 4. Обработка результата с position_id
¦   L-- 5. Интеграция с TradingLogger

STOP ордера в BACKTEST:
EnhancedTradingBot._manage_existing_positions()
    > check_stops_on_price_update(symbol, current_price)
    > _check_stop_trigger_with_price()
    > _trigger_stop_order() > on_order_update()

Комиссии и slippage:
+-- MARKET ордера: 0.1% slippage + 0.04% taker fee
+-- LIMIT ордера: 0% slippage + 0.02% maker fee  
+-- STOP ордера: 0.01% slippage + 0.04% taker fee
L-- BACKTEST режим: минимальный slippage для точности

Статистика и мониторинг:
+-- orders_sent/filled/rejected/canceled
+-- active_orders_count, active_stops
+-- latency_ms, reconnect_count
+-- connection_state, uptime_seconds
L-- demo_mode, execution_mode
=================================================================

## 15. Модуль: position_manager.py
Типы данных:
position_manager.py — единый владелец состояния позиций, PnL и исполнения ордеров
+-- Роль в системе:
    +-- Source of Truth для всех активных позиций
    +-- Преобразование TradeSignalIQTS → OrderReq (с учётом risk_context)
    +-- Управление стоп-лоссами (включая trailing)
    +-- Ведение статистики и дедупликации сигналов
    +-- Интеграция с ExchangeManager (DI) и AdaptiveExitManager
    L-- Запись в БД через TradingLogger (trade_log)
+
+-- Основной класс
+
L-- PositionManager
    +-- Конструктор:
          symbols_meta: Dict (tick_size, step_size, min_notional и т.д.)
          db_dsn: str
          trade_log: TradingLogger
          price_feed: Optional[PriceFeed]
          execution_mode: "LIVE"|"DEMO"|"BACKTEST"
          db_engine: Optional[Engine]
          signal_validator: Optional[SignalValidator] (DI)
    +
    +-- DI-внедрение:
          L-- set_exchange_manager(em: ExchangeManagerInterface) → обязательно после создания
          L-- exit_manager: Optional[AdaptiveExitManager] → может быть внедрён позже
    +
    +-- Внутреннее состояние:
          _positions: Dict[str, PositionSnapshot]          → активные позиции
          _pending_orders: Dict[str, PendingOrder]         → ожидающие исполнения
          _active_stop_orders: Dict[str, Dict]             → отслеживание стопов (включая trailing)
          _processed_correlations: deque(maxlen=5000)      → защита от дублей
          _position_ids: Dict[str, int]                    → кэш id из БД positions
          _stats: PMStats                                  → счётчики сигналов, ордеров, PnL
    +
    +-- Генерация ID:
          L-- _generate_unique_order_id() → "entry_ETHUSDT_1732023456789_42"
    +
    +-- Обработка входящих сигналов
          L-- handle_signal(signal: TradeSignalIQTS) → List[OrderReq] | None
                • Дедупликация по correlation_id + validation_hash
                • Валидация через SignalValidator (если передан)
                • Проверка на уже открытую позицию (has_active_position)
                • Расчёт размера позиции из risk_context.position_size
                • Округление qty по step_size, price по tick_size
                • Создание entry-ордера (MARKET или LIMIT)
                • Создание initial stop-loss (STOP_MARKET, reduce_only=True)
                • При trailing_enabled → создание/обновление trailing stop
                L-- Все ордера регистрируются в _pending_orders и _active_stop_orders
    +
    +-- Управление стоп-лоссами и trailing stop
          +-- _build_initial_stop_order(signal, entry_price, qty, correlation_id) → OrderReq
          +-- _build_trailing_stop_order(...) → OrderReq
          +-- _update_active_stop_tracking(symbol, stop_info)
          L-- _get_current_stop_price(symbol) → приоритет: ExchangeManager → кэш PM
    +
    +-- Обработка обновлений от биржи
          L-- handle_order_update(update: OrderUpd) → None
                • Обновление статуса pending_orders
                • При FULL FILL → создание PositionSnapshot
                • При стоп-триггере → закрытие позиции, расчёт realized PnL
                • Удаление из _active_stop_orders при отмене/исполнении
    +
    +-- Управление жизненным циклом позиции
          +-- open_position(signal, entry_fill) → PositionSnapshot
          +-- close_position(position_id, exit_price, reason) → TradeResult
          +-- update_position_pnl(symbol, current_price) → обновление unrealized PnL
          L-- get_position(symbol) → PositionSnapshot | None
    +
    +-- Утилиты для расчёта размера
          +-- _calculate_order_quantity(symbol, usdt_amount) → Decimal (с округлением)
          +-- _round_price(price, symbol) / _round_qty(qty, symbol)
          L-- _check_min_notional(symbol, qty, price)
    +
    +-- Система событий
          L-- add_event_handler(handler: EventHandler)
          L-- _emit_event(event: PositionEvent)
                → POSITION_OPENED, POSITION_CLOSED, ORDER_PLACED, STOP_UPDATED и т.д.
    +
    L-- Статистика и отладка
          +-- get_stats() → Dict (сигналы, ордера, PnL, дубли и т.д.)
          +-- get_active_stops() → Dict всех активных стопов
          L-- has_active_position(symbol) → bool
		  
Интеграция в поток данных:
TradeSignal > PositionManager.handle_signal() > OrderReq > ExchangeManager.place_order()
    v
OrderUpd (fill) > PositionManager.update_on_fill() > PositionSnapshot > TradingLogger
    v
STOP trigger > PositionManager.on_stop_triggered() > ExchangeManager.check_stops_on_price_update()

Особенности реализации:
+-- Единый владелец состояния позиций и PnL
+-- Поддержка всех режимов: LIVE / DEMO / BACKTEST
+-- Автоматический расчёт размера позиции на основе риск-контекста
+-- Встроенный трейлинг-стоп с конфигурируемыми параметрами
+-- Квантование цен и объёмов согласно биржевым правилам
+-- Управление cooldown между сигналами
+-- Полная потокобезопасность через threading.RLock
+-- Event-система для оповещения подписчиков
+-- Валидация сигналов и защита от дубликатов
+-- Автоматическое закрытие стоп-ордеров при выходе из позиции
+-- Сохранение всех операций в БД через TradingLogger
L-- Поддержка fee_total_usdt для точного расчёта PnL
=================================================================
## 16. Модуль: exit_system.py
Типы данных:
exit_system.py — адаптивная система управления выходом из позиции (AdaptiveExitManager)
+-- Роль в системе:
    +-- Основной источник решений о закрытии позиции
    +-- Динамическое управление стоп-лоссом (breakeven + trailing)
    +-- Упреждающий выход по каскадному развороту тренда (1m → 5m)
    +-- Полная интеграция с PositionManager (DI)
    L-- Заменяет старый жёсткий ExitManager
+
+-- Основные классы
+
+-- ExitDecision (TypedDict)
    +-- should_exit: bool
    +-- reason: str
    +-- urgency: "high"|"medium"|"low"
    +-- confidence: float [0.0–1.0]
    +-- pnl_pct: float
    +-- type: "reversal"|"weakening"|"trailing"|"breakeven"
    +-- new_stop_loss / new_take_profit: Optional[float]
    +-- trailing_type, stop_distance_pct
    L-- details: Dict (полные сигналы детекторов)
+
+-- ExitSignalDetector
    |   Каскадный детектор разворота (младшие ТФ → старшие)
    |
    +-- Конструктор:
    ¦     global_timeframe="5m"
    ¦     trend_timeframe="1m"
    ¦
    +-- Детекторы:
    ¦     • global_detector → MLGlobalTrendDetector (LightGBM 5m модель)
    ¦     • trend_detector  → RoleBasedOnlineTrendDetector (1m CUSUM + роль)
    ¦
    +-- Пороги (cascading_thresholds):
    ¦     both_levels_sum ≥ 0.80      → HIGH (каскадный разворот)
    ¦     global_hint ≥ 0.50          → намёк от 5m
    ¦     lower_tf_min ≥ 0.55         → сильный сигнал младшего ТФ
    ¦     trend_min ≥ 0.40            → минимальный сигнал 1m
    ¦
    L-- async analyze_exit_signal(data: Dict[TF, pd.DataFrame], position_direction) → Dict
          • 0. Каскадный разворот (приоритет)
          1. Глобальный разворот (HIGH)
          2. Локальный + глобальный намёк
          3. Ослабление тренда (MEDIUM)
          4. Разворот младших ТФ
          5. Общая уверенность (LOW)
+
L-- AdaptiveExitManager (главный класс, внедряется в EnhancedTradingBot)
    +-- Конструктор:
          global_timeframe="5m", trend_timeframe="1m"
          breakeven_activation=0.005   # 0.5%
          trailing_activation=0.015    # 1.5%
          trailing_distance=0.01       # 1.0%
    +
    +-- Состояние позиции:
          position["exit_tracking"] = {
              "peak_price": float,
              "breakeven_moved": bool,
              "trailing_active": bool
          }
    +
    +-- Основной метод:
          L-- update_position_stops(position: Dict, current_price: float, market_data: Dict) → Dict
                • Сначала: ExitSignalDetector.analyze_exit_signal() → should_exit?
                • Если да → возврат ExitDecision(should_exit=True, ...)
                • Если нет → расчёт trailing/breakeven:
                    – update_trailing_state() → new_stop_loss?
                    – calculate_trailing_stop() → адаптивный trailing
                • Возврат: new_stop_loss, changed, reason
    +
    +-- Управление trailing stop
          +-- update_trailing_state() → централизованное обновление peak_price, breakeven, trailing_active
          +-- calculate_trailing_stop() → адаптивный % в зависимости от волатильности (ATR)
          L-- _get_trailing_config_for_symbol() → fallback на дефолты (совместимость с PositionManager)
    +
    L-- Вспомогательные методы:
          • _check_reversal(), _check_weakening()
          • _combine_exit_signals()
          • _get_current_stop_price() → совместимость с PositionManager
		  
=================================================================================
## 17. Модуль: signal_validator.py
структура:

signal_validator.py — централизованная система валидации всех торговых сигналов и ордеров
+-- Назначение:
¦   +-- Единая точка проверки корректности сигналов на всех этапах пайплайна
¦   +-- Защита от некорректных входов, стопов, размеров позиции
¦   +-- Поддержка stops_precomputed + risk_context (новый формат IQTS)
¦   +-- Разделение ошибок и предупреждений (strict_mode)
¦   L-- Интеграция через DI и декоратор @validate_signal
¦
+-- Основные классы
¦
+-- ValidationResult
¦   +-- valid: bool
¦   +-- errors: List[str]
¦   +-- warnings: List[str]
¦   +-- layer: Optional[str] (с версии 2.1)
¦   +-- to_dict() → Dict
¦   +-- __bool__() → valid
¦   L-- merge(other) → ValidationResult (объединение результатов)
¦
L-- SignalValidator
    +-- Конструктор:
    ¦     strict_mode: bool = False      → warnings → errors при strict_mode=True
    ¦     logger: Optional[logging.Logger] = None
    ¦
    +-- Валидация DetectorSignal
    ¦   L-- validate_detector_signal(signal: DetectorSignal) → ValidationResult
    ¦         • обязательные поля: ok, direction, confidence
    ¦         • direction ∈ DirectionLiteral
    ¦         • confidence ∈ [0.0, 1.0]
    ¦         • согласованность ok ↔ confidence
    ¦         L-- проверка metadata (опционально)
    ¦
    +-- Валидация TradeSignalIQTS (основной выход IQTS)
    ¦   L-- validate_trade_signal_iqts(signal: TradeSignalIQTS) → ValidationResult
    ¦         • при stops_precomputed=True → обязателен risk_context
    ¦         • проверка всех полей risk_context (position_size, initial_stop_loss, take_profit)
    ¦         • поддержка Direction enum, int и строк
    ¦         • согласованность SL/TP с направлением (BUY/SELL)
    ¦         • R:R соотношение (warning при <1.5)
    ¦         • размер позиции (warning при <5 USDT)
    ¦         L-- confidence warning при <0.5
    ¦
    +-- Валидация TradeSignal (для PositionManager)
    ¦   L-- validate_trade_signal(signal: StrategySignal) → ValidationResult
    ¦         • обязательные поля: symbol, intent, decision_price
    ¦         • intent ∈ [LONG_OPEN, SHORT_OPEN, LONG_CLOSE, SHORT_CLOSE, WAIT, HOLD, FLAT]
    ¦         L-- проверка correlation_id (warning если отсутствует)
    ¦
    +-- Валидация OrderReq
    ¦   L-- validate_order_req(order_req: OrderReq) → ValidationResult
    ¦         • обязательные поля: client_order_id, symbol, side, type, qty
    ¦         • проверка qty > 0
    ¦         • проверка price/stop_price > 0 (если указаны)
    ¦         L-- warning при отсутствии correlation_id
    ¦
    +-- Комплексная проверка всего потока
    ¦   L-- validate_signal_flow(
    ¦           detector_signal?,
    ¦           trade_signal_iqts?,
    ¦           trade_signal?,
    ¦           order_req?
    ¦       ) → Dict[str, ValidationResult]
    ¦
    +-- Статические утилиты
    ¦   +-- check_price_sanity(price, symbol, min/max) → (bool, error_msg)
    ¦   +-- check_stop_loss_sanity(entry, sl, direction, max_dist_pct=10%) → (bool, error_msg)
    ¦   L-- calculate_risk_reward_ratio(entry, sl, tp) → float
    ¦
    L-- Декоратор @validate_signal
          • Автоматическая валидация возвращаемого значения функции
          • signal_type: 'detector' | 'trade_iqts' | 'trade' | 'order' | 'auto'
          • При ошибке → логирует + возвращает None
          • Работает как с async, так и с sync функциями
¦
+-- Глобальный singleton
¦   +-- _global_validator: Optional[SignalValidator] = None
¦   L-- get_validator(strict_mode=False) → SignalValidator (ленивая инициализация)
¦
L-- Быстрые функции (для inline-проверок)
    +-- quick_validate_detector_signal(signal, verbose=False) → bool | ValidationResult
    +-- quick_validate_trade_signal_iqts(signal) → bool
    +-- quick_validate_trade_signal(signal) → bool
    L-- quick_validate_order_req(order_req) → bool
			
Глобальный доступ:
+-- Глобальный валидатор (singleton):
¦   +-- _global_validator: Optional[SignalValidator]
¦   L-- get_validator(strict_mode: bool = False) > SignalValidator
¦
L-- Функции быстрой проверки:
    +-- quick_validate_detector_signal() > Union[bool, ValidationResult]
    +-- quick_validate_trade_signal_iqts() > bool
    +-- quick_validate_trade_signal() > bool
    L-- quick_validate_order_req() > bool
	
Интеграция в систему:
Точки валидации в потоке данных:
    ThreeLevelHierarchicalConfirmator.analyze()
        > @validate_signal('detector')
        > DetectorSignal > SignalValidator.validate_detector_signal()

    ImprovedQualityTrendSystem.analyze_and_trade()
        > @validate_signal('trade_iqts') 
        > TradeSignalIQTS > SignalValidator.validate_trade_signal_iqts()

    PositionManager.handle_signal()
        > @validate_signal('trade')
        > StrategySignal > SignalValidator.validate_trade_signal()

    ExchangeManager.place_order()
        > @validate_signal('order')
        > OrderReq > SignalValidator.validate_order_req()
=================================================================

## 18 МОДУЛЬ: risk_manager.py

risk_manager.py — централизованный и единственный источник расчёта риск-параметров
+-- Назначение:
    +-- Единая точка входа: calculate_risk_context() → RiskContext
    +-- Полная замена старой логики из improved_algorithm.py
    +-- 100% типобезопасность, трассируемость и валидация
    +-- Критическая защита от ошибок (пустой контекст + hash)
    L-- Используется в ImprovedQualityTrendSystem → TradeSignalIQTS

Типы данных:
risk_manager.py
+-- Direction (IntEnum)
    BUY = 1
    SELL = -1
    FLAT = 0
    +-- opposite() → Direction
    +-- __str__() → "BUY"/"SELL"/"FLAT"
+-- RegimeType = Literal["strong_uptrend", "weak_uptrend", ...]
+-- RiskLimits (dataclass)
    max_portfolio_risk: float = 0.02
    max_daily_loss: float = 0.05
    stop_loss_atr_multiplier: float = 2.0
    take_profit_atr_multiplier: float = 3.0
    min_position_usdt: float = 10.0
    max_position_pct: float = 0.10
+-- RiskContext (TypedDict)
    position_size: float
    initial_stop_loss: float
    take_profit: float
    atr: float
    stop_atr_multiplier: float
    tp_atr_multiplier: float
    volatility_regime: float
    regime: Optional[str]
    computed_at_ms: int
    risk_manager_version: str
    validation_hash: str

Основной класс:
L-- EnhancedRiskManager (реализует RiskManagerInterface)
    +-- Конструктор:
    ¦     limits: RiskLimits
    ¦     account_balance: float = 100_000.0
    ¦     daily_pnl: float = 0.0
    ¦
    +-- Константы:
    ¦     VERSION = "3.0.0"
    ¦
    +-- Внутреннее состояние:
    ¦ +-- daily_pnl: float
    ¦ +-- account_balance: float
    ¦ +-- limits: RiskLimits
    ¦ L-- logger: logging.Logger
    ¦
    +-- Главный метод:
    ¦ L-- calculate_risk_context(
    ¦         signal: DetectorSignal,
    ¦         current_price: float,
    ¦         atr: float,
    ¦         account_balance: Optional[float] = None,
    ¦         regime: Optional[str] = None
    ¦     ) → RiskContext
    ¦
    ¦       Алгоритм:
    ¦       1. Валидация входных данных → _validate_inputs()
    ¦       2. Проверка daily loss limit → should_close_all_positions()
    ¦       3. Определение direction из сигнала
    ¦       4. Адаптивный ATR-множитель по режиму:
    ¦            • strong_* → 1.8× / 2.8×
    ¦            • weak_*   → 2.2× / 3.5×
    ¦            • sideways → 2.5× / 4.0×
    ¦       5. Расчёт стоп-лосса:
    ¦            SL = price ± atr × multiplier
    ¦       6. Расчёт размера позиции:
    ¦            risk_amount = balance × max_portfolio_risk
    ¦            position_size = risk_amount / (atr × stop_multiplier)
    ¦       7. Ограничения:
    ¦            • min_position_usdt
    ¦            • max_position_pct от баланса
    ¦       8. Тейк-профит = entry ± atr × tp_multiplier
    ¦       9. Формирование RiskContext + validation_hash
    ¦      10. Логирование полного контекста
    ¦
    +-- Защита от ошибок:
    ¦ L-- При любой ошибке → _create_empty_context(reason) + warning
    ¦
    +-- Хэширование:
    ¦ L-- compute_risk_hash(ctx: RiskContext) → str (SHA256)
    ¦       • Гарантия неизменности контекста
    ¦       • Используется в TradingLogger и PositionManager
    ¦
    +-- Мониторинг:
    ¦ +-- should_close_all_positions() → bool (daily loss превышен)
    ¦ +-- update_daily_pnl(pnl: float) → None
    ¦ L-- get_risk_status() → Dict (все лимиты, текущее состояние)
    ¦
    +-- Утилиты:
    ¦ +-- direction_to_side(dir: Direction) → "BUY"|"SELL"
    ¦ +-- side_to_direction(side: str) → Direction
    ¦ L-- normalize_direction(value) → Direction
    ¦
    L-- Валидация:
          L-- validate_risk_context(ctx: RiskContext) → bool


============================================================================

# 19. Модуль: trading_logger.py
Назначение:
    - Логирование торговых событий: сигналы, сделки, ошибки
    - CRUD операции с БД: позиции, ордера, сделки, символы
    - Асинхронная буферизация записей для высокой производительности
    - Статистика и мониторинг торговой активности

Типы данных:
    trading_logger.py
    +-- SymbolInfo(Dict[str, Any]) — информация о торговом символе
    +-- TradeRecord(Dict[str, Any]) — запись о завершённой сделке
    +-- PositionRecord(Dict[str, Any]) — запись позиции в БД
    +-- OrderRecord(Dict[str, Any]) — запись ордера в БД
    L-- AlertCallback = Callable[[str, Dict[str, Any]], None]

Основной класс:
L-- TradingLogger
    +-- Конструктор:
    ¦   +-- market_db_path: str = "trading_databases.sqlite"
    ¦   +-- trades_db_path: str = "position_trades.sqlite"
    ¦   +-- on_alert: Optional[AlertCallback]
    ¦   +-- pool_size: int = 4
    ¦   +-- enable_async: bool = True
    ¦   L-- logger_instance: Optional[logging.Logger]
    ¦
    +-- Основные публичные методы:
    ¦   +-- async on_candle_ready(symbol: str, candle: Candle1m, recent: List[Candle1m]) > None
    ¦   +-- async on_market_event(event: Dict[str, Any]) > None
    ¦   +-- on_connection_state_change(state: Dict[str, Any]) > None
    ¦   +-- get_last_candle(symbol: str) > Optional[Dict[str, Any]]
    ¦   +-- record_signal(symbol: str, signal_type: str, **kwargs) > None
    ¦   +-- record_trade(trade_data: Dict[str, Any], **kwargs) > None
    ¦   +-- record_error(error_data: Dict[str, Any], **kwargs) > None
    ¦   +-- async record_signal_async(symbol: str, signal_type: str, **kwargs) > None
    ¦   +-- async record_trade_async(trade_data: Dict[str, Any], **kwargs) > None
    ¦   +-- async record_error_async(error_data: Dict[str, Any], **kwargs) > None
    ¦   +-- async flush() > None
    ¦   +-- log_signal_generated(...) > None (совместимость)
    ¦   +-- log_position_opened(...) > None (совместимость)
    ¦   +-- log_position_closed(...) > None (совместимость)
    ¦   +-- log_order_created(...) > None (совместимость)
    ¦   +-- log(entry_type: str, data: Dict[str, Any], **kwargs) > None
    ¦   +-- async log_async(entry_type: str, data: Dict[str, Any], **kwargs) > None
    ¦   +-- get_symbol_info(symbol: str) > Optional[SymbolInfo]
    ¦   +-- get_all_symbols() > List[SymbolInfo]
    ¦   +-- upsert_symbol(symbol_info: SymbolInfo) > None
    ¦   +-- create_position(position: PositionRecord) > Optional[int]
    ¦   +-- update_position(position_id: int, updates: Dict[str, Any]) > bool
    ¦   +-- get_position_by_id(position_id: int) > Optional[PositionRecord]
    ¦   +-- close_position(position_id: int, exit_price: Decimal, exit_reason: str, ...) > bool
    ¦   +-- get_orders_for_position(position_id: int, status: str = None, limit: int = None) > List[Dict[str, Any]]
    ¦   +-- create_order_from_req(order_req: OrderReq, position_id: Optional[int]) > bool
    ¦   +-- update_order_on_upd(order_upd: OrderUpd) > None
    ¦   +-- update_order(client_order_id: str, updates: Dict[str, Any]) > bool
    ¦   +-- get_order(client_order_id: str) > Optional[OrderRecord]
    ¦   +-- create_trade_record(trade: TradeRecord) > Optional[int]
    ¦   +-- get_trade_history(symbol: Optional[str], limit: int = 100) > List[TradeRecord]
    ¦   +-- get_stats() > Dict[str, Any]
    ¦   +-- get_trading_stats(symbol: Optional[str]) > Dict[str, Any]
    ¦   +-- get_open_positions_db(symbol: Optional[str]) > List[PositionRecord]
    ¦   +-- async start_async() > None
    ¦   +-- async stop_async() > None
    ¦   L-- close() > None
    ¦
    +-- Внутренние вспомогательные методы:
    ¦   +-- _normalize_db_value(v) > приводит значения к SQLite-совместимым типам
    ¦   +-- _normalize_params(data: Dict[str, Any]) > Dict[str, Any]
    ¦   +-- _write_log_entry(entry_type: str, data: Dict[str, Any], dedup_key: Optional[str]) > None
    ¦   +-- _check_duplicate(dedup_key: Optional[str]) > bool
    ¦   +-- _log_sync(entry_type: str, data: Dict[str, Any], **kwargs) > None
    ¦   +-- async _log_async(entry_type: str, data: Dict[str, Any], **kwargs) > None
    ¦   +-- async _ensure_async_started() > None
    ¦   +-- async _start_async_workers() > None
    ¦   +-- async _stop_async_workers() > None
    ¦   +-- async _async_worker(queue_type: str, queue: asyncio.Queue) > None
    ¦   +-- async _enqueue_async(entry_type: str, data: Dict[str, Any]) > None
    ¦   +-- _create_trade_record_from_position(position, ...) > Optional[int]
    ¦   L-- ensure_trading_schema() > None
    ¦
    L-- Интеграция:
        +-- BotLifecycleManager > _create_trade_log() > TradingLogger
        +-- PositionManager > create_position() / update_position() / close_position()
        +-- EnhancedTradingBot > create_order_from_req() / update_order_on_upd()
        +-- MarketAggregator > on_candle_ready() / on_market_event()
        L-- ExchangeManager > on_connection_state_change()

Интеграция в поток данных:
MarketAggregator > on_candle_ready() > _last_candle[symbol]
    v
PositionManager > create_position() / close_position() > TradingLogger
    v
EnhancedTradingBot > create_order_from_req() > TradingLogger
    v
ExchangeManager > update_order_on_upd() > TradingLogger

Особенности реализации:
+-- Единый источник истины для всех торговых данных
+-- Поддержка как синхронного, так и асинхронного режима записи
+-- Автоматическая нормализация Decimal > float для SQLite
+-- Встроенная дедупликация записей по dedup_key
+-- Асинхронные очереди с пулом воркеров для высокой производительности
+-- Совместимые методы log_* для обратной совместимости
+-- Автоматическое создание trade record при закрытии позиции
+-- Полный набор CRUD операций для positions, orders, trades, symbols
+-- Поддержка обратных вызовов on_alert при критических ошибках
L-- Автоматическое создание схемы БД при инициализации
 
=============================================================================

## 20. МОДУЛЬ: ml_labeling_tool_v3.py

ml_labeling_tool_v3.py
L-- LabelingConfig (dataclass)
    +-- Параметры подключения:
    ¦   +-- db_engine: Engine | None
    ¦   +-- symbol: str = "ETHUSDT"
    ¦   L-- timeframe: str = "5m"
    ¦
    +-- Параметры CUSUM:
    ¦   +-- cusum_z_threshold: float
    ¦   +-- cusum_conf_threshold: float
    ¦   +-- hold_bars: int
    ¦   L-- buffer_bars: int
    ¦
    +-- Параметры EXTREMUM:
    ¦   +-- extremum_confirm_bar: int
    ¦   +-- extremum_window: int
    ¦   L-- min_signal_distance: int
    ¦
    +-- Параметры PELT_ONLINE:
    ¦   +-- pelt_window: int
    ¦   +-- pelt_pen: float
    ¦   +-- pelt_min_size: int
    ¦   L-- pelt_confirm_bar: int
    ¦
    +-- Общие параметры:
    ¦   +-- method: str = "CUSUM_EXTREMUM"  (нормализуется в UPPERCASE + валидация)
    ¦   +-- fee_percent: float
    ¦   +-- min_profit_target: float
    ¦   L-- tool: Any = None (обратная ссылка на AdvancedLabelingTool)
    ¦
    L-- __post_init__()
        +-- Инициализация db_engine при необходимости
        L-- Базовая валидация конфигурации

L-- DataLoader
    +-- Конструктор:
    ¦   +-- db_engine: Engine | None
    ¦   +-- symbol: str
    ¦   +-- timeframe: str
    ¦   L-- config: Optional[LabelingConfig]
    ¦
    +-- Основные задачи:
    ¦   +-- _initialize_features() > None
    ¦   ¦   L-- Формирует список feature_names (совместимый с боевым ботом и trainer'ом)
    ¦   +-- connect() / disconnect()
    ¦   +-- load_indicators() > pd.DataFrame
    ¦   ¦   +-- Чтение candles_5m из market_data.sqlite
    ¦   ¦   L-- Загрузка индикаторов (EMA/ADX/BB/VWAP/CUSUM и др.)
    ¦   +-- validate_data_quality(df) > (bool, Dict)
    ¦   ¦   L-- Проверка ts, OHLC, пропусков, консистентности индикаторов
    ¦   +-- load_labeled_data() > pd.DataFrame
    ¦   ¦   L-- Загрузка существующих меток из labeling_results
    ¦   +-- safe_correlation_calculation(df, columns) > pd.DataFrame
    ¦   L-- get_data_stats() > Dict[str, Any]
    ¦       +-- total_candles, period
    ¦       +-- total_labels, buy_labels, sell_labels
    ¦       L-- avg_confidence
    ¦
    L-- Атрибуты:
        +-- db_engine: Engine
        +-- symbol: str
        +-- timeframe: str
        L-- feature_names: List[str]

L-- AdvancedLabelingTool
    +-- Конструктор: __init__(config: LabelingConfig)
    ¦   +-- Нормализация метода разметки:
    ¦   ¦   +-- _VALID_METHODS = {"CUSUM", "EXTREMUM", "PELT_ONLINE", "CUSUM_EXTREMUM"}
    ¦   ¦   +-- config.method > UPPERCASE
    ¦   ¦   L-- Фолбэк на "CUSUM_EXTREMUM" при неизвестном методе
    ¦   +-- Создание DataLoader (ENGINE из MARKET_DB_DSN)
    ¦   +-- self.engine = data_loader.connect()
    ¦   +-- self.feature_names = data_loader.feature_names
    ¦   +-- _ensure_table_exists() > создание/проверка labeling_results
    ¦   +-- _ensure_training_snapshot_tables()
    ¦   ¦   +-- training_dataset
    ¦   ¦   +-- training_dataset_meta
    ¦   ¦   L-- training_feature_importance
    ¦   L-- Установка базового порога PnL (pnl_threshold)
    ¦
    +-- Работа с snapshot'ами:
    ¦   +-- _ensure_training_snapshot_tables() > None
    ¦   ¦   +-- Проверка структуры training_dataset (PRAGMA table_info)
    ¦   ¦   +-- Миграция старых схем (features_json / is_negative / anti_trade_mask)
    ¦   ¦   L-- Создание таблиц и индексов при отсутствии
    ¦   +-- _validate_snapshot_frame(df) > None
    ¦   ¦   +-- Проверка обязательных колонок (ts, datetime, reversal_label, sample_weight)
    ¦   ¦   +-- Проверка диапазона меток (0–3)
    ¦   ¦   +-- Удаление NaN/дубликатов
    ¦   ¦   L-- Подготовка к записи в training_dataset
    ¦   +-- Формирование snapshot'а в training_dataset + запись метаданных в training_dataset_meta
    ¦   L-- export_feature_importance(...) > int
    ¦       +-- Нормализация входа (DataFrame / Series / dict / list[(feature, importance)])
    ¦       +-- Опциональный top_n
    ¦       L-- Запись в training_feature_importance (run_id, model_name, feature, importance, rank)
    ¦
    +-- Генерация сигналов (labeling_results):
    ¦   +-- load_data() > pd.DataFrame
    ¦   ¦   L-- Загрузка свечей 5m + индикаторов для разметки
    ¦   +-- _cusum_reversals(df) > List[Dict]
    ¦   ¦   L-- Развороты по CUSUM с учётом z-score и confidence
    ¦   +-- _extremum_reversals(df) > List[Dict]
    ¦   ¦   L-- Локальные экстремумы high/low в скользящем окне
    ¦   +-- _pelt_offline_reversals(df) > List[Dict]
    ¦   ¦   L-- Change-point detection (ruptures) по всему ряду
    ¦   +-- _cusum_extremum_hybrid(df) > List[Dict]
    ¦   ¦   L-- Гибрид CUSUM + EXTREMUM
    ¦   +-- _get_all_existing_signals() > List[Dict]
    ¦   ¦   L-- Загрузка уже сохранённых сигналов для symbol
    ¦   L-- merge_conflicting_labels() > int
    ¦       +-- Обнаружение конфликтующих меток / слабых сигналов
    ¦       L-- Перезапись на HOLD при невыполнении PnL/качества
    ¦
    +-- PnL-анализ и подтверждение:
    ¦   +-- _calculate_pnl_to_index(df, entry_idx, signal_type, end_idx)
    ¦   +-- _calculate_pnl(df, entry_idx, signal_type)
    ¦   ¦   L-- Учёт комиссии (fee_percent) и min_profit_target
    ¦   +-- _smart_confirmation_system(df, signal_idx, signal_type) > Dict
    ¦   ¦   L-- Поиск лучшего бара подтверждения / отмены сигнала
    ¦   L-- _get_confirmation_bars(signal_type) > int
    ¦
    +-- Аналитика качества разметки:
    ¦   +-- advanced_quality_analysis() > Dict[str, Any]
    ¦   ¦   +-- Подсчёт успеха сигналов и PnL
    ¦   ¦   +-- Поиск лучшего метода разметки
    ¦   ¦   +-- Детекция проблем (class imbalance, слабые сигналы и пр.)
    ¦   ¦   L-- Подробный лог/консольный отчёт
    ¦   +-- detect_label_leakage() > Dict[str, Any]
    ¦   ¦   +-- Поиск утечки меток (корреляции признаков с label)
    ¦   ¦   L-- Диагностика потенциальной data leakage
    ¦   L-- create_cv_splits(...) > Dict
    ¦       L-- Временная кросс-валидация (blocked time series CV)
    ¦
    +-- Сервисные методы:
    ¦   +-- configure_settings() > None
    ¦   +-- show_stats() > None
    ¦   L-- close() > None
    ¦       +-- Закрытие SQLAlchemy engine
    ¦       L-- Освобождение ресурсов
    ¦
    L-- Точка входа (CLI):
        +-- if __name__ == '__main__':
        ¦   +-- Создание LabelingConfig (symbol="ETHUSDT")
        ¦   +-- Инициализация AdvancedLabelingTool
        ¦   L-- Запуск enhanced_main_menu() (интерактивный режим)
        L-- Использование:
            +-- Разметка данных (выбор метода)
            +-- Формирование snapshot'ов
            +-- Анализ качества
            L-- Экспорт важности признаков
=============================================================================

## 21. МОДУЛЬ: ml_train_global_v2.py

МОДУЛЬ: train_ml_global_v2_windowed.py
ml_train_windowed.py  
L-- ModelTrainer (основной класс обучения с окном истории)
    +-- Конструктор: __init__(db_dsn: str, symbol: str, lookback: int = 11)
    ¦   +-- self.lookback = lookback (по умолчанию 11 баров)
    ¦   +-- self.base_feature_names = BASE_FEATURE_NAMES (21 признак)
    ¦   +-- self.feature_names = _generate_windowed_feature_names()  
    ¦   ¦   L-- генерирует [feat_t0, feat_t-1, ..., feat_t-(lookback-1)] > 231 признака при lookback=11
    ¦   L-- logging: "?? Создано 231 признаков (21 ? 11 баров)"
    ¦
    +-- Подготовка данных:
    ¦   +-- prepare_training_data(run_id: str) > (X_df, y_series, w_series)
    ¦   ¦   +-- Загрузка из `training_dataset` (пропуск класса 3)
    ¦   ¦   +-- Векторизованное оконное преобразование (numpy)
    ¦   ¦   +-- Порядок: **[t0, t-1, ..., t-(N-1)]** — самый свежий бар *первый*
    ¦   ¦   +-- Автоматическая фильтрация коротких последовательностей
    ¦   ¦   L-- Возврат pandas DF + Series (метки и веса)
    ¦   L-- _generate_windowed_feature_names() > List[str]
    ¦
    +-- Обучение модели:
    ¦   +-- train_model(run_id: str, use_scaler: bool = False) > dict
    ¦   ¦   +-- Разделение: train (70%) / val (15%) / test (15%) по времени
    ¦   ¦   +-- LightGBM: multiclass, 3 класса (BUY/SELL/HOLD)
    ¦   ¦   +-- Callbacks: thermometer + early stopping
    ¦   ¦   +-- Diagnostics:
    ¦   ¦   ¦   +-- Анализ утечки (train/val/test accuracy gap)
    ¦   ¦   ¦   +-- precision-min sweep по тесту (от 0.45 до 0.90)
    ¦   ¦   ¦   +-- Sensitivity-анализ (±0.05 по tau, ±0.02 по delta)
    ¦   ¦   ¦   +-- tau curves (spd/f1 vs tau)
    ¦   ¦   ¦   +-- PR-curves, max-proba scatter, feature importance
    ¦   ¦   ¦   L-- Агрегированная важность по *базовым* признакам (cmo_14 = сумма cmo_14_t0…t-10)
    ¦   ¦   L-- Сохранение:
    ¦   ¦       +-- .joblib (модель + scaler + metadata + lookback + base_feature_names)
    ¦   ¦       +-- _report.json (полный отчёт)
    ¦   ¦       +-- _cm_val/test.png (матрицы ошибок)
    ¦   ¦       L-- _feat_importance_base_aggregated.csv
    ¦   ¦
    ¦   +-- tune_tau_for_spd_range(...) > (tau, stats)
    ¦   ¦   L-- Оптимизация ? под SPD [8–25] и min precision, с cooldown=2
    ¦   ¦
    ¦   +-- _eval_decision_metrics(...) > dict
    ¦   ¦   L-- Реалистичная оценка: только активные BUY/SELL после cooldown
    ¦   ¦
    ¦   L-- decide(proba, tau, delta=0.08, cooldown_bars=2) > np.ndarray[int]
    ¦       L-- Совместим с `MLGlobalDetector.analyze()` (в боевой системе)
    ¦
    +-- Диагностика после обучения:
    ¦   +-- post_training_diagnostics(...)  
    ¦   ¦   +-- Гистограммы (TP/FP/FN для BUY/SELL)
    ¦   ¦   +-- Scatter max-proba vs true class
    ¦   ¦   +-- Precision–Recall curves (one-vs-rest)
    ¦   ¦   +-- SPD vs ? / Precision/Recall/F1 vs SPD графики
    ¦   ¦   L-- Важность признаков (gain + агрегация по лагам)
    ¦   L-- plot_precision_spd_curve(...)  
    ¦
    L-- save_training_report(...)  
        L-- JSON + визуализации confusion matrix

+-- DataLoader (вспомогательный класс)
¦   +-- Конструктор: db_dsn, symbol
¦   +-- connect() / close()
¦   +-- load_training_dataset(run_id) > pd.DataFrame  
¦   ¦   L-- SELECT из `training_dataset` с сортировкой по ts
¦   L-- load_market_data() > pd.DataFrame  
¦       L-- чтение `candles_5m` (не используется напрямую в обучении)

+-- Утилиты верхнего уровня:
¦   +-- _infer_bars_per_day_from_run_id(run_id) > int  
¦   +-- thermometer_progress_callback(logger, width=30, period=10)  
¦   L-- main()
¦       +-- Проверка market_data.sqlite
¦       +-- Поиск последнего run_id для symbol/timeframe=5m в `training_dataset_meta`
¦       +-- Инициализация ModelTrainer
¦       +-- Вызов train_model(...)
¦       L-- Вывод итоговых метрик (val/test accuracy, precision, recall)

+-- Глобальные константы:
¦   +-- LOOKBACK_WINDOW = 11 (ключевое изменение!)
¦   +-- TIMEFRAME_TO_BARS = {...}
¦   +-- BASE_FEATURE_NAMES (21 признак, как в ml_train_global_v2.py, но без `cusum_1m_recent`)
¦   L-- MARKET_DB_DSN = "sqlite:///data/market_data.sqlite"

L-- Особенности реализации:
    +-- ? Полностью совместим по API с `ml_train_global_v2.py` (кроме интерфейса данных)
    +-- ? Векторизованная подготовка окон (ускорение ~5–10?)
    +-- ? Чёткий порядок лагов: **t0 — самый свежий бар**, дальше — t-1, t-2...
    +-- ? Агрегированная важность: сумма по лагам > базовые признаки (для интерпретируемости)
    +-- ? Диагностика утечки: train/val/test gap + accuracy baseline
    +-- ? Подбор ? на **тестовом наборе** (честная оценка)
    +-- ? Генерация всех отчётов в `models/training_logs/`
    L-- ?? Модельная упаковка включает `lookback` и `base_feature_names` > `MLGlobalDetector` может корректно реконструировать окно при инференсе
	
=============================================================================
## 22.Модуль: backtest_setup.py
backtest_setup.py — умный генератор конфигурации для режима BACKTEST
+-- Назначение:
    +-- Автоматически определяет максимально доступный диапазон исторических данных в БД
    +-- Формирует полностью готовый runtime_cfg для BotLifecycleManager
    +-- Учитывает реальное наличие данных по каждому символу
    +-- Печатает красивый отчёт о доступном периоде и количестве свечей
    +-- Работает без жёстко заданных дат — только по содержимому БД
    L-- Гарантирует, что бэктест запустится только при наличии достаточных данных
+
+-- Ключевые функции
+
+-- get_available_data_range(symbols, timeframe=None) → (start_ts_ms, end_ts_ms)
      • Автоопределение таблицы (1m / 5m) по приоритету или cfg.BACKTEST_TIMEFRAME
      • Запрос MIN(ts) и MAX(ts) по всем указанным символам
      • Фильтрация только тех символов, которые реально есть в таблице
      • Вывод подробного отчёта:
            – Общий диапазон дат
            – Длительность в днях/часах
            – Количество 5-минутных свечей
            – Список символов с индивидуальными диапазонами
      L-- Возвращает None при отсутствии данных
+
+-- build_backtest_config() → dict
      • Создаёт временный TradingLogger для доступа к БД (через cfg.MARKET_DB_DSN)
      • Вызывает cfg.build_runtime_config() с правильным trading_logger
      • Получает актуальный список символов из runtime_cfg
      • Определяет диапазон через get_available_data_range()
      • Формирует финальный конфиг со всеми нужными полями:
            "execution_mode": "BACKTEST"
            "backtest": {
                "start_time_ms": ...,
                "end_time_ms": ...,
                "speed": cfg.BACKTEST_SPEED,
                "auto_shutdown": True,
                "period_description": "Complete historical range (X days)"
            }
      L-- Возвращает конфиг, готовый для передачи в BotLifecycleManager
+
+-- Вспомогательные утилиты
      +-- _get_engine() → SQLAlchemy Engine по MARKET_DB_DSN
      +-- _table_exists(), _columns_exist() → безопасная проверка схемы
      +-- _resolve_tables_config() → поддержка кастомных имён таблиц через cfg.MARKET_CANDLES_TABLES
      L-- _detect_timeframe_and_table() → fallback 1m → 5m, если предпочитаемый ТФ недоступен
	  
==================================================================================================
=== ПОТОК ОБРАБОТКИ 1M СВЕЧИ ===

1. WebSocket Binance > DemoMarketAggregatorPhased._on_kline_1m()
   ¦ Файл: market_aggregator.py
   ¦ Описание: Получение raw kline данных из WebSocket
   v
2. market_aggregator.py > _kline_to_candle1m()
   ¦ Преобразование JSON в структуру Candle1m
   ¦ Поля: symbol, ts, open, high, low, close, volume
   v
3. market_aggregator.py > _on_candle_ready_1m()
   ¦ Проверка финализации свечи (finalized=True)
   ¦ Добавление в buffer: _symbol_buffers_1m
   v
4. market_data_utils.py > upsert_candles_1m()
   ¦ Сохранение OHLCV в БД (без индикаторов)
   ¦ Таблица: candles_1m
   ¦ Async операция через asyncio.create_task()
   v
5. market_data_utils.py > update_1m_cusum()
   ¦ Инкрементальный расчет CUSUM индикаторов:
   ¦ +-- cusum (накопительная сумма)
   ¦ +-- cusum_state (-1, 0, 1)
   ¦ +-- cusum_zscore (нормализованное значение)
   ¦ +-- cusum_conf (уверенность сигнала)
   ¦ +-- cusum_pos, cusum_neg (положительная/отрицательная статистика)
   ¦ L-- cusum_reason (текстовое описание)
   v
6. market_data_utils.py > _update_1m_indicators_for_last_candle()
   ¦ Расчет технических индикаторов для последней свечи:
   ¦ +-- EMA (3, 7, 9, 15, 30)
   ¦ +-- CMO14 (Chande Momentum Oscillator)
   ¦ +-- ADX14 (Average Directional Index)
   ¦ +-- Plus_DI14, Minus_DI14 (Directional Indicators)
   ¦ L-- ATR14 (Average True Range)
   ¦ Итого: 19 индикаторов на 1m таймфрейме
   v
7. market_data_utils.py > upsert_candles_1m() [повторно]
   ¦ Сохранение свечи С ИНДИКАТОРАМИ в БД
   ¦ UPDATE существующей записи
   v
8. run_bot.py > on_candle_ready(symbol, candle_1m)
   ¦ Файл: run_bot.py:453 (внутри _create_trade_log)
   ¦ Колбэк вызывается из market_aggregator
   v
9. run_bot.py > MainBotAdapter.handle_candle_ready()
   ¦ Файл: run_bot.py:1568
   ¦ Обновление буфера DataProviderFromDB
   ¦ L-- update_from_candle_event(symbol, candle)
   ¦     L-- Добавление в _in_memory_buffer['1m']
   v
10. Проверка таймфрейма в on_candle_ready()
    ¦ interval_ms = ts_close - ts + 1
    ¦ Если 59_000 <= interval_ms <= 61_000 > это 1m свеча
    ¦ 
    ¦ ? Для 1m свечи: НЕ запускается ML-анализ
    ¦ ? Только сохранение в БД и обновление buffer
    ¦ 
    ¦ ML-анализ запускается ТОЛЬКО на 5m свечах:
    ¦ if timeframe != '5m':
    ¦     logger.debug("Skipping analysis for 1m candle (waiting for 5m)")
    ¦     return
    v
11. Использование 1m данных при анализе 5m свечи
    ¦
    ¦ Когда приходит 5m свеча > запускается ML-анализ
    ¦ v
    ¦ DataProviderFromDB.get_market_data(symbol, ['1m', '5m'])
    ¦ +-- Читает последние 1000 свечей 1m из БД
    ¦ ¦   L-- SELECT * FROM candles_1m WHERE symbol = ? ORDER BY ts DESC LIMIT 1000
    ¦ ¦
    ¦ +-- Загружает ВСЕ 19 индикаторов на 1m
    ¦ ¦   +-- ema3, ema7, ema9, ema15, ema30
    ¦ ¦   +-- cmo14, adx14, plus_di14, minus_di14, atr14
    ¦ ¦   L-- cusum, cusum_state, cusum_zscore, cusum_conf и др.
    ¦ ¦
    ¦ L-- Эти данные используются в:
    ¦     L-- RoleBasedOnlineTrendDetector (1m CUSUM детектор)
    ¦         L-- Подтверждение тренда на нижнем таймфрейме
    v
12. Агрегация 1m данных для 5m фич (микроструктура)
    ¦
    ¦ market_data_utils.py > _get_cusum_signals_1m()
    ¦ +-- Читает последние 5 свечей 1m (составляющие текущую 5m свечу)
    ¦ +-- Анализирует CUSUM сигналы внутри 5m периода
    ¦ L-- Создает агрегированные фичи для ML-модели:
    ¦     +-- cusum_1m_recent (последний CUSUM state)
    ¦     +-- cusum_1m_quality_score (качество сигнала)
    ¦     +-- cusum_1m_trend_aligned (согласованность тренда)
    ¦     +-- cusum_1m_price_move (движение цены)
    ¦     +-- is_trend_pattern_1m (паттерн тренда)
    ¦     +-- body_to_range_ratio_1m (размер тела свечи)
    ¦     L-- close_position_in_range_1m (позиция закрытия)
    v
    Эти 7 фич добавляются к 5m свече как микроструктурные индикаторы
    L-- Используются ML-моделью для повышения точности прогноза
	


=== ОСОБЕННОСТИ ОБРАБОТКИ 1M ===

Кэширование состояния CUSUM:
+-- MarketDataUtils._cusum_1m_state: Dict[str, dict]
+-- Хранит текущее состояние для каждого символа
L-- Позволяет инкрементальный расчет без полной перезагрузки

Асинхронность:
+-- Сохранение в БД: asyncio.create_task(upsert_candles_1m())
+-- Не блокирует основной поток
L-- Расчет индикаторов происходит синхронно

Буферизация:
+-- market_aggregator: _symbol_buffers_1m (deque для WebSocket данных)
+-- DataProviderFromDB: _in_memory_buffer['1m'] (последние 1000 свечей)
L-- Используется для быстрого доступа без обращения к БД

Частота обновлений:
+-- WebSocket: ~1 раз в секунду (промежуточные обновления)
+-- Финализация свечи: каждые 60 секунд (ts кратно 60000)
L-- Расчет индикаторов: только для финализированных свечей

Связь с 5m анализом:
+-- 1m индикаторы НЕ используются напрямую в ML-модели
+-- Используются для:
¦   +-- RoleBasedOnlineTrendDetector (CUSUM на 1m)
¦   L-- Микроструктурные фичи для 5m свечи
L-- ML-модель работает только на 5m таймфрейме

Объем данных:
+-- 1 день = 1440 свечей 1m
+-- ML анализ использует последние 1000 свечей 1m (~16 часов)
L-- Размер в БД: ~50 KB на 1000 свечей (с индикаторами)

=== ИНДИКАТОРЫ НА 1M ТАЙМФРЕЙМЕ ===

Базовые (9):
+-- ema3, ema7, ema9, ema15, ema30 (5 EMA)
+-- cmo14 (Chande Momentum)
+-- adx14 (Trend Strength)
+-- plus_di14, minus_di14 (Directional Movement)
L-- atr14 (Volatility)

CUSUM детектор (9):
+-- cusum (накопительная сумма)
+-- cusum_state (состояние: -1/0/1)
+-- cusum_zscore (нормализованное значение)
+-- cusum_conf (confidence уровень)
+-- cusum_reason (причина сигнала)
+-- cusum_price_mean (среднее цены)
+-- cusum_price_std (стандартное отклонение)
+-- cusum_pos (положительная статистика)
L-- cusum_neg (отрицательная статистика)

ИТОГО: 19 индикаторов на 1m

ПОТОК ОБРАБОТКИ 5M СВЕЧИ

1. WebSocket Binance > DemoMarketAggregatorPhased._on_kline_5m()
   v
2. market_aggregator.py:XXX > _on_candle_ready_5m()
   v
3. market_data_utils.py:754 > compute_5m_features_incremental()
   ¦   L-- _compute_5m_features_for_last_candle()
   ¦       +-- Расчет 26 индикаторов
   ¦       L-- CUSUM анализ
   v
4. market_data_utils.py:XXX > upsert_candles_5m() [сохранение в БД]
   v
5. run_bot.py:453 > on_candle_ready(symbol, candle_5m)
   v
6. run_bot.py:524 > DataProviderFromDB.get_market_data(['1m', '5m'])
   ¦   +-- _load_from_db() [чтение из БД]
   ¦   +-- _get_buffered_data() [чтение из buffer]
   ¦   L-- _merge_data_sources() [слияние с проверкой индикаторов]
   v
7. ImprovedQualityTrendSystem.generate_signal(market_data)
   v
8. MLGlobalDetector.analyze() [ML-модель получает все индикаторы]
===================================================================

=== ЗАВИСИМОСТИ МЕЖДУ МОДУЛЯМИ ===

----------------------T---------------------------------------¬
¦ Модуль              ¦ Зависит от                            ¦
+---------------------+---------------------------------------+
¦ run_bot.py          ¦ ALL (главный координатор)             ¦
¦ trade_bot.py        ¦ iqts_standards, ImprovedQualityTrend  ¦
¦ market_aggregator   ¦ market_data_utils, iqts_standards     ¦
¦ market_data_utils   ¦ ТОЛЬКО стандартная библиотека + DB    ¦
¦ iqts_detectors      ¦ ml_detector, iqts_standards           ¦
¦ ml_detector         ¦ ТОЛЬКО numpy, pandas, lightgbm        ¦
L---------------------+----------------------------------------
================================================================

5. КОНФИГУРАЦИОННЫЕ КОНСТАНТЫ
=== ВАЖНЫЕ КОНСТАНТЫ ===

Таймфреймы:
+-- ONE_M_MS = 60_000 (1 минута)
L-- FIVE_M_MS = 300_000 (5 минут)

Размеры окон индикаторов:
+-- EMA периоды: [3, 7, 9, 15, 30]
+-- CMO период: 14
+-- ADX период: 14
L-- VWAP период: 96

ML-модель:
+-- Путь: models/ml_global_5m_lgbm.joblib
+-- Min confidence: 0.53
L-- Количество фич: 21

Buffer размеры:
+-- DataProviderFromDB._buffer_size: 1000
L-- market_aggregator buffers: deque(maxlen=...)
	
==================================================================	
 БЫСТРАЯ НАВИГАЦИЯ (номера строк)

run_bot.py:
+-- 453: on_candle_ready() [обработчик событий]
+-- 1044: DataProviderFromDB [класс]
+-- 1109: get_market_data() [чтение данных]
L-- 1147: _merge_data_sources() [КРИТИЧНО для индикаторов]

market_data_utils.py:
+-- 754: compute_5m_features_bulk()
+-- 906: _compute_5m_features_for_last_candle()
L-- 562: upsert_candles_5m()

market_history.py:
+-- 210: warmup_5m_indicators()
L-- 220: compute_5m_features_bulk() [вызов]
=====================================================

### Таблица candles_1m

CREATE TABLE candles_1m (
      symbol      TEXT    NOT NULL,
      ts          INTEGER NOT NULL,
      ts_close    INTEGER,
      open        REAL, high REAL, low REAL, close REAL,
      volume      REAL, count INTEGER, quote REAL,
      finalized   INTEGER DEFAULT 1,
      checksum    TEXT,
      created_ts  INTEGER,
      ema3 REAL,
      ema7 REAL,
      ema9 REAL,
      ema15 REAL,
      ema30 REAL,
      cmo14 REAL,
      adx14 REAL,
      plus_di14 REAL,
      minus_di14 REAL,
      atr14 REAL,
      cusum REAL,
      cusum_state INTEGER,
      cusum_zscore REAL,
      cusum_conf REAL,
      cusum_reason TEXT,
      cusum_price_mean REAL,
      cusum_price_std REAL,
      cusum_pos REAL,
      cusum_neg REAL,
      PRIMARY KEY(symbol, ts)
    )

=======================================================
### Таблица candles_5m


sql
CREATE TABLE candles_5m (
      symbol              TEXT    NOT NULL,
      ts                  INTEGER NOT NULL,
      ts_close            INTEGER,
      open REAL, high REAL, low REAL, close REAL,
      volume REAL, count INTEGER, quote REAL,
      finalized INTEGER DEFAULT 1,
      checksum  TEXT,
      created_ts INTEGER,
	  # для ML LightGBM
      price_change_5 REAL,
      trend_momentum_z REAL,
      cmo_14 REAL,
      macd_histogram REAL,
      trend_acceleration_ema7 REAL,
      regime_volatility REAL,
      bb_width REAL,
      adx_14 REAL,
      plus_di_14 REAL,
      minus_di_14 REAL,
      atr_14_normalized REAL,
      volume_ratio_ema3 REAL,
      candle_relative_body REAL,
      upper_shadow_ratio REAL,
      lower_shadow_ratio REAL,
      price_vs_vwap REAL,
      bb_position REAL,
	  # с нижнего TF 1m для ML LightGBM
      cusum_1m_recent INTEGER,
      cusum_1m_quality_score REAL,
      cusum_1m_trend_aligned INTEGER,
      cusum_1m_price_move REAL,
      is_trend_pattern_1m INTEGER,
      body_to_range_ratio_1m REAL,
      close_position_in_range_1m REAL,
	  # CUSUM fallback не используется ML LightGBM
      cusum REAL,
      cusum_state INTEGER,
      cusum_zscore REAL,
      cusum_conf REAL,
      cusum_reason TEXT,
      cusum_price_mean REAL,
      cusum_price_std REAL,
      cusum_pos REAL,
      cusum_neg REAL,
      PRIMARY KEY(symbol, ts)
    )

===========================================================	
	
### Таблица orders

sql
CREATE TABLE orders (
     client_order_id TEXT PRIMARY KEY,
     position_id INTEGER,
     symbol TEXT NOT NULL,
     type TEXT NOT NULL,
     side TEXT NOT NULL,
     tif TEXT,
     qty DECIMAL(18,8) NOT NULL,
     price DECIMAL(18,8),
     stop_price DECIMAL(18,8),
     reduce_only INTEGER NOT NULL DEFAULT 0,
     status TEXT NOT NULL DEFAULT 'NEW',
     cancel_requested INTEGER NOT NULL DEFAULT 0,
     exchange_order_id TEXT,
     correlation_id TEXT,
     created_ts BIGINT DEFAULT (strftime('%s','now')*1000),
     updated_ts BIGINT DEFAULT (strftime('%s','now')*1000)
                    )

=============================================================			
### Таблица positions
## 6. Module: additional_info.py

sql
CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('LONG','SHORT')),
    status TEXT NOT NULL CHECK (status IN ('OPEN','CLOSING','CLOSED','FLAT')) DEFAULT 'OPEN',
    entry_ts BIGINT NOT NULL,
    entry_price DECIMAL(18,8) NOT NULL,
    qty DECIMAL(18,8) NOT NULL,
    position_usdt DECIMAL(18,8) NOT NULL,
    exit_ts BIGINT,
    exit_price DECIMAL(18,8),
    realized_pnl_usdt DECIMAL(18,8),
    realized_pnl_pct DECIMAL(18,8),
    leverage DECIMAL(18,8),
    fee_total_usdt DECIMAL(18,8),
    reason_entry TEXT,
    reason_exit TEXT,
    correlation_id TEXT,
    created_ts BIGINT DEFAULT (strftime('%s','now')*1000),
    updated_ts BIGINT DEFAULT (strftime('%s','now')*1000)
                    )

====================================================================
### Таблица trades


CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    entry_ts BIGINT NOT NULL,
    exit_ts BIGINT,
    entry_price DECIMAL(18,8) NOT NULL,
    exit_price DECIMAL(18,8),
    side TEXT NOT NULL,
    quantity DECIMAL(18,8) NOT NULL,
    position_size_usdt DECIMAL(18,8) NOT NULL,
    gross_pnl_percent DECIMAL(18,8),
    gross_pnl_usdt DECIMAL(18,8),
    net_pnl_percent DECIMAL(18,8),
    net_pnl_usdt DECIMAL(18,8),
    fee_total DECIMAL(18,8),
    duration_seconds INT,
    reason TEXT,
    exit_reason TEXT,
    bars_in_trade INTEGER
                    )
===================================================================
### positions_risk_audit
CREATE TABLE IF NOT EXISTS positions_risk_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    correlation_id TEXT,
    validation_hash TEXT,
    risk_context_json TEXT,  -- ? Полный RiskContext
    planned_sl DECIMAL(18,8),
    actual_sl DECIMAL(18,8),
    sl_slippage_pct DECIMAL(18,8),  -- ? Процент slippage
    planned_tp DECIMAL(18,8),
    actual_tp DECIMAL(18,8),
    tp_slippage_pct DECIMAL(18,8),
    planned_position_size DECIMAL(18,8),
    actual_position_size DECIMAL(18,8),
    size_slippage_pct DECIMAL(18,8),
    timestamp_ms BIGINT NOT NULL,
    FOREIGN
