
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

run_bot.py — точка входа и оркестратор всей торговой системы
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
¦   ¦   L-- Аудит, PnL, журнал сигналов/ордеров/сделок
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
+-- Состояние (внутренние поля):
¦   +-- _shutdown_event: asyncio.Event (флаг глобальной остановки)
¦   +-- _components: Optional[ComponentsContainer]
¦   +-- _is_running: bool — бот запущен
¦   +-- _stopping: bool — процесс остановки инициирован
¦   +-- _main_loop_task: Optional[asyncio.Task] — health-check + auto-shutdown BACKTEST
¦   +-- _trading_task: Optional[asyncio.Task] — основной поток данных / торговля
¦   +-- _monitoring_task: Optional[asyncio.Task] — периодический мониторинг компонентов
¦   L-- _stop_lock: asyncio.Lock() — защита от повторных вызовов stop()
¦
+-- Публичные методы:
¦   +-- async start() → None
¦   ¦     • эмитит LIFECYCLE_STARTING
¦   ¦     • создаёт все компоненты через _create_components()
¦   ¦     • подписывает on_candle_ready цепочкой:
¦   ¦         1) TradingLogger.on_candle_ready()
¦   ¦         2) MainBotAdapter.on_candle_ready() → EnhancedTradingBot
¦   ¦     • запускает фоновые задачи:
¦   ¦         – _run_main_loop()           (health-check + backtest auto-shutdown)
¦   ¦         – _run_main_bot_monitoring() (агрегированная статистика каждые N секунд)
¦   ¦         – market_aggregator.start()  (основной источник свечей)
¦   ¦     • выполняет warmup истории через MarketHistoryManager (если включен)
¦   ¦     • прогревает стратегию/бота на исторических данных (MAIN_BOT_BOOTSTRAPPED)
¦   ¦     • эмитит цепочку событий:
¦   ¦         L-- LIFECYCLE_STARTING → COMPONENTS_CREATED → HISTORY_LOADED → MAIN_BOT_BOOTSTRAPPED → LIFECYCLE_STARTED
¦   ¦     L-- устанавливает _is_running = True
¦   ¦
¦   +-- async stop() → None
¦   ¦     • защищён _stop_lock (реентерабельность)
¦   ¦     • выставляет _stopping = True и _shutdown_event.set()
¦   ¦     • отменяет фоновые задачи (_trading_task, _main_loop_task, _monitoring_task)
¦   ¦     • ждёт их завершения с таймаутом shutdown_timeout_seconds
¦   ¦     • при таймауте эмитит SHUTDOWN_TIMEOUT_WARNING и принудительно отменяет задачи
¦   ¦     • вызывает *cleanup() для graceful остановки всех компонентов
¦   ¦     • эмитит LIFECYCLE_STOPPED (или LIFECYCLE_STOP_FAILED при ошибке)
¦   ¦     L-- сбрасывает флаги и ссылки на задачи (*is_running, *stopping, *_task)
¦   ¦
¦   +-- async wait_for_shutdown() → None
¦   ¦     • блокирует до установки _shutdown_event
¦   ¦     • используется в main()/run_backtest_mode() как "главный await"
¦   ¦     L-- корректно обрабатывает asyncio.CancelledError
¦   ¦
¦   +-- add_event_handler(handler: BotLifecycleEventHandler) → None
¦   +-- remove_event_handler(handler: BotLifecycleEventHandler) → None
¦   L-- *emit_event(event_type: str, data: Dict) → None
¦         • собирает BotLifecycleEvent и рассылает всем event_handlers
¦         • основные типы событий:
¦             L-- LIFECYCLE** / COMPONENTS** / HISTORY** / MONITORING** / BACKTEST_COMPLETED / CRITICAL_ERROR / WARNING
¦
+-- Создание компонентов (приватные фабрики):
¦   +-- _create_logger() → logging.Logger
¦   ¦     • настраивает основной логгер TradingBot/бота
¦   +-- _create_trade_log(logger) → TradingLogger
¦   ¦     • подключение к БД/файлам для аудита
¦   ¦     • в LIVE/DEMO режимах привязывает market_engine к бирже
¦   +-- _create_async_store() → Any (опционально)
¦   ¦     • асинхронное key-value хранилище (кэш / сервисные данные)
¦   +-- MarketDataUtils (из trade_log.market_engine)
¦   ¦     • враппер над market_engine для чтения свечей 1m/5m
¦   ¦     • используется историческим менеджером и стратегией
¦   +-- _create_history_manager(market_data_utils, logger) → MarketHistoryManager
¦   ¦     • async-движок прогрева истории (live + backtest)
¦   ¦     • следит за «текущим временем» BACKTEST и фильтрует данные до него
¦   +-- _create_strategy(logger) → ImprovedQualityTrendSystem (singleton)
¦   ¦     • создаёт стратегию и связывает её с history_manager / utils
¦   +-- _create_risk_manager() → EnhancedRiskManager (по конфигу)
¦   +-- _create_exit_manager() → AdaptiveExitManager (по конфигу, общий для стратегии/бота/PM)
¦   +-- _create_position_manager() → PositionManagerImpl
¦   ¦     • DI: trade_log, exit_manager, risk-параметры, execution_mode
¦   +-- _create_exchange_manager() → ExchangeManager (Binance / DEMO / BACKTEST)
¦   +-- _create_market_aggregator() → MarketAggregator
¦   ¦     • читает рынок (биржа/БД) и эмитит on_candle_ready
¦   L-- _create_main_bot_adapter() → MainBotAdapter
¦         • оборачивает EnhancedTradingBot
¦         • реализует MainBotInterface
¦         • объединяет trade_bot + PositionManager + ExitManager + RiskManager
¦         • подписывает on_candle_ready: TradingLogger → MainBotAdapter
¦
+-- Основные циклы:
¦   +-- async _run_main_loop() → None
¦   ¦     • бесконечный цикл до _shutdown_event.set()
¦   ¦     • периодически вызывает _check_components_health()
¦   ¦     • в BACKTEST + auto_shutdown следит за market_aggregator.backtest_completed:
¦   ¦         – при завершении: эмитит BACKTEST_COMPLETED и инициирует stop()
¦   ¦     • любой сбой → MAIN_LOOP_ITERATION_ERROR / MAIN_LOOP_ERROR + попытка мягкого stop()
¦   ¦
¦   L-- async _run_main_bot_monitoring() → None
¦         • каждые N секунд (≈60) собирает агрегированную статистику:
¦             – main_bot.get_stats() → MONITORING_STATS
¦             – main_bot.get_component_health() → COMPONENTS_UNHEALTHY при проблемах
¦             – history_manager uptime + buffer_stats → HISTORY_MANAGER_STATUS
¦         • при ошибке мониторинга эмитит MONITORING_ERROR / MONITORING_CRITICAL_ERROR
¦
+-- Сигналы завершения:
¦   L-- _setup_signal_handlers()
¦         • регистрирует обработчики SIGINT / SIGTERM
¦         • при получении сигнала создаёт задачу stop() в event loop
¦
+-- Очистка ресурсов:
¦   L-- async _cleanup() → None
¦         • останавливает market_aggregator (stop/close)
¦         • корректно завершает TradingLogger, ExchangeManager, PositionManager, HistoryManager
¦         • закрывает async_store/подключения к БД (если есть)
¦         L-- логирует все этапы освобождения ресурсов
¦
L-- Свойства:
+-- is_running: bool
L-- components: Optional[ComponentsContainer]

Дополнительные функции в модуле
+-- async def main() → None
¦     • читает CLI аргументы (режим "backtest" через sys.argv)
¦     • валидирует конфиг через cfg.validate_config()
¦     • автоопределяет BACKTEST по cfg.EXECUTION_MODE
¦     • создаёт runtime_cfg = cfg.build_runtime_config(trading_logger=None)
¦     • формирует event_handler для BotLifecycleEvent (логгер + консольные сообщения)
¦     • создаёт BotLifecycleManager(runtime_cfg, event_handlers=[event_handler])
¦     • запускает цикл:
¦           – await bot_manager.start()
¦           – await bot_manager.wait_for_shutdown()
¦           – при KeyboardInterrupt/Exception → лог/печать ошибки
¦     L-- в finally: await bot_manager.stop() (graceful shutdown)
¦
L-- async def run_backtest_mode() → None
• переинициализирует runtime_cfg с учётом BACKTEST (в т.ч. TradingLogger)
• создаёт отдельный backtest_event_handler (расширенный отчёт по символам и PnL)
• создаёт BotLifecycleManager(runtime_cfg, event_handlers=[backtest_event_handler])
• запускает backtest: start() → wait_for_shutdown()
• по завершении генерирует финальный отчёт:
¦     – статистика по каждому символу (total, winrate, net_pnl_pct и т.д.)
¦     – суммарный PnL по портфелю
L-- всегда вызывает stop() в finally, даже при ошибках/KeyboardInterrupt

