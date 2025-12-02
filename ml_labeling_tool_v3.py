"""
ml_labeling_tool_v3.py
 версия инструмента разметки
"""
import numpy as np
from sqlalchemy import text
import pandas as pd
import sys
import hashlib, json
from dataclasses import dataclass
from config import BASE_FEATURE_NAMES
from iqts_standards import Direction
from typing import Tuple, List, Dict,  Any, Optional
from datetime import datetime, UTC
import warnings
import logging
import traceback

# msvcrt is Windows-only, make import conditional
try:
    import msvcrt
except ImportError:
    msvcrt = None  # Not available on Linux/macOS

# === Безопасный импорт ruptures ===
try:
    import ruptures as rpt
    RUPTURES_AVAILABLE = True
except ImportError:
    rpt = None
    RUPTURES_AVAILABLE = False
    logging.warning("⚠️ ruptures не установлен — функции BinSeg и PELT отключены")

# === Безопасный импорт scipy ===
try:
    from scipy.stats import linregress as scipy_linregress
    SCIPY_AVAILABLE = True
except ImportError:
    scipy_linregress = None
    SCIPY_AVAILABLE = False
    logging.warning("⚠️ scipy не установлен — линейная регрессия использует numpy fallback")

FEATURE_SQL_TYPE_OVERRIDES: dict[str, str] = {
    "is_trend_pattern_1m": "INTEGER",
    "cusum_price_conflict": "INTEGER",
    "cusum_state_conflict": "INTEGER",
    # если решишь добавить в BASE_FEATURE_NAMES:
    "cusum_1m_recent": "INTEGER",
    "cusum_1m_trend_aligned": "INTEGER",
}

# --- DDL для таблиц снапшота тренировочного датасета ---
def _build_training_dataset_sql() -> str:
    """
    Генерирует DDL для training_dataset на основе BASE_FEATURE_NAMES.
    Все фичи по умолчанию REAL, исключения в FEATURE_SQL_TYPE_OVERRIDES.
    """
    feature_lines: list[str] = []
    for name in BASE_FEATURE_NAMES:
        sql_type = FEATURE_SQL_TYPE_OVERRIDES.get(name, "REAL")
        feature_lines.append(f"    {name: <28} {sql_type},")

    feature_block = "\n".join(feature_lines)

    return f"""
CREATE TABLE IF NOT EXISTS training_dataset (
    run_id           TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    timeframe        TEXT    NOT NULL,
    ts               INTEGER NOT NULL,
    datetime         TEXT    NOT NULL,
    reversal_label   INTEGER NOT NULL,
    sample_weight    REAL    NOT NULL,
{feature_block}
    created_at       TEXT    NOT NULL,
    PRIMARY KEY (run_id, symbol, ts)
);
"""