Состояния жизненного цикла BotLifecycleManager
+-- INIT
¦   • после **init**, компоненты ещё не созданы (_components = None, _is_running = False)
¦
+-- STARTING
¦   • вызывается start()
¦   • эмитится LIFECYCLE_STARTING
¦   • создаются все компоненты, прогревается история, регистрируются подписчики
¦   L-- при успехе → переход в RUNNING
¦
+-- RUNNING
¦   • фоновые задачи: _run_main_loop, _run_main_bot_monitoring, market_aggregator.start
¦   • _is_running = True, _stopping = False
¦   • нормальная торговая работа/бэктест
¦
+-- STOP_REQUESTED
¦   • вызван stop() или сработал auto_shutdown / сигнал ОС
¦   • _stopping = True, _shutdown_event.set()
¦   L-- идёт отмена задач и переход в SHUTTING_DOWN
¦
+-- SHUTTING_DOWN
¦   • отмена фоновых задач + ожидание с таймаутом
¦   • вызов _cleanup()
¦   • эмитится LIFECYCLE_STOPPED или LIFECYCLE_STOP_FAILED
¦   L-- после завершения → STOPPED
¦
L-- STOPPED
• _is_running = False, все задачи обнулены
• _components может быть очищен (по логике _cleanup)
L-- готов к повторному запуску (при необходимости)

Flow входа (запуск бота)
+-- CLI / **main**
¦   +-- if **name** == "**main**":
¦   ¦   +-- os.makedirs("data", exist_ok=True)
¦   ¦   L-- asyncio.run(main())
¦   ¦
¦   L-- main()
¦       +-- проверка аргумента "backtest":
¦       ¦   • если sys.argv[1] == "backtest" → run_backtest_mode()
¦       ¦   • иначе: обычный режим (LIVE/DEMO)
¦       +-- cfg.validate_config() → RuntimeError при ошибках конфигурации
¦       +-- auto-detect BACKTEST по cfg.EXECUTION_MODE
¦       +-- cfg.build_runtime_config(trading_logger=None) → runtime_cfg
¦       +-- создание event_handler для BotLifecycleEvent (лог + консоль)
¦       +-- создание BotLifecycleManager(runtime_cfg, [event_handler])
¦       +-- запуск жизненного цикла:
¦       ¦   • await bot_manager.start()
¦       ¦   • await bot_manager.wait_for_shutdown()
¦       ¦   • обработка KeyboardInterrupt/Exception (лог/печать)
¦       L-- в finally: await bot_manager.stop() → корректная остановка компонентов
¦
L-- Flow входа в BACKTEST (run_backtest_mode)
+-- main() / CLI с аргументом "backtest" или EXECUTION_MODE="BACKTEST"
+-- подготовка runtime_cfg c учётом backtest-настроек
+-- создание backtest_event_handler с расчётом отчёта по символам
+-- создание BotLifecycleManager(runtime_cfg, [backtest_event_handler])
+-- await bot_manager.start() (инициализация + прогрев истории)
+-- await bot_manager.wait_for_shutdown()
L-- по завершении backtest_completed или сигналу ОС → stop() → отчёт → выход

Flow выхода (graceful shutdown / аварийный выход)
+-- Инициаторы выхода:
¦   +-- Пользовательский SIGINT/SIGTERM (Ctrl+C, kill)
¦   +-- BACKTEST_COMPLETED (авто-shutdown в _run_main_loop при backtest_completed=True)
¦   +-- CRITICAL_ERROR / MAIN_LOOP_ERROR / MONITORING_CRITICAL_ERROR
¦   L-- внешние вызовы bot_manager.stop()
¦
+-- Обработчик сигнала ОС (_setup_signal_handlers)
¦   +-- при SIGINT/SIGTERM → создаёт asyncio.create_task(bot_manager.stop())
¦   L-- выставляет _shutdown_event → wait_for_shutdown() завершается
¦
+-- Реализация stop()
¦   +-- защита от повторного вызова через _stop_lock и _stopping
¦   +-- отмена _trading_task / _main_loop_task / _monitoring_task
¦   +-- ожидание задач через asyncio.wait_for(..., timeout=shutdown_timeout)
¦   +-- при таймауте → SHUTDOWN_TIMEOUT_WARNING + повторный cancel + gather(exceptions=True)
¦   +-- вызов _cleanup() → остановка всех компонентов
¦   +-- эмиссия LIFECYCLE_STOPPED (или LIFECYCLE_STOP_FAILED при ошибке)
¦   L-- сброс внутренних флагов и задач
¦
L-- Аварийный путь
+-- Любая неперехваченная ошибка в _run_main_loop → MAIN_LOOP_ERROR + попытка stop()
+-- Любая критическая ошибка в мониторинге → MONITORING_CRITICAL_ERROR + _shutdown_event.set()
+-- В main()/run_backtest_mode: Exception → печать stacktrace + гарантированный stop() в finally

Исключения
+-- BotLifecycleError(Exception)
¦   +-- Базовая ошибка жизненного цикла бота
¦   +-- Бросается при фатальных ошибках start()/stop() (например, LIFECYCLE_STOP_FAILED)
¦   L-- Оборачивает исходное исключение для более чистого внешнего API
¦
L-- ComponentInitializationError(Exception)
+-- Ошибка инициализации одного из компонентов в _create_components()
+-- Примеры:
¦   • TradingLogger.market_engine не инициализирован → нельзя создать MarketDataUtils
¦   • невалидный конфиг risk_limits / exit_system и т.п.
+-- При возникновении:
¦   • эмитится CRITICAL_ERROR с деталями
¦   • start() завершается с исключением → main()/run_backtest_mode логируют и вызывают stop()
L-- гарантирует, что бот не запустится в частично инициализированном состоянии

============================================================================
## 5. Модуль: trade_bot.py

Это центральный компонент торговой системы, обеспечивающий координацию всех подсистем: получение данных,
генерацию сигналов, управление позициями, риск-контроль, мониторинг и исполнение ордеров через
 ExecutionEngine/ExchangeManager/PositionManager.