CREATE_TRAINING_DATASET_SQL = _build_training_dataset_sql()
CREATE_TRAINING_DATASET_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_training_dataset_run_id      ON training_dataset(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_training_dataset_symbol      ON training_dataset(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_training_dataset_run_sym     ON training_dataset(run_id, symbol)",
    "CREATE INDEX IF NOT EXISTS idx_training_dataset_sym_ts      ON training_dataset(symbol, ts)",
    "CREATE INDEX IF NOT EXISTS idx_training_dataset_run_ts      ON training_dataset(run_id, ts)"
]

CREATE_TRAINING_DATASET_META_SQL = """
CREATE TABLE IF NOT EXISTS training_dataset_meta (
    run_id             TEXT PRIMARY KEY,
    status             TEXT NOT NULL,               -- CREATING|READY|FAILED
    error_msg          TEXT,
    symbol             TEXT NOT NULL,
    timeframe          TEXT NOT NULL,
    range_start_ts     INTEGER,
    range_end_ts       INTEGER,
    rows_total         INTEGER,
    pos_count          INTEGER,
    neg_count          INTEGER,
    class_dist_json    TEXT,
    hold_bars          INTEGER,
    buffer_bars        INTEGER,
    seed               INTEGER,
    labeling_method    TEXT,
    feature_names_json TEXT,
    featureset_version TEXT,
    features_hash      TEXT,
    config_json        TEXT,
    nan_drop_rows      INTEGER,
    issues_json        TEXT,
    source_hashes_json TEXT,
    created_at         TEXT NOT NULL
);
"""
CREATE_TRAINING_FEATURE_IMPORTANCE_SQL = """
CREATE TABLE IF NOT EXISTS training_feature_importance (
    run_id      TEXT    NOT NULL,
    model_name  TEXT    NOT NULL,
    feature     TEXT    NOT NULL,
    importance  REAL    NOT NULL,
    rank        INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (run_id, model_name, feature)
);
"""
CREATE_TRAINING_FEATURE_IMPORTANCE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_tfi_run_id       ON training_feature_importance(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_tfi_model_run    ON training_feature_importance(model_name, run_id)"
]
# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from sqlalchemy.engine import Engine, create_engine
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
MARKET_DB_DSN: str = f"sqlite:///{DATA_DIR}/market_data.sqlite"

@dataclass
class LabelingConfig:
    """Конфигурация разметки с SQLAlchemy"""
    db_engine: Engine = None
    symbol: str = "ETHUSDT"
    timeframe: str = "5m"

    # PELT Online
    pelt_window: int = 1000
    pelt_pen: float = 1
    pelt_min_size: int = 10
    pelt_confirm_bar: int = 3

    # CUSUM
    cusum_z_threshold: float = 3 # минимальный |cusum_zscore|,
    cusum_conf_threshold: float = 1  # минимальная cusum_conf
    hold_bars: int = 3               # Более короткий холд
    buffer_bars: int = 5             # Меньше буферных баров

    # Extremum (min/max)
    extremum_confirm_bar: int = 2
    extremum_window: int = 8
    min_signal_distance: int = 3
    # Фильтры
    method: str = "CUSUM_EXTREMUM"
    # PnL параметры
    fee_percent: float = 0.0004
    min_profit_target: float = 0.001
    tool: Any = None

    # === HOLD разметка: пороги волатильности (нормализованное значение ATR/price) ===
    atr_low_threshold: float = 0.005      # 0.5% волатильности = низкая
    atr_high_threshold: float = 0.015     # 1.5% волатильности = высокая

    # === HOLD разметка: пороги силы тренда (R² линейной регрессии) ===
    trend_weak_threshold: float = 0.3    # слабый тренд
    trend_strong_threshold: float = 0.7  # сильный тренд

    # === HOLD разметка: пороги движения цены ===
    price_range_threshold: float = 0.003  # 0.3% движение = консолидация

    # === HOLD разметка: плотность меток для консолидаций ===
    consolidation_hold_every_n_bars: int = 3

    # === HOLD разметка: минимальная прибыль для "слабого" профита ===
    weak_profit_threshold: float = 0.002  # 0.2%

    # === HOLD разметка: веса для различных типов HOLD ===
    consolidation_sample_weight: float = 1.5  # Повышенный вес для консолидаций

    # === HOLD разметка: минимальная длина окна для MID ===
    hold_min_window_bars: int = 6
    hold_min_mid_end_gap: int = 3
    hold_mid_margin_left: int = 1
    hold_mid_margin_right: int = 1

    def __post_init__(self):
        if self.db_engine is None:
            self.db_engine = create_engine(MARKET_DB_DSN)


class DataLoader:
    """Улучшенный загрузчик данных с SQLAlchemy"""

    def __init__(self, db_engine: Engine = None, symbol: str = "ETHUSDT",
                 timeframe: str = "5m", config: LabelingConfig = None):
        self.db_engine = db_engine or create_engine(MARKET_DB_DSN)
        self.symbol = symbol
        self.timeframe = timeframe
        self.config = config  # ← Сохраняем config
        self.feature_names = BASE_FEATURE_NAMES


    def connect(self) -> Engine:
        """Установка соединения с БД через SQLAlchemy"""
        try:
            # Проверяем соединение
            with self.db_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info(f"✅ Подключено к БД через SQLAlchemy: {self.db_engine.url}")
            return self.db_engine
        except Exception as err:
            raise ConnectionError(f"Ошибка подключения к БД: {err}")

    def disconnect(self):
        """Закрытие соединения - для SQLAlchemy обычно не требуется"""
        # SQLAlchemy автоматически управляет соединениями
        logger.info("✅ Соединение с БД будет закрыто автоматически")

    def load_indicators(self) -> pd.DataFrame:
        """Загрузка данных с индикаторами через SQLAlchemy"""
        query = """
            SELECT * FROM candles_5m 
            WHERE symbol = ? 
            ORDER BY ts
        """
        try:
            df = pd.read_sql_query(query, self.db_engine, params=(self.symbol,))

            if df.empty:
                raise ValueError(f"Нет данных для символа {self.symbol}")

            # Преобразование времени
            if 'ts' in df.columns and 'datetime' not in df.columns:
                df['datetime'] = pd.to_datetime(df['ts'], unit='ms')

            required_cols = ["cusum", "cusum_state", "cusum_zscore", "cusum_conf"]
            missing = [c for c in required_cols if c not in df.columns]
            if missing:
                logger.info("CUSUM 5m: отсутствуют колонки %s (таблица candles_5m).", missing)
            else:
                # guard: если исторически встречались строковые статусы
                df["cusum_state"] = df["cusum_state"].replace({"BUY": 1, "SELL": -1, "HOLD": 0})
                # итог: Int64-категория 1/2/0, NaN -> 0
                df["cusum_state"] = pd.to_numeric(df["cusum_state"], errors="coerce").fillna(0).astype("Int64")

                # alias для отчётов/моделей (совпадает с кодировкой инструмента разметки)
                df["cusum_signal"] = df["cusum_state"]

                # числовые столбцы безопасно к float
                df["cusum"] = pd.to_numeric(df["cusum"], errors="coerce")
                df["cusum_zscore"] = pd.to_numeric(df["cusum_zscore"], errors="coerce")
                df["cusum_conf"] = pd.to_numeric(df["cusum_conf"], errors="coerce")

            logger.info(f"✅ Загружено {len(df)} свечей с индикаторами")
            return df

        except Exception as err:
            raise RuntimeError(f"Ошибка загрузки индикаторов: {err}")

    def validate_data_quality(self, df: pd.DataFrame) -> Tuple[bool, Dict[str, Any]]:
        """Расширенная валидация качества данных с детальной диагностикой"""
        if df.empty:
            return False, {"error": "DataFrame пуст"}

        required_columns = ['open', 'high', 'low', 'close', 'volume', 'ts']
        checks = {
            'min_rows': len(df) > 100,
            'required_columns': all(col in df.columns for col in required_columns),
            'no_nan_close': df['close'].isna().sum() == 0,
            'high_low_valid': (df['high'] >= df['low']).all(),
            'high_open_valid': (df['high'] >= df['open']).all(),
            'high_close_valid': (df['high'] >= df['close']).all(),
            'low_open_valid': (df['low'] <= df['open']).all(),
            'low_close_valid': (df['low'] <= df['close']).all(),
            'positive_volume': (df['volume'] >= 0).all(),
            'timestamp_monotonic': df['ts'].is_monotonic_increasing
        }

        quality_ok = all(checks.values())
        diagnostics = {
            'passed_checks': sum(checks.values()),
            'total_checks': len(checks),
            'failed_checks': [k for k, v in checks.items() if not v],
            'basic_stats': {
                'period': f"{df['datetime'].min()} to {df['datetime'].max()}" if 'datetime' in df.columns else 'N/A',
                'total_rows': len(df),
                'nan_count': df.isna().sum().sum()
            }
        }
        # DataLoader.validate_data_quality(df) → проверки CUSUM 5m
        for col in ["cusum", "cusum_zscore", "cusum_conf", "cusum_state"]:
            if col not in df.columns:
                logger.warning("Отсутствует колонка %s (CUSUM 5m).", col)

        if "cusum_state" in df.columns:
            bad_mask = ~df["cusum_state"].isin([0, 1, -1]) & df["cusum_state"].notna()
            if bad_mask.any():
                logger.warning("Неверные значения cusum_state встречаются на %d строках.", int(bad_mask.sum()))

        if not quality_ok:
            logger.warning(f"⚠️ Проблемы качества данных: {diagnostics['failed_checks']}")

        return quality_ok, diagnostics

    def load_labeled_data(self) -> pd.DataFrame:
        """Загрузка размеченных данных - ИСПРАВЛЕННАЯ ВЕРСИЯ С ПРОВЕРКАМИ"""
        query = """
            SELECT 
                lr.symbol,
                lr.timestamp as extreme_timestamp,
                lr.reversal_label,
                lr.reversal_confidence as confidence,
                lr.labeling_method as method,
                lr.price_change_after as pnl,
                lr.features_json,
                lr.created_at,
                c.* 
            FROM labeling_results lr
            LEFT JOIN candles_5m c ON lr.timestamp = c.ts AND lr.symbol = c.symbol
            WHERE lr.symbol = ?
            ORDER BY lr.timestamp
        """
        try:
            positives = pd.read_sql_query(
                query,
                self.db_engine,
                params=(self.symbol,)
            )

            if positives.empty:
                logger.warning(f"❌ Нет размеченных данных для символа {self.symbol}")
                return pd.DataFrame()

            # ⬇️ ИСПРАВЛЕНИЕ: проверяем существование колонки перед обработкой
            if 'extreme_timestamp' in positives.columns:
                # Преобразуем в числовой формат, некорректные значения станут NaN
                positives['extreme_timestamp'] = pd.to_numeric(positives['extreme_timestamp'], errors='coerce')
                # Удаляем строки с некорректными timestamp
                initial_count = len(positives)
                positives = positives.dropna(subset=['extreme_timestamp'])
                removed_count = initial_count - len(positives)
                if removed_count > 0:
                    logger.warning(f"⚠️ Удалено {removed_count} строк с некорректными timestamp")
            else:
                logger.warning("⚠️ Колонка 'extreme_timestamp' не найдена в результатах")

            logger.info(f"✅ Загружено {len(positives)} размеченных примеров для {self.symbol}")
            if 'reversal_label' in positives.columns:
                logger.info(f"📊 Распределение меток: {positives['reversal_label'].value_counts().to_dict()}")
            else:
                logger.warning("⚠️ Колонка 'reversal_label' не найдена")

            return positives

        except Exception as err:
            logger.error(f"❌ Ошибка загрузки размеченных данных: {err}")
            return pd.DataFrame()

    def safe_correlation_calculation(self, df, columns):
        """Безопасный расчет корреляций с обработкой нулевой дисперсии"""
        try:
            # Убедимся, что columns существует в df
            available_columns = [col for col in columns if col in df.columns]

            if len(available_columns) < 2:
                return pd.DataFrame()

            # Выбираем только числовые колонки
            numeric_cols = df[available_columns].select_dtypes(include=[np.number])

            if len(numeric_cols.columns) < 2:
                return pd.DataFrame()

            # Убираем колонки с нулевой дисперсией
            numeric_cols = numeric_cols.loc[:, numeric_cols.std() > 0]

            if len(numeric_cols.columns) < 2:
                return pd.DataFrame()

            # Убираем строки с NaN
            numeric_cols = numeric_cols.dropna()

            if len(numeric_cols) < 2:
                return pd.DataFrame()

            # Безопасный расчет корреляций
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                corr_matrix = numeric_cols.corr().abs()

            return corr_matrix

        except Exception as err:
            logger.debug(f"Ошибка расчета корреляций: {err}")
            return pd.DataFrame()

    def get_data_stats(self) -> Dict[str, Any]:
        """Получение статистики данных через SQLAlchemy"""
        stats = {
            'symbol': self.symbol,
            'total_candles': 0,
            'period': 'N/A',
            'total_labels': 0,
            'buy_labels': 0,
            'sell_labels': 0,
            'avg_confidence': 0.0
        }

        try:
            # Используем self.db_engine.connect() вместо self.conn
            with self.db_engine.connect() as conn:
                # Статистика свечей
                candles_result = conn.execute(
                    text("SELECT COUNT(*), MIN(ts), MAX(ts) FROM candles_5m WHERE symbol = :symbol"),
                    {'symbol': self.symbol}
                ).fetchone()

                if candles_result:
                    total_candles, min_ts, max_ts = candles_result
                    stats['total_candles'] = total_candles or 0
                    if min_ts and max_ts:
                        stats['period'] = f"{pd.to_datetime(min_ts, unit='ms')} to {pd.to_datetime(max_ts, unit='ms')}"

                # Статистика меток
                labels_result = conn.execute(
                    text("""
                        SELECT COUNT(*), AVG(reversal_confidence),
                               SUM(CASE WHEN reversal_label = 1 THEN 1 ELSE 0 END),
                               SUM(CASE WHEN reversal_label = 2 THEN 1 ELSE 0 END)
                        FROM labeling_results WHERE symbol = :symbol
                    """),
                    {'symbol': self.symbol}
                ).fetchone()

                if labels_result:
                    total_labels, avg_conf, buy_labels, sell_labels = labels_result
                    stats['total_labels'] = total_labels or 0
                    stats['buy_labels'] = buy_labels or 0
                    stats['sell_labels'] = sell_labels or 0
                    stats['avg_confidence'] = float(avg_conf) if avg_conf else 0.0

        except Exception as err:
            logger.error(f"Ошибка получения статистики: {err}")

        return stats

class AdvancedLabelingTool:
    """
    Улучшенный инструмент разметки с SQLAlchemy
    """

    def _ensure_training_snapshot_tables(self) -> None:
        """
        Создаёт (если отсутствуют) таблицы training_dataset и training_dataset_meta + индексы.
        Умная миграция: пересоздает таблицу только если структура устарела.
        """

        with self.engine.begin() as conn:
            # Проверяем структуру существующей таблицы
            try:
                result = conn.execute(text("PRAGMA table_info(training_dataset)")).fetchall()
                existing_columns = [row[1] for row in result]

                # Проверяем наличие старых колонок
                has_old_structure = (
                        'features_json' in existing_columns or
                        'is_negative' in existing_columns or
                        'anti_trade_mask' in existing_columns
                )

                if has_old_structure:
                    logger.info("🔄 Обнаружена старая структура training_dataset - пересоздаем таблицу")
                    conn.execute(text("DROP TABLE IF EXISTS training_dataset"))
                elif existing_columns:
                    logger.info("✅ Структура training_dataset актуальна (29 колонок)")
            except Exception:
                # Таблицы нет - это нормально, создастся ниже
                logger.info("📋 Таблица training_dataset не существует - будет создана")

            # Создаем таблицы (IF NOT EXISTS защищает от повторного создания)
            conn.execute(text(CREATE_TRAINING_DATASET_SQL))
            for sql in CREATE_TRAINING_DATASET_INDEXES_SQL:
                conn.execute(text(sql))
            conn.execute(text(CREATE_TRAINING_DATASET_META_SQL))
            conn.execute(text(CREATE_TRAINING_FEATURE_IMPORTANCE_SQL))
            for sql in CREATE_TRAINING_FEATURE_IMPORTANCE_INDEXES_SQL:
                conn.execute(text(sql))

        logger.info("✅ Таблицы training_dataset, training_dataset_meta, training_feature_importance проверены/созданы")

    def __init__(self, config: LabelingConfig):
        self.config = config

        logger = logging.getLogger(__name__)
        _VALID_METHODS = {"CUSUM", "EXTREMUM", "PELT_ONLINE", "CUSUM_EXTREMUM"}

        m = (getattr(self.config, "method", None) or "CUSUM_EXTREMUM").upper()
        if m not in _VALID_METHODS:
            logger.warning(
                "Unknown labeling method '%s'. Falling back to 'CUSUM_EXTREMUM'. "
                "Allowed: %s",
                m, sorted(_VALID_METHODS),
            )
            m = "CUSUM_EXTREMUM"
        self.logger = logger
        self.config.method = m
        self.config.tool = self
        self.data_loader = DataLoader(
            db_engine=create_engine(MARKET_DB_DSN),
            symbol=config.symbol,
            timeframe=config.timeframe,
            config=config
        )

        self.labels = []
        self.engine = self.data_loader.connect()
        self.feature_names = self.data_loader.feature_names
        self._ensure_table_exists()
        self.config.pnl_threshold = 0.001
        logger.info(f"✅ Инициализирован AdvancedLabelingTool для {config.symbol}")

    def _check_table_exists(self, table_name: str) -> bool:
        """Проверка существования таблицы через SQLAlchemy"""
        from sqlalchemy import inspect
        try:
            inspector = inspect(self.engine)
            return table_name in inspector.get_table_names()
        except Exception as err:
            logger.error(f"Ошибка проверки таблицы {table_name}: {err}")
            return False

    def _validate_snapshot_frame(self, df: pd.DataFrame):
        """
        Проверяет и очищает датафрейм перед записью в БД.
        Валидирует структуру для 3-классовой модели.
        """
        # Обязательные колонки для новой структуры
        required = ["ts", "datetime", "reversal_label", "sample_weight"]
        missing = [col for col in required if col not in df.columns]
        if missing:
            raise ValueError(f"Snapshot validation failed: missing required columns: {missing}")

        # Проверка значений reversal_label (0=NO_SIGNAL/HOLD, 1=BUY, 2=SELL)
        allowed_labels = [0, 1, 2]
        if not df["reversal_label"].isin(allowed_labels).all():
            invalid_labels = list(df.loc[~df["reversal_label"].isin(allowed_labels), "reversal_label"].unique())
            raise ValueError(
                f"Invalid reversal_label values: {invalid_labels}. Expected: {allowed_labels}")

        # Удаление дубликатов по ts
        duplicates_count = df.duplicated(subset=["ts"]).sum()
        if duplicates_count > 0:
            logger.warning(f"⚠️  Найдено {duplicates_count} дубликатов по ts - удаляем")
            df = df.drop_duplicates(subset=["ts"], keep="first")

        # Удаление строк с NaN в критичных колонках
        critical_cols = ["ts", "reversal_label", "sample_weight"]
        nan_mask = df[critical_cols].isna().any(axis=1)
        nan_drop_rows = nan_mask.sum()
        if nan_drop_rows > 0:
            logger.warning("⚠️  Удаляем %d строк с NaN в критичных колонках", nan_drop_rows)
            df = df[~nan_mask]

        # Базовые метрики
        issues = {
            "duplicates_removed": int(duplicates_count),
            "nan_drop_rows": int(nan_drop_rows),
            "class_balance": df["reversal_label"].value_counts().to_dict(),
        }

        # ── TS-валидация ──────────────────────────────────────────
        # 1) При необходимости сортируем по ts
        if not df["ts"].is_monotonic_increasing:
            logger.warning("⚠️  ts не монотонно возрастают — сортируем по ts")
            df = df.sort_values("ts").reset_index(drop=True)
            issues["ts_sorted"] = True
        else:
            issues["ts_sorted"] = False

        # 2) Поиск разрывов по времени, если знаем шаг таймфрейма
        timeframe_ms_map = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
        tf = str(getattr(self.config, "timeframe", "5m")).lower()
        expected_step = timeframe_ms_map.get(tf)

        ts_gaps_count = 0
        ts_gap_max = 0

        if expected_step is not None:
            ts_series = df["ts"].astype("int64").sort_values()
            diffs = ts_series.diff().dropna()
            bad_diffs = diffs[diffs != expected_step]

            if not bad_diffs.empty:
                ts_gaps_count = int(bad_diffs.shape[0])
                ts_gap_max = int(bad_diffs.max())
                logger.warning(
                    "⚠️  Найдено %d разрывов по ts (ожидали шаг %d мс, max_gap=%d мс)",
                    ts_gaps_count,
                    expected_step,
                    ts_gap_max,
                )

        issues["ts_gaps_count"] = ts_gaps_count
        issues["ts_gap_max"] = ts_gap_max

        return df, issues, int(nan_drop_rows), int(duplicates_count)


    def _ensure_table_exists(self):
        """Создание расширенных таблиц через SQLAlchemy (идемпотентно)"""
        from sqlalchemy import text

        # labeling_results: единая схема
        if not self._check_table_exists('labeling_results'):
            logger.info("📋 Создание таблицы labeling_results...")

            create_table_sql = text("""
                CREATE TABLE IF NOT EXISTS labeling_results (
                    symbol TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    timeframe TEXT NOT NULL,
                    reversal_label INTEGER NOT NULL,
                    reversal_confidence REAL DEFAULT 1.0,
                    labeling_method TEXT NOT NULL,
                    labeling_params TEXT,
                    extreme_index INTEGER,
                    extreme_price REAL,
                    extreme_timestamp INTEGER NOT NULL,
                    confirmation_index INTEGER,
                    confirmation_timestamp INTEGER,
                    price_change_after REAL,
                    features_json TEXT,
                    is_high_quality INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (symbol, extreme_timestamp, reversal_label)
                )
            """)

            # Индексы: запросы часто идут без reversal_label, добавим покрывающий индекс.
            create_index_1 = text("""
                CREATE INDEX IF NOT EXISTS idx_labeling_results_symbol_ts
                ON labeling_results(symbol, extreme_timestamp)
            """)
            # Если у тебя часто есть фильтр по timeframe — добавь композитный индекс:
            create_index_2 = text("""
                CREATE INDEX IF NOT EXISTS idx_labeling_results_symbol_tf_ts
                ON labeling_results(symbol, timeframe, extreme_timestamp)
            """)

            # Используем begin() для атомарности
            with self.engine.begin() as conn:
                conn.execute(create_table_sql)
                conn.execute(create_index_1)
                conn.execute(create_index_2)

            logger.info("✅ Таблица labeling_results создана")
        else:
            logger.info("✅ Таблица labeling_results уже существует")

    def load_data(self) -> pd.DataFrame:
        """Загрузка данных"""
        try:
            df = self.data_loader.load_indicators()
            quality_ok, diagnostics = self.data_loader.validate_data_quality(df)

            if not quality_ok:
                logger.warning(f"⚠️ Проблемы качества данных: {diagnostics}")

            return df
        except Exception as err:
            raise RuntimeError(f"Ошибка загрузки данных: {err}")

    def _get_all_existing_signals(self) -> List[Dict]:
        """Получение всех существующих меток символа из БД"""
        if not self._check_table_exists('labeling_results'):
            return []

        try:
            query = """
                SELECT extreme_timestamp, extreme_index, reversal_label, labeling_method 
                FROM labeling_results 
                WHERE symbol = :symbol 
                ORDER BY extreme_index
            """

            with self.engine.connect() as conn:
                result = conn.execute(
                    text(query),
                    {'symbol': self.config.symbol}
                ).fetchall()

            signals = []
            for extreme_ts, extreme_idx, reversal_label, method in result:
                signals.append({
                    'extreme_timestamp': extreme_ts,
                    'extreme_index': extreme_idx,
                    'reversal_label': reversal_label,
                    'labeling_method': method
                })

            logger.info(f"📊 Загружено {len(signals)} существующих меток из БД")
            return signals

        except Exception as err:
            logger.error(f"❌ Ошибка загрузки существующих меток: {err}")
            return []

    def _calculate_pnl_to_index(self, df: pd.DataFrame, entry_idx: int, signal_type: str, end_idx: int) -> Tuple[
        float, bool]:
        """
        Универсальный расчет PnL до конкретного индекса с корректной формулой
        """
        # ✓ ИСПРАВЛЕНО: Проверка обеих границ
        if entry_idx >= len(df) or end_idx >= len(df):
            return 0.0, False

        try:
            entry_price = df['close'].iloc[entry_idx]
            exit_price = df['close'].iloc[end_idx]

            if entry_price <= 0 or exit_price <= 0:
                return 0.0, False

            # ✓ ИСПРАВЛЕНО: Правильная формула
            if signal_type == 'BUY':
                net_pnl = (exit_price * (1 - self.config.fee_percent) /
                           (entry_price * (1 + self.config.fee_percent))) - 1
            else:  # SELL
                net_pnl = (entry_price * (1 - self.config.fee_percent) /
                           (exit_price * (1 + self.config.fee_percent))) - 1

            is_profitable_enough = net_pnl >= self.config.min_profit_target
            return net_pnl, is_profitable_enough

        except (IndexError, ZeroDivisionError, KeyError) as err:
            logger.warning(f"Ошибка расчета PnL для индекса {entry_idx}: {err}")
            return 0.0, False

    # =========================================================================
    # ОСНОВНЫЕ МЕТОДЫ РАЗМЕТКИ (из исходного кода)
    # =========================================================================

    def _calculate_pnl(self, df: pd.DataFrame, entry_idx: int, signal_type: str) -> Tuple[float, bool]:
        """
        Корректный расчёт PnL с проверкой границ и правильной формулой комиссий
        """
        exit_idx = entry_idx + self.config.hold_bars

        # ✓ ИСПРАВЛЕНО: Проверка границ ДО обращения к iloc
        if entry_idx >= len(df) or exit_idx >= len(df):
            return 0.0, False

        try:
            entry_price = df['close'].iloc[entry_idx]
            exit_price = df['close'].iloc[exit_idx]

            if entry_price <= 0 or exit_price <= 0:
                return 0.0, False

            # ✓ ИСПРАВЛЕНО: Правильная формула с учётом комиссий
            if signal_type == 'BUY':
                # Покупка: платим комиссию при входе и выходе
                net_pnl = (exit_price * (1 - self.config.fee_percent) /
                           (entry_price * (1 + self.config.fee_percent))) - 1
            else:  # SELL
                # Шорт: платим комиссию при входе и выходе
                net_pnl = (entry_price * (1 - self.config.fee_percent) /
                           (exit_price * (1 + self.config.fee_percent))) - 1

            is_profitable_enough = net_pnl >= self.config.min_profit_target
            return net_pnl, is_profitable_enough

        except (IndexError, ZeroDivisionError, KeyError) as err:
            logger.warning(f"Ошибка расчета PnL для индекса {entry_idx}: {err}")
            return 0.0, False

    def _interpret_pnl_results(self, total_metrics: Dict[str, float]):
        """Интерпретация результатов PnL с корректными процентами"""
        total_pnl = total_metrics.get('total_pnl', 0)
        success_rate = total_metrics.get('success_rate', 0)
        pnl_ratio = total_metrics.get('pnl_ratio', 0)

        print(f"\n🔍 ИНТЕРПРЕТАЦИЯ РЕЗУЛЬТАТОВ:")

        # Оценка успешности
        if success_rate > 0.6:
            print(f"   🎯 Высокая успешность: {success_rate:.1%} (>60%)")
        elif success_rate > 0.4:
            print(f"   ✅ Средняя успешность: {success_rate:.1%} (40-60%)")
        else:
            print(f"   ⚠️  Низкая успешность: {success_rate:.1%} (<40%)")

        # Оценка соотношения PNL
        if pnl_ratio > 3:
            print(f"   💎 Отличное соотношение прибыли/убытков: {pnl_ratio:.1f}:1")
        elif pnl_ratio > 2:
            print(f"   ✅ Хорошее соотношение прибыли/убытков: {pnl_ratio:.1f}:1")
        elif pnl_ratio > 1:
            print(f"   ⚠️  Удовлетворительное соотношение: {pnl_ratio:.1f}:1")
        else:
            print(f"   ❌ Проблемное соотношение: {pnl_ratio:.1f}:1")

        # Оценка общей прибыльности (используем проценты как есть)
        if total_pnl > 0.1:  # >10%
            print(f"   🚀 Высокая общая прибыльность: {total_pnl:+.1%}")
        elif total_pnl > 0.02:  # >2%
            print(f"   ✅ Положительная прибыльность: {total_pnl:+.1%}")
        elif total_pnl > 0:  # >0%
            print(f"   ⚠️  Слабая прибыльность: {total_pnl:+.1%}")
        else:
            print(f"   ❌ Убыточная стратегия: {total_pnl:+.1%}")

    def _smart_confirmation_system(self, df: pd.DataFrame, signal_idx: int, signal_type: str) -> dict:
        """Умная система подтверждения"""
        confirm_bar = self._get_confirmation_bars(signal_type)

        confirmation_data = {
            'confirmed': False,
            'confidence_boost': 0.0,
            'early_rejection': False,
            'confirmation_index': signal_idx + confirm_bar,
            'price_change': 0.0
        }

        if signal_idx + 3 >= len(df):
            confirmation_data['early_rejection'] = True
            return confirmation_data

        price_at_signal = df['close'].iloc[signal_idx]
        price_after_3bars = df['close'].iloc[signal_idx + 3]
        expected_move = 0.005

        if signal_type == 'BUY':
            move_percent = (price_after_3bars - price_at_signal) / price_at_signal
            if move_percent < -expected_move:
                confirmation_data['early_rejection'] = True
        else:
            move_percent = (price_at_signal - price_after_3bars) / price_at_signal
            if move_percent < -expected_move:
                confirmation_data['early_rejection'] = True

        if not confirmation_data['early_rejection'] and signal_idx + confirm_bar < len(df):
            confirmation_data['confirmed'] = True
            confirmation_data['price_change'] = move_percent
            if abs(move_percent) > expected_move:
                confirmation_data['confidence_boost'] = 0.15

        return confirmation_data

    def merge_conflicting_labels(self):
        """Последовательная валидация всей разметки"""
        try:
            df = self.load_data()
            with self.engine.begin() as conn:
                # Получаем все BUY/SELL метки отсортированные по времени
                signals_query = """
                    SELECT rowid, extreme_timestamp, reversal_label, labeling_method, price_change_after
                    FROM labeling_results 
                    WHERE symbol = :symbol AND reversal_label IN (1,2)
                    ORDER BY extreme_timestamp
                """
                signals = conn.execute(text(signals_query), {'symbol': self.config.symbol}).fetchall()

                if not signals:
                    print("✅ Нет BUY/SELL меток для валидации")
                    return 0

                fixed_count = 0

                # Обрабатываем каждую пару последовательных сигналов
                for i in range(len(signals) - 1):
                    current_signal = signals[i]
                    next_signal = signals[i + 1]

                    current_ts = current_signal.extreme_timestamp
                    next_ts = next_signal.extreme_timestamp

                    # Получаем индексы для расчета PNL
                    current_idx_match = df.index[df['ts'] == current_ts].tolist()
                    next_idx_match = df.index[df['ts'] == next_ts].tolist()

                    if not current_idx_match or not next_idx_match:
                        continue

                    current_idx = int(current_idx_match[0])
                    next_idx = int(next_idx_match[0])

                    # Пересчитываем PNL до следующего сигнала
                    signal_type = 'BUY' if current_signal.reversal_label == 1 else 'SELL'
                    pnl, _ = self._calculate_pnl_to_index(df, current_idx, signal_type, next_idx)

                    # Если PNL < 0.1% - исправляем разметку
                    if abs(pnl) < 0.001:
                        print(f"🔄 Исправляем малоприбыльный сигнал на ts={current_ts}, PNL={pnl:.4f}")

                        # Удаляем текущий сигнал
                        conn.execute(text("DELETE FROM labeling_results WHERE rowid = :rowid"),
                                     {'rowid': current_signal.rowid})

                        # Ставим HOLD на следующем баре
                        if current_idx + 1 < len(df):
                            next_ts_hold = int(df.iloc[current_idx + 1]['ts'])
                            conn.execute(text("""
                                INSERT OR IGNORE INTO labeling_results 
                                (symbol, timestamp, timeframe, reversal_label, reversal_confidence, 
                                 labeling_method, extreme_timestamp, price_change_after)
                                VALUES (:symbol, :ts, :tf, 0, 1.0, 'VALIDATED_HOLD', :ts, 0.0)
                            """), {'symbol': self.config.symbol, 'ts': next_ts_hold, 'tf': self.config.timeframe})

                        fixed_count += 1

                print(f"✅ Исправлено малоприбыльных сигналов: {fixed_count}")
                return fixed_count

        except Exception as err:
            logger.error(f"❌ Ошибка валидации разметки: {err}")
            raise

    def _get_confirmation_bars(self, signal_type: str) -> int:
        """
        Возвращает базовое количество баров подтверждения в зависимости от метода разметки.
        Делает метод устойчивым к регистру/вводу пользователя.
        """
        import logging
        logger = logging.getLogger(__name__)

        # Нормализация метода к верхнему регистру (устойчивость к вводу)
        method = (getattr(self.config, "method", "") or "").upper()

        # Базовая логика (без изменений поведения для известных методов)
        if method == "PELT_ONLINE":
            base_confirmation = 3
        elif method in ("CUSUM", "EXTREMUM", "CUSUM_EXTREMUM"):
            base_confirmation = 2
        else:
            logger.warning(
                "Unknown labeling method '%s' in _get_confirmation_bars; using base_confirmation=2",
                method
            )
            base_confirmation = 2

        confirmation_bars = base_confirmation
        if self.config.method == 'EXTREMUM':
            confirmation_bars += 1  # специальная логика для EXTREMUM

        logger.debug(f"Определение подтверждения для {signal_type} сигнала")
        return max(1, min(5, confirmation_bars))  # ограничиваем разумными пределами

    def _binseg_reversals(self, df: pd.DataFrame) -> List[Dict]:
        """
        Улучшенный BinSeg с лучшей настройкой параметров
        """
        if not RUPTURES_AVAILABLE:
            logger.warning("⚠️ Библиотека ruptures недоступна")
            return []

        # ⚡ ОПТИМИЗАЦИЯ: Более разумное ограничение данных
        optimal_samples = min(5000, len(df))
        if len(df) > optimal_samples:
            logger.info(f"⚡ Сокращаем данные с {len(df)} до {optimal_samples} samples")
            df = df.iloc[-optimal_samples:].copy()

        # Подготовка данных
        close_vals = df['close'].astype(float).values

        # 🎯 ИСПОЛЬЗУЕМ РАЗНЫЕ ТИПЫ СИГНАЛОВ ДЛЯ ЛУЧШЕГО ОБНАРУЖЕНИЯ
        signals = {}

        # 1. Логарифмические доходности (основной сигнал)
        log_prices = np.log(np.clip(close_vals, 1e-12, None))
        returns = np.diff(log_prices)
        returns = np.insert(returns, 0, 0)
        signals['returns'] = returns

        # 2. Нормализованные цены (дополнительный сигнал)
        price_mean = np.mean(close_vals)
        price_std = np.std(close_vals)
        if price_std > 0:
            normalized_prices = (close_vals - price_mean) / price_std
            signals['normalized'] = normalized_prices

        # 3. Волатильность (для обнаружения изменений в волатильности)
        volatility = np.abs(returns) * 100  # Процентная волатильность
        signals['volatility'] = volatility

        print("🔍 BinSeg: расширенный подбор параметров...")

        # 🎯 РАСШИРЕННЫЙ ПОДБОР ПАРАМЕТРОВ
        best_result = None
        best_score = -np.inf

        # Тестируем разные комбинации
        for signal_name, signal_data in signals.items():
            for n_bkps in [8, 12, 15, 18, 20, 25]:  # Более широкий диапазон
                for model in ["l2", "rbf"]:  # Только работающие модели
                    try:
                        if n_bkps >= len(signal_data) // 3:
                            continue

                        # Запускаем BinSeg
                        algo = rpt.Binseg(model=model, min_size=15, jump=8).fit(signal_data)
                        changepoints = algo.predict(n_bkps=n_bkps)

                        # Фильтруем корректные точки
                        changepoints = [cp for cp in changepoints if 15 < cp < len(df) - 15]

                        if len(changepoints) < 3:  # Нужно минимум 3 точки для анализа
                            continue

                        # Оцениваем качество разбиения
                        score = self._evaluate_segmentation_improved(df, changepoints, signal_name)

                        # Бонус за большее количество валидных сигналов
                        potential_signals = self._count_potential_signals(df, changepoints)
                        score += min(potential_signals * 0.01, 0.1)  # Бонус до 0.1

                        if score > best_score:
                            best_score = score
                            best_result = {
                                'signal': signal_name,
                                'model': model,
                                'n_bkps': n_bkps,
                                'changepoints': changepoints,
                                'score': score,
                                'potential_signals': potential_signals
                            }

                        print(
                            f"  {signal_name:12} model={model}, n_bkps={n_bkps:2} → {len(changepoints):2} точек, score={score:.3f}, signals={potential_signals}")

                    except Exception as err:
                        # print(f"  {signal_name:12} model={model}, n_bkps={n_bkps:2} → ошибка: {err}")
                        continue

        if not best_result:
            logger.warning("❌ BinSeg не смог найти точки разрыва")
            return []

        changepoints = best_result['changepoints']
        print(
            f"✅ Лучшая конфигурация: {best_result['signal']}, model={best_result['model']}, n_bkps={best_result['n_bkps']}")
        print(
            f"📊 Найдено точек разрыва: {len(changepoints)}, потенциальных сигналов: {best_result['potential_signals']}")

        # Преобразование точек разрыва в торговые сигналы
        results = self._convert_changepoints_to_signals_improved(df, changepoints)

        logger.info(f"📊 BinSeg найдено {len(results)} сигналов")

        # 📊 СТАТИСТИКА СИГНАЛОВ
        if results:
            buy_count = sum(1 for r in results if r['type'] == 'BUY')
            sell_count = sum(1 for r in results if r['type'] == 'SELL')
            avg_confidence = np.mean([r['confidence'] for r in results])
            print(f"📈 Итоги: {buy_count} BUY, {sell_count} SELL, средняя уверенность: {avg_confidence:.2f}")

        return results

    def _convert_changepoints_to_signals_improved(self, df: pd.DataFrame, changepoints: List[int]) -> List[Dict]:
        """
        Улучшенное преобразование с лучшей логикой определения BUY/SELL
        """
        results = []

        for i in range(1, len(changepoints) - 1):
            current_cp = changepoints[i]  # Точка разрыва
            prev_cp = changepoints[i - 1]  # Начало предыдущего тренда
            next_cp = changepoints[i + 1]  # Конец текущего тренда

            if current_cp >= len(df) or next_cp >= len(df) or prev_cp >= len(df):
                continue

            # 🔍 УЛУЧШЕННЫЙ АНАЛИЗ ТРЕНДОВ
            # Предыдущий тренд (2/3 сегмента для надежности)
            prev_segment_len = current_cp - prev_cp
            analysis_start = prev_cp + prev_segment_len // 3  # Игнорируем начало сегмента
            price_prev_start = df['close'].iloc[analysis_start]
            price_prev_end = df['close'].iloc[current_cp - 1]
            prev_trend = (price_prev_end - price_prev_start) / price_prev_start

            # Текущий тренд (2/3 сегмента)
            current_segment_len = next_cp - current_cp
            analysis_end = current_cp + (2 * current_segment_len) // 3
            if analysis_end >= len(df):
                analysis_end = len(df) - 1

            price_current_start = df['close'].iloc[current_cp]
            price_current_end = df['close'].iloc[analysis_end]
            current_trend = (price_current_end - price_current_start) / price_current_start

            # 🎯 УЛУЧШЕННЫЕ КРИТЕРИИ:
            min_trend_strength = 0.003  # 0.3% минимальное изменение

            # СИГНАЛ BUY: сильное падение → сильный рост
            if (prev_trend < -min_trend_strength and
                    current_trend > min_trend_strength and
                    abs(current_trend) > abs(prev_trend) * 0.3):  # Меньше требований к силе

                rev_type = "BUY"
                confidence = min(abs(current_trend) * 15 + abs(prev_trend) * 10, 0.95)

            # СИГНАЛ SELL: сильный рост → сильное падение
            elif (prev_trend > min_trend_strength and
                  current_trend < -min_trend_strength and
                  abs(current_trend) > abs(prev_trend) * 0.3):

                rev_type = "SELL"
                confidence = min(abs(current_trend) * 15 + abs(prev_trend) * 10, 0.95)

            else:
                continue

            # 🚀 ВХОД НА СЛЕДУЮЩЕЙ СВЕЧЕ
            entry_index = current_cp + 1
            if entry_index >= len(df):
                continue

            # ✅ ПОДТВЕРЖДЕНИЕ СИГНАЛА
            confirmation = self._smart_confirmation_system(df, entry_index, rev_type)

            if confirmation['early_rejection']:
                continue

            conf_idx = confirmation['confirmation_index']
            if conf_idx >= len(df):
                conf_idx = len(df) - 1

            results.append({
                'index': entry_index,
                'type': rev_type,
                'confidence': confidence,
                'extreme_index': current_cp,
                'extreme_timestamp': int(df['ts'].iloc[current_cp]),
                'confirmation_index': conf_idx,
                'confirmation_timestamp': int(df['ts'].iloc[conf_idx]),
                'method': 'BINSEG',
                'reversal_label': 1 if rev_type == 'BUY' else 2,
            })

        return results

    def _evaluate_segmentation_improved(self, df: pd.DataFrame, changepoints: List[int], signal_type: str) -> float:
        """
        Улучшенная оценка качества сегментации
        """
        if len(changepoints) < 3:
            return -np.inf

        close_vals = df['close'].values
        total_variance = np.var(close_vals)

        if total_variance == 0:
            return 0.0

        # Вычисляем объясненную дисперсию
        segments = []
        start_idx = 0
        for cp in changepoints:
            if cp > start_idx:
                segments.append((start_idx, cp))
                start_idx = cp
        segments.append((start_idx, len(close_vals)))

        # Вычисляем внутрисегментную дисперсию
        within_segment_variance = 0.0
        segment_quality = 0.0

        for start, end in segments:
            if end - start > 2:  # Минимум 3 точки в сегменте
                segment_data = close_vals[start:end]
                seg_variance = np.var(segment_data)
                within_segment_variance += seg_variance * (end - start)

                # Оцениваем качество сегмента (линейность)
                if len(segment_data) > 3:
                    x = np.arange(len(segment_data))
                    correlation = np.corrcoef(x, segment_data)[0, 1]
                    if not np.isnan(correlation):
                        segment_quality += abs(correlation) * (end - start)

        within_segment_variance /= len(close_vals)
        explained_variance = 1 - (within_segment_variance / total_variance)

        # Качество сегментов (чем более линейны сегменты, тем лучше)
        segment_quality /= len(close_vals)

        # Штраф за слишком много/мало сегментов
        optimal_segments = len(close_vals) // 100  # Оптимум ~1 сегмент на 100 баров
        segment_penalty = abs(len(segments) - optimal_segments) * 0.02

        final_score = explained_variance * 0.7 + segment_quality * 0.3 - segment_penalty

        return max(0, final_score)

    def _count_potential_signals(self, df: pd.DataFrame, changepoints: List[int]) -> int:
        """
        Быстрая оценка количества потенциальных сигналов
        """
        count = 0
        for i in range(1, len(changepoints) - 1):
            cp = changepoints[i]
            if cp + 1 < len(df):
                count += 1
        return count

    def _evaluate_segmentation(self, df: pd.DataFrame, changepoints: List[int]) -> float:
        """
        Оценка качества сегментации
        """
        if len(changepoints) < 2:
            return -np.inf

        close_vals = df['close'].values
        total_variance = np.var(close_vals)

        if total_variance == 0:
            return 0.0

        # Вычисляем объясненную дисперсию
        explained_variance = 0.0
        segments = []

        # Формируем сегменты
        start_idx = 0
        for cp in changepoints:
            if cp > start_idx:
                segments.append((start_idx, cp))
                start_idx = cp
        segments.append((start_idx, len(close_vals)))

        # Вычисляем внутрисегментную дисперсию
        within_segment_variance = 0.0
        for start, end in segments:
            if end - start > 1:
                segment_data = close_vals[start:end]
                within_segment_variance += np.var(segment_data) * (end - start)

        within_segment_variance /= len(close_vals)
        explained_variance = 1 - (within_segment_variance / total_variance)

        # Штрафуем за слишком много сегментов
        penalty = len(changepoints) * 0.01

        return explained_variance - penalty

    def _convert_changepoints_to_signals(self, df: pd.DataFrame, changepoints: List[int]) -> List[Dict]:
        """
        Преобразование точек разрыва в торговые сигналы
        """
        results = []

        for i in range(len(changepoints) - 1):
            start = changepoints[i]
            end = changepoints[i + 1]

            if start >= len(df) or end >= len(df):
                continue

            # Анализируем тренд в сегменте
            segment_prices = df['close'].iloc[start:end].values
            price_start = segment_prices[0]
            price_end = segment_prices[-1]

            # Определяем направление тренда
            price_change = (price_end - price_start) / price_start
            trend_up = price_change > 0

            # Анализируем предыдущий сегмент (если есть)
            if i > 0:
                prev_start = changepoints[i - 1]
                prev_segment = df['close'].iloc[prev_start:start].values
                prev_trend_up = (prev_segment[-1] - prev_segment[0]) / prev_segment[0] > 0

                # Определяем разворот
                rev_type = None
                if not prev_trend_up and trend_up:
                    rev_type = "BUY"
                elif prev_trend_up and not trend_up:
                    rev_type = "SELL"

                if rev_type:
                    # Вход на следующей свече после точки разрыва
                    entry_index = start + 1
                    if entry_index >= len(df):
                        continue

                    # Расчет уверенности на основе величины изменения
                    confidence = min(abs(price_change) * 20, 0.95)
                    confidence = max(confidence, 0.3)

                    # Подтверждение сигнала
                    confirmation = self._smart_confirmation_system(df, entry_index, rev_type)
                    conf_idx = confirmation['confirmation_index']
                    if conf_idx >= len(df):
                        conf_idx = len(df) - 1

                    results.append({
                        'index': entry_index,
                        'type': rev_type,
                        'confidence': confidence,
                        'extreme_index': start,
                        'extreme_timestamp': int(df['ts'].iloc[start]),
                        'confirmation_index': conf_idx,
                        'confirmation_timestamp': int(df['ts'].iloc[conf_idx]),
                        'method': 'BINSEG',
                        'reversal_label': 1 if rev_type == 'BUY' else 2,
                    })

        return results

    def _pelt_offline_reversals(self, df: pd.DataFrame) -> List[Dict]:
        """
        Оптимизированная PELT-разметка с заменой на Binseg и защитой от зависаний.
        Вход — следующая свеча после экстремума (как у EXTREMUM).
        """
        if not RUPTURES_AVAILABLE:
            logger.warning("⚠️ Библиотека ruptures недоступна")
            return []

        if len(df) < 500:
            logger.warning(f"⚠️ Недостаточно данных для PELT Offline: {len(df)}")
            return []

        logger.info(f"📊 PELT Offline (Binseg): анализ {len(df)} свечей...")

        # ⚡ Ограничение размера для скорости
        MAX_SAMPLES = 5000
        if len(df) > MAX_SAMPLES:
            logger.info(f"⚡ Сокращение данных с {len(df)} → {MAX_SAMPLES}")
            df = df.iloc[-MAX_SAMPLES:].copy()

        # === 1. Подготовка сигнала ===
        try:
            # Используем логарифм цен — устойчиво к масштабу и look-ahead
            close_vals = df['close'].astype(float).values
            signal = np.log(np.clip(close_vals, 1e-12, None))
        except Exception as e:
            logger.error(f"❌ Ошибка подготовки сигнала: {e}")
            return []

        # === 2. Интерактивный выбор целевого количества сигналов ===
        print("\n" + "=" * 60)
        print("🎯 НАСТРОЙКА BINSEG: Целевое количество сигналов")
        print("=" * 60)
        print("Выберите стратегию:")
        print("   📊 [1] Консервативная:  3–5 / день  (~600–1000)")
        print("   📊 [2] Сбалансированная: 10–15 / день (~2000–3000) ← рекомендуется")
        print("   📊 [3] Агрессивная:     20–30 / день (~4000–6000)")
        print("   ⚙️  [4] Своё значение")

        choice = input("\nВаш выбор [2]: ").strip()
        if choice == '1':
            target_signals_daily = 4.0
        elif choice == '3':
            target_signals_daily = 25.0
        elif choice == '4':
            try:
                val = input("Количество сигналов в день [12]: ").strip()
                target_signals_daily = float(val) if val else 12.0
                if not (1 <= target_signals_daily <= 50):
                    print("⚠️ Диапазон 1–50. Установлено 12.")
                    target_signals_daily = 12.0
            except ValueError:
                print("⚠️ Неверный формат. Установлено 12.")
                target_signals_daily = 12.0
        else:
            target_signals_daily = 12.0

        print(f"✅ Выбрано: ~{target_signals_daily:.1f} сигналов/день")

        # === 3. Расчёт целевого числа changepoints ===
        bars_per_day = 288  # 5m
        n_samples = len(signal)
        expected_signals = target_signals_daily * (n_samples / bars_per_day)

        # Эмпирический коэффициент: changepoints ≈ 2.55 × сигналы (из практики)
        SIGNAL_TO_CHANGEPOINT_RATIO = 2.55
        target_changepoints = int(expected_signals * SIGNAL_TO_CHANGEPOINT_RATIO)

        print(f"🎯 Цель: {target_changepoints} changepoints (≈ {expected_signals:.0f} сигналов)")

        # === 4. Вычисление change points через Binseg (быстро и стабильно) ===
        try:
            algo = rpt.Binseg(model="l2", min_size=10, jump=5).fit(signal)
            changepoints = algo.predict(n_bkps=target_changepoints)
            # Убираем последнюю точку (ruptures добавляет len(signal) как искусственный конец)
            changepoints = [cp for cp in changepoints if cp < len(df)]
            logger.info(f"✅ Найдено {len(changepoints)} changepoints")
        except Exception as e:
            logger.error(f"❌ Ошибка Binseg: {e}")
            return []

        # === 5. Построение BUY/SELL по трендам между changepoints ===
        results = []
        for i in range(1, len(changepoints) - 1):
            prev_cp = changepoints[i - 1]
            cur_cp = changepoints[i]
            next_cp = changepoints[i + 1]

            if cur_cp >= len(df):
                continue

            # Тренд до точки: [prev_cp → cur_cp)
            trend_prev_up = df['close'].iat[cur_cp - 1] > df['close'].iat[prev_cp]
            # Тренд после точки: [cur_cp → next_cp)
            trend_next_up = df['close'].iat[next_cp - 1] > df['close'].iat[cur_cp]

            rev_type = None
            if not trend_prev_up and trend_next_up:
                rev_type = "BUY"
            elif trend_prev_up and not trend_next_up:
                rev_type = "SELL"
            if rev_type is None:
                continue

            # 🔁 ВХОД НА СЛЕДУЮЩЕЙ СВЕЧЕ ПОСЛЕ ЭКСТРЕМУМА (правильно!)
            entry_idx = cur_cp + 1
            if entry_idx >= len(df):
                continue

            # Расчёт confidence: нормализованный price move
            move_abs = abs(df['close'].iat[next_cp - 1] - df['close'].iat[cur_cp])
            move_rel = move_abs / df['close'].iat[cur_cp]
            confidence = np.clip(move_rel * 10, 0.3, 0.95)  # 0.3–0.95

            # Подтверждение сигнала
            confirmation = self._smart_confirmation_system(df, entry_idx, rev_type)
            conf_idx = confirmation['confirmation_index']
            if conf_idx >= len(df):
                conf_idx = len(df) - 1

            # Формируем результат
            results.append({
                'index': entry_idx,
                'type': rev_type,
                'confidence': float(confidence),
                'extreme_index': int(cur_cp),
                'extreme_timestamp': int(df['ts'].iloc[cur_cp]),
                'confirmation_index': int(conf_idx),
                'confirmation_timestamp': int(df['ts'].iloc[conf_idx]),
                'method': 'PELT_OFFLINE_BINSEG',
                'reversal_label': 1 if rev_type == 'BUY' else 2,
            })

        # === 6. Отчёт ===
        logger.info(f"📊 Найдено {len(results)} разворотов")
        if results:
            buy_cnt = sum(1 for r in results if r['type'] == 'BUY')
            sell_cnt = sum(1 for r in results if r['type'] == 'SELL')
            avg_conf = np.mean([r['confidence'] for r in results])
            print(f"📈 Сигналы: {buy_cnt} BUY, {sell_cnt} SELL, средняя уверенность: {avg_conf:.2f}")

        return results

    def _cusum_reversals(self, df):
        """
        Генерация реверсивных сигналов по готовым полям CUSUM 5m из БД.
        Кодировка: BUY=1, SELL=2, HOLD=0 (HOLD игнорируем).
        Отбор кандидатов по |cusum_zscore| и/или cusum_conf с подтверждением.
        """
        cfg = self.config  # LabelingConfig
        out = []

        # Требуемые колонки
        need = ["ts", "cusum_state", "cusum_zscore", "cusum_conf"]
        if any(c not in df.columns for c in need):
            logger.warning("CUSUM reversals: нет нужных колонок %s", need)
            return out

        # индексы-кандидаты: сильный z или уверенность
        z = df["cusum_zscore"].astype(float)
        conf = df["cusum_conf"].astype(float)
        state = df["cusum_state"].astype("Int64")  # 1/2/0

        cand_mask = (z.abs() >= float(getattr(cfg, "cusum_z_threshold", 1.0))) | \
                    (conf >= float(getattr(cfg, "cusum_conf_threshold", 0.6)))

        idxs = df.index[cand_mask & state.isin([1, 2])]
        if len(idxs) == 0:
            return out

        for i in idxs:
            s = int(state.loc[i])
            signal_type = "BUY" if s == Direction.BUY else "SELL"
            label = Direction.BUY if s == Direction.BUY else Direction.SELL
            base_conf = float(conf.loc[i]) if pd.notna(conf.loc[i]) else 0.0
            base_conf = max(0.2, min(0.95, base_conf))  # мягкие границы

            # подтверждение
            confirm = self._smart_confirmation_system(df, i, signal_type)

            # Создаем полную структуру сигнала с ВСЕМИ необходимыми полями
            signal_data = {
                "index": int(i),
                "type": signal_type,  # "BUY"/"SELL"
                "confidence": max(0.0, min(0.99, base_conf + float(confirm.get("confidence_boost", 0.0)))),
                "extreme_timestamp": int(df["ts"].loc[i]) if "ts" in df.columns and pd.notna(df["ts"].loc[i]) else None,
                "confirmation_index": confirm['confirmation_index'],
                "confirmation_timestamp": int(df["ts"].iloc[confirm['confirmation_index']]) if confirm[
                                                                                                   'confirmation_index'] < len(
                    df) else None,
                "method": 'CUSUM'
            }

            out.append(signal_data)

        return out

    def _extremum_reversals(self, df: pd.DataFrame) -> List[Dict]:
        """Экстремум реверсии с последовательным связыванием и перескоком окна"""
        window = self.config.extremum_window
        confirm_bar = max(1, min(5, self.config.extremum_confirm_bar))
        min_distance = getattr(self.config, 'min_signal_distance', 10)
        low = df['low'].values
        high = df['high'].values
        results = []

        # Начинаем поиск с начала окна
        i = window
        last_extremum_type = None  # 'BUY' или 'SELL'

        while i < len(df) - window:
            current_low = low[i]
            current_high = high[i]

            # Проверяем на минимум в окне
            is_low_extreme = current_low == np.min(low[i - window:i + window + 1])
            # Проверяем на максимум в окне
            is_high_extreme = current_high == np.max(high[i - window:i + window + 1])

            signal_type = None

            # Определяем тип сигнала с учетом последовательности
            if is_low_extreme and last_extremum_type != 'BUY':
                signal_type = 'BUY'
            elif is_high_extreme and last_extremum_type != 'SELL':
                signal_type = 'SELL'

            # Если нашли подходящий экстремум
            if signal_type and i + confirm_bar < len(df):
                confirmation = self._smart_confirmation_system(df, i, signal_type)
                if not confirmation['early_rejection']:
                    results.append({
                        'index': i,
                        'type': signal_type,
                        'confidence': 0.7 + confirmation['confidence_boost'],
                        'extreme_timestamp': df['ts'].iloc[i],
                        'confirmation_index': confirmation['confirmation_index'],
                        'confirmation_timestamp': df['ts'].iloc[confirmation['confirmation_index']],
                        'method': 'EXTREMUM'
                    })
                    # Обновляем состояние и перескакиваем окно
                    last_extremum_type = signal_type
                    i += min_distance  # Перескакиваем на min_distance вперед
                    continue  # Пропускаем обычное увеличение i

            # Если экстремум не найден или не прошел подтверждение - двигаемся на 1 бар
            i += 1

        return results

    def _cusum_extremum_hybrid(self, df):
        """
        Гибрид CUSUM (из БД) + экстремумы. Тип должен совпадать, расстояние по индексам ≤ 2.
        Уверенность — среднее двух сигналов (или по вашей текущей формуле).
        """
        cusum_signals = self._cusum_reversals(df)
        extremum_signals = self._extremum_reversals(df)

        if not cusum_signals:
            return extremum_signals
        if not extremum_signals:
            return cusum_signals

        # быстрый мап по типу
        by_type = {"BUY": [], "SELL": []}
        for s in cusum_signals:
            by_type[s["type"]].append(s)

        out = []
        for e in extremum_signals:
            group = by_type.get(e["type"], [])
            # находим ближайший по индексу из CUSUM (порог 2 бара)
            best = None
            best_d = None
            for c in group:
                d = abs(int(c["index"]) - int(e["index"]))
                if d <= 2 and (best is None or d < best_d):
                    best, best_d = c, d
            if best is not None:
                # объединяем
                conf = (float(best["confidence"]) + float(e.get("confidence", 0.0))) / 2.0
                merged = e.copy()  # Используем копию extremum сигнала как основу
                merged["confidence"] = max(0.0, min(0.99, conf))
                # Сохраняем все необходимые поля из extremum сигнала
                out.append(merged)
            else:
                out.append(e)

        return out

    # =========================================================================
    # ВОССТАНОВЛЕННЫЕ МЕТОДЫ
    # =========================================================================

    def advanced_quality_analysis(self) -> Dict[str, Any]:
        """
        Расширенный анализ качества разметки с общим PNL+ и PNL-
        """
        logger.info("🔍 Запуск расширенного анализа качества...")

        # ⬇️ ГАРАНТИРУЕМ что всегда возвращается dict
        default_result = {
            'methods_performance': [],
            'total_metrics': {},
            'best_method': {'method': 'N/A', 'success_rate': 0.0},
            'data_quality_issues': [],
            'timestamp': datetime.now().isoformat()
        }

        try:
            # ⬇️ ОБНОВЛЕННЫЙ ЗАПРОС: добавляем суммы PNL+ и PNL-
            query = """
                SELECT 
                    labeling_method,
                    reversal_label,
                    COUNT(*) as total_signals,
                    AVG(reversal_confidence) as avg_confidence,
                    AVG(price_change_after) as avg_profit,
                    SUM(price_change_after) as total_pnl,
                    -- ⬇️ ДОБАВЛЯЕМ СУММЫ ДЛЯ PNL+ И PNL-
                    SUM(CASE WHEN price_change_after > 0 THEN price_change_after ELSE 0 END) as total_positive_pnl,
                    SUM(CASE WHEN price_change_after < 0 THEN price_change_after ELSE 0 END) as total_negative_pnl,
                    SUM(CASE WHEN price_change_after >= :min_profit THEN 1 ELSE 0 END) as profitable_signals,
                    SUM(CASE WHEN price_change_after < -:min_profit THEN 1 ELSE 0 END) as loss_signals,
                    MIN(price_change_after) as min_profit,
                    MAX(price_change_after) as max_profit
                FROM labeling_results 
                WHERE symbol = :symbol
                GROUP BY labeling_method, reversal_label
            """

            df_quality = pd.read_sql_query(
                query,
                self.engine,
                params={
                    'min_profit': self.config.min_profit_target,
                    'symbol': self.config.symbol
                }
            )

            if df_quality.empty:
                logger.warning("❌ Нет данных для анализа качества")
                return default_result

            # === ВАЛИДАЦИЯ МЕТОК С PNL=0 ===
            validation_warnings = []

            # Загружаем все метки для валидации
            all_labels_query = """
                SELECT 
                    extreme_timestamp,
                    reversal_label,
                    price_change_after,
                    labeling_method
                FROM labeling_results 
                WHERE symbol = :symbol
                ORDER BY extreme_timestamp
            """

            df_all_labels = pd.read_sql_query(
                all_labels_query,
                self.engine,
                params={'symbol': self.config.symbol}
            )

            validated_zero_pnl = 0
            invalid_zero_pnl = 0

            # Проверяем только BUY/SELL метки
            for idx, row in df_all_labels[df_all_labels['reversal_label'].isin([1, 2])].iterrows():
                if row['price_change_after'] == 0.0:
                    # Ищем следующую метку
                    next_labels = df_all_labels[df_all_labels['extreme_timestamp'] > row['extreme_timestamp']]

                    if next_labels.empty:
                        validation_warnings.append({
                            'timestamp': row['extreme_timestamp'],
                            'label': row['reversal_label'],
                            'method': row['labeling_method'],
                            'issue': 'Нет следующей метки'
                        })
                        invalid_zero_pnl += 1
                    else:
                        next_label = next_labels.iloc[0]
                        if next_label['reversal_label'] != 0:
                            validation_warnings.append({
                                'timestamp': row['extreme_timestamp'],
                                'label': row['reversal_label'],
                                'method': row['labeling_method'],
                                'issue': f'Следующая метка не HOLD (reversal_label={next_label["reversal_label"]})'
                            })
                            invalid_zero_pnl += 1
                        else:
                            validated_zero_pnl += 1

            analysis = {
                'methods_performance': df_quality.to_dict('records'),
                'total_metrics': self._calculate_total_metrics(df_quality),
                'best_method': self._find_best_method(df_quality),
                'data_quality_issues': self._detect_data_quality_issues(),
                'validation': {
                    'validated_zero_pnl': validated_zero_pnl,
                    'invalid_zero_pnl': invalid_zero_pnl,
                    'warnings': validation_warnings
                },
                'timestamp': datetime.now().isoformat()
            }

            self._log_quality_analysis(analysis)

            # Вывод результатов валидации
            if validated_zero_pnl > 0:
                print(f"\n✅ Валидировано меток с PnL=0: {validated_zero_pnl}")

            if validation_warnings:
                print(f"\n⚠️  WARNINGS: Найдено {len(validation_warnings)} некорректных меток с PnL=0:")
                for w in validation_warnings[:10]:  # показываем первые 10
                    label_str = 'BUY' if w['label'] == 1 else 'SELL'
                    print(f"   • ts={w['timestamp']} | {label_str} | {w['method']} | {w['issue']}")
                if len(validation_warnings) > 10:
                    print(f"   ... и еще {len(validation_warnings) - 10} warnings")

            return analysis

        except Exception as err:
            logger.error(f"Ошибка расширенного анализа: {err}")
            return default_result

    def _calculate_total_metrics(self, df_quality: pd.DataFrame) -> Dict[str, float]:
        """Расчет общих метрик с PNL+ и PNL-"""
        if df_quality.empty:
            return {}

        total_profitable = df_quality['profitable_signals'].sum()
        total_signals = df_quality['total_signals'].sum()
        overall_success = total_profitable / total_signals if total_signals > 0 else 0

        # ⬇️ ДОБАВЛЯЕМ РАСЧЕТ PNL+ И PNL-
        total_positive_pnl = df_quality['total_positive_pnl'].sum()
        total_negative_pnl = df_quality['total_negative_pnl'].sum()
        total_pnl = df_quality['total_pnl'].sum()

        return {
            'total_signals': int(total_signals),
            'profitable_signals': int(total_profitable),
            'success_rate': float(overall_success),
            'avg_confidence': float(df_quality['avg_confidence'].mean()),
            'avg_profit': float(df_quality['avg_profit'].mean()),
            'total_pnl': float(total_pnl),
            # ⬇️ НОВЫЕ МЕТРИКИ
            'total_positive_pnl': float(total_positive_pnl),
            'total_negative_pnl': float(total_negative_pnl),
            'pnl_ratio': abs(total_positive_pnl / total_negative_pnl) if total_negative_pnl != 0 else float('inf')
        }

    def _find_best_method(self, df_quality: pd.DataFrame) -> Dict[str, Any]:
        """Поиск лучшего метода - ВСПОМОГАТЕЛЬНЫЙ МЕТОД"""
        if df_quality.empty:
            return {'method': 'N/A', 'success_rate': 0.0}

        # Используем правильное имя колонки из SQL-запроса
        if 'labeling_method' not in df_quality.columns:
            # Если колонка называется иначе, используем первую доступную
            method_col = df_quality.columns[0] if len(df_quality.columns) > 0 else 'method'
        else:
            method_col = 'labeling_method'

        profitable_methods = df_quality[df_quality['profitable_signals'] > 0].copy()

        if profitable_methods.empty:
            return {'method': 'N/A', 'success_rate': 0.0}

        profitable_methods['success_rate'] = (
                profitable_methods['profitable_signals'] / profitable_methods['total_signals']
        )

        best_idx = profitable_methods['success_rate'].idxmax()
        best_method = profitable_methods.loc[best_idx]

        return {
            'method': best_method[method_col],
            'success_rate': float(best_method['success_rate']),
            'total_signals': int(best_method['total_signals']),
            'avg_profit': float(best_method['avg_profit'])
        }

    def configure_settings(self):
        """Настройка параметров - базовая реализация"""
        print("\n⚙️  НАСТРОЙКА ПАРАМЕТРОВ:")
        print("Доступные параметры:")
        print(f"1. Метод разметки: {self.config.method}")
        print(f"2. Min profit target: {self.config.min_profit_target}")
        print(f"3. Hold bars: {self.config.hold_bars}")
        print("Реализация настройки параметров требует дополнительной разработки")

    def show_stats(self):
        """Показать статистику - базовая реализация"""
        stats = self.data_loader.get_data_stats()
        print("\n📊 СТАТИСТИКА ДАННЫХ:")
        for key, value in stats.items():
            print(f"   {key}: {value}")

    def _detect_data_quality_issues(self) -> List[str]:
        """Обнаружение проблем качества данных - SQLAlchemy версия"""
        issues = []

        try:
            # Используем self.engine вместо self.conn
            # Проверка на дубликаты меток
            query_duplicates = """
                SELECT timestamp, COUNT(*) as cnt 
                FROM labeling_results 
                WHERE symbol = :symbol
                GROUP BY timestamp 
                HAVING COUNT(*) > 1
            """
            duplicates = pd.read_sql_query(
                query_duplicates,
                self.engine,
                params={'symbol': self.config.symbol}
            )
            if not duplicates.empty:
                issues.append(f"Обнаружены дубликаты меток: {len(duplicates)} случаев")

            # Остальные проверки аналогично исправляем...
            # ...

        except Exception as err:
            issues.append(f"Ошибка при проверке качества: {err}")

        return issues

    def _log_quality_analysis(self, analysis: Dict[str, Any]):
        """Логирование анализа качества с корректным форматированием процентов"""
        logger.info("\n" + "=" * 60)
        logger.info("📊 РАСШИРЕННЫЙ АНАЛИЗ КАЧЕСТВА")
        logger.info("=" * 60)

        total_metrics = analysis.get('total_metrics', {})
        best_method = analysis.get('best_method', {})
        issues = analysis.get('data_quality_issues', [])

        total_pnl_value = total_metrics.get('total_pnl', 0)
        total_positive_pnl = total_metrics.get('total_positive_pnl', 0)
        total_negative_pnl = total_metrics.get('total_negative_pnl', 0)
        pnl_ratio = total_metrics.get('pnl_ratio', 0)
        avg_profit = total_metrics.get('avg_profit', 0)

        # ⬇️ ИСПРАВЛЕНО: умножаем на 100 для процентов
        logger.info(f"📈 Общая успешность: {total_metrics.get('success_rate', 0):.1%}")
        logger.info(f"💰 Общий PNL+: {total_positive_pnl:+.3f} ({total_positive_pnl * 100:+.1f}%)")
        logger.info(f"💸 Общий PNL-: {total_negative_pnl:+.3f} ({total_negative_pnl * 100:+.1f}%)")
        logger.info(f"📊 Соотношение PNL: {pnl_ratio:.1f}:1" if pnl_ratio != float(
            'inf') else "📊 Соотношение PNL: ∞ (нет убытков)")
        logger.info(f"🏆 Лучший метод: {best_method.get('method', 'N/A')} ({best_method.get('success_rate', 0):.1%})")
        logger.info(f"📊 Всего сигналов: {total_metrics.get('total_signals', 0)}")
        logger.info(f"💵 Средний PnL: {avg_profit:.4f} ({avg_profit:.1%})")
        logger.info(f"🎯 Совокупный PnL: {total_pnl_value:+.3f} ({total_pnl_value * 100:+.1f}%)")

        if issues:
            logger.warning("⚠️ Обнаруженные проблемы:")
            for issue in issues:
                logger.warning(f"   • {issue}")
        else:
            logger.info("✅ Критических проблем не обнаружено")

        # ⬇️ ИСПРАВЛЕННЫЙ КОНСОЛЬНЫЙ ВЫВОД
        print(f"\n🎯 ИТОГИ АНАЛИЗА:")
        print(f"   📈 Успешность: {total_metrics.get('success_rate', 0):.1%} сделок")
        print(f"   💰 Прибыль: {total_positive_pnl:+.3f} ({total_positive_pnl * 100:+.1f}%)")
        print(f"   💸 Убытки: {total_negative_pnl:+.3f} ({total_negative_pnl * 100:+.1f}%)")
        print(f"   📊 Чистая прибыль: {total_pnl_value:+.3f} ({total_pnl_value * 100:+.1f}%)")

        if pnl_ratio > 2:
            print(f"   ✅ Соотношение: {pnl_ratio:.1f}:1 (отличное)")
        elif pnl_ratio > 1:
            print(f"   ⚠️  Соотношение: {pnl_ratio:.1f}:1 (удовлетворительное)")
        else:
            print(f"   ❌ Соотношение: {pnl_ratio:.1f}:1 (требует улучшения)")

    def export_feature_importance(self, *args, run_id: str | None = None, model_name: str = "unknown",
                                  top_n: int | None = None, **kwargs) -> int:
        """
        Сохраняет важность признаков в SQLite (таблица training_feature_importance).
        Поддерживает гибкие входы:
          • export_feature_importance(df, ...)            # df с колонками ['feature','importance'] или индекс=feature
          • export_feature_importance(series, ...)        # pandas.Series: index=feature, values=importance
          • export_feature_importance(dict, ...)          # dict: feature -> importance
          • export_feature_importance(list_of_tuples, ...)# [(feature, importance), ...]
        Параметры:
          run_id    — к какому снапшоту относится важность (обязательно для консистентности; если None, берём последний READY по symbol)
          model_name— имя/идентификатор модели (например, 'lgbm_v1')
          top_n     — при задании сохраняем только top-N по важности
        Возвращает: число сохранённых строк.
        """


        # 0) ensure DDL
        self._ensure_training_snapshot_tables()

        # 1) определить run_id, если не передан
        if run_id is None:
            with self.engine.begin() as conn:
                row = conn.execute(text("""
                    SELECT run_id
                      FROM training_dataset_meta
                     WHERE status='READY' AND symbol=:symbol
                  ORDER BY created_at DESC
                     LIMIT 1
                """), {"symbol": self.config.symbol}).mappings().first()
            if not row:
                raise RuntimeError("Нет готового снапшота (status=READY). Укажите run_id вручную.")
            run_id = row["run_id"]

        # 2) извлечь входные данные importance
        importance_obj = None
        if args:
            importance_obj = args[0]
        elif "importance" in kwargs:
            importance_obj = kwargs["importance"]
        elif "df" in kwargs:
            importance_obj = kwargs["df"]

        if importance_obj is None:
            raise ValueError("Не переданы данные важности фич (df/series/dict/list).")

        # 3) нормализация входа -> DataFrame с колонками ['feature','importance']
        if isinstance(importance_obj, pd.DataFrame):
            df_imp = importance_obj.copy()
            # допускаем разные варианты имен
            if "feature" not in df_imp.columns or "importance" not in df_imp.columns:
                if df_imp.shape[1] == 1:  # одна колонка важностей, index = feature
                    df_imp = df_imp.reset_index()
                    df_imp.columns = ["feature", "importance"]
                elif df_imp.shape[1] >= 2:
                    # пробуем первые две как feature/importance
                    cols = list(df_imp.columns)
                    df_imp = df_imp[[cols[0], cols[1]]].copy()
                    df_imp.columns = ["feature", "importance"]
        elif hasattr(importance_obj, "to_frame"):  # Series
            s = importance_obj
            df_imp = s.to_frame(name="importance").reset_index()
            if df_imp.columns[0] != "feature":
                df_imp.columns = ["feature", "importance"]
        elif isinstance(importance_obj, dict):
            df_imp = pd.DataFrame(list(importance_obj.items()), columns=["feature", "importance"])
        elif isinstance(importance_obj, (list, tuple)):
            df_imp = pd.DataFrame(importance_obj, columns=["feature", "importance"])
        else:
            raise TypeError(f"Неподдерживаемый тип входа для важности фич: {type(importance_obj)}")

        # убрать NaN/inf и привести типы
        df_imp = df_imp.dropna(subset=["feature"]).copy()
        df_imp["importance"] = pd.to_numeric(df_imp["importance"], errors="coerce")
        df_imp = df_imp.replace([np.inf, -np.inf], np.nan).dropna(subset=["importance"])

        if df_imp.empty:
            raise ValueError("Таблица важности фич пуста после нормализации входных данных.")

        # нормировать на сумму=1 (общепринято для интерпретации)
        ssum = float(df_imp["importance"].sum())
        if ssum > 0:
            df_imp["importance"] = df_imp["importance"] / ssum

        # сортировка и топ-N
        df_imp = df_imp.sort_values("importance", ascending=False).reset_index(drop=True)
        if top_n is not None and top_n > 0:
            df_imp = df_imp.head(int(top_n))

        df_imp["rank"] = np.arange(1, len(df_imp) + 1, dtype=int)
        created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        df_imp["run_id"] = run_id
        df_imp["model_name"] = str(model_name)
        df_imp["created_at"] = created_at

        # порядок колонок для записи
        out_cols = ["run_id", "model_name", "feature", "importance", "rank", "created_at"]
        df_out = df_imp[out_cols].copy()

        # 4) сохранить в БД (upsert по PRIMARY KEY)
        inserted = 0
        with self.engine.begin() as conn:
            # удалим предыдущие записи для (run_id, model_name), чтобы не копить ряды при повторном экспорте
            conn.execute(text("""
                DELETE FROM training_feature_importance
                 WHERE run_id=:rid AND model_name=:mname
            """), {"rid": run_id, "mname": model_name})

            df_out.to_sql("training_feature_importance", self.engine, if_exists="append", index=False)
            inserted = len(df_out)

        logger.info("✅ Важность фич сохранена: run_id=%s, model=%s, rows=%d", run_id, model_name, inserted)
        # для удобства — печать top-10
        preview = df_out.sort_values("rank").head(10)
        print("\n🏷️ TOP важностей (до 10 строк):")
        for _, r in preview.iterrows():
            print(f"  {r['rank']:>2}. {r['feature']:<30} {r['importance']:.4f}")

        return inserted

    def cross_validation_split(self, n_splits: int = 5, test_size: float = 0.2) -> Dict[str, Any]:
        """
        Временная кросс-валидация БЕЗ генерации негативов.
        Датасет формируется как при снапшоте:
          - метки из labeling_results (reversal_label ∈ {0,1,2})
          - фичи из candles_* (load_indicators)
          - жёсткая валидация (_validate_snapshot_frame)
        Разбиение: time-based, без утечки будущего.
        """
        import pandas as pd
        from collections import Counter
        import traceback
        logger.info(f"🎯 Создание {n_splits}-fold кросс-валидации...")

        try:
            # 1) Сборка датасета как в снапшоте
            raw_df, _meta = self._build_training_snapshot_dataframe()

            # 2) Жёсткая валидация/очистка
            df_clean, issues, nan_drop_rows, duplicates_count = self._validate_snapshot_frame(raw_df)
            if df_clean.empty:
                raise ValueError("Датасет пуст после валидации")

            dataset = df_clean.reset_index(drop=True)

            # 3) Временная метка
            if "ts" in dataset.columns:
                try:
                    # Пробуем миллисекунды (стандарт для Binance)
                    dataset["datetime"] = pd.to_datetime(dataset["ts"], unit="ms", utc=True)
                except (pd.errors.OutOfBoundsDatetime, OverflowError):
                    try:
                        # Если не сработало - пробуем секунды
                        dataset["datetime"] = pd.to_datetime(dataset["ts"], unit="s", utc=True)
                    except (pd.errors.OutOfBoundsDatetime, OverflowError):
                        # Крайний случай - синтетическая шкала
                        logger.warning("⚠️ Не удалось конвертировать ts, используем синтетическую шкалу")
                        dataset["datetime"] = pd.date_range(start="2020-01-01", periods=len(dataset), freq="5min",
                                                            tz="UTC")
            elif "datetime" not in dataset.columns:
                dataset["datetime"] = pd.date_range(start="2020-01-01", periods=len(dataset), freq="5min", tz="UTC")
            # 4) Сортировка по времени
            dataset = dataset.sort_values("datetime").reset_index(drop=True)

            total_samples = len(dataset)
            if not (0 < test_size < 1):
                raise ValueError("test_size должен быть в (0,1)")
            test_samples = max(1, int(total_samples * test_size))

            # sanity-check на объём
            if total_samples < max(n_splits * 10, n_splits + test_samples):
                raise ValueError(
                    f"Недостаточно данных: {total_samples} samples для {n_splits} фолдов (test_size={test_size})")

            logger.info(f"📊 Датасет для CV: {total_samples} строк")

            # 5) Формируем временные фолды (blocked, без перемешивания)
            splits = []
            # шаг смещения окна так, чтобы мы получили n_splits фолдов
            step = (total_samples - test_samples) // n_splits
            step = max(step, 1)

            for i in range(n_splits):
                start_idx = i * step
                end_idx = start_idx + test_samples
                if end_idx > total_samples:
                    end_idx = total_samples
                    start_idx = max(0, end_idx - test_samples)

                test_indices = list(range(start_idx, end_idx))
                train_indices = list(range(0, start_idx)) + list(range(end_idx, total_samples))

                # на всякий: без пересечений и покрытие всех индексов
                if len(set(train_indices).intersection(test_indices)) != 0:
                    logger.warning(f"⚠️ Пересечение индексов в фолде {i + 1}")
                if len(set(train_indices + test_indices)) != total_samples:
                    logger.warning(f"⚠️ Неполное покрытие индексов в фолде {i + 1}")

                split_info = {
                    "fold": i + 1,
                    "train_indices": train_indices,
                    "test_indices": test_indices,
                    "train_size": len(train_indices),
                    "test_size": len(test_indices),
                    "train_period": (
                        f"{dataset.iloc[train_indices[0]]['datetime']} → {dataset.iloc[train_indices[-1]]['datetime']}"
                        if train_indices else "N/A"
                    ),
                    "test_period": (
                        f"{dataset.iloc[test_indices[0]]['datetime']} → {dataset.iloc[test_indices[-1]]['datetime']}"
                        if test_indices else "N/A"
                    ),
                }
                splits.append(split_info)

            result = {
                "n_splits": n_splits,
                "test_size": test_size,
                "total_samples": total_samples,
                "splits": splits,
                "class_distribution": dict(Counter(dataset["reversal_label"])),
            }

            logger.info(f"✅ Кросс-валидация создана: {n_splits} фолдов, {total_samples} samples")
            print("\n✅ Кросс-валидация создана успешно!")
            print(f"📊 Всего samples: {total_samples}")
            print(f"🎯 Фолдов: {n_splits}")
            print(f"📈 Распределение классов: {result['class_distribution']}")
            return result

        except Exception as err:
            logger.error(f"❌ Ошибка создания кросс-валидации: {err}")
            logger.error(f"🔍 Детали ошибки: {traceback.format_exc()}")
            return {}

    def detect_label_leakage(self) -> Dict[str, Any]:
        """
        Обнаружение утечки меток — с расширенной диагностикой
        """
        logger.info("🔍 Проверка на утечку меток...")

        duplicate_pairs = []
        high_corr_features = []
        issues = []

        try:
            df_positives = self.data_loader.load_labeled_data()

            if df_positives.empty:
                return {
                    'leakage_detected': False,
                    'issues': ['Нет размеченных меток для анализа'],
                    'high_corr_features': [],
                    'duplicate_feature_pairs': [],
                    'total_positives': 0
                }

            logger.info(f"📊 Анализируем {len(df_positives)} размеченных примеров")

            # РАСШИРЕННАЯ ДИАГНОСТИКА
            print(f"\n📈 ДЕТАЛЬНАЯ СТАТИСТИКА МЕТОК:")
            print(f"   • Всего меток: {len(df_positives)}")
            print(f"   • BUY (1): {len(df_positives[df_positives['reversal_label'] == 1])}")
            print(f"   • SELL (2): {len(df_positives[df_positives['reversal_label'] == 2])}")

            # Анализ по методам разметки
            if 'method' in df_positives.columns:
                method_stats = df_positives['method'].value_counts()
                print(f"   • По методам: {method_stats.to_dict()}")

            # Анализ качества меток
            if 'pnl' in df_positives.columns:
                avg_pnl = df_positives['pnl'].mean()
                profitable = len(df_positives[df_positives['pnl'] > 0])
                success_rate = profitable / len(df_positives)
                print(f"   • Успешность: {success_rate:.1%} ({profitable}/{len(df_positives)})")
                print(f"   • Средний PnL: {avg_pnl:.4f}")

            # Проверка 1: Высокая корреляция фич с меткой
            available_features = [col for col in self.data_loader.feature_names if col in df_positives.columns]

            print(f"\n🔍 АНАЛИЗ ФИЧ ({len(available_features)} доступно):")

            feature_correlations = []
            for feature in available_features:
                if df_positives[feature].dtype not in [np.float64, np.int64]:
                    continue

                clean_data = df_positives[[feature, 'reversal_label']].dropna()
                if len(clean_data) < 10:  # Минимум 10 samples
                    continue

                try:
                    correlation = clean_data[feature].corr(clean_data['reversal_label'])
                    if not np.isnan(correlation):
                        feature_correlations.append((feature, abs(correlation)))

                        if abs(correlation) > 0.8:
                            high_corr_features.append((feature, correlation))
                except:
                    continue

            # Сортируем фичи по корреляции
            feature_correlations.sort(key=lambda x: x[1], reverse=True)

            # Показываем топ-5 самых коррелированных фич
            if feature_correlations:
                print("   🏆 Топ-5 фич по корреляции с меткой:")
                for feature, corr in feature_correlations[:5]:
                    leak_warning = " ⚠️ УТЕЧКА!" if abs(corr) > 0.8 else ""
                    print(f"      • {feature}: {corr:.4f}{leak_warning}")

            if high_corr_features:
                issues.append(f"Очень высокая корреляция с меткой: {len(high_corr_features)} фич")
                for feature, corr in high_corr_features:
                    logger.warning(f"   ⚠️ {feature}: {corr:.4f}")

            # Проверка 2: Дублирующиеся фичи
            if len(available_features) > 1:
                try:
                    # Используем только числовые фичи
                    numeric_features = [f for f in available_features
                                        if df_positives[f].dtype in [np.float64, np.int64]]

                    if len(numeric_features) > 1:
                        corr_matrix = self.data_loader.safe_correlation_calculation(df_positives, numeric_features)

                        for i in range(len(corr_matrix.columns)):
                            for j in range(i + 1, len(corr_matrix.columns)):
                                if corr_matrix.iloc[i, j] > 0.95:
                                    duplicate_pairs.append((
                                        corr_matrix.columns[i],
                                        corr_matrix.columns[j],
                                        corr_matrix.iloc[i, j]
                                    ))

                        if duplicate_pairs:
                            issues.append(f"Дублирующиеся фичи: {len(duplicate_pairs)} пар")
                            print(f"\n🔁 ДУБЛИРУЮЩИЕСЯ ФИЧИ:")
                            for pair in duplicate_pairs[:3]:  # Показываем только первые 3
                                print(f"   • {pair[0]} ≈ {pair[1]} (corr={pair[2]:.3f})")
                except Exception as corr_err:
                    issues.append(f"Ошибка расчета корреляций: {corr_err}")

            leakage_detected = len(high_corr_features) > 0

            result = {
                'leakage_detected': leakage_detected,
                'issues': issues,
                'high_corr_features': high_corr_features,
                'duplicate_feature_pairs': duplicate_pairs,
                'total_positives': len(df_positives),
                'available_features': len(available_features),
                'feature_correlations': feature_correlations[:10]  # Топ-10 фич
            }

            if leakage_detected:
                logger.warning("⚠️ Обнаружена потенциальная утечка меток!")
                print("\n❌ ВЫВОД: Обнаружена утечка меток!")
            else:
                logger.info("✅ Утечка меток не обнаружена")
                print("\n✅ ВЫВОД: Утечка меток не обнаружена")

            return result

        except Exception as err:
            logger.error(f"❌ Ошибка проверки утечки: {err}")
            return {
                'leakage_detected': False,
                'issues': [f'Ошибка при анализе: {err}'],
                'high_corr_features': [],
                'duplicate_feature_pairs': [],
                'total_positives': 0
            }

    # =========================================================================
    # СУЩЕСТВУЮЩИЕ МЕТОДЫ ИЗ ИСХОДНОГО КОДА (адаптированные)
    # =========================================================================

    def save_to_db(self, results: List[Dict]):
        """Сохранение результатов в БД через SQLAlchemy - УЛУЧШЕННАЯ ОБРАБОТКА ОШИБОК"""
        from sqlalchemy import text

        insert_sql = text("""
            INSERT OR REPLACE INTO labeling_results 
            (symbol, timestamp, timeframe, reversal_label, reversal_confidence,
             labeling_method, labeling_params, extreme_index, extreme_price, extreme_timestamp,
             confirmation_index, confirmation_timestamp, price_change_after, features_json,
             is_high_quality, created_at)
            VALUES (:symbol, :timestamp, :timeframe, :reversal_label, :reversal_confidence,
                    :labeling_method, :labeling_params, :extreme_index, :extreme_price, :extreme_timestamp,
                    :confirmation_index, :confirmation_timestamp, :price_change_after, :features_json,
                    :is_high_quality, CURRENT_TIMESTAMP)
        """)

        successful_saves = 0
        with self.engine.connect() as conn:
            for res in results:
                try:
                    #   гарантируем что timestamp будут INTEGER
                    timestamp = int(res.get('timestamp', 0))
                    extreme_timestamp = int(res.get('extreme_timestamp', 0))
                    confirmation_timestamp = int(res.get('confirmation_timestamp', 0))

                    #   проверяем обязательные поля
                    if timestamp == 0 or extreme_timestamp == 0:
                        logger.warning(f"⚠️ Пропуск записи с некорректными timestamp: {res.get('symbol')}")
                        continue

                    conn.execute(insert_sql, {
                        'symbol': res['symbol'],
                        'timestamp': timestamp,
                        'timeframe': res['timeframe'],
                        'reversal_label': res['reversal_label'],
                        'reversal_confidence': res.get('reversal_confidence', 1.0),
                        'labeling_method': res['labeling_method'],
                        'labeling_params': json.dumps(res.get('labeling_params', {})),
                        'extreme_index': res.get('extreme_index'),
                        'extreme_price': res.get('extreme_price'),
                        'extreme_timestamp': extreme_timestamp,
                        'confirmation_index': res.get('confirmation_index'),
                        'confirmation_timestamp': confirmation_timestamp,
                        'price_change_after': res.get('price_change_after'),
                        'features_json': res.get('features_json'),
                        'is_high_quality': res.get('is_high_quality', 1)
                    })
                    successful_saves += 1

                except Exception as row_err:
                    logger.error(f"❌ Ошибка сохранения записи {res.get('symbol', 'N/A')}: {row_err}")
                    continue

            conn.commit()

        logger.info(f"✅ Сохранено {successful_saves}/{len(results)} меток через SQLAlchemy")
        if successful_saves < len(results):
            logger.warning(f"⚠️ Не сохранено {len(results) - successful_saves} меток из-за ошибок")

    def manual_mode(self):
        """Ручная разметка - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
        df = self.load_data()
        print("\n=== РУЧНАЯ РАЗМЕТКА ===")
        print("Формат: <индекс>,<BUY/SELL>. 'done' для завершения.")

        while True:
            user_input = input(">> ").strip()
            if user_input.lower() == 'done':
                break
            if ',' not in user_input:
                print("Формат: индекс,тип")
                continue
            try:
                idx_str, typ = user_input.split(',')
                idx = int(idx_str.strip())
                typ = typ.strip().upper()
                if typ not in ['BUY', 'SELL']:
                    print("Только BUY или SELL")
                    continue
                if idx < 0 or idx >= len(df):
                    print(f"0 ≤ индекс < {len(df)}")
                    continue

                exit_idx = min(idx + self.config.hold_bars, len(df) - 1)
                pnl, is_profitable = self._calculate_pnl_to_index(df, idx, typ, exit_idx)
                print(f"PnL: {pnl:.4f} (требуется: ≥{self.config.min_profit_target:.4f})")

                if not is_profitable:
                    print(f"⚠️  Метка не достигла profit target! Все равно сохранить? (y/n)")
                    confirm = input(">> ").strip().lower()
                    if confirm != 'y':
                        continue

                label = 1 if typ == 'BUY' else 2

                row_dict = df.iloc[idx].to_dict()
                for k, v in row_dict.items():
                    if pd.isna(v):
                        row_dict[k] = None
                    elif isinstance(v, pd.Timestamp):
                        row_dict[k] = v.isoformat()
                    elif isinstance(v, (np.integer, np.int64)):
                        row_dict[k] = int(v)
                    elif isinstance(v, (np.floating, np.float64)):
                        row_dict[k] = float(v)
                    elif isinstance(v, str):
                        row_dict[k] = v

                timestamp = int(df['ts'].iloc[idx])

                result = {
                    'symbol': self.config.symbol,
                    'timestamp': timestamp,  # ← INTEGER
                    'timeframe': self.config.timeframe,
                    'reversal_label': label,
                    'reversal_confidence': 1.0,
                    'labeling_method': 'MANUAL',
                    'extreme_index': idx,
                    'extreme_price': df['close'].iloc[idx],
                    'extreme_timestamp': timestamp,  # ← INTEGER
                    'confirmation_index': idx,
                    'confirmation_timestamp': timestamp,  # ← INTEGER
                    'price_change_after': pnl,
                    'features_json': json.dumps(row_dict),
                    'is_high_quality': 1 if is_profitable else 0
                }
                self.save_to_db([result])
                self.labels.append({'index': idx, 'type': typ, 'pnl': pnl})
                print(f"✅ Метка {typ} сохранена (PnL: {pnl:.4f})")
            except Exception as err:
                print(f"Ошибка: {err}")

    def analyze_pnl_distribution(self, method: str = None):
        """
        Анализ распределения PnL через гистограмму
        """
        try:
            import matplotlib.pyplot as plt
            import seaborn as sns
        except ImportError:
            print("❌ Для анализа PnL требуется matplotlib и seaborn")
            print("   Установите: pip install matplotlib seaborn")
            return

        logger.info("📊 Анализ распределения PnL...")

        try:
            # Загружаем размеченные данные
            query = """
                SELECT labeling_method, price_change_after as pnl, reversal_label
                FROM labeling_results 
                WHERE symbol = :symbol
            """

            if method:
                query += " AND labeling_method = :method"
                params = {'symbol': self.config.symbol, 'method': method}
            else:
                params = {'symbol': self.config.symbol}

            df_pnl = pd.read_sql_query(query, self.engine, params=params)

            if df_pnl.empty:
                logger.warning("❌ Нет данных для анализа PnL")
                return

            print(f"\n📈 СТАТИСТИКА PnL:")
            print(f"   • Всего сигналов: {len(df_pnl)}")
            print(f"   • Средний PnL: {df_pnl['pnl'].mean():.4f}")
            print(f"   • Медиана PnL: {df_pnl['pnl'].median():.4f}")
            print(f"   • Std PnL: {df_pnl['pnl'].std():.4f}")
            print(f"   • Min PnL: {df_pnl['pnl'].min():.4f}")
            print(f"   • Max PnL: {df_pnl['pnl'].max():.4f}")
            print(f"   • PnL > 0: {(df_pnl['pnl'] > 0).sum()} ({(df_pnl['pnl'] > 0).mean():.1%})")
            print(f"   • PnL < 0: {(df_pnl['pnl'] < 0).sum()} ({(df_pnl['pnl'] < 0).mean():.1%})")

            # Анализ по методам
            if 'labeling_method' in df_pnl.columns:
                print(f"\n📊 РАСПРЕДЕЛЕНИЕ ПО МЕТОДАМ:")
                method_stats = df_pnl.groupby('labeling_method').agg({
                    'pnl': ['count', 'mean', 'std', 'min', 'max'],
                }).round(4)
                print(method_stats)

            # Создаем гистограмму
            plt.figure(figsize=(12, 8))

            # Гистограмма 1: Общее распределение
            plt.subplot(2, 2, 1)
            plt.hist(df_pnl['pnl'], bins=50, alpha=0.7, edgecolor='black')
            plt.axvline(df_pnl['pnl'].mean(), color='red', linestyle='--', label=f'Среднее: {df_pnl["pnl"].mean():.4f}')
            plt.axvline(0, color='green', linestyle='-', label='Нулевая отметка')
            plt.xlabel('PnL')
            plt.ylabel('Частота')
            plt.title('Распределение PnL (все методы)')
            plt.legend()
            plt.grid(True, alpha=0.3)

            # Гистограмма 2: Только положительные PnL
            plt.subplot(2, 2, 2)
            positive_pnl = df_pnl[df_pnl['pnl'] > 0]['pnl']
            if len(positive_pnl) > 0:
                plt.hist(positive_pnl, bins=30, alpha=0.7, color='green', edgecolor='black')
                plt.axvline(positive_pnl.mean(), color='red', linestyle='--',
                            label=f'Среднее: {positive_pnl.mean():.4f}')
                plt.xlabel('PnL (> 0)')
                plt.ylabel('Частота')
                plt.title('Распределение положительных PnL')
                plt.legend()
                plt.grid(True, alpha=0.3)

            # Boxplot по методам
            plt.subplot(2, 2, 3)
            if 'labeling_method' in df_pnl.columns and df_pnl['labeling_method'].nunique() > 1:
                df_pnl.boxplot(column='pnl', by='labeling_method', ax=plt.gca())
                plt.title('PnL по методам разметки')
                plt.suptitle('')  # Убираем автоматический заголовок
                plt.xticks(rotation=45)

            # Кумулятивная распределение
            plt.subplot(2, 2, 4)
            sorted_pnl = np.sort(df_pnl['pnl'])
            cumulative = np.arange(1, len(sorted_pnl) + 1) / len(sorted_pnl)
            plt.plot(sorted_pnl, cumulative, linewidth=2)
            plt.axvline(0, color='green', linestyle='--', alpha=0.7, label='Нулевая отметка')
            plt.xlabel('PnL')
            plt.ylabel('Кумулятивная вероятность')
            plt.title('Кумулятивное распределение PnL')
            plt.grid(True, alpha=0.3)
            plt.legend()

            plt.tight_layout()

            # Сохраняем график
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"pnl_analysis_{self.config.symbol}_{timestamp}.png"
            plt.savefig(filename, dpi=150, bbox_inches='tight')
            plt.show()

            print(f"\n💾 График сохранен как: {filename}")

            # Детальный анализ выбросов
            self._analyze_pnl_outliers(df_pnl)

        except Exception as err:
            logger.error(f"❌ Ошибка анализа PnL: {err}")
            print(f"❌ Ошибка: {err}")

    def _analyze_pnl_outliers(self, df_pnl: pd.DataFrame):
        """Анализ выбросов в PnL"""
        print(f"\n🔍 АНАЛИЗ ВЫБРОСОВ PnL:")

        Q1 = df_pnl['pnl'].quantile(0.25)
        Q3 = df_pnl['pnl'].quantile(0.75)
        IQR = Q3 - Q1
        lower_bound = Q1 - 1.5 * IQR
        upper_bound = Q3 + 1.5 * IQR

        outliers = df_pnl[(df_pnl['pnl'] < lower_bound) | (df_pnl['pnl'] > upper_bound)]

        print(f"   • Выбросы (IQR метод): {len(outliers)} сигналов")
        print(f"   • Границы выбросов: [{lower_bound:.4f}, {upper_bound:.4f}]")

        if not outliers.empty and 'labeling_method' in outliers.columns:
            print(f"   • Выбросы по методам:")
            outlier_methods = outliers['labeling_method'].value_counts()
            for method, count in outlier_methods.items():
                print(f"      - {method}: {count} выбросов")

        # Анализ экстремальных значений
        if len(df_pnl) > 0:
            extreme_positive = df_pnl.nlargest(5, 'pnl')
            extreme_negative = df_pnl.nsmallest(5, 'pnl')

            print(f"\n📈 ТОП-5 самых прибыльных сигналов:")
            for _, row in extreme_positive.iterrows():
                method = row.get('labeling_method', 'N/A')
                print(f"   • {method}: {row['pnl']:.4f}")

            print(f"\n📉 ТОП-5 самых убыточных сигналов:")
            for _, row in extreme_negative.iterrows():
                method = row.get('labeling_method', 'N/A')
                print(f"   • {method}: {row['pnl']:.4f}")

    def quick_pnl_analysis(self):
        """Быстрый анализ PnL без графиков"""
        query = """
            SELECT labeling_method, price_change_after as pnl 
            FROM labeling_results 
            WHERE symbol = :symbol
        """

        try:
            df_pnl = pd.read_sql_query(query, self.engine, params={'symbol': self.config.symbol})

            if df_pnl.empty:
                print("❌ Нет данных для анализа")
                return

            print(f"\n📊 БЫСТРЫЙ АНАЛИЗ PnL:")
            print(f"   • Всего сигналов: {len(df_pnl)}")
            print(f"   • Средний PnL: {df_pnl['pnl'].mean():.4f}")
            print(f"   • Медиана PnL: {df_pnl['pnl'].median():.4f}")
            print(f"   • Успешность: {(df_pnl['pnl'] > 0).mean():.1%}")

            # Анализ по методам
            if 'labeling_method' in df_pnl.columns:
                print(f"\n📈 ПО МЕТОДАМ:")
                for method in df_pnl['labeling_method'].unique():
                    method_data = df_pnl[df_pnl['labeling_method'] == method]
                    success_rate = (method_data['pnl'] > 0).mean()
                    avg_pnl = method_data['pnl'].mean()
                    print(
                        f"   • {method}: {len(method_data)} сигналов, успешность: {success_rate:.1%}, средний PnL: {avg_pnl:.4f}")

        except Exception as err:
            print(f"❌ Ошибка быстрого анализа PnL: {err}")

    def clear_labeling_table(self, confirmation_required: bool = True):
        """
        Очистка таблицы с разметками
        """
        try:
            if confirmation_required:
                # Показываем статистику перед очисткой
                stats_query = """
                    SELECT labeling_method, COUNT(*) as count 
                    FROM labeling_results 
                    WHERE symbol = :symbol 
                    GROUP BY labeling_method
                """
                stats = pd.read_sql_query(stats_query, self.engine, params={'symbol': self.config.symbol})

                if stats.empty:
                    print("✅ Таблица labeling_results уже пуста")
                    return

                print(f"\n⚠️  ВНИМАНИЕ: БУДЕТ УДАЛЕНО ВСЕХ МЕТОК!")
                print(f"📊 Текущая статистика для {self.config.symbol}:")
                total_count = 0
                for _, row in stats.iterrows():
                    print(f"   • {row['labeling_method']}: {row['count']} меток")
                    total_count += row['count']
                print(f"   • ВСЕГО: {total_count} меток")

                confirm = input(f"\n❓ Вы уверены? Это действие необратимо! (y/N): ").strip().lower()
                if confirm != 'y':
                    print("❌ Очистка отменена")
                    return

            # Выполняем очистку
            delete_query = "DELETE FROM labeling_results WHERE symbol = :symbol"
            with self.engine.connect() as conn:
                result = conn.execute(text(delete_query), {'symbol': self.config.symbol})
                conn.commit()

            deleted_count = result.rowcount
            logger.info(f"🧹 Очищено {deleted_count} меток для символа {self.config.symbol}")
            print(f"✅ Очищено {deleted_count} меток")

        except Exception as err:
            logger.error(f"❌ Ошибка очистки таблицы: {err}")
            print(f"❌ Ошибка: {err}")

    def clear_all_labeling_tables(self):
        """
        Очистка ВСЕХ данных разметки (все символы)
        """
        try:
            print(f"\n⚠️  КРИТИЧЕСКОЕ ВНИМАНИЕ!")
            print(f"   Будет удалена ВСЯ таблица labeling_results!")
            print(f"   Все метки для всех символов будут потеряны!")

            # Показываем общую статистику
            stats_query = "SELECT COUNT(*) as total_count FROM labeling_results"
            total_count = pd.read_sql_query(stats_query, self.engine).iloc[0]['total_count']
            print(f"   Всего меток в базе: {total_count}")

            confirm1 = input(f"\n❓ Вы абсолютно уверены? (yes/NO): ").strip().lower()
            if confirm1 != 'yes':
                print("❌ Очистка отменена")
                return

            # Очищаем всю таблицу
            delete_query = "DELETE FROM labeling_results"
            with self.engine.connect() as conn:
                result = conn.execute(text(delete_query))
                conn.commit()

            deleted_count = result.rowcount
            logger.info(f"🧹 Очищена вся таблица labeling_results: {deleted_count} меток")
            print(f"✅ Очищена вся таблица: {deleted_count} меток")

        except Exception as err:
            logger.error(f"❌ Ошибка полной очистки таблицы: {err}")
            print(f"❌ Ошибка: {err}")

    # =========================================================================
    # УЛУЧШЕННЫЕ МЕТОДЫ РАЗМЕТКИ HOLD С АНАЛИЗОМ ВОЛАТИЛЬНОСТИ И ТРЕНДА
    # =========================================================================

    def _calculate_range_metrics(self, range_bars: pd.DataFrame) -> dict:
        """
        Анализ волатильности и тренда для диапазона баров.

        Args:
            range_bars: DataFrame с барами диапазона (должен содержать 'close', опционально 'atr')

        Returns:
            dict: {
                'atr_normalized': float,  # ATR нормализованный к цене
                'price_range': float,     # движение цены в диапазоне
                'trend_strength': float,  # R² линейной регрессии (0-1)
                'volatility_level': str,  # 'LOW', 'MEDIUM', 'HIGH'
                'trend_level': str        # 'WEAK', 'MODERATE', 'STRONG'
            }
        """
        if range_bars.empty or len(range_bars) < 2:
            return {
                'atr_normalized': 0.0,
                'price_range': 0.0,
                'trend_strength': 0.0,
                'volatility_level': 'MEDIUM',
                'trend_level': 'WEAK'
            }

        close_prices = range_bars['close'].values

        # ✅ ИСПРАВЛЕНО: Проверка всех возможных имен колонок ATR (в правильном порядке!)
        atr_normalized = 0.0
        atr_candidates = ['atr_14_normalized', 'atr', 'atr_14']  # ← Ваша БД использует atr_14_normalized

        atr_col_found = None
        for col in atr_candidates:
            if col in range_bars.columns:
                atr_col_found = col
                break

        if atr_col_found:
            atr_values = range_bars[atr_col_found].dropna()
            if len(atr_values) > 0:
                atr_mean = float(atr_values.mean())

                if 'normalized' in atr_col_found.lower():
                    atr_normalized = atr_mean  # Уже нормализовано
                else:
                    # Нормализуем к цене
                    price_mean = float(range_bars['close'].mean())
                    if price_mean > 0:
                        atr_normalized = atr_mean / price_mean

        # Fallback: если ATR не найден, оцениваем через (high - low)
        elif 'high' in range_bars.columns and 'low' in range_bars.columns:
            hl_range = (range_bars['high'] - range_bars['low']).mean()
            price_mean = float(range_bars['close'].mean())
            if price_mean > 0:
                atr_normalized = float(hl_range) / price_mean

        # Движение цены (от первого до последнего бара)
        price_start = float(close_prices[0])
        price_end = float(close_prices[-1])
        price_range = abs(price_end - price_start) / price_start if price_start > 0 else 0.0

        # Сила тренда через линейную регрессию (R²)
        trend_strength = 0.0
        if len(close_prices) >= 3:
            x = np.arange(len(close_prices))

            # ✅ ИСПРАВЛЕНО: Безопасная проверка scipy
            try:
                from scipy.stats import linregress
                slope, intercept, r_value, p_value, std_err = linregress(x, close_prices)
                trend_strength = float(r_value ** 2)  # R² показывает линейность
            except ImportError:
                # Fallback: простая корреляция через numpy
                try:
                    correlation = np.corrcoef(x, close_prices)[0, 1]
                    if not np.isnan(correlation):
                        trend_strength = float(correlation ** 2)
                except Exception as e:
                    logger.debug(f"Не удалось рассчитать trend_strength: {e}")
                    trend_strength = 0.0
            except Exception as e:
                # Любые другие ошибки при расчете регрессии
                logger.debug(f"Ошибка linregress: {e}")
                try:
                    correlation = np.corrcoef(x, close_prices)[0, 1]
                    if not np.isnan(correlation):
                        trend_strength = float(correlation ** 2)
                except Exception:
                    trend_strength = 0.0

        # Классификация волатильности
        volatility_level = self._classify_volatility(atr_normalized)

        # Классификация тренда
        trend_level = self._classify_trend(trend_strength)

        return {
            'atr_normalized': atr_normalized,
            'price_range': price_range,
            'trend_strength': trend_strength,
            'volatility_level': volatility_level,
            'trend_level': trend_level
        }

    def _classify_volatility(self, atr_normalized: float) -> str:
        """
        Классифицирует волатильность на основе нормализованного ATR.

        Args:
            atr_normalized: ATR деленный на цену (или уже normalized из БД)

        Returns:
            'LOW', 'MEDIUM', или 'HIGH'
        """
        cfg = self.config

        # Пороги из конфига (или дефолтные)
        low_threshold = getattr(cfg, 'atr_low_threshold', 0.005)  # 0.5%
        high_threshold = getattr(cfg, 'atr_high_threshold', 0.015)  # 1.5%

        if atr_normalized < low_threshold:
            return 'LOW'
        elif atr_normalized > high_threshold:
            return 'HIGH'
        else:
            return 'MEDIUM'

    def _classify_trend(self, trend_strength: float) -> str:
        """
        Классифицирует силу тренда на основе R² (коэффициент детерминации).

        Args:
            trend_strength: R² от линейной регрессии (0. 0 - 1.0)

        Returns:
            'WEAK', 'MODERATE', или 'STRONG'
        """
        cfg = self.config

        # Пороги из конфига (или дефолтные)
        weak_threshold = getattr(cfg, 'trend_weak_threshold', 0.3)  # R² < 0.3
        strong_threshold = getattr(cfg, 'trend_strong_threshold', 0.7)  # R² > 0.7

        if trend_strength < weak_threshold:
            return 'WEAK'
        elif trend_strength > strong_threshold:
            return 'STRONG'
        else:
            return 'MODERATE'

    def _classify_range(self, metrics: dict, pnl: float) -> Optional[str]:
        """
        Определяет тип диапазона для разметки HOLD.

        Args:
            metrics: словарь с метриками диапазона из _calculate_range_metrics()
            pnl: PnL диапазона

        Returns:
            str or None: тип диапазона или None (не размечаем как HOLD)
                - 'HOLD_AFTER_LOSS': убыточные диапазоны
                - 'HOLD_CONSOLIDATION': явная консолидация
                - 'HOLD_WEAK_PROFIT': слабая прибыль без тренда
                - 'HOLD_CHOPPY': рваный рынок (высокая волатильность, слабый тренд)
        """
        cfg = self.config
        min_profit = getattr(cfg, 'min_profit_target', 0.001)
        weak_profit = getattr(cfg, 'weak_profit_threshold', 0.002)
        price_range_thr = getattr(cfg, 'price_range_threshold', 0.003)

        # Убыточный — приоритет 1
        if pnl < min_profit:
            return "HOLD_AFTER_LOSS"

        # Консолидация — приоритет 2 (низкая волатильность + слабый тренд + малое движение)
        if (metrics['volatility_level'] == 'LOW' and
            metrics['trend_level'] == 'WEAK' and
            metrics['price_range'] < price_range_thr):
            return "HOLD_CONSOLIDATION"

        # Слабая прибыль без тренда — приоритет 3
        if (pnl < weak_profit and
            metrics['trend_level'] == 'WEAK'):
            return "HOLD_WEAK_PROFIT"

        # Рваный рынок — приоритет 4 (высокая волатильность без направления)
        if (metrics['volatility_level'] == 'HIGH' and
            metrics['trend_level'] == 'WEAK'):
            return "HOLD_CHOPPY"

        # Прибыльный сильный тренд — НЕ размечаем как HOLD
        return None

    def _generate_holds_for_range(self, ts_list: List[int], range_type: str) -> List[Dict]:
        """
        Генерирует HOLD-метки для диапазона.

        Args:
            ts_list: Список timestamp из candles_5m (int64!)
            range_type: Тип диапазона

        Returns:
            List[Dict]: HOLD-метки для вставки
        """
        holds = []

        if not ts_list or len(ts_list) < 2:
            return holds

        # Базовая структура HOLD-метки
        def _create_hold(ts: int, method: str) -> Dict:
            # ✅ КРИТИЧНО: явное приведение к int (Python int64)
            ts_int64 = int(ts)

            return {
                "symbol": self.config.symbol,
                "timestamp": ts_int64,
                "timeframe": self.config.timeframe,
                "reversal_label": 0,
                "reversal_confidence": 1.0,
                "labeling_method": method,
                "labeling_params": None,
                "extreme_index": None,
                "extreme_price": None,
                "extreme_timestamp": ts_int64,  # ✅ Дублируем как int64
                "confirmation_index": None,
                "confirmation_timestamp": None,
                "price_change_after": 0.0,
                "features_json": None,
                "is_high_quality": 1,
            }

        # Генерация по типу диапазона
        if range_type == "HOLD_CONSOLIDATION":
            step = getattr(self.config, 'consolidation_hold_every_n_bars', 3)
            for i in range(1, len(ts_list) - 1, step):
                holds.append(_create_hold(ts_list[i], "HOLD_CONSOLIDATION"))

        elif range_type == "HOLD_AFTER_LOSS":
            min_window = getattr(self.config, 'min_window_bars', 6)
            if len(ts_list) >= min_window:
                mid_idx = len(ts_list) // 2
                holds.append(_create_hold(ts_list[mid_idx], "HOLD_AFTER_LOSS_MID"))
            holds.append(_create_hold(ts_list[-1], "HOLD_AFTER_LOSS_END"))

        elif range_type == "HOLD_WEAK_PROFIT":
            if len(ts_list) >= 6:
                mid_idx = len(ts_list) // 2
                holds.append(_create_hold(ts_list[mid_idx], "HOLD_WEAK_PROFIT_MID"))
            holds.append(_create_hold(ts_list[-1], "HOLD_WEAK_PROFIT_END"))

        elif range_type == "HOLD_CHOPPY":
            holds.append(_create_hold(ts_list[-1], "HOLD_CHOPPY"))

        return holds

    def mark_unprofitable_ranges_as_negatives(self) -> dict:
        """
        Улучшенная разметка HOLD с анализом волатильности и тренда.

        Анализирует ВСЕ диапазоны между сигналами (не только убыточные):
        - Убыточные (pnl < min_profit_target) → HOLD обязательно
        - Прибыльные но слабые → HOLD с учетом условий
        - Консолидации (низкая волатильность + слабый тренд) → HOLD плотно
        - Периоды после сильных движений → HOLD (ожидание новой возможности)

        Returns:
            dict: {
                'updated_losers': int,
                'hold_after_loss': int,
                'hold_consolidation': int,
                'hold_weak_profit': int,
                'hold_choppy': int,
                'total_holds': int
            }
        """
        symbol = self.config.symbol
        tf = self.config.timeframe
        candles_table = f"candles_{tf}"
        thr = float(getattr(self.config, "min_profit_target", 0.001))

        logger.info(f"🔧 Улучшенная HOLD-разметка с анализом волатильности и тренда | {symbol} {tf}")

        # Статистика
        stats = {
            'updated_losers': 0,
            'hold_after_loss': 0,
            'hold_consolidation': 0,
            'hold_weak_profit': 0,
            'hold_choppy': 0,
            'total_holds': 0,
            'ranges_analyzed': 0,
            'ranges_skipped': 0
        }

        inserted_rows = []

        with self.engine.begin() as conn:
            # 1) Загружаем ВСЕ BUY/SELL метки (отсортированные по времени)
            all_signals = pd.read_sql(
                text("""
                    SELECT reversal_label, extreme_timestamp, price_change_after
                      FROM labeling_results
                     WHERE symbol=:symbol AND timeframe=:tf
                       AND reversal_label IN (1,2)
                     ORDER BY extreme_timestamp
                """),
                conn,
                params={"symbol": symbol, "tf": tf},
            )

            if all_signals.empty or len(all_signals) < 2:
                logger.info(f"ℹ️ Недостаточно сигналов для анализа диапазонов")
                return stats

            # 2) Существующие HOLD (для идемпотентности)
            existing_holds = set()
            res = conn.execute(
                text("""
                    SELECT extreme_timestamp
                      FROM labeling_results
                     WHERE symbol=:symbol AND timeframe=:tf
                       AND reversal_label=0
                """),
                {"symbol": symbol, "tf": tf},
            )
            for r in res:
                existing_holds.add(int(r[0]))

            new_holds_in_batch = set()

            # 3) Обрабатываем каждую пару последовательных сигналов
            for i in range(len(all_signals) - 1):
                current_sig = all_signals.iloc[i]
                next_sig = all_signals.iloc[i + 1]

                ts_cur = int(current_sig.extreme_timestamp)
                ts_next = int(next_sig.extreme_timestamp)
                pnl = float(current_sig.price_change_after) if pd.notna(current_sig.price_change_after) else 0.0

                # 3.1) Обновляем убыточные сигналы (pnl < threshold)
                if pnl < thr:
                    conn.execute(
                        text("""
                            UPDATE labeling_results
                               SET price_change_after = 0.0
                             WHERE symbol=:symbol AND timeframe=:tf
                               AND extreme_timestamp=:ts
                        """),
                        {"symbol": symbol, "tf": tf, "ts": ts_cur},
                    )
                    stats['updated_losers'] += 1

                # 3.2) ✅ ИСПРАВЛЕНО: Загружаем бары диапазона с правильным именем колонки ATR
                range_bars_result = conn.execute(
                    text(f"""
                        SELECT ts, open, high, low, close, volume,
                               COALESCE(atr_14_normalized, NULL) as atr
                          FROM {candles_table}
                         WHERE symbol=:symbol
                           AND ts > :ts_cur
                           AND ts < :ts_next
                         ORDER BY ts ASC
                    """),
                    {"symbol": symbol, "ts_cur": ts_cur, "ts_next": ts_next},
                ).fetchall()

                if not range_bars_result:
                    stats['ranges_skipped'] += 1
                    continue

                # Конвертируем в DataFrame
                range_bars = pd.DataFrame(
                    range_bars_result,
                    columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'atr']
                )

                stats['ranges_analyzed'] += 1

                # 3.3) Рассчитываем метрики диапазона
                try:
                    metrics = self._calculate_range_metrics(range_bars)
                except Exception as e:
                    logger.warning(f"⚠️ Ошибка расчета метрик для диапазона {ts_cur}-{ts_next}: {e}")
                    continue

                # 3.4) Классифицируем диапазон
                try:
                    range_type = self._classify_range(metrics, pnl)
                except Exception as e:
                    logger.warning(f"⚠️ Ошибка классификации диапазона {ts_cur}-{ts_next}: {e}")
                    continue

                if range_type is None:
                    # Прибыльный тренд — не размечаем
                    continue

                # 3.5) Генерируем HOLD-метки
                ts_list = range_bars['ts'].astype('int64').tolist()
                try:
                    holds = self._generate_holds_for_range(ts_list, range_type)
                except Exception as e:
                    logger.warning(f"⚠️ Ошибка генерации HOLD для диапазона {ts_cur}-{ts_next}: {e}")
                    continue

                # 3. 6) Фильтруем дубликаты и добавляем в batch
                for hold in holds:
                    ts_hold = hold['extreme_timestamp']
                    if ts_hold not in existing_holds and ts_hold not in new_holds_in_batch:
                        inserted_rows.append(hold)
                        new_holds_in_batch.add(ts_hold)

                        # Обновляем статистику по типам
                        method = hold['labeling_method']
                        if 'LOSS' in method:
                            stats['hold_after_loss'] += 1
                        elif 'CONSOLIDATION' in method:
                            stats['hold_consolidation'] += 1
                        elif 'WEAK_PROFIT' in method:
                            stats['hold_weak_profit'] += 1
                        elif 'CHOPPY' in method:
                            stats['hold_choppy'] += 1

            # 4) Пакетная вставка HOLD
            if inserted_rows:
                pd.DataFrame(inserted_rows).to_sql(
                    "labeling_results", conn, if_exists="append", index=False
                )

        stats['total_holds'] = len(inserted_rows)

        # Логируем результаты
        logger.info(
            "✅ HOLD разметка завершена:\n"
            f"   • Диапазонов проанализировано: {stats['ranges_analyzed']}\n"
            f"   • Диапазонов пропущено (нет баров): {stats['ranges_skipped']}\n"
            f"   • Убыточные диапазоны: {stats['hold_after_loss']} HOLD\n"
            f"   • Консолидации: {stats['hold_consolidation']} HOLD (плотная разметка)\n"
            f"   • Слабая прибыль: {stats['hold_weak_profit']} HOLD\n"
            f"   • Рваный рынок: {stats['hold_choppy']} HOLD\n"
            f"   • ИТОГО: {stats['total_holds']} HOLD меток\n"
            f"   • Обновлено убыточных сигналов: {stats['updated_losers']}"
        )

        return stats

    def _calculate_unprofitable_hold_ranges(self, df: pd.DataFrame, signals: List[Dict]) -> List[Dict]:
        """
        Диапазоны между последовательными BUY/SELL, где PnL < порога (или <0 — см. условие).
        Возвращает элементы: {start_index, end_index, pnl, signal_type, range_length}
        """
        if not signals or len(signals) < 2:
            return []

        # — фильтруем только реальные сделки и приводим типы
        clean = []
        ts_to_idx = {int(ts): i for i, ts in enumerate(df["ts"].astype(int))}
        for s in signals:
            rl = int(s.get("reversal_label", -1))
            if rl not in (1, 2):
                continue
            idx = s.get("extreme_index")
            if idx is None or not isinstance(idx, (int, np.integer)):
                ts = s.get("extreme_timestamp")
                if ts is None or int(ts) not in ts_to_idx:
                    continue
                idx = ts_to_idx[int(ts)]
            if 0 <= idx < len(df):
                clean.append({"extreme_index": int(idx), "reversal_label": rl})

        if len(clean) < 2:
            return []

        clean.sort(key=lambda x: x["extreme_index"])
        hold_ranges = []

        for i in range(len(clean) - 1):
            start_idx = clean[i]["extreme_index"]
            raw_end = clean[i + 1]["extreme_index"]
            # считаем PnL до последнего БАРА перед следующим сигналом
            end_idx = max(start_idx + 1, raw_end - 1)
            if end_idx <= start_idx or end_idx >= len(df):
                continue

            signal_type = 'BUY' if clean[i]["reversal_label"] == 1 else 'SELL'
            pnl, _ = self._calculate_pnl_to_index(df, start_idx, signal_type, end_idx)

            # выбери одно из условий:
            # if pnl < 0:                                  # строго убыточные
            if pnl < float(self.config.min_profit_target):  # «неприбыльные по порогу»
                hold_ranges.append({
                    "start_index": start_idx,
                    "end_index": raw_end,  # важно: полуинтервал [start, raw_end)
                    "pnl": float(pnl),
                    "signal_type": signal_type,
                    "range_length": raw_end - start_idx
                })

        self.logger.info(f"📊 Найдено {len(hold_ranges)} убыточных диапазонов между сигналами")
        return hold_ranges

    def _run_auto_with_method(self, strategy_func, method_name: str, df: pd.DataFrame = None):
        """Запуск автоматического режима с расчетом PnL до следующей метки"""
        if df is None:
            df = self.load_data()
        signals = strategy_func(df)
        results = []

        # Сортируем сигналы по индексу для поиска следующей метки
        sorted_signals = sorted(signals, key=lambda x: x['index'])

        for i, sig in enumerate(sorted_signals):
            idx = sig['index']

            # Определяем индекс выхода: либо следующая метка, либо hold_bars
            if i + 1 < len(sorted_signals):
                # Есть следующий сигнал - выходим на баре перед ним
                next_idx = sorted_signals[i + 1]['index']
                exit_idx = max(idx + 1, next_idx - 1)  # минимум +1 бар от входа
            else:
                # Последний сигнал - используем hold_bars
                exit_idx = idx + self.config.hold_bars

            # Проверка границ
            if exit_idx >= len(df):
                exit_idx = len(df) - 1

            if exit_idx <= idx:  # защита от некорректных индексов
                continue

            # Расчет PnL до найденного индекса выхода
            pnl, is_profitable = self._calculate_pnl_to_index(df, idx, sig['type'], exit_idx)

            row_dict = df.iloc[idx].to_dict()
            for k, v in row_dict.items():
                if pd.isna(v):
                    row_dict[k] = None
                elif isinstance(v, pd.Timestamp):
                    row_dict[k] = v.isoformat()
                elif isinstance(v, (np.integer, np.int64)):
                    row_dict[k] = int(v)
                elif isinstance(v, (np.floating, np.float64)):
                    row_dict[k] = float(v)
                elif isinstance(v, str):
                    row_dict[k] = v

            timestamp = int(df['ts'].iloc[idx])
            extreme_timestamp = int(sig.get('extreme_timestamp', timestamp))
            confirmation_timestamp = int(sig.get('confirmation_timestamp', timestamp))

            # создаем безопасный словарь параметров без циклических ссылок
            labeling_params = {}
            for k, v in self.config.__dict__.items():
                if k in ['db_engine', 'tool']:  # исключаем несериализуемые объекты
                    continue
                try:
                    # Пробуем сериализовать каждый параметр
                    json.dumps(v)
                    labeling_params[k] = v
                except (TypeError, ValueError):
                    # Если не сериализуется, преобразуем в строку
                    labeling_params[k] = str(v)

            results.append({
                'symbol': self.config.symbol,
                'timestamp': timestamp,
                'timeframe': self.config.timeframe,
                'reversal_label': 1 if sig['type'] == 'BUY' else 2,
                'reversal_confidence': sig['confidence'],
                'labeling_method': method_name,
                'labeling_params': json.dumps(labeling_params),
                'extreme_index': idx,
                'extreme_price': float(df['close'].iloc[idx]),
                'extreme_timestamp': extreme_timestamp,
                'confirmation_index': sig['confirmation_index'],
                'confirmation_timestamp': confirmation_timestamp,
                'price_change_after': pnl,
                'features_json': json.dumps(row_dict),
                'is_high_quality': 1 if is_profitable else 0
            })

        if results:
            self.save_to_db(results)
            profitable_count = sum(1 for r in results if r['is_high_quality'] == 1)
            total_count = len(results)
            print(
                f"✅ Сохранено {total_count} сигналов ({profitable_count} прибыльных, {total_count - profitable_count} убыточных)")
            return len(results)
        else:
            print(f"❌ Сигналы не найдены")
            return 0

    def _run_auto(self, strategy_func, method_name: str):
        """Обертка для совместимости"""
        return self._run_auto_with_method(strategy_func, method_name)

    def enhanced_main_menu(self):
        """Улучшенное главное меню - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
        while True:
            print("\n" + "=" * 60)
            print("           ML LABELING TOOL v3 — РАСШИРЕННОЕ МЕНЮ")
            print("=" * 60)

            stats = self.data_loader.get_data_stats()
            print(
                f"📊 Символ: {self.config.symbol} | Свечей: {stats.get('total_candles', 'N/A')} | Меток: {stats.get('total_labels', 'N/A')}")

            print("\n🎯 Режимы разметки:")
            print("[0]  BinSeg - быстрая бинарная сегментация (авто)")
            print("[1] PELT Offline - автоподбор penalty (авто)")
            print("[2] CUSUM (авто)")
            print("[3] Min/Max экстремумы (авто)")
            print("[4] CUSUM + Min/Max гибрид (авто)")
            print("[5] Ручная разметка")

            print("\n📈 Анализ и качество:")
            print("[6] Расширенный анализ качества")
            print("[7] Проверка на утечку меток")
            print("[8] Экспорт важности фич")
            print("[9] Кросс-валидация")
            print("[16] Анализ распределения PnL")
            print("[17] Быстрый анализ PnL")

            print("\n⚙️ Управление данными:")
            print("[11] Настройки параметров")
            print("[13] Статистика меток")
            print("[14] Экспорт данных для обучения")
            print("[18] Очистка таблицы меток (текущий символ)")
            print("[19] Очистка таблицы меток по всем символам")
            print("[20] Пометка убыточных диапазонов как негативов")
            print("[21] Устранение дублирования меток")


            print("\n[22] Выход")

            choice = input("\nВаш выбор: ").strip()
            if choice == '0':
                self._run_auto(self._binseg_reversals, "BINSEG")
            elif choice == '1':
                self._run_auto(self._pelt_offline_reversals, "PELT_OFFLINE")
            elif choice == '2':
                self._run_auto(self._cusum_reversals, "CUSUM")
            elif choice == '3':
                self._run_auto(self._extremum_reversals, "EXTREMUM")
            elif choice == '4':
                self._run_auto(self._cusum_extremum_hybrid, "CUSUM_EXTREMUM")
            elif choice == '5':
                self.manual_mode()
            elif choice == '6':
                self.advanced_quality_analysis()
            elif choice == '7':
                self.detect_label_leakage()
            elif choice == '8':
                try:
                    print("\n🏷️ Экспорт важности фич в БД (training_feature_importance)")
                    rid = input("run_id (пусто = последний READY): ").strip() or None
                    model_name = input("Имя модели [unknown]: ").strip() or "unknown"
                    top_n_inp = input("Сохранить top-N (пусто = все): ").strip()
                    top_n = int(top_n_inp) if top_n_inp else None

                    saved = self.export_feature_importance(run_id=rid, model_name=model_name, top_n=top_n)
                    print(f"✅ Сохранено записей важности: {saved}")
                except Exception as err:
                    print(f"❌ Ошибка: {err}")

            elif choice == '9':
                try:
                    n_splits = int(input("Количество фолдов [5]: ") or "5")
                    result = self.cross_validation_split(n_splits=n_splits)
                    if result:
                        print(f"✅ Создано {n_splits} фолдов кросс-валидации")
                except Exception as err:
                    print(f"❌ Ошибка: {err}")

            elif choice == '11':
                self.configure_settings()
            elif choice == '13':
                self.show_stats()
            elif choice == '14':
                try:
                    print("\n📦 Зафиксировать снапшот тренировочного датасета в БД")
                    print("ℹ️  Используются все готовые метки из labeling_results")
                    print(f"   Метод разметки: {self.config.method}")
                    print(f"   Символ: {self.config.symbol}, таймфрейм: {self.config.timeframe}")

                    confirm = input("\n❓ Продолжить создание снапшота? (y/N): ").strip().lower()
                    if confirm != 'y':
                        print("❌ Создание снапшота отменено")
                        continue

                    run_id = self.create_training_snapshot()
                    print(f"✅ Снапшот создан: run_id={run_id}")
                except Exception as err:
                    print(f"❌ Ошибка при экспорте: {err}")
            elif choice == '16':
                try:
                    method = input("Метод для анализа [опционально]: ").strip()
                    if method:
                        self.analyze_pnl_distribution(method=method)
                    else:
                        self.analyze_pnl_distribution()
                except Exception as err:
                    print(f"❌ Ошибка: {err}")
            elif choice == '17':
                try:
                    self.quick_pnl_analysis()
                except Exception as err:
                    print(f"❌ Ошибка: {err}")
            elif choice == '18':
                try:
                    self.clear_labeling_table()
                except Exception as err:
                    print(f"❌ Ошибка: {err}")
            elif choice == '19':
                try:
                    self.clear_all_labeling_tables()
                except Exception as err:
                    print(f"❌ Ошибка: {err}")
            elif choice == '20':
                    try:
                        stats = self.mark_unprofitable_ranges_as_negatives()
                        print(f"✅ HOLD разметка завершена:")
                        print(f"   • Убыточные диапазоны: {stats.get('hold_after_loss', 0)} HOLD")
                        print(f"   • Консолидации: {stats.get('hold_consolidation', 0)} HOLD")
                        print(f"   • Слабая прибыль: {stats.get('hold_weak_profit', 0)} HOLD")
                        print(f"   • Рваный рынок: {stats.get('hold_choppy', 0)} HOLD")
                        print(f"   • ИТОГО: {stats.get('total_holds', 0)} HOLD меток")
                    except Exception as err:
                        print(f"❌ Ошибка: {err}")
            elif choice == '21':
                try:
                    count = self.merge_conflicting_labels()
                    print(f"✅ Объединено конфликтных меток: {count}")
                except Exception as err:
                    print(f"❌ Ошибка: {err}")

            elif choice == '22':
                print("👋 До свидания!")
                break
            else:
                print("❌ Неверный выбор")

    def _expand_hold_ranges(self, labels_df: pd.DataFrame, all_timestamps: set) -> dict:
        """
        ВЕРСИЯ БЕЗ РАСШИРЕНИЯ HOLD.
        Возвращает только исходные метки по исходным ts, без заливки промежутков.
        Надёжно работает при дубликатах колонок и нескольких строках на один ts.
        """

        if labels_df is None or labels_df.empty:
            return {}

        # 1) убрать дубликаты колонок, сохранить первый столбец с данным именем
        labels_df = labels_df.loc[:, ~labels_df.columns.duplicated()].copy()

        # 2) найти колонки 'ts' и 'reversal_label' (без учета регистра)
        cols_lower = {c.lower(): c for c in labels_df.columns}
        ts_col = cols_lower.get('ts')
        lab_col = cols_lower.get('reversal_label')

        if ts_col is None or lab_col is None:
            self.logger.warning("Нет необходимых колонок 'ts' и/или 'reversal_label' в labels_df")
            return {}

        df = labels_df[[ts_col, lab_col]].copy()

        # 3) привести к числовым типам; выбросить NaN/нечисловые
        df[ts_col] = pd.to_numeric(df[ts_col], errors='coerce')
        df[lab_col] = pd.to_numeric(df[lab_col], errors='coerce')
        df = df.dropna(subset=[ts_col, lab_col])

        # 4) привести к int (после dropna безопасно)
        df[ts_col] = df[ts_col].astype(np.int64)
        df[lab_col] = df[lab_col].astype(np.int64)

        # 5) оставить только ts, которые есть в all_timestamps (если заданы)
        if all_timestamps:
            # all_timestamps может быть set(int) — ок
            df = df[df[ts_col].isin(all_timestamps)]

        if df.empty:
            return {}

        # 6) если по одному ts несколько меток — берём ПОСЛЕДНЮЮ (по порядку строк)
        # (Если нужно первую — замените .last() на .first())
        df = df.groupby(ts_col, as_index=False)[lab_col].last()

        # 7) вернуть как dict{ts:int -> label:int}
        return dict(zip(df[ts_col].tolist(), df[lab_col].tolist()))

    def _build_training_snapshot_dataframe(self):
        import pandas as pd
        from sqlalchemy import text
        market_df = self.data_loader.load_indicators()
        if market_df is None or market_df.empty:
            raise RuntimeError("Пустые рыночные данные")
        if "ts" not in market_df.columns:
            raise RuntimeError("В market_df отсутствует колонка 'ts'")

        # ✅ ДОБАВЬТЕ DEBUG:
        logger.info(f"🔍 market_df колонок: {len(market_df.columns)}")
        logger.info(f"🔍 Первые 10 колонок: {list(market_df.columns[:10])}")

        # Проверяем признаки из BASE_FEATURE_NAMES
        missing_features = [f for f in BASE_FEATURE_NAMES if f not in market_df.columns]
        if missing_features:
            logger.warning(f"⚠️ Отсутствуют признаки: {missing_features}")
        else:
            logger.info("✅ Все BASE_FEATURE_NAMES присутствуют в market_df")

        # Проверяем заполненность
        sample_row = market_df.iloc[0][BASE_FEATURE_NAMES]
        null_count = sample_row.isna().sum()
        logger.info(f"🔍 В первой строке NULL признаков: {null_count}/{len(BASE_FEATURE_NAMES)}")
        if null_count > 0:
            null_features = sample_row[sample_row.isna()].index.tolist()
            logger.warning(f"⚠️ NULL признаки в первой строке: {null_features}")

        logger.info(f"✅ Загружено {len(market_df)} свечей с индикаторами")
        with self.engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT extreme_timestamp AS ts, reversal_label
                FROM labeling_results
                WHERE symbol=:symbol AND timeframe=:timeframe AND reversal_label IN (0,1,2)
                ORDER BY extreme_timestamp
            """), {"symbol": self.config.symbol, "timeframe": self.config.timeframe}).fetchall()
        if not rows:
            raise RuntimeError("Нет меток в labeling_results")
        labels_df = pd.DataFrame(rows, columns=["ts", "reversal_label"])
        valid_mask = labels_df['reversal_label'].isin([0, 1, 2])
        invalid_count = (~valid_mask).sum()
        if invalid_count > 0:
            logger.warning(f"⚠️ Найдено {invalid_count} меток с недопустимыми значениями - удаляем")
            labels_df = labels_df[valid_mask]

        logger.info(
            f"✅ Загружено {len(labels_df)} меток "
            f"(NO_SIGNAL/HOLD: {(labels_df['reversal_label'] == 0).sum()}, "
            f"BUY: {(labels_df['reversal_label'] == 1).sum()}, "
            f"SELL: {(labels_df['reversal_label'] == 2).sum()})")

        all_timestamps = set(market_df['ts'].values)
        expanded_labels = self._expand_hold_ranges(labels_df, all_timestamps)
        hold_count = sum(1 for l in expanded_labels.values() if l == 0)
        logger.info(f"✅ После расширения HOLD (label=0): {hold_count}")

        # 🔹 используем только бары, у которых реально есть метка в labeling_results
        mapped = market_df["ts"].map(expanded_labels)
        labeled_mask = mapped.notna()
        labeled_df = market_df.loc[labeled_mask].copy()

        # ✅ ДОБАВЬТЕ DEBUG:
        logger.info(f"🔍 labeled_df: {len(labeled_df)} строк, {len(labeled_df.columns)} колонок")

        # Проверяем признаки
        sample_labeled = labeled_df.iloc[0][BASE_FEATURE_NAMES]
        null_labeled = sample_labeled.isna().sum()
        logger.info(f"🔍 В первой строке labeled_df NULL признаков: {null_labeled}/{len(BASE_FEATURE_NAMES)}")

        if null_labeled > 0:
            null_features_labeled = sample_labeled[sample_labeled.isna()].index.tolist()
            logger.warning(f"⚠️ NULL признаки в labeled_df: {null_features_labeled}")

        if labeled_df.empty:
            raise RuntimeError("После применения expanded_labels не осталось размеченных баров")

        labeled_df["reversal_label"] = mapped[labeled_mask].astype(int)

        # ✅ ДОБАВЬТЕ: Удаляем строки с NULL в критичных признаках
        critical_features = ['cmo_14', 'adx_14', 'bb_position', 'atr_14_normalized', 'trend_acceleration_ema7']
        before_null_drop = len(labeled_df)
        labeled_df = labeled_df.dropna(subset=critical_features)
        after_null_drop = len(labeled_df)

        dropped_count = before_null_drop - after_null_drop
        if dropped_count > 0:
            logger.info(f"🔧 Удалено {dropped_count} меток с NULL индикаторами (прогрев)")

        if labeled_df.empty:
            raise RuntimeError("❌ После удаления NULL не осталось размеченных данных!")

        # sanity-check: только 0/1/2
        invalid_mask = ~labeled_df["reversal_label"].isin([0, 1, 2])
        if invalid_mask.any():
            invalid_count = invalid_mask.sum()
            logger.warning(f"⚠️ Обнаружено {invalid_count} недопустимых меток после маппинга — удаляем их")
            labeled_df = labeled_df[~invalid_mask]

        if labeled_df.empty:
            raise RuntimeError("Все размеченные бары отфильтрованы как некорректные (после проверки 0/1/2)")

        class_counts_before = labeled_df["reversal_label"].value_counts().to_dict()
        logger.info(
            "   ДО downsample (по размеченным барам): HOLD=%s, BUY=%s, SELL=%s",
            class_counts_before.get(0, 0),
            class_counts_before.get(1, 0),
            class_counts_before.get(2, 0),
        )

        # 🔹 HOLD = только явные HOLD-метки из labeling_results
        hold_df = labeled_df[labeled_df["reversal_label"] == 0]
        signals_df = labeled_df[labeled_df["reversal_label"] != 0]

        # ограничиваем количество HOLD
        n_hold_max = 10_000
        if len(hold_df) > 0:
            hold_sample = hold_df.sample(
                n=min(n_hold_max, len(hold_df)),
                random_state=42,
            )
        else:
            hold_sample = hold_df

        logger.info(
            "✅ Downsample HOLD: %s → %s",
            len(hold_df),
            len(hold_sample),
        )

        # итоговый датасет на этой стадии
        dataset_df = pd.concat([hold_sample, signals_df], ignore_index=True).sort_values("ts").reset_index(drop=True)

        # ✅ ДОБАВЬТЕ DEBUG:
        logger.info(f"🔍 dataset_df ДО фильтрации колонок: {len(dataset_df)} строк, {len(dataset_df.columns)} колонок")

        # Проверяем есть ли признаки
        features_present = [f for f in BASE_FEATURE_NAMES if f in dataset_df.columns]
        logger.info(f"🔍 Признаков в dataset_df: {len(features_present)}/{len(BASE_FEATURE_NAMES)}")

        sample_dataset = dataset_df.iloc[0][features_present]
        null_dataset = sample_dataset.isna().sum()
        logger.info(f"🔍 NULL в первой строке dataset_df: {null_dataset}/{len(features_present)}")


        class_counts_after = dataset_df["reversal_label"].value_counts().to_dict()
        total = len(dataset_df)

        #  Явная проверка на допустимые метки
        invalid_labels = set(class_counts_after.keys()) - {0, 1, 2}
        if invalid_labels:
            logger.warning(f"⚠️ Обнаружены недопустимые метки: {invalid_labels}")
            dataset_df = dataset_df[dataset_df["reversal_label"].isin([0, 1, 2])]
            class_counts_after = dataset_df["reversal_label"].value_counts().to_dict()
            total = len(dataset_df)

        logger.info(
            "✅ Финальный датасет: %s (NO_SIGNAL=%s, BUY=%s, SELL=%s)",
            total,
            class_counts_after.get(0, 0),
            class_counts_after.get(1, 0),
            class_counts_after.get(2, 0),
        )
        # Балансируем веса только для 0/1/2
        if class_counts_after:
            max_count = max(class_counts_after.values())
            weights_map = {}
            for label in [0, 1, 2]:
                count = class_counts_after.get(label, 0)
                if count > 0:
                    weights_map[label] = max_count / count
                else:
                    weights_map[label] = 0.0  # Класс отсутствует
                    logger.warning(f"⚠️ Класс {label} отсутствует в датасете")

            dataset_df["sample_weight"] = dataset_df["reversal_label"].map(weights_map)
        else:
            logger.error("❌ class_counts_after пуст!")
            dataset_df["sample_weight"] = 1.0
        # служебные поля + все фичи из BASE_FEATURE_NAMES
        feature_columns = list(BASE_FEATURE_NAMES)
        allowed_columns = ["ts","datetime",
            "reversal_label","sample_weight",
            *feature_columns,]
        # оставляем только нужные колонки, не падаем если чего-то нет
        dataset_df = dataset_df[[c for c in allowed_columns if c in dataset_df.columns]]
        dataset_df['symbol'] = self.config.symbol
        dataset_df['run_id'] = None
        dataset_df['timeframe'] = self.config.timeframe
        dataset_df['created_at'] = None
        meta_info = {
            "class_dist": {
                "no_signal": int(class_counts_after.get(0, 0)),
                "buy": int(class_counts_after.get(1, 0)),
                "sell": int(class_counts_after.get(2, 0)),
                "total": total,
            },
            "buffer_bars": getattr(self.config, "buffer_bars", None),
            "seed": getattr(self.config, "seed", None),
            "config_json": {
                "method": self.config.method,
                "timeframe": self.config.timeframe,
                "symbol": self.config.symbol,
            },
            "issues": {},
        }

        return dataset_df, meta_info

    def create_training_snapshot(self, run_id: str | None = None) -> str:
        """
        Формирует БД-снапшот тренировочного датасета:
          - гарантирует наличие таблиц snapshot (DDL)
          - пишет meta(status=CREATING)
          - собирает датасет (позитивы/негативы, anti_trade_mask, sample_weight)
          - валидирует данные
          - пишет строки в training_dataset и агрегаты в training_dataset_meta
          - переключает meta(status=READY)
        Возвращает run_id.
        """

        self._ensure_training_snapshot_tables()

        # Параметры и run_id
        created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not run_id:
            payload = {
                "symbol": self.config.symbol,
                "timeframe": self.config.timeframe,
                "method": self.config.method,
            }
            sh = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:8]
            stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
            run_id = f"{self.config.symbol.replace('/', '_')}_{self.config.timeframe}_{stamp}_{sh}"
        # META: status=CREATING
        meta_defaults = {
            "run_id": run_id,
            "status": "CREATING",
            "error_msg": None,
            "symbol": self.config.symbol,
            "timeframe": self.config.timeframe,
            "created_at": created_at
        }
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO training_dataset_meta(run_id,status,error_msg,symbol,timeframe,created_at)
                VALUES (:run_id,:status,:error_msg,:symbol,:timeframe,:created_at)
                ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, error_msg=NULL
            """), meta_defaults)

        try:
            # 1) Сборка датасета (ваш существующий конвейер)
            dataset_df, meta_info = self._build_training_snapshot_dataframe()

            # 2) Валидация/очистка
            try:
                df_clean, issues, nan_drop_rows, duplicates_count = self._validate_snapshot_frame(dataset_df)
            except Exception as e:
                logger.info("Snapshot validation failed: %s", e)
                raise

            # 3) Диапазон дат и метрики
            range_start_ts = int(df_clean["ts"].min()) if len(df_clean) else None
            range_end_ts = int(df_clean["ts"].max()) if len(df_clean) else None

            if "anti_trade_mask" in df_clean.columns and len(df_clean) > 0:
                try:
                    issues["anti_trade_coverage"] = float(df_clean["anti_trade_mask"].mean())
                except Exception:
                    pass

            rows_total = int(len(df_clean))
            pos_count = int((df_clean["reversal_label"].isin([1, 2])).sum()) if rows_total else 0
            hold_bars = int((df_clean["reversal_label"] == 0).sum()) if rows_total else 0
            neg_count = 0  # по новой логике негативы не генерируем

            # 4) Запись строк snapshot
            df_clean = df_clean.copy()
            df_clean["run_id"] = run_id
            df_clean["symbol"] = self.config.symbol
            df_clean["timeframe"] = self.config.timeframe
            df_clean["created_at"] = created_at


            # Формируем таблицу ТОЛЬКО на основе BASE_FEATURE_NAMES
            required_columns = (
                    [
                        'run_id',
                        'symbol',
                        'timeframe',
                        'ts',
                        'datetime',
                        'reversal_label',
                        'sample_weight',
                    ]
                    + BASE_FEATURE_NAMES
                    + ['created_at']
            )

            # Оставляем только колонки из DDL
            final_columns = [col for col in required_columns if col in df_clean.columns]
            df_for_db = df_clean[final_columns].copy()

            # batch insert
            df_for_db.to_sql("training_dataset", self.engine, if_exists="append", index=False)

            # 5) Обновление META (READY) — без featureset/source_hash/feature_names
            meta_payload = {
                "run_id": run_id,
                "status": "READY",
                "error_msg": None,
                "rows_total": rows_total,
                "pos_count": pos_count,
                "neg_count": neg_count,
                "hold_bars": hold_bars,
                "class_dist_json": json.dumps(meta_info.get("class_dist", {})),
                "buffer_bars": meta_info.get("buffer_bars"),
                "seed": meta_info.get("seed"),
                "labeling_method": self.config.method,
                "config_json": json.dumps(meta_info.get("config_json", {})),
                "nan_drop_rows": int(nan_drop_rows),
                "issues_json": json.dumps(issues, ensure_ascii=False),
                "range_start_ts": range_start_ts,
                "range_end_ts": range_end_ts,
            }
            with self.engine.begin() as conn:
                conn.execute(text("""
                    UPDATE training_dataset_meta
                       SET status=:status,
                           error_msg=:error_msg,
                           rows_total=:rows_total,
                           pos_count=:pos_count,
                           neg_count=:neg_count,
                           class_dist_json=:class_dist_json,
                           hold_bars=:hold_bars,
                           buffer_bars=:buffer_bars,
                           seed=:seed,
                           labeling_method=:labeling_method,
                           config_json=:config_json,
                           nan_drop_rows=:nan_drop_rows,
                           issues_json=:issues_json,
                           range_start_ts=:range_start_ts,
                           range_end_ts=:range_end_ts
                     WHERE run_id=:run_id
                """), meta_payload)

            logger.info(
                "✅ Snapshot READY | run_id=%s | rows=%s | pos=%s | neg=%s | anti_trade=%s | range=[%s..%s] | nan_drop=%s",
                run_id, rows_total, pos_count, neg_count,
                (f"{issues.get('anti_trade_coverage'):.4f}" if isinstance(issues.get("anti_trade_coverage"),
                                                                          float) else "N/A"),
                range_start_ts, range_end_ts, nan_drop_rows
            )
            return run_id

        except Exception as e:
            err = str(e)
            logger.info("❌ Ошибка формирования снапшота: %s", err)
            with self.engine.begin() as conn:
                conn.execute(text("""
                    UPDATE training_dataset_meta
                       SET status='FAILED', error_msg=:err
                     WHERE run_id=:run_id
                """), {"run_id": run_id, "err": err})
            raise

    def close(self):
        """Корректно закрывает соединения и освобождает ресурсы."""
        try:
            if hasattr(self, "data_loader") and hasattr(self.data_loader, "db_engine"):
                self.data_loader.db_engine.dispose()
                logger.info("🔌 SQLAlchemy engine закрыт.")
            if hasattr(self, "engine"):
                self.engine.dispose()
                logger.info("🧹 Подключение к базе данных закрыто.")
        except Exception as err:
            logger.warning(f"⚠️ Ошибка при закрытии соединений: {err}")


# === ЗАПУСК ===
if __name__ == '__main__':
    tool = None
    try:
        # ИСПОЛЬЗУЕМ НАСТРОЙКИ ПО УМОЛЧАНИЮ ИЗ LabelingConfig
        # без дублирования параметров
        config = LabelingConfig(
            symbol="ETHUSDT",
            # Все остальные параметры берутся из defaults класса LabelingConfig
        )

        tool = AdvancedLabelingTool(config)
        tool.enhanced_main_menu()

    except KeyboardInterrupt:
        print("\n👋 Прервано пользователем.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")

        logger.error(f"Детали: {traceback.format_exc()}")
        sys.exit(1)
    finally:
        if tool is not None:
            tool.close()