Основное назначение модуля
- Управление полным торговым циклом.
- Интеграция стратегии ImprovedQualityTrendSystem.
- Координация risk_manager и adaptive exit logic.
- Получение рыночных данных через DataProvider.
- Передача сигналов и ордеров в PositionManager → ExchangeManager.
- Мониторинг системы и отправка уведомлений.
- Ведение внутреннего состояния открытых позиций.
- Обработка событий "новая свеча" в event-driven режиме.

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
¦
¦   +-- Конструктор:
¦   ¦     • config: Dict
¦   ¦     • data_provider: DataProvider
¦   ¦     • execution_engine: ExecutionEngine
¦   ¦     • trading_system: Optional[ImprovedQualityTrendSystem]
¦   ¦     • risk_manager: Optional[EnhancedRiskManager]
¦   ¦     • exit_manager: Optional[AdaptiveExitManager]
¦   ¦     • validator: Optional[SignalValidator]
¦
¦   +-- Инициализация:
¦   ¦   +-- _setup_logging() → logging.Logger
¦   ¦   +-- _setup_monitoring() → None
¦   ¦   +-- _validate_connections() → None
¦   ¦   L-- загрузка статуса trading_system (безопасно)
¦
¦   +-- DI-компоненты:
¦   ¦   +-- trading_system          → ImprovedQualityTrendSystem
¦   ¦   +-- exit_manager            → AdaptiveExitManager (DI или созданный внутри)
¦   ¦   +-- risk_manager            → EnhancedRiskManager
¦   ¦   +-- validator               → SignalValidator(strict_mode=False)
¦   ¦   +-- monitoring_system       → EnhancedMonitoringSystem
¦   ¦   L-- position_tracker        → PositionTracker
¦
¦   +-- Управление данными:
¦   ¦   +-- _get_market_data() → Optional[Dict[str, pd.DataFrame]]
¦   ¦   +-- _parse_timeframe(tf: str) → int
¦   ¦   L-- _basic_validate_market_data(dict) → bool
¦
¦   +-- Основной внешне вызываемый метод:
¦   ¦   L-- async on_candle_ready(symbol, candle: Dict, recent_candles: List[Dict]) → None
¦   ¦       • определяет timeframe (по _timeframe или длительности)
¦   ¦       • фильтрует невалидную цену
¦   ¦       • получает market_data через DataProvider
¦   ¦       • проверяет реальную позицию через PositionManager
¦   ¦       • если позиция есть → _manage_existing_positions()
¦   ¦       • если позиции нет → генерация сигнала только на 5m
¦
¦   +-- Работа с сигналами:
¦   ¦   +-- _process_trade_signal(signal: TradeSignalIQTS) → None
¦   ¦   ¦     • валидация risk_context при stops_precomputed
¦   ¦   ¦     • IQTS → PM-сигнал (_convert_iqts_signal_to_trade_signal)
¦   ¦   ¦     • вызов position_manager.handle_signal()
¦   ¦   ¦     • отправка order_req в ExchangeManager.place_order()
¦   ¦   ¦     • логирование slippage, risk_context
¦   ¦   ¦     • регистрация позиции в active_positions + PositionTracker
¦   ¦   L-- _convert_iqts_signal_to_trade_signal() → Optional[Dict]
¦
¦   +-- Управление существующими позициями:
¦   ¦   +-- _manage_existing_positions(market_data) → None
¦   ¦   ¦     • определение current_price через ExchangeManager.get_current_price()
¦   ¦   ¦     • проверка выполнения стоп-ордеров (DEMO/BACKTEST)
¦   ¦   ¦     • обновление unrealized_pnl
¦   ¦   ¦     • exit_manager.should_exit_position() → решение о выходе
¦   ¦   ¦     • закрытие через execution_engine.close_position()
¦   ¦   ¦     • трейлинг-логика через exit_manager.update_trailing_state()
¦   ¦   ¦     • если SL изменился → pm.build_stop_order() → exchange.place_order()
¦   ¦   +-- _update_position_stop_loss(position_id, new_sl) → None
¦   ¦   L-- _handle_position_closed(position_id, close_price) → None
¦
¦   +-- Поддержка брокера / состояние позиций:
¦   ¦   +-- _update_positions() → None
¦   ¦   ¦     • сверка позиций брокера с active_positions
¦   ¦   ¦     • автоматическое закрытие несоответствующих
¦   ¦   L-- _calculate_trade_result(position, close_price) → TradeResult
¦
¦   +-- Уведомления:
¦   ¦   +-- _send_trade_notification(trade_signal, execution_result) → None
¦   ¦   L-- _send_position_closed_notification(position_id, trade_result) → None
¦
¦   +-- Аварийные процедуры:
¦   ¦   +-- async _emergency_shutdown() → None (закрывает ВСЕ позиции)
¦   ¦   L-- async shutdown() → None:
¦   ¦       • отмена фоновых задач
¦   ¦       • shutdown() трейдинговой системы
¦   ¦       • остановка monitoring_system
¦   ¦       • закрытие data_provider/execution_engine
¦
¦   +-- Публичные методы:
¦   ¦   +-- async start() → None
¦   ¦   L-- get_status() → Dict
¦
¦   +-- Состояние:
¦       • is_running: bool
¦       • loop_count: int
¦       • active_positions: Dict
¦       • last_signal_time, last_trade_time: datetime
¦       • monitoring tasks
¦       • position_tracker
¦       • trading_system, exit_manager, validator
¦
L-- PositionTracker
    +-- Конструктор(max_history=1000)
    ¦   • positions: Dict[position_id → данные позиции]
    ¦   • closed_positions: deque (только метаданные)
    ¦
    +-- Методы управления:
    ¦   +-- add_position(id, data) → None
    ¦   +-- get_position(id) → Optional[Dict]
    ¦   +-- get_all_positions() → Dict
    ¦   +-- has_active_position(symbol) → bool
    ¦   L-- close_position(id, close_price, pnl) → None
    ¦
    +-- Расчёт PnL:
    ¦   +-- update_position_pnl(id, current_price) → None
    ¦   +-- calculate_realized_pnl(id, close_price) → float
    ¦   L-- get_total_unrealized_pnl() → float
    ¦
    L-- История:
        +-- get_closed_positions(limit=100)
        L-- кольцевой буфер closed_positions (maxlen)


Общая архитектура обработки

Поток данных и сигналов
1. MarketAggregator вызывает:  
   → on_candle_ready(symbol, candle, recent_candles)
2. EnhancedTradingBot валидирует свечу, определяет TF.  
3. Загружает market_data из DataProvider для всех TF.  
4. Проверяет наличие открытой позиции (PositionManager → позиция).  

Два режима:
**A) Есть открытая позиция:**  
→ только управление: _manage_existing_positions()

**B) Нет позиции и свеча 5m:**  
→ generate_signal() → _process_trade_signal()

---

4. Обработка входящего IQTS сигнала

Конвертация IQTS → PositionManager-сигнала
Метод: _convert_iqts_signal_to_trade_signal()

Преобразует:
- direction → intent (LONG_OPEN / SHORT_OPEN)  
- добавляет correlation_id  
- прокидывает risk_context (size, SL, TP)  
- decision_price обязателен  

Обработка через PositionManager
Метод: _process_trade_signal()
Flow:
1. Валидация risk_context (если stops_precomputed=True)
2. Конвертация IQTS → PM-сигнал
3. PM.handle_signal() → OrderReq
4. ExchangeManager.place_order(OrderReq)
5. Логирование slippage, сохранение в PositionTracker

---

Управление позициями

Основная логика (_manage_existing_positions)
- Получение current_price через ExchangeManager.get_current_price()  
- Проверка и исполнение стоп-ордеров (Backtest/Demo)  
- Обновление unrealized PnL через PositionTracker  
- ExitManager.should_exit_position():  
  → каскад жёстких условий, TP/SL, trailing exit  
- При необходимости закрытие позиции через ExecutionEngine  
- Trailing SL через ExitManager.update_trailing_state()

Если SL пересчитан:  
→ PositionManager.build_stop_order() → ExchangeManager.place_order()

---

Управление ордерами и позициями через PositionManager

EnhancedTradingBot **не создаёт** ордера напрямую.  
Вся логика открытия/закрытия/стопов делегирована:

IQTS → PM.handle_signal() → OrderReq → ExchangeManager

PositionManager:
- создаёт Entry/Exit/Stop ордера  
- ведёт in-memory и БД состояние позиций  
- отправляет события PositionEvent  
- обеспечивает целостность стопов и trailing

---

Структура внутреннего состояния EnhancedTradingBot

active_positions[position_id] содержит:
- signal (исходный IQTS сигнал)  
- execution_result (результат ордера)  
- opened_at / closed_at  
- exit_tracking (peak, trailing flags)  
- risk_context (копия исходного risk_context)  
- stops_precomputed (bool)

PositionTracker.positions содержит:
- объект позиции с ключевыми полями (entry, dir, size, pnl)

PositionTracker.closed_positions:
- deque фиксированной длины для метаданных закрытых позиций  
- предотвращает утечки памяти

---

Валидация рыночных данных

_local `_basic_validate_market_data` проверяет:
- наличие всех OHLCV колонок  
- отсутствие NaN  
- high ≥ max(open, close)  
- low ≤ min(open, close)  
- значения > 0  
Используется при _get_market_data() и on_candle_ready()

---

Нотификации и мониторинг

EnhancedMonitoringSystem обеспечивает:
- Telegram-уведомления  
- Email-уведомления  
- Отчёты о PnL, трейдах и аномалиях  

Бот вызывает:
- _send_trade_notification  
- _send_position_closed_notification  
- мониторинг через monitoring_system.monitor_enhanced_performance()

---

Безопасное завершение работы

Методы:
- _emergency_shutdown — закрытие всех позиций в форс-мажоре  
- shutdown — корректный выход:
  - отмена фоновых задач  
  - остановка trading_system  
  - закрытие data_provider и execution_engine  
  - финальная статистика  

---

Key особенности

- Полная DI-поддержка trading_system, exit_manager, risk_manager.  
- Новая логика разделения входа и управления:  
  → вход только на 5m, трейлинг/выход — на всех ТФ.  
- Trailing SL делегирован AdaptiveExitManager.  
- PositionTracker не хранит тяжёлые объекты закрытых позиций.  
- Полная поддержка Direction enum и числовых значений.  
- Прямая интеграция с PositionManager для всех ордеров.


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

ExchangeManager v2 — универсальный менеджер исполнения ордеров.
Поддерживает режимы LIVE / DEMO / BACKTEST с единым интерфейсом.
Отвечает за приём OrderReq, валидацию, исполнение/эмуляцию ордеров, мониторинг STOP/TP и генерацию OrderUpd/ExchangeEvent.

Структура:

+-- ExchangeManagerError(Exception)
¦   +-- Базовая ошибка ExchangeManager
¦
+-- InvalidOrderError(ExchangeManagerError)
¦   L-- Ошибка валидации ордера (неверные поля, значения, инварианты)
¦
+-- ConnectionError(ExchangeManagerError)
¦   L-- Ошибка соединения с биржей / user-data stream
¦
+-- ExchangeApiError(ExchangeManagerError)
¦   +-- error_code: Optional[str]
¦   L-- Ошибка уровня биржевого API (коды/сообщения биржи)
¦
+-- ActiveOrder (dataclass)
¦   +-- client_order_id: str
¦   +-- symbol: str
¦   +-- side: Literal["BUY","SELL"]
¦   +-- type: Literal["MARKET","LIMIT","STOP_MARKET","STOP","TAKE_PROFIT","TAKE_PROFIT_MARKET"]
¦   +-- qty: Decimal
¦   +-- price: Optional[Decimal]
¦   +-- stop_price: Optional[Decimal]
¦   +-- filled_qty: Decimal
¦   +-- status: str ("NEW","WORKING","FILLED","REJECTED","CANCELED")
¦   +-- correlation_id: Optional[str]
¦   +-- timestamp_ms: int
¦   +-- reduce_only: bool
¦   +-- exchange_order_id: Optional[str]
¦   L-- trigger_price: Optional[Decimal] (цена триггера для STOP/TP)
¦
+-- ConnectionState (dataclass)
¦   +-- status: Literal["connected","disconnected","connecting","error"]
¦   +-- last_heartbeat: Optional[int]
¦   +-- reconnect_count: int
¦   +-- error_message: Optional[str]
¦   L-- connected_at: Optional[int]
¦
L-- ExchangeManager
    +-- Конструктор:
    ¦   +-- __init__(
    ¦       base_url: str,
    ¦       on_order_update: Callable[[OrderUpd], None],
    ¦       trade_log: Optional[Any] = None,
    ¦       *,
    ¦       demo_mode: bool = True,
    ¦       is_testnet: bool = False,
    ¦       logger_instance: Optional[logging.Logger] = None,
    ¦       metrics: Optional[Any] = None,
    ¦       event_handlers: Optional[List[ExchangeEventHandler]] = None,
    ¦       ws_url: Optional[str] = None,
    ¦       execution_mode: str = "DEMO",
    ¦       timeout_seconds: Optional[int] = None,
    ¦       symbols_meta: Optional[Dict[str, Dict[str, Any]]] = None
    ¦   )
    ¦   • Режимы: execution_mode ∈ {"LIVE","DEMO","BACKTEST"}
    ¦   • demo_mode / is_testnet для эмуляции
    ¦   • on_order_update — обязательный callback для OrderUpd
    ¦   • trade_log — источник последних цен/метаданных (fallback)
    ¦   • symbols_meta — биржевые ограничения (tick_size, min_notional, precision)
    ¦   • execution_mode управляет:
    ¦     - _is_backtest_mode
    ¦     - _use_sync_stop_check (STOP монитор из бота или из фонового треда)
    ¦   • Инициализация:
    ¦     - self._active_orders: Dict[client_order_id → ActiveOrder]
    ¦     - self._orders_by_symbol: Dict[symbol → Set[order_ids]]
    ¦     - self._price_feed: Optional[PriceFeed]
    ¦     - self._stats: счётчики/метрики
    ¦     - self._event_handlers: List[ExchangeEventHandler]
    ¦     - self._connection_state: ConnectionState
    ¦     - DEMO: _demo_latency_ms, _demo_slippage_pct, _demo_stop_slippage_pct
    ¦     - BACKTEST/DEMO: _use_sync_stop_check управляет запуском фонового stop monitor
    ¦
    +-- Получение аккаунта:
    ¦   +-- get_account_info() → Dict
    ¦       • LIVE: заглушка (ожидается реальная реализация через API)
    ¦       • DEMO/BACKTEST: возвращает фиктивный аккаунт (балансы, total_balance_usdt)
    ¦
    +-- Метаданные символов:
    ¦   +-- _get_default_symbols_meta() → Dict[str, Dict[str, Any]]
    ¦       • tick_size, step_size, min_notional, precision для ETHUSDT/BTCUSDT/BNBUSDT
    ¦       • вызывается только если symbols_meta не передан явно
    ¦
    +-- Event system:
    ¦   +-- add_event_handler(handler: ExchangeEventHandler) → None
    ¦   +-- remove_event_handler(handler: ExchangeEventHandler) → None
    ¦   L-- _emit_event(event: ExchangeEvent) → None
    ¦       • вызывает всех подписчиков, логирует ошибки обработчиков
    ¦
    +-- Основной интерфейс размещения ордеров:
    ¦   +-- place_order(order_req: OrderReq) → Dict[str, Any]
    ¦   ¦   • Единая точка входа для OrderReq (MARKET/LIMIT/STOP/TP)
    ¦   ¦   • Шаги:
    ¦   ¦     1) Нормализация типа и stop_price/price (STOP семейство)
    ¦   ¦     2) Валидация _validate_order_req()
    ¦   ¦     3) Если STOP/TP → регистрация через _place_order_demo()
    ¦   ¦     4) Для MARKET/LIMIT:
    ¦   ¦        - выбор цены исполнения (fill_price) по приоритетам:
    ¦   ¦          • price (LIMIT)
    ¦   ¦          • _price_feed (close)
    ¦   ¦          • risk_context.entry_price/decision_price
    ¦   ¦          • fallback из trade_log._last_candle
    ¦   ¦        - если price недоступна → REJECTED (no_price_available)
    ¦   ¦     5) Проверка ценового инварианта SL/TP:
    ¦   ¦        - LONG: SL < Entry < TP
    ¦   ¦        - SHORT: TP < Entry < SL
    ¦   ¦        → при нарушении: REJECTED (price_invariant_violation)
    ¦   ¦     6) Проверка min_notional по symbols_meta
    ¦   ¦     7) Расчёт commission с округлением (0.04%, 6 знаков)
    ¦   ¦     8) Вычисление validation_hash по risk_context (_compute_validation_hash)
    ¦   ¦     9) Генерация correlation_id (если отсутствует)
    ¦   ¦    10) Формирование OrderUpd (status=FILLED) и вызов on_order_update
    ¦   ¦    11) Обновление статистики _stats и возврат ACK-ответа (Dict)
    ¦   ¦
    ¦   +-- _compute_validation_hash(risk_context: Dict[str, Any]) → str
    ¦   ¦   • SHA256 от канонизированного JSON risk_context (без validation_hash)
    ¦   ¦   • используется для проверки консистентности risk_context ↔ исполнение
    ¦   ¦
    ¦   +-- cancel_order(client_order_id: str) → Dict[str, Any]
    ¦   ¦   • Если нет ордера → REJECTED (not found)
    ¦   ¦   • DEMO: _cancel_order_demo()
    ¦   ¦   L-- LIVE: _cancel_order_live() (пока заглушка → DEMO)
    ¦   ¦
    ¦   +-- _cancel_order_demo(client_order_id: str) → Dict[str, Any]
    ¦       • Посылает OrderUpd(status="CANCELED"), удаляет из _active_orders
    ¦       • Обновляет статистику orders_canceled
    ¦
    +-- DEMO/BACKTEST режим:
    ¦   +-- _place_order_demo(req: OrderReq) → Dict[str, Any]
    ¦   ¦   • Универсальное размещение:
    ¦   ¦     - STOP/TP: регистрация ActiveOrder с trigger_price = stop_price
    ¦   ¦     - trailing update:
    ¦   ¦       • по correlation_id с маркерами ("trail","update","trailing")
    ¦   ¦       • вызывает update_stop_order(...) без создания дублей
    ¦   ¦     - MARKET: таймер → _demo_fill_order()
    ¦   ¦     - LIMIT: статус WORKING через _demo_send_working_update()
    ¦   ¦
    ¦   +-- _demo_send_working_update(order: ActiveOrder) → None
    ¦   ¦   • Отправляет OrderUpd(status="WORKING")
    ¦   ¦
    ¦   +-- _calculate_commission(price: Decimal, qty: Decimal, is_maker: bool) → Decimal
    ¦   ¦   • maker: 0.02%, taker: 0.04%
    ¦   ¦   • логирует расчёт и возвращает комиссию в USDT
    ¦   ¦
    ¦   +-- _demo_fill_order(client_order_id: str) → None
    ¦   ¦   • Унифицированное исполнение:
    ¦   ¦     - STOP (trigger_price != None): fill по trigger_price
    ¦   ¦       • BACKTEST: без slippage
    ¦   ¦       • DEMO: небольшой slippage (demo_stop_slippage_pct)
    ¦   ¦     - MARKET: текущая цена +/– slippage (demo_slippage_pct)
    ¦   ¦     - LIMIT: по заявленной price (без slippage)
    ¦   ¦     • вызывает _calculate_commission(), _send_order_update(FILLED)
    ¦   ¦     • удаляет ордер через _remove_active_order()
    ¦   ¦
    ¦   L-- _demo_reject_order(order: ActiveOrder, reason: str) → None
    ¦       • Отправляет OrderUpd(status="REJECTED"), очищает активный ордер
    ¦
    +-- STOP мониторинг:
    ¦   +-- check_stops_on_price_update(symbol: str, current_price: float) → None
    ¦   ¦   • Синхронная проверка для BACKTEST/DEMO/LIVE
    ¦   ¦   • Вызывается извне (ботом) при закрытии свечи
    ¦   ¦   • Логика:
    ¦   ¦     - перебирает активные STOP/STOP_MARKET для symbol
    ¦   ¦     - _check_stop_trigger_with_price(order, current_price)
    ¦   ¦     - при срабатывании:
    ¦   ¦       • безопасно удаляет ордер из _active_orders
    ¦   ¦       • вызывает _trigger_stop_order(order, execution_price=stop_price)
    ¦   ¦
    ¦   +-- _check_stop_trigger_with_price(order: ActiveOrder, current_price: float) → bool
    ¦   ¦   • Использует high/low из текущей свечи (price_feed) или current_price
    ¦   ¦   • Учитывает:
    ¦   ¦     - is_closing_long / is_closing_short (side + reduce_only)
    ¦   ¦     - STOP vs TAKE_PROFIT
    ¦   ¦     - tolerance (0.01%) для float
    ¦   ¦   • Логирует подробности при срабатывании
    ¦   ¦
    ¦   +-- _ensure_stop_monitor_running() → None
    ¦   ¦   • Запускает фоновый монитор _stop_monitor_loop() (если demo_mode и не sync-check)
    ¦   ¦
    ¦   +-- _stop_monitor_loop() → None
    ¦   ¦   • Фоновый цикл:
    ¦   ¦     - перебирает STOP/TP ордера
    ¦   ¦     - _check_stop_trigger(order) → при True:
    ¦   ¦       • _remove_active_order()
    ¦   ¦       • _trigger_stop_order(order, execution_price=stop_price)
    ¦   ¦
    ¦   +-- _check_stop_trigger(order: ActiveOrder) → bool
    ¦   ¦   • STOP/TP по текущему close (через price_feed)
    ¦   ¦   • учитывает reduce_only, BUY/SELL, STOP/TP логику
    ¦   ¦   • логирует каждые N проверок (для мониторинга)
    ¦   ¦
    ¦   L-- _trigger_stop_order(order: ActiveOrder, execution_price: float) → None
    ¦       • Прямое исполнение STOP:
    ¦       • Расчёт fill_price, filled_qty, commission (0.04%)
    ¦       • Формирование OrderUpd(FILLED, reduce_only=True)
    ¦       • Прямой вызов on_order_update()
    ¦       • Удаление ордера и обновление статистики
    ¦
    +-- Управление STOP ордерами:
    ¦   L-- update_stop_order(symbol: str, new_stop_price: Decimal, correlation_id: str) → None
    ¦       • Обновляет существующий STOP/TP для symbol
    ¦       • Обновляет stop_price + correlation_id
    ¦       • Если не найден → InvalidOrderError (нет initial стопа)
    ¦
    +-- LIVE режим (заглушки):
    ¦   +-- _place_order_live(req: OrderReq) → Dict[str, Any]
    ¦   ¦   • Логирует попытку
    ¦   ¦   • Пока переадресует в _place_order_demo()
    ¦   ¦
    ¦   +-- _cancel_order_live(client_order_id: str) → Dict[str, Any]
    ¦   ¦   • Аналогично, пока → _cancel_order_demo()
    ¦   ¦
    ¦   +-- connect_user_stream() → None
    ¦   ¦   • DEMO: помечает статус "connected"
    ¦   ¦   • LIVE: заглушка, выбрасывает ConnectionError
    ¦   ¦
    ¦   L-- disconnect_user_stream() → None
    ¦       • DEMO: останавливает stop monitor, обновляет статус
    ¦       • LIVE: заглушка
    ¦
    +-- Источник цен и валидация:
    ¦   +-- get_current_price(symbol: str) → Optional[float]
    ¦   ¦   • Унифицированный доступ к цене через _price_feed(symbol)
    ¦   ¦   • Поддерживает dict с "close" или float
    ¦   ¦
    ¦   +-- set_price_feed_callback(cb: PriceFeed) → None
    ¦   ¦   • Устанавливает колбэк для DEMO/STOP мониторинга
    ¦   ¦
    ¦   L-- _validate_order_req(req: OrderReq) → None
    ¦       • Проверка обязательных полей (client_order_id, symbol, side, type, qty)
    ¦       • side ∈ {"BUY","SELL"}
    ¦       • type ∈ {"MARKET","LIMIT","STOP_MARKET","STOP","TAKE_PROFIT","TAKE_PROFIT_MARKET"}
    ¦       • LIMIT: требует price > 0
    ¦       • STOP/TP: требуют stop_price > 0
    ¦       • reduce_only, если задан — строго bool
    ¦       • При нарушении → InvalidOrderError
    ¦
    +-- Вспомогательные методы:
    ¦   +-- _send_order_update(update: OrderUpd) → None
    ¦   ¦   • Вызывает on_order_update(update)
    ¦   ¦   • Эмитит ExchangeEvent("ORDER_UPDATE_RECEIVED") через _emit_event
    ¦   ¦
    ¦   +-- _remove_active_order(client_order_id: str) → None
    ¦   ¦   • Удаляет ордер из _active_orders и _orders_by_symbol
    ¦   ¦   • Корректирует счётчики active_stops
    ¦   ¦   • Очищает вспомогательные счётчики (например, _stop_check_counter)
    ¦   ¦
    ¦   +-- get_connection_state() → Dict[str, Any]
    ¦   ¦   • DEMO: всегда "CONNECTED" (для health-check)
    ¦   ¦   • LIVE: asdict(ConnectionState)
    ¦   ¦
    ¦   +-- get_stats() → Dict[str, Any]
    ¦   ¦   • Возвращает:
    ¦   ¦     - orders_sent, orders_filled, orders_rejected, orders_canceled
    ¦   ¦     - active_stops, active_orders_count
    ¦   ¦     - avg_latency_ms, connection_state, demo_mode, uptime_seconds
    ¦   ¦
    ¦   +-- _get_uptime_seconds() → int
    ¦   ¦   • По connected_at в ConnectionState
    ¦   ¦
    ¦   L-- reset_for_backtest() → None
    ¦       • Останавливает stop monitor
    ¦       • Очищает _active_orders / _orders_by_symbol
    ¦       • Сбрасывает _stats и _connection_state
    ¦
    L-- get_active_orders(symbol: Optional[str] = None) → List[Dict[str, Any]]
        • Возвращает список активных ордеров (фильтр по symbol)
        • Включает reduce_only, stop_price, status и др.


---------------------------------------------------------------------
Flow входа (ордер):

1) Trading/PositionManager формирует OrderReq и вызывает:
   → ExchangeManager.place_order(order_req)

2) place_order:
   • нормализует тип ордера и stop_price
   • валидирует OrderReq через _validate_order_req()
   • увеличивает счётчик orders_sent

3) Если ордер STOP/TP:
   • регистрируется через _place_order_demo()
   • создаётся ActiveOrder с trigger_price = stop_price
   • отправляется OrderUpd(status="NEW"/"WORKING")
   • ордер ожидает триггера (price-feed + STOP монитор / sync-check)

4) Если ордер MARKET/LIMIT:
   • определяется fill_price:
     - LIMIT: price
     - MARKET: price_feed(symbol).close, fallback: risk_context / trade_log
   • проверяется инвариант SL/TP для risk_context
   • проверяется min_notional по symbols_meta
   • рассчитывается комиссия (0.04%) и validation_hash
   • формируется OrderUpd(FILLED) и вызывается on_order_update()
   • обновляется статистика orders_filled


---------------------------------------------------------------------
Flow выхода (исполнение / STOP / отмена):

1) Исполнение MARKET/LIMIT:
   • place_order → мгновенный OrderUpd(FILLED)
   • on_order_update() обновляет PositionManager/бота
   • статистика: orders_filled++

2) Исполнение STOP/TP:
   • STOP зарегистрирован в _active_orders как ActiveOrder с trigger_price
   • Варианты триггера:
     a) check_stops_on_price_update(symbol, current_price)
        - вызывается ботом при закрытии свечи
        - _check_stop_trigger_with_price(order, current_price) → True?
        - при True: _remove_active_order() → _trigger_stop_order()
     b) фоновый _stop_monitor_loop() (если включён)
        - использует _check_stop_trigger(order) по price_feed
   • _trigger_stop_order():
     - формирует OrderUpd(FILLED, reduce_only=True)
     - вызывает on_order_update()
     - обновляет статистику orders_filled

3) Trailing обновления:
   • PositionManager/ExitManager формируют новый STOP с тем же symbol
   • _place_order_demo():
     - по correlation_id, содержащему "trail"/"update"/"trailing"
       → update_stop_order(symbol, new_stop_price, correlation_id)
     - без создания дублирующих STOP ордеров

4) Отмена ордера:
   • cancel_order(client_order_id)
     - DEMO: _cancel_order_demo()
       → OrderUpd(CANCELED) + _remove_active_order()
     - LIVE: _cancel_order_live() (пока fallback → DEMO)
     - обновляет orders_canceled


---------------------------------------------------------------------
Состояния и статистика:

1) Основные внутренние структуры:
   • _active_orders: Dict[client_order_id → ActiveOrder]
   • _orders_by_symbol: Dict[symbol → Set[client_order_id]]
   • _price_feed: PriceFeed (callable(symbol) → candle/price)
   • _connection_state: ConnectionState
   • _event_handlers: List[ExchangeEventHandler]
   • _is_backtest_mode: bool
   • _use_sync_stop_check: bool (STOP монитор внутри/снаружи)
   • _stop_monitor_active / _stop_monitor_thread: состояние фонового мониторинга

2) Статистика _stats:
   • orders_sent, orders_filled, orders_rejected, orders_canceled
   • reconnects_count
   • total_latency_ms, latency_samples, avg_latency_ms
   • active_stops
   • last_order_ts

3) Диагностика:
   • get_connection_state() → словарь статуса (DEMO: всегда CONNECTED)
   • get_stats() → агрегированный срез по всем счётчикам
   • get_active_orders(symbol) → текущее состояние ордеров по символу

4) Reset для бэктеста:
   • reset_for_backtest():
     - останавливает stop monitor
     - очищает активные ордера и статистику
     - сбрасывает ConnectionState


---------------------------------------------------------------------
Исключения и обработка ошибок:

1) Классы ошибок:
   • ExchangeManagerError — базовая ошибка модуля
   • InvalidOrderError — любая проблема с OrderReq:
     - отсутствующие/некорректные поля
     - неверный тип ордера/side
     - qty/price/stop_price <= 0
     - отсутствие STOP/price для соответствующих типов
   • ConnectionError — проблемы подключения к user stream / бирже
   • ExchangeApiError — ошибка стороннего API (содержит error_code)

2) Основные места генерации ошибок:
   • _validate_order_req() → InvalidOrderError
   • update_stop_order() → InvalidOrderError, если нет активного STOP для symbol
   • connect_user_stream() в LIVE режиме → ConnectionError (пока не реализовано)
   • place_order():
     - price_invariant_violation (SL/TP инварианты)
     - min_notional_violation
     - execution_error (любая непредвиденная ошибка внутри try)
   • _compute_validation_hash(), _trigger_stop_order(), _demo_fill_order()
     - логируют ошибку и возвращают безопасный REJECTED/cleanup

3) Стратегия обработки:
   • Любые исключения внутри place_order / cancel_order:
     - ловятся, логируются, возвращается Dict с status="REJECTED"
   • Ошибки в on_order_update callback:
     - логируются, но не прерывают внутреннюю очистку состояния
   • Ошибки в STOP мониторинге:
     - логируются, цикл продолжает работу или делает паузу (sleep)

=================================================================

## 15. Модуль: position_manager.py

Единый владелец состояния позиций и PnL. 
Концентрирует всю логику: обработку торговых сигналов, построение ордеров, учёт исполнений, управление стопами и взаимодействие с БД через trade_log.

### Основные обязанности модуля:
+ Преобразование TradeSignalIQTS → OrderReq.
+ Ведение in-memory состояния позиций.
+ Создание, модификация и фиксация Entry / Exit / Stop-ордеров.
+ Синхронизация с ExchangeManager (проставление/отмена стопов).
+ Учёт комиссий, PnL, закрытие сделок и обновление БД.
+ Эмит событий PositionEvent в EventBus.

### Структура PositionManager

+-- PositionManager
¦   +-- handle_signal()  
¦   +-- update_on_fill()  
¦   +-- build_entry_order()  
¦   +-- build_exit_order()  
¦   +-- build_stop_order()  
¦   +-- quantize_price(), quantize_qty()  
¦   +-- get_position(), get_open_positions_snapshot()  
¦   +-- get_stats(), reset_for_backtest()  
¦   +-- set_exchange_manager()  
¦   +-- add_event_handler(), _emit_event()  
¦   +-- _process_entry_fill(), _process_exit_fill()  
¦   +-- _handle_open_signal(), _handle_close_signal(), _handle_wait_signal()  
¦   +-- _save_position_to_db(), _init_position_ids_cache()  
¦   L-- _get_current_stop_price(), _update_active_stop_tracking()

Дополнительные внутренние объекты:
+-- SymbolMeta
+-- PendingOrder
+-- PMStats

### Ключевая логика

1) Обработка сигналов  
   - Проверка корреляции (dedupe).  
   - Валидация через встроенный SignalValidator или базовую схему.  
   - Распределение по intent:
     • LONG_OPEN / SHORT_OPEN → _handle_open_signal  
     • LONG_CLOSE / SHORT_CLOSE → _handle_close_signal  
     • WAIT → trailing stop update через AdaptiveExitManager  
     • HOLD → игнор  
   - Каждый OrderReq регистрируется в trade_log, затем эмитится PositionEvent.

2) Открытие позиций (Entry)  
   - Для Entry обязательно наличие risk_context.position_size.  
   - decision_price берётся из сигнала.  
   - Строится MARKET-ордер.  
   - PendingOrder хранится до FILLED.  
   - После FILLED:
     • создаётся PositionSnapshot  
     • сохраняется в БД (create_position)  
     • создаётся initial STOP (если приходил risk_context.initial_stop_loss)  
     • стоп отправляется через ExchangeManager, фиксируется в _active_stop_orders.

3) Закрытие позиций (Exit)  
   - Полное или частичное закрытие.  
   - PnL считает по Decimal, с учётом fee_total_usdt.  
   - Причина закрытия определяется в порядке приоритетов:
     • is_trailing_stop = True  
     • STOP_MARKET / STOP → stop loss  
     • иначе → SIGNAL_EXIT  
   - При полном закрытии:
     • позиция обновляется в памяти → FLAT  
     • закрытие позиции в БД (close_position)  
     • стопы отменяются (async)  
     • PositionEvent POSITION_CLOSED.

4) Trailing stop (WAIT)  
   - WAIT-сигнал обязан содержать metadata.trailing_update_request.  
   - Через exit_manager.calculate_trailing_stop() вычисляется новый стоп.  
   - Если стоп изменился → build_stop_order(is_trailing=True)  
   - Стоп обновляется на бирже (create/update), кэшируется в _active_stop_orders.

5) Управление стоп-ордерами  
   - Active stop всегда один на символ.  
   - _get_current_stop_price() использует ExchangeManager как источник истины, fallback — локальный кэш.  
   - При любом FILLED стопе PM удаляет трекинг и эмитит STOP_ORDER_FILLED.

6) Работа с pending_orders  
   - Все Entry/Exit/Stop заносятся в _pending_orders.  
   - После FILLED — удаляются.  
   - Метаданные pending_orders используются для определения is_trailing_stop, direction, risk_context.

7) Хранение состояния и БД  
   - В памяти: self._positions, self._active_stop_orders, _symbol_states, _position_ids.  
   - В БД через trade_log:
     • create_order_from_req  
     • update_order_on_upd  
     • create_position  
     • update_position  
     • close_position  

8) Режимы работы (execution_mode)  
   - LIVE / DEMO / BACKTEST  
   - В DEMO/BACKTEST используется виртуальный баланс, расширенная логика сброса состояния (reset_for_backtest).

9) Quantize и биржевые ограничения  
   - quantize_price и quantize_qty учитывают tick_size / step_size символа.  
   - Проверка min_notional через is_min_notional_met.

### Основные данные
В памяти для каждого символа:
- статус позиции  
- side (LONG/SHORT)  
- qty (Decimal)  
- avg_entry_price (Decimal)  
- unrealized/realized pnl  
- fee_total_usdt  
- active_stop (цена, сторона, correlation_id)  
- trailing state (last update, max pnl, count, timestamps)

### Главный поток работы
1. Поступает сигнал → handle_signal  
2. Создаётся и сохраняется ордер  
3. Когда поступает fill → update_on_fill  
4. Обработчик entry/exit формирует позицию или закрывает её  
5. Генерируются события, обновляются стопы  
6. Вся критическая информация фиксируется в БД через trade_log


=================================================================
## 16. Модуль: exit_system.py

Модуль отвечает за принятие решений по выходу из позиции:
- каскадный анализ сигналов (1m → 5m),
- "жёсткие" выходы (SL/TP/время удержания),
- защита прибыли (break-even, trailing stop),
- расчёт адаптивного трейлинг-стопа и обновление стоп-цен.

Структура:

+-- ExitDecision (TypedDict, total=False)
¦   +-- should_exit: bool
¦   +-- reason: str
¦   +-- urgency: str
¦   +-- confidence: float
¦   +-- details: Dict[str, Any]
¦   +-- pnl_pct: float
¦   +-- type: str
¦   +-- new_stop_loss: Optional[float]
¦   +-- new_take_profit: Optional[float]
¦   +-- trailing_type: Optional[str]
¦   L-- stop_distance_pct: Optional[float]
¦
+-- ExitSignalDetector
¦   +-- __init__(global_timeframe: Timeframe = "5m",
¦   ¦           trend_timeframe: Timeframe = "1m")
¦   ¦   • global_timeframe  → "старший" ТФ (5m)
¦   ¦   • trend_timeframe   → "младший" ТФ (1m)
¦   ¦   • global_detector   → MLGlobalTrendDetector (глобальный тренд)
¦   ¦   • trend_detector    → RoleBasedOnlineTrendDetector (локальный тренд)
¦   ¦   • cascading_thresholds:
¦   ¦     - both_levels_sum
¦   ¦     - global_hint
¦   ¦     - lower_tf_min
¦   ¦     - trend_min
¦   ¦   • classic_thresholds:
¦   ¦     - high_global_reversal, high_trend_weak
¦   ¦     - high_global_hint, medium_trend_weak
¦   ¦     - medium_trend_hint, low_total
¦   ¦   • logger
¦   ¦
¦   +-- async analyze_exit_signal(
¦   ¦       data: Dict[Timeframe, pd.DataFrame],
¦   ¦       position_direction: Direction
¦   ¦   ) → Dict
¦   ¦   • validate_market_data(data)
¦   ¦   • global_signal = global_detector.analyze(data)
¦   ¦   • trend_signal  = trend_detector.analyze(data)
¦   ¦   • exit_signals:
¦   ¦     - global_reversal (полный разворот 5m)
¦   ¦     - trend_weakening (ослабление тренда 1m)
¦   ¦     - trend_reversal  (полный разворот 1m)
¦   ¦   • комбинирует через _combine_exit_signals(...)
¦   ¦   • возвращает словарь, совместимый с ExitDecision
¦   ¦
¦   +-- _check_reversal(signal: DetectorSignal,
¦   ¦                  position_direction: Direction) → Dict[str, Any]
¦   ¦   • ok? если нет → detected=False
¦   ¦   • direction (1/-1/0), confidence
¦   ¦   • pos_dir = normalize_direction_v2(position_direction)
¦   ¦   • is_reversal:
¦   ¦     - BUY позиция, а сигнал SELL
¦   ¦     - SELL позиция, а сигнал BUY
¦   ¦   • при развороте:
¦   ¦     - detected=True
¦   ¦     - confidence = signal_confidence
¦   ¦
¦   +-- _check_weakening(signal: DetectorSignal,
¦   ¦                   position_direction: Direction) → Dict
¦   ¦   • Тренд в нашу сторону, но слабый
¦   ¦   • is_same_direction = (position_direction == signal_direction)
¦   ¦   • is_weak = (confidence < 0.65) или not ok
¦   ¦   • is_weakening = is_same_direction and is_weak
¦   ¦   • при ослаблении:
¦   ¦     - detected=True
¦   ¦     - confidence = 1 - signal_confidence
¦   ¦
¦   +-- _check_cascading_reversal(signals: Dict,
¦   ¦                            position_direction: Direction) → Dict
¦   ¦   • КАСКАДНЫЙ РАЗВОРОТ (младший → старший):
¦   ¦     1) trend_rev or trend_weak detected
¦   ¦     2) global_reversal detected
¦   ¦     3) trend_confidence + global_confidence ≥ both_levels_sum
¦   ¦     4) global_hint (глобальный ≥ global_hint)
¦   ¦     5) lower_tf_min (тренд ≥ lower_tf_min)
¦   ¦   • При выполнении:
¦   ¦     - detected=True
¦   ¦     - urgency='high'
¦   ¦     - reason='cascading_reversal'
¦   ¦     - confidence = средняя по двум уровням
¦   ¦     - details: расшифровка каскадного разворота
¦   ¦
¦   L-- _combine_exit_signals(signals: Dict,
¦       ¦                    position_direction: Direction) → Dict
¦       ¦   • Приоритеты:
¦       ¦     0) _check_cascading_reversal() → HIGH
¦       ¦     1) global_reversal (5m, high_global_reversal) → HIGH
¦       ¦     2) trend_weakening + global_hint → HIGH/MEDIUM
¦       ¦     3) low_total по взвешенной уверенности → LOW
¦       ¦   • Возвращает словарь:
¦       ¦     - should_exit, reason, urgency, confidence, details
¦       L     - используется как ExitDecision (без новых стоп-полей)
¦
L-- AdaptiveExitManager
    +-- __init__(global_timeframe: Timeframe = "5m",
    ¦           trend_timeframe: Timeframe = "1m")
    ¦   • exit_detector = ExitSignalDetector(...)
    ¦   • trailing_stop_activation = 0.015 (1.5% прибыли)
    ¦   • trailing_stop_distance  = 0.01  (1% от пика)
    ¦   • breakeven_activation    = 0.008 (0.8% прибыли)
    ¦   • max_hold_time_base      = 2 часа
    ¦   • logger
    ¦
    +-- _calculate_pnl_pct(entry_price: float,
    ¦                     current_price: float,
    ¦                     direction: Direction) → float
    ¦   • Для BUY:  (current - entry) / entry
    ¦   • Для SELL: (entry - current) / entry
    ¦
    +-- async should_exit_position(
    ¦       position: Dict,
    ¦       market_data: Dict[Timeframe, pd.DataFrame],
    ¦       current_price: float
    ¦   ) → Tuple[bool, str, ExitDecision]
    ¦   • Строгая валидация:
    ¦     - position: dict
    ¦     - position['signal']: dict
    ¦     - обязательные поля в signal:
    ¦       ['direction','entry_price','stop_loss','take_profit']
    ¦   • Извлекает:
    ¦     - opened_at, direction, entry_price, stop_loss, take_profit
    ¦   • pnl_pct = _calculate_pnl_pct(...)
    ¦   • LAYER 1: _check_hard_exits(...) → hard_exit
    ¦     - stop_loss_hit / take_profit_hit
    ¦     - max_hold_time (адаптивный)
    ¦   • LAYER 2: exit_detector.analyze_exit_signal(...) → signal_exit
    ¦     - urgency ∈ {high, medium, low}
    ¦     - high  → всегда выходим (profit/loss)
    ¦     - medium → выходим только при прибыли (pnl_pct > 0)
    ¦     - low → игнорируем
    ¦   • LAYER 3: _check_profit_protection(...) → profit_exit
    ¦     - break-even / trailing_stop
    ¦   • Возвращает:
    ¦     - (True, reason, ExitDecision)  при любом сработавшем условии
    ¦     - (False, "no_exit_condition", ExitDecision) иначе
    ¦
    +-- _check_hard_exits(
    ¦       direction: Direction,
    ¦       current_price: float,
    ¦       stop_loss: float,
    ¦       take_profit: float,
    ¦       opened_at: datetime,
    ¦       pnl_pct: float
    ¦   ) → Dict
    ¦   • Жёсткие условия:
    ¦     1) Стоп-лосс:
    ¦        - BUY:  current_price <= stop_loss
    ¦        - SELL: current_price >= stop_loss
    ¦     2) Тейк-профит:
    ¦        - BUY:  current_price >= take_profit
    ¦        - SELL: current_price <= take_profit
    ¦     3) max_hold_time (адаптивно от pnl_pct):
    ¦        - base           = 2h
    ¦        - pnl > +2%      → 1.5 * base
    ¦        - pnl < -0.5%    → 0.7 * base
    ¦        - hold_time > max_hold_time → should_exit=True
    ¦   • Возвращает словарь ('should_exit','reason','type'='hard', ...)
    ¦
    +-- _check_profit_protection(
    ¦       direction: Direction,
    ¦       current_price: float,
    ¦       entry_price: float,
    ¦       pnl_pct: float,
    ¦       position: Dict
    ¦   ) → Dict
    ¦   • Работает только при pnl_pct > 0
    ¦   • Инициализирует position['exit_tracking'], если отсутствует:
    ¦     - peak_price
    ¦     - breakeven_moved
    ¦     - trailing_active
    ¦   • Обновляет peak_price по направлению:
    ¦     - BUY  → max(peak, current)
    ¦     - SELL → min(peak, current)
    ¦   • Break-even:
    ¦     - при pnl_pct ≥ breakeven_activation:
    ¦       • breakeven_moved = True
    ¦       • стоп переносится ближе к entry (через update_position_stops)
    ¦   • Trailing stop:
    ¦     - при pnl_pct ≥ trailing_stop_activation:
    ¦       • trailing_active = True
    ¦       • trailing_stop рассчитывается от peak_price
    ¦       • при пробое trailing_stop → should_exit=True (trailing_stop_hit)
    ¦   • Возвращает словарь ('should_exit','reason','type'='protection', ...)
    ¦
    +-- update_position_stops(
    ¦       position: Dict,
    ¦       current_price: float
    ¦   ) → Dict
    ¦   • Использует:
    ¦     - signal.direction, entry_price, original_stop_loss
    ¦     - position['exit_tracking']
    ¦   • pnl_pct = _calculate_pnl_pct(...)
    ¦   • new_stop_loss = original_stop_loss
    ¦   • Break-even:
    ¦     - при pnl_pct ≥ breakeven_activation и breakeven_moved=True:
    ¦       • BUY:  new_stop_loss = entry * 1.002
    ¦       • SELL: new_stop_loss = entry * 0.998
    ¦   • Trailing:
    ¦     - при trailing_active=True:
    ¦       • BUY:  new_stop_loss = max(new_stop_loss, peak*(1 - trailing_dist))
    ¦       • SELL: new_stop_loss = min(new_stop_loss, peak*(1 + trailing_dist))
    ¦   • Возвращает:
    ¦     - {'stop_loss': new_stop_loss,
    ¦        'updated': new_stop_loss != original_stop_loss,
    ¦        'reason': 'trailing' или 'breakeven'}
    ¦
    +-- calculate_trailing_stop(
    ¦       current_price: float,
    ¦       entry_price: float,
    ¦       side: str,              # "LONG" / "SHORT"
    ¦       max_pnl_percent: float,
    ¦       current_stop_price: Optional[float] = None,
    ¦       symbol: str = "UNKNOWN"
    ¦   ) → Dict[str, Any]
    ¦   • Жёсткая валидация входных параметров:
    ¦     - current_price > 0, entry_price > 0
    ¦     - side ∈ {"LONG","SHORT"}
    ¦   • trailing_pct       = trailing_stop_distance * 100
    ¦   • min_distance_pct   = 0.1
    ¦   • stop_pnl_threshold = max(0, max_pnl_percent - trailing_pct)
    ¦   • Для side="LONG":
    ¦     - new_stop = entry * (1 + stop_pnl_threshold/100)
    ¦     - new_stop > current_stop_price (если задан)
    ¦     - new_stop < current_price * (1 - min_distance_pct/100)
    ¦   • Для side="SHORT":
    ¦     - new_stop = entry * (1 + stop_pnl_threshold/100)
    ¦     - new_stop < current_stop_price (если задан)
    ¦     - new_stop > current_price * (1 + min_distance_pct/100)
    ¦   • Возвращает при успехе:
    ¦     - new_stop_price, beneficial=True, reason, stop_distance_pct,
    ¦       trailing_pct, distance_from_entry_pct, entry_price, current_price,
    ¦       new_stop_loss, new_take_profit=None, trailing_type="adaptive_trailing"
    ¦   • При ошибках:
    ¦     - validation_error / calculation_error, beneficial=False
    ¦
    +-- update_trailing_state(
    ¦       position: Dict,
    ¦       current_price: float
    ¦   ) → Dict[str, Any]
    ¦   • Централизованное управление:
    ¦     - peak_price
    ¦     - breakeven_moved
    ¦     - trailing_active
    ¦   • Инициализирует exit_tracking при отсутствии
    ¦   • Обновляет peak_price по направлению
    ¦   • Считает pnl_pct
    ¦   • При breakeven/trailing условиях:
    ¦     - изменяет new_stop_loss
    ¦     - выставляет reason = 'breakeven_adjust' или 'trailing_adjust'
    ¦   • Возвращает:
    ¦     - {"new_stop_loss", "changed", "reason", "tracking", "pnl_pct"}
    ¦
    L-- _get_trailing_config_for_symbol(symbol: str) → Dict[str, Any]
        • Пытается загрузить get_trailing_stop_config(symbol) из config.py
        • При ошибке:
        •   - логирует предупреждение
        •   - возвращает дефолты:
        •     enabled, trailing_percent, min_profit_percent,
        •     activation_delay_candles, max_updates_per_position,
        •     price_change_threshold_percent, min_stop_distance_pct


---------------------------------------------------------------------
Flow входа (анализ выхода / обновление стопов):

1) EnhancedTradingBot / PositionManager передаёт:
   • позицию (position: Dict с полями signal, opened_at, exit_tracking и др.)
   • market_data: Dict[Timeframe, DataFrame] для 1m/5m
   • current_price: float (текущая рыночная цена)

2) Для принятия решения о закрытии:
   → AdaptiveExitManager.should_exit_position(position, market_data, current_price)
   • LAYER 1: _check_hard_exits(...)
     - мгновенный выход по SL/TP/времени удержания
   • LAYER 2: exit_detector.analyze_exit_signal(...)
     - каскадная логика (1m/5m), HIGH/MEDIUM/LOW urgency
   • LAYER 3: _check_profit_protection(...)
     - защита прибыли: break-even и trailing stop

3) Для обновления стоп-цен без закрытия:
   → AdaptiveExitManager.update_trailing_state(position, current_price)
   → AdaptiveExitManager.update_position_stops(position, current_price)
   • возвращают new_stop_loss / updated флаги
   • далее PositionManager/ExchangeManager обновляют STOP-ордер


---------------------------------------------------------------------
Flow выхода (решение об exit):

1) should_exit_position(..) возвращает:
   • should_exit: bool
   • exit_reason: str
   • decision: ExitDecision (включая pnl_pct и детали)

2) При should_exit=True:
   • Если reason ∈ {'stop_loss_hit','take_profit_hit','max_hold_time'}:
     - тип: 'hard'
     - приоритет: максимальный
   • Если reason начинается с 'signal_exit_...':
     - тип: 'signal'
     - HIGH → немедленный выход (даже с убытком)
     - MEDIUM → выход только при положительном PnL
   • Если reason ∈ {'trailing_stop_hit'}:
     - тип: 'protection'
     - выход ради фиксации прибыли

3) Торговая система использует решение:
   • закрывает позицию через PositionManager/ExchangeManager
   • обновляет статистику и логирование (PnL, причина выхода)


---------------------------------------------------------------------
Состояния:

1) ExitSignalDetector:
   • global_timeframe / trend_timeframe
   • global_detector (MLGlobalTrendDetector)
   • trend_detector (RoleBasedOnlineTrendDetector)
   • cascading_thresholds / classic_thresholds
   • logger

2) AdaptiveExitManager:
   • exit_detector
   • trailing_stop_activation / trailing_stop_distance
   • breakeven_activation
   • max_hold_time_base
   • logger

3) Внутри position (используется модулем):
   • signal:
     - direction, entry_price, stop_loss, take_profit
   • opened_at: datetime
   • exit_tracking:
     - peak_price
     - breakeven_moved (bool)
     - trailing_active (bool)


---------------------------------------------------------------------
Исключения и обработка ошибок:

1) Явные классы исключений не определены в модуле, но:

   • calculate_trailing_stop(...)
     - при некорректных входных:
       - current_price ≤ 0, entry_price ≤ 0
       - side not in {"LONG","SHORT"}
       → ValueError внутри try/except
       → возвращается dict:
         - beneficial=False
         - reason='validation_error: ...'
         - error=строка ошибки

     - при любых неожиданных ошибках:
       → логирует с exc_info
       → возвращает dict:
         - beneficial=False
         - reason='calculation_error: ...'

2) should_exit_position(...):
   • при некорректной position/signal:
     - логирует error
     - возвращает:
       - should_exit=False
       - reason='invalid_position' / 'invalid_signal' / 'missing_signal_fields'
       - ExitDecision с соответствующим reason и details

3) validate_market_data(..) в analyze_exit_signal():
   • при невалидных входных данных:
     - возвращает заглушку:
       - should_exit=False
       - reason='invalid_data'
       - confidence=0.0

4) _get_trailing_config_for_symbol(symbol):
   • при ошибке импорта или вызова config.get_trailing_stop_config:
     - логирует warning
     - использует безопасные дефолтные значения

		  
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
