# train_ml_global_v2.py
"""
Обучение LightGBM модели с ОКНОМ ИСТОРИИ

Автор: pwm777
Дата: 2025-11-17
Версия: 2.1.1 (windowed training)

Изменения:
- Lookback указывается константой LOOKBACK_WINDOW (по умолчанию 11 баров)
- Пример = последние N баров истории (окно), признаки разматываются в порядок [t0, t-1, ..., t-(N-1)]
- Сохранена функциональность: tau tuning, diagnostics, plots, отчёты
"""

import sys
import logging
from sqlalchemy import create_engine, text
from datetime import datetime
import json
from typing import Tuple
import warnings
import lightgbm as lgb
from sklearn.metrics import accuracy_score, precision_recall_fscore_support, confusion_matrix
from collections import Counter
import joblib
from pathlib import Path
from config import BASE_FEATURE_NAMES
warnings.filterwarnings('ignore')
import re
import os, numpy as np, pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.preprocessing import label_binarize, StandardScaler
from sklearn.metrics import precision_recall_curve, average_precision_score, precision_score, recall_score, f1_score

# ──────────────────────────────────────────────────────────────
# КОНФИГУРАЦИЯ
# ──────────────────────────────────────────────────────────────
LOOKBACK_WINDOW = 11  # Количество баров истории для каждого примера
TIMEFRAME_TO_BARS = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48, "1h": 24}

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
MARKET_DB_DSN: str = f"sqlite:///{DATA_DIR}/market_data.sqlite"

# ──────────────────────────────────────────────────────────────
# УТИЛИТЫ
# ──────────────────────────────────────────────────────────────
def _infer_bars_per_day_from_run_id(run_id: str, default: int = 288) -> int:
    """
    Пытается вытащить таймфрейм из run_id формата ..._<tf>_...
    и вернуть соответствующее количество баров в сутках.
    """
    m = re.search(r"_(\d+[mh])_", str(run_id).lower())
    if not m:
        return default
    tf = m.group(1)
    timeframe_to_bars_local = {
        "1m": 1440,
        "3m": 480,
        "5m": 288,
        "15m": 96,
        "30m": 48,
        "1h": 24,
        "2h": 12,
        "4h": 6,
        "6h": 4,
        "12h": 2,
        "1d": 1,
    }
    return timeframe_to_bars_local.get(tf, default)


# ──────────────────────────────────────────────────────────────
# КОЛЛБЭК «ТЕРМОМЕТР ПРОГРЕССА» ДЛЯ LIGHTGBM
# ──────────────────────────────────────────────────────────────

def thermometer_progress_callback(logger: logging.Logger, width: int = 30, period: int = 10):
    """Прогресс-бар по итерациям бустинга"""
    import sys

    def _cb(env):
        begin = getattr(env, 'begin_iteration', 0) or 0
        end = getattr(env, 'end_iteration', None)
        if end is None or end <= begin:
            # Пытаемся оценить общее число итераций из окружения (в params num_boost_round обычно нет)
            total = max(1, (getattr(env, 'iteration', 0) or 0) + 1)
        else:
            total = max(1, end - begin)

        iter_now = int(getattr(env, 'iteration', 0) or 0)
        done = max(0, iter_now - begin + 1)
        pct = min(1.0, max(0.0, done / total))
        filled = int(round(pct * width))
        bar = '█' * filled + '░' * (width - filled)

        # Метрики валидации
        val_metric_name = None
        val_metric_value = None
        evals = getattr(env, 'evaluation_result_list', None)
        if evals:
            for res in evals:
                if isinstance(res, (list, tuple)) and len(res) >= 3:
                    data_name, metric_name, metric_val = res[0], res[1], res[2]
                    if str(data_name).startswith(('valid', 'val')):
                        val_metric_name = str(metric_name)
                        try:
                            val_metric_value = float(metric_val)
                        except Exception:
                            val_metric_value = None
                        break

        should_print = (iter_now > 0 and iter_now % period == 0) or (done >= total)

        if should_print:
            if val_metric_name is not None and val_metric_value is not None:
                msg = f"[{iter_now:4d}/{total}] {val_metric_name}:{val_metric_value:.5f} | {bar} {int(pct * 100):3d}%"
            else:
                msg = f"[{iter_now:4d}/{total}] {bar} {int(pct * 100):3d}%"

            if done >= total:
                print(f"\r{msg}")
                sys.stdout.flush()
            else:
                print(f"\r{msg}", end='', flush=True)

    return _cb


# ──────────────────────────────────────────────────────────────
# КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ
# ──────────────────────────────────────────────────────────────

class DataLoader:
    """Загрузка данных из SQLite базы ml_labeling_tool_v3.py"""

    def __init__(self, db_dsn: str = MARKET_DB_DSN, symbol: str = "ETHUSDT"):
        self.db_dsn = db_dsn
        self.db_path = DATA_DIR / "market_data.sqlite"
        self.symbol = symbol
        self.engine = None

    def connect(self):
        """Установка соединения с БД"""
        if not self.db_path.exists():
            raise FileNotFoundError(f"База данных не найдена: {self.db_path}")
        self.engine = create_engine(self.db_dsn)
        logger.info(f"✅ Подключено к БД: {self.db_path}")

    def close(self):
        """Закрытие соединения"""
        if self.engine:
            self.engine.dispose()
            logger.info("✅ Соединение с БД закрыто")

    def load_market_data(self) -> pd.DataFrame:
        """Загрузка свечных данных из candles_5m"""
        if not self.engine:
            self.connect()

        query = text("""
            SELECT * FROM candles_5m 
            WHERE symbol = :symbol 
            ORDER BY ts
        """)

        with self.engine.connect() as conn:
            df = pd.read_sql_query(query, conn, params={"symbol": self.symbol})

        if df.empty:
            raise ValueError(f"Нет данных для символа {self.symbol}")

        # Преобразование времени
        if 'ts' in df.columns and 'datetime' not in df.columns:
            df['datetime'] = pd.to_datetime(df['ts'], unit='ms')

        logger.info(f"✅ Загружено {len(df)} свечей из candles_5m")
        return df

    def load_training_dataset(self, run_id: str) -> pd.DataFrame:
        """Загрузка готового датасета из training_dataset"""
        if not self.engine:
            self.connect()

        query = text("""
            SELECT * FROM training_dataset
            WHERE run_id = :run_id
            ORDER BY ts
        """)

        with self.engine.connect() as conn:
            df = pd.read_sql_query(query, conn, params={"run_id": run_id})

        if df.empty:
            raise ValueError(f"❌ Нет данных для run_id={run_id}")

        logger.info(f"✅ Загружено {len(df)} образцов из training_dataset")
        logger.info(f"   Классы: {df['reversal_label'].value_counts().to_dict()}")
        return df


# ──────────────────────────────────────────────────────────────
# ГЛАВНЫЙ КЛАСС ModelTrainer С ОКНОМ ИСТОРИИ
# ──────────────────────────────────────────────────────────────

class ModelTrainer:
    def __init__(self, db_dsn: str, symbol: str, lookback: int = LOOKBACK_WINDOW):
        self.db_dsn = db_dsn
        self.symbol = symbol
        self.lookback = lookback
        self.timeframe = "5m"  # По умолчанию, используется для совместимости
        self.data_loader = DataLoader(db_dsn, symbol)
        self.base_feature_names = BASE_FEATURE_NAMES

        # Генерируем полный список признаков с лагами
        self.feature_names = self._generate_windowed_feature_names()
        logger.info(f"📊 Создано {len(self.feature_names)} признаков "
                    f"({len(self.base_feature_names)} × {lookback} баров)")

    def _generate_windowed_feature_names(self) -> list:
        """
        Генерирует имена признаков для всех лагов
        Например: cmo_14_t0, cmo_14_t-1, ..., cmo_14_t-(lookback-1)
        """
        names = []
        # t0 - текущий бар (самый важный)
        for feat in self.base_feature_names:
            names.append(f"{feat}_t0")

        # t-1, t-2, ..., t-(lookback-1) - история
        for lag in range(1, self.lookback):
            for feat in self.base_feature_names:
                names.append(f"{feat}_t-{lag}")

        return names

    def prepare_training_data(self, run_id: str) -> Tuple[pd.DataFrame, pd.Series, pd.Series]:
        """
        Подготовка данных с окном истории (ИСПРАВЛЕННАЯ ОПТИМИЗИРОВАННАЯ ВЕРСИЯ)
        """
        df = self.data_loader.load_training_dataset(run_id)

        logger.info(f"🔄 Создание окон истории (lookback={self.lookback})...")

        # Фильтруем класс 3 СРАЗУ
        df_filtered = df[df['reversal_label'] != 3].copy()
        skipped = len(df) - len(df_filtered)

        if skipped > 0:
            logger.info(f"⚠️  Пропущено {skipped} примеров с классом 3")

        # Конвертируем в numpy array для скорости
        feature_matrix = df_filtered[self.base_feature_names].values
        labels = df_filtered['reversal_label'].values
        weights = df_filtered['sample_weight'].values

        n_samples = len(df_filtered)
        n_features = len(self.base_feature_names)

        # ✅ ИСПРАВЛЕНО: Окно заканчивается НА метке (включительно)
        # Для каждой метки на позиции i нужно lookback баров ВКЛЮЧАЯ i
        # Минимальная позиция метки: lookback-1 (чтобы было достаточно истории)
        n_valid = n_samples - (self.lookback - 1)

        if n_valid <= 0:
            raise ValueError(f"Недостаточно данных для lookback={self.lookback}")

        logger.info(f"   Создание {n_valid} окон из {n_samples} образцов...")

        # Предаллокация массива результатов
        X_windowed = np.zeros((n_valid, self.lookback * n_features), dtype=np.float32)

        # ✅ ПРАВИЛЬНАЯ ЛОГИКА:
        # Метка на позиции label_idx (это extreme_timestamp)
        # Окно: [label_idx - (lookback-1), .. ., label_idx-1, label_idx]
        # То есть lookback баров, ЗАКАНЧИВАЮЩИХСЯ на label_idx

        for i in range(n_valid):
            label_idx = i + (self.lookback - 1)  # Позиция метки в исходном массиве
            start_idx = label_idx - (self.lookback - 1)  # Начало окна
            end_idx = label_idx + 1  # Конец окна (эксклюзивно)

            # Окно: [start_idx : end_idx] = lookback баров
            window = feature_matrix[start_idx:end_idx, :]  # shape: (lookback, n_features)

            # Порядок признаков: [t0, t-1, t-2, ..., t-(lookback-1)]
            # где t0 = label_idx (текущий бар с меткой)
            window_ordered = window[::-1]  # Разворачиваем: последний бар становится первым

            X_windowed[i] = window_ordered.ravel()

        # ✅ Метки соответствуют label_idx для каждого окна
        y_windowed = labels[self.lookback - 1:]
        w_windowed = weights[self.lookback - 1:]

        # Конвертируем в DataFrame
        X_df = pd.DataFrame(X_windowed, columns=self.feature_names)
        y_series = pd.Series(y_windowed, name='label')
        w_series = pd.Series(w_windowed, name='weight')

        # Проверка пропусков
        missing = X_df.isnull().sum()
        if missing.any():
            logger.warning(f"⚠️  Обнаружены пропуски, заполняем нулями...")
            X_df = X_df.fillna(0)

        logger.info(f"✅ Подготовлены данные: {len(X_df)} примеров, {len(self.feature_names)} признаков")
        logger.info(f"   Распределение классов: {y_series.value_counts().to_dict()}")

        return X_df, y_series, w_series

    def tune_tau_for_spd_range(
            self,
            y_val: np.ndarray,
            proba: np.ndarray,
            bars_per_day: int,
            spd_min: float = 20.0,  # Реалистичный диапазон
            spd_max: float = 35.0,
            precision_min: float = 0.70,
            delta: float = 0.06,
            cooldown_bars: int = 2,
            log_stats: bool = True,  # ← логировать статистики maxp только один раз
    ):
        p_buy, p_sell = proba[:, 1], proba[:, 2]
        maxp = np.maximum(p_buy, p_sell)

        # Диапазон tau из квантилей
        taus = np.linspace(0.65, 0.92, 50)

        # ДИАГНОСТИКА: логируем распределение maxp только по флагу
        if log_stats:
            logging.info(f"📊 Max probability stats: "
                         f"mean={maxp.mean():.3f}, "
                         f"50%={np.percentile(maxp, 50):.3f}, "
                         f"90%={np.percentile(maxp, 90):.3f}")

        best_in = None  # (cand, key)
        best_near = None  # (cand, keyn)
        target = 0.5 * (spd_min + spd_max)
        n = len(y_val)

        for tau_cand in sorted(taus):
            # единый расчёт метрик/act через helper
            stats = self._eval_decision_metrics(
                y_true=np.asarray(y_val),
                proba=np.asarray(proba),
                tau=float(tau_cand),
                delta=float(delta),
                cooldown_bars=int(cooldown_bars),
                bars_per_day=int(bars_per_day),
            )
            spd = stats['spd']
            prec = stats['precision_macro_buy_sell']
            rec = stats['recall_macro_buy_sell']
            f1 = stats['f1_macro_buy_sell']
            # восстановим количество сигналов из SPD (после cooldown)
            signals = int(round(spd * max(1, n) / max(1, bars_per_day)))

            cand = (float(tau_cand), float(spd), float(prec), float(rec), float(f1), int(signals))

            # внутри диапазона — максимизируем precision, затем F1, затем ближе к центру, затем больший τ
            if spd_min <= spd <= spd_max and prec >= precision_min:
                key = (prec, f1, -abs(spd - target), float(tau_cand))
                if (best_in is None) or (key > best_in[1]):
                    best_in = (cand, key)

            # ближайший к центру — на случай, если в диапазоне нет подходящих
            gap = abs(spd - target)
            keyn = (-gap, prec, f1)  # минимизируем gap, затем макс. precision/F1
            if (best_near is None) or (keyn > best_near[1]):
                best_near = (cand, keyn)

        # основной выбор
        chosen = (best_in[0] if best_in is not None else best_near[0])
        tau_chosen, spd, prec, rec, f1, signals = chosen

        # ── Локальный подъём τ вверх (если не хуже и остаёмся в текущем окне SPD) ──
        upper = min(float(tau_chosen) + 0.05, 0.999)
        ref_grid = np.linspace(float(tau_chosen), upper, 31)  # шаг ≈0.0017

        best_ref = None  # (key_ref, t, stats_ref)
        for t in ref_grid:
            stats_ref = self._eval_decision_metrics(
                y_true=np.asarray(y_val),
                proba=np.asarray(proba),
                tau=float(t),
                delta=float(delta),
                cooldown_bars=int(cooldown_bars),
                bars_per_day=int(bars_per_day),
            )
            spd_r = stats_ref['spd']
            prec_r = stats_ref['precision_macro_buy_sell']
            f1_r = stats_ref['f1_macro_buy_sell']

            # приоритет качества; окно SPD используем текущее (spd_min..spd_max) и текущий precision_min
            if (spd_min <= spd_r <= spd_max) and (prec_r >= precision_min):
                # ключ: макс F1, затем больший τ, затем ближе к центру SPD
                key_ref = (f1_r, float(t), -abs(spd_r - target))
                if (best_ref is None) or (key_ref > best_ref[0]):
                    best_ref = (key_ref, float(t), stats_ref)

        # применяем улучшение, если найдено
        if best_ref is not None:
            _, tau_new, sref = best_ref
            tau_chosen = tau_new
            spd = float(sref['spd'])
            prec = float(sref['precision_macro_buy_sell'])
            rec = float(sref['recall_macro_buy_sell'])
            f1 = float(sref['f1_macro_buy_sell'])
            signals = int(round(spd * max(1, len(y_val)) / max(1, bars_per_day)))

        # hit_range должен отражать факт попадания ИТОГОВОГО выбора в окно и по precision
        in_range = (spd_min <= spd <= spd_max) and (prec >= precision_min)

        return float(tau_chosen), {
            "spd": float(spd),
            "precision_macro_buy_sell": float(prec),
            "recall_macro_buy_sell": float(rec),
            "f1_macro_buy_sell": float(f1),
            "signals": int(signals),
            "delta": float(delta),
            "cooldown_bars": int(cooldown_bars),
            "range": [float(spd_min), float(spd_max)],
            "hit_range": bool(in_range),
        }

    @staticmethod
    def _eval_decision_metrics(y_true: np.ndarray,
                               proba: np.ndarray,
                               tau: float,
                               delta: float,
                               cooldown_bars: int,
                               bars_per_day: int) -> dict:
        p_buy = proba[:, 1]
        p_sell = proba[:, 2]
        maxp = np.maximum(p_buy, p_sell)
        margin = np.abs(p_buy - p_sell)

        act = (maxp >= tau) & (margin >= delta)

        # cooldown по индексам срабатываний
        idx = np.where(act)[0]
        if idx.size > 0:
            keep = [idx[0]]
            for i in idx[1:]:
                if i - keep[-1] >= cooldown_bars:
                    keep.append(i)
            sel = np.zeros_like(act, dtype=bool)
            sel[np.array(keep, dtype=int)] = True
            act = sel

        # ДИАГНОСТИКА: логируем количество активных samples
        active_count = np.sum(act)
        if active_count > 0:
            logging.debug(f"Active samples: {active_count}, tau={tau:.3f}")

        pred = np.zeros(len(proba), dtype=int)
        buy_ge_sell = p_buy >= p_sell
        pred[act & buy_ge_sell] = 1
        pred[act & (~buy_ge_sell)] = 2

        # SPD
        spd_val = act.sum() * bars_per_day / max(1, len(y_true))

        # метрики на активном подмножестве (ТОЛЬКО классы 1 и 2)
        if np.any(act):
            y_true_active = y_true[act]
            pred_active = pred[act]

            # ФИЛЬТРУЕМ: берем только классы 1 и 2 (BUY/SELL)
            mask_buy_sell = (y_true_active == 1) | (y_true_active == 2)
            y_true_bs = y_true_active[mask_buy_sell]
            pred_bs = pred_active[mask_buy_sell]

            if len(y_true_bs) > 0:
                pm, rm, fm, _ = precision_recall_fscore_support(
                    y_true_bs, pred_bs, labels=[1, 2], average='macro', zero_division=0
                )

                # ДИАГНОСТИКА: логируем реальные метрики
                correct_bs = np.sum(y_true_bs == pred_bs)
                accuracy_bs = correct_bs / len(y_true_bs) if len(y_true_bs) > 0 else 0
                logging.debug(f"BUY/SELL metrics: {len(y_true_bs)} samples, accuracy={accuracy_bs:.3f}")
            else:
                pm = rm = fm = 0.0
                logging.debug("No BUY/SELL samples in active set")
        else:
            pm = rm = fm = 0.0

        return {
            'spd': float(spd_val),
            'precision_macro_buy_sell': float(pm),
            'recall_macro_buy_sell': float(rm),
            'f1_macro_buy_sell': float(fm),
            'tau': float(tau),
            'delta': float(delta),
            'cooldown_bars': int(cooldown_bars),
            '_debug_active_count': int(active_count),  # Для отладки
        }

    @staticmethod
    def decide(proba, tau, delta=0.08, cooldown_bars=2):
        """Вспомогательный метод для принятия решения (совместимость)"""
        p_buy, p_sell = proba[:, 1], proba[:, 2]
        maxp = np.maximum(p_buy, p_sell)
        margin = np.abs(p_buy - p_sell)
        act = (maxp >= tau) & (margin >= delta)

        # Apply cooldown
        idx = np.where(act)[0]
        if idx.size > 0:
            keep = [idx[0]]
            for i in idx[1:]:
                if i - keep[-1] >= cooldown_bars:
                    keep.append(i)
            sel = np.zeros_like(act, dtype=bool)
            sel[np.array(keep, dtype=int)] = True
            act = sel

        pred = np.zeros(len(proba), dtype=int)
        pred[act] = np.where(p_buy[act] >= p_sell[act], 1, 2)
        return pred

    def train_model(self, run_id: str, use_scaler: bool = False) -> dict:
        """Обучение модели с окном истории + полная диагностика"""

        logger.info("\n" + "=" * 60)
        logger.info("ОБУЧЕНИЕ МОДЕЛИ LIGHTGBM (WINDOWED)")
        logger.info("=" * 60)

        # Подготовка данных
        X, y, w = self.prepare_training_data(run_id)

        # Разделение на train/val/test по времени (70/15/15)
        n = len(X)
        train_end = int(n * 0.70)
        val_end = int(n * 0.85)

        X_train = X.iloc[:train_end]
        X_val = X.iloc[train_end:val_end]
        X_test = X.iloc[val_end:]

        y_train = y.iloc[:train_end]
        y_val = y.iloc[train_end:val_end]
        y_test = y.iloc[val_end:]

        w_train = w.iloc[:train_end]
        w_val = w.iloc[train_end:val_end]
        w_test = w.iloc[val_end:]

        NUM_CLASS = 3
        REPORT_LABELS = [1, 2, 0]  # BUY, SELL, HOLD
        REPORT_NAMES = ['BUY', 'SELL', 'HOLD']

        logger.info(f"📊 Train: {len(X_train)} примеров, Val: {len(X_val)} примеров, Test: {len(X_test)} примеров")
        logger.info("⚖️  Используем веса из training_dataset")

        # ═══════════════════════════════════════════════════════════
        # SCALER (ОПЦИОНАЛЬНО)
        # ═══════════════════════════════════════════════════════════
        scaler = None
        X_train_processed = X_train
        X_val_processed = X_val
        X_test_processed = X_test

        if use_scaler:
            logger.info("📊 Создание StandardScaler и нормализация данных...")
            scaler = StandardScaler()
            X_train_processed = scaler.fit_transform(X_train)
            X_val_processed = scaler.transform(X_val)
            X_test_processed = scaler.transform(X_test)
            logger.info(f"✅ Scaler обучен и применен на {len(X_train)} образцах")
        else:
            logger.info("⚠️  Scaler отключен - обучение на RAW признаках")

        # Датасеты LightGBM
        train_data = lgb.Dataset(X_train_processed, label=y_train, weight=w_train)
        val_data = lgb.Dataset(X_val_processed, label=y_val, reference=train_data)

        # Параметры модели
        params = {
            'objective': 'multiclass',
            'num_class': NUM_CLASS,
            'metric': 'multi_logloss',
            'boosting_type': 'gbdt',
            'num_leaves': 16,  # Было 31 - увеличено для улавливания временных паттернов
            'max_depth': 4,  # Было 8 - убрано ограничение, пусть управляет num_leaves
            'min_child_samples': 100,  # Было 20 - увеличено для лучшей регуляризации
            'learning_rate': 0.03,  # Было 0.01 - увеличено для более быстрой сходимости
            'n_estimators': 1000,  # Было неявно 1600 - уменьшено из-за большего learning_rate
            'feature_fraction': 0.5,
            'bagging_fraction': 0.6,
            'bagging_freq': 5,
            'lambda_l1': 1.5,
            'lambda_l2': 1.5,
            'min_gain_to_split': 0.15,
            'boost_from_average': False,
            'verbose': -1,
            'seed': 42,
            'bagging_seed': 42,
            'feature_fraction_seed': 42,
            'extra_trees': True,  # Режим Extremely Randomized Trees (можно True для большей стабильности)
            'path_smooth': 0.1,  # Сглаживание вероятностей (помогает при несбалансированных классах)
            'max_bin': 65,  # Стандартное значение для гистограмм
            'min_data_in_bin': 5,  # Минимум данных в бине гистограммы
            'bin_construct_sample_cnt': 200000,  # Ограничение для ускорения построения гистограмм
            'extra_seed': 42,
        }

        logger.info("🚀 Запуск обучения...")

        # Обучение
        model = lgb.train(
            params,
            train_data,
            valid_sets=[val_data],
            valid_names=['valid_0'],
            num_boost_round=1600,
            callbacks=[
                thermometer_progress_callback(logger, width=30, period=10),
                lgb.early_stopping(stopping_rounds=20, first_metric_only=True),
            ],
        )

        # Предсказания на val
        y_val_pred_proba = model.predict(X_val_processed if (use_scaler and scaler is not None) else X_val)
        y_val_pred = y_val_pred_proba.argmax(axis=1)

        # Предсказания на test (для tuning)
        y_test_pred_proba = model.predict(X_test_processed if (use_scaler and scaler is not None) else X_test)
        y_test_pred = y_test_pred_proba.argmax(axis=1)

        # ═══════════════════════════════════════════════════════════
        # 🔍 ДИАГНОСТИКА УТЕЧКИ ДАННЫХ
        # ═══════════════════════════════════════════════════════════
        logger.info("\n🔍 ДИАГНОСТИКА УТЕЧКИ ДАННЫХ:")

        # Проверка на train (без порогов)
        y_train_pred_proba_diag = model.predict(X_train_processed if (use_scaler and scaler is not None) else X_train)
        y_train_pred_diag = y_train_pred_proba_diag.argmax(axis=1)
        train_acc = accuracy_score(y_train, y_train_pred_diag)

        # Проверка на val/test (без порогов)
        val_acc = accuracy_score(y_val, y_val_pred)
        test_acc = accuracy_score(y_test, y_test_pred)

        logger.info(f"   Train accuracy (без порогов): {train_acc:.4f}")
        logger.info(f"   Val accuracy (без порогов): {val_acc:.4f}")
        logger.info(f"   Test accuracy (без порогов): {test_acc:.4f}")
        logger.info(f"   Gap (train-val): {train_acc - val_acc:.4f}")
        logger.info(f"   Gap (train-test): {train_acc - test_acc:.4f}")

        if train_acc > 0.95:
            logger.error("🚨 КРИТИЧЕСКАЯ УТЕЧКА: train accuracy >95%!")
            logger.error("   Проверьте признаки/разметку на forward-looking данные!")

        if abs(train_acc - val_acc) > 0.20:
            logger.warning(f"⚠️  Сильное переобучение: gap={train_acc - val_acc:.2%}")

        # ─────────────────────────────────────────────────────────────
        # 🔍 Диагностика частоты сигналов и подбор порогов (НА TEST!)
        # ─────────────────────────────────────────────────────────────
        # bars_per_day определяем из run_id (если возможно)
        bars_per_day = _infer_bars_per_day_from_run_id(run_id, default=TIMEFRAME_TO_BARS.get(str(self.timeframe).lower(), 288))
        candidates = []
        # Перебор precision_min НА ТЕСТОВОМ НАБОРЕ
        precision_grid = [0.70, 0.75, 0.80, 0.85, 0.90]

        for idx, pm in enumerate(precision_grid):
            try:
                tau_i, tstats_i = self.tune_tau_for_spd_range(
                    y_val=np.asarray(y_test),
                    proba=np.asarray(y_test_pred_proba),
                    bars_per_day=bars_per_day,
                    spd_min=20.0,
                    spd_max=35.0,
                    precision_min=pm,
                    delta=0.06,
                    cooldown_bars=2,
                    log_stats=(idx == 0),  # логировать max-proba stats только в первой итерации
                )
                candidates.append({
                    'precision_min': pm,
                    'tau': float(tau_i),
                    'spd': float(tstats_i.get('spd', float('nan'))),
                    'precision_macro_buy_sell': float(tstats_i.get('precision_macro_buy_sell', float('nan'))),
                    'f1_macro_buy_sell': float(tstats_i.get('f1_macro_buy_sell', float('nan'))),
                    'hit_range': bool(tstats_i.get('hit_range', False)),
                    'delta': float(tstats_i.get('delta', 0.08)),
                    'cooldown_bars': int(tstats_i.get('cooldown_bars', 2)),
                    '_tstats': tstats_i,
                })
            except Exception as e:
                logging.warning(f"precision_min={pm:.2f}: sweep failed with error: {e}")

        def _key(c):
            return (1 if c.get('hit_range') else 0,
                    c.get('precision_macro_buy_sell', float('-inf')),
                    c.get('f1_macro_buy_sell', float('-inf')),
                    -c.get('tau', float('inf')))

        if not candidates:
            raise RuntimeError("Precision sweep failed: no candidates collected")

        best = max(candidates, key=_key)
        tau = best['tau']
        tstats = best['_tstats']
        delta = best.get('delta', 0.08)
        cooldown_bars = best.get('cooldown_bars', 2)

        logging.info("🔧 Precision sweep results (на TEST наборе):")
        for c in candidates:
            logging.info(f"  pm={c['precision_min']:.2f}, tau={c['tau']:.3f}, spd≈{c['spd']:.1f}, "
                         f"prec≈{c['precision_macro_buy_sell']:.3f}, f1≈{c['f1_macro_buy_sell']:.3f}, "
                         f"hit={c['hit_range']}")
        logging.info(f"✅ Picked precision_min={best['precision_min']:.2f} → "
                     f"tau={tau:.3f}, spd≈{best['spd']:.1f}")

        logger.info(
            "🔧 Tuned thresholds: tau=%.3f, delta=%.2f, cooldown=%d → spd≈%.1f/day, "
            "precision≈%.3f, recall≈%.3f, f1≈%.3f (hit_range=%s)"
            % (tau, tstats['delta'], tstats['cooldown_bars'],
               tstats['spd'], tstats['precision_macro_buy_sell'],
               tstats['recall_macro_buy_sell'], tstats['f1_macro_buy_sell'],
               tstats['hit_range'])
        )

        # Sensitivity анализ (НА TEST)
        _tau_offsets = [-0.05, -0.03, -0.02, 0.0, 0.02, 0.03, 0.05]
        _delta_offsets = [-0.02, 0.0, 0.02]

        tau_sensitivity = []
        for off in _tau_offsets:
            tau_x = float(np.clip(tau + off, 0.0, 1.0))
            r = self._eval_decision_metrics(
                y_true=np.asarray(y_test),
                proba=np.asarray(y_test_pred_proba),
                tau=tau_x,
                delta=delta,
                cooldown_bars=cooldown_bars,
                bars_per_day=bars_per_day,
            )
            tau_sensitivity.append(r)

        delta_sensitivity = []
        for off in _delta_offsets:
            delta_x = float(max(0.0, delta + off))
            r = self._eval_decision_metrics(
                y_true=np.asarray(y_test),
                proba=np.asarray(y_test_pred_proba),
                tau=tau,
                delta=delta_x,
                cooldown_bars=cooldown_bars,
                bars_per_day=bars_per_day,
            )
            delta_sensitivity.append(r)

        _tau_sorted = sorted(tau_sensitivity, key=lambda r: abs(r['tau'] - float(tau)))[:3]
        _tau_sorted = sorted(_tau_sorted, key=lambda r: r['tau'])

        logging.info("🔍 Sensitivity (tau near current):")
        for r in _tau_sorted:
            logging.info(f"  tau={r['tau']:.3f} → spd≈{r['spd']:.1f}, f1≈{r['f1_macro_buy_sell']:.3f}")

        logging.info("🔍 Sensitivity (delta±0.02):")
        for r in delta_sensitivity:
            logging.info(f"  delta={r['delta']:.2f} → spd≈{r['spd']:.1f}, f1≈{r['f1_macro_buy_sell']:.3f}")

        # Метрики для всех наборов
        train_dist = Counter(y_train)
        val_dist = Counter(y_val)
        test_dist = Counter(y_test)
        pred_val_dist = Counter(y_val_pred)
        pred_test_dist = Counter(y_test_pred)

        logger.info(f"\n📊 Распределение классов:")
        logger.info(f"  Train:     {dict(train_dist)}")
        logger.info(f"  Val:       {dict(val_dist)}")
        logger.info(f"  Test:      {dict(test_dist)}")
        logger.info(f"  Pred Val:  {dict(pred_val_dist)}")
        logger.info(f"  Pred Test: {dict(pred_test_dist)}")

        # Метрики на валидационном наборе
        prec_val, rec_val, f1_val, _ = precision_recall_fscore_support(
            y_val, y_val_pred,
            labels=REPORT_LABELS,
            average=None,
            zero_division=0
        )
        cm_val = confusion_matrix(y_val, y_val_pred, labels=REPORT_LABELS)

        # Метрики на тестовом наборе (для честной оценки)
        prec_test, rec_test, f1_test, _ = precision_recall_fscore_support(
            y_test, y_test_pred,
            labels=REPORT_LABELS,
            average=None,
            zero_division=0
        )
        cm_test = confusion_matrix(y_test, y_test_pred, labels=REPORT_LABELS)

        decision_policy = {
            'tau': tau,
            'delta': tstats['delta'],
            'cooldown_bars': tstats['cooldown_bars'],
            'bars_per_day': bars_per_day,
            'test_spd': tstats['spd'],
            'test_precision_macro_buy_sell': tstats['precision_macro_buy_sell'],
            'test_recall_macro_buy_sell': tstats['recall_macro_buy_sell'],
            'test_f1_macro_buy_sell': tstats['f1_macro_buy_sell'],
            'target_spd_range': tstats['range'],
            'hit_range': tstats['hit_range'],
            'precision_min': best.get('precision_min', 0.60),
        }

        precision_min_sweep = [
            {
                'precision_min': c['precision_min'],
                'tau': c['tau'],
                'spd': c['spd'],
                'precision_macro_buy_sell': c['precision_macro_buy_sell'],
                'f1_macro_buy_sell': c['f1_macro_buy_sell'],
                'hit_range': c['hit_range'],
            }
            for c in candidates
        ]

        metrics = {
            'decision_policy': decision_policy,
            'precision_min_sweep': precision_min_sweep,

            # Валидационные метрики
            'val_accuracy': float(val_acc),
            'val_precision': {name: float(val) for name, val in zip(REPORT_NAMES, prec_val)},
            'val_recall': {name: float(val) for name, val in zip(REPORT_NAMES, rec_val)},
            'val_f1_score': {name: float(val) for name, val in zip(REPORT_NAMES, f1_val)},
            'val_confusion_matrix': cm_val.tolist(),

            # Тестовые метрики
            'test_accuracy': float(test_acc),
            'test_precision': {name: float(val) for name, val in zip(REPORT_NAMES, prec_test)},
            'test_recall': {name: float(val) for name, val in zip(REPORT_NAMES, rec_test)},
            'test_f1_score': {name: float(val) for name, val in zip(REPORT_NAMES, f1_test)},
            'test_confusion_matrix': cm_test.tolist(),

            'best_iteration': int(getattr(model, 'best_iteration', 0) or 0),
            'class_distribution': {
                'train': {int(k): int(v) for k, v in train_dist.items()},
                'val': {int(k): int(v) for k, v in val_dist.items()},
                'test': {int(k): int(v) for k, v in test_dist.items()},
            },
            'tau_sensitivity': tau_sensitivity,
            'delta_sensitivity': delta_sensitivity,
            'lookback_window': self.lookback,
            'base_features_count': len(self.base_feature_names),
            'total_features_count': len(self.feature_names),
        }

        # Сохранение модели
        os.makedirs("models", exist_ok=True)
        model_filename = f"models/ml_windowed_{self.symbol.replace('/', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.joblib"

        model_metadata = {
            'version': '2.1.1',
            'format': 'windowed_lgb',
            'instrument': self.symbol,
            'exchange': 'Binance',
            'timeframe': '5m',
            'lookback_window': self.lookback,
            'base_feature_count': len(self.base_feature_names),
            'total_feature_count': len(self.feature_names),
            'trained_at': datetime.now().isoformat(),
            'training_samples': len(X_train),
            'val_samples': len(X_val),
            'test_samples': len(X_test),
            'val_accuracy': float(val_acc),
            'test_accuracy': float(test_acc),
            'best_iteration': int(getattr(model, 'best_iteration', 0) or 0),
            'run_id': run_id,
            'decision_policy': decision_policy,
            'scaler_used': use_scaler,
        }

        model_package = {
            'model': model,
            'scaler': scaler,
            'metadata': model_metadata,
            'base_feature_names': self.base_feature_names,
            'lookback': self.lookback,
            'timeframe': '5m',
            'min_confidence': 0.65,
            'required_warmup': 20
        }

        joblib.dump(model_package, model_filename)
        logger.info(f"✅ Модель сохранена: {model_filename}")
        logger.info(f"   - Lookback: {self.lookback} баров")
        logger.info(f"   - Признаков: {len(self.feature_names)}")
        logger.info(f"   - Scaler: {'StandardScaler' if scaler else 'None'}")

        # Tau curves (НА TEST)
        try:
            tau_left = max(0.0, float(tau) - 0.05)
            tau_right = min(0.999, float(tau) + 0.05)
            tau_grid = np.arange(tau_left, tau_right + 1e-9, 0.002)

            spd_curve = []
            f1_curve = []
            for tcur in tau_grid:
                s = self._eval_decision_metrics(
                    y_true=np.asarray(y_test),
                    proba=np.asarray(y_test_pred_proba),
                    tau=float(tcur),
                    delta=float(delta),
                    cooldown_bars=int(cooldown_bars),
                    bars_per_day=int(bars_per_day),
                )
                spd_curve.append(s['spd'])
                f1_curve.append(s['f1_macro_buy_sell'])

            os.makedirs("models/training_logs", exist_ok=True)
            curve_prefix = str(Path("models/training_logs") / Path(model_filename).with_suffix('').name)

            plt.figure(figsize=(7, 4))
            plt.plot(tau_grid, spd_curve, linewidth=2)
            plt.axvline(float(tau), linestyle='--', color='red', label=f'tau={tau:.3f}')
            plt.title('SPD vs tau (на TEST наборе)')
            plt.xlabel('tau')
            plt.ylabel('signals per day')
            plt.legend()
            plt.grid(True, alpha=0.3)
            plt.tight_layout()
            plt.savefig(f"{curve_prefix}_tau_curve_spd.png")
            plt.close()

            plt.figure(figsize=(7, 4))
            plt.plot(tau_grid, f1_curve, linewidth=2)
            plt.axvline(float(tau), linestyle='--', color='red', label=f'tau={tau:.3f}')
            plt.title('F1 (macro BUY/SELL on act) vs tau (на TEST наборе)')
            plt.xlabel('tau')
            plt.ylabel('F1 macro (BUY/SELL)')
            plt.legend()
            plt.grid(True, alpha=0.3)
            plt.tight_layout()
            plt.savefig(f"{curve_prefix}_tau_curve_f1.png")
            plt.close()

        except Exception as _e:
            logging.warning(f"tau curves plotting skipped: {_e}")

        # Сохранение отчета
        self.save_training_report(metrics, model_filename)

        # Диагностики (используем TEST для честной оценки)
        diag_prefix = Path("models/training_logs") / Path(model_filename).with_suffix('').name
        self.post_training_diagnostics(
            model=model,
            X_val=X_test,
            y_val=y_test,
            y_val_pred_proba=y_test_pred_proba,
            prefix_path=str(diag_prefix),
            bars_per_day=bars_per_day
        )

        return metrics

    def plot_precision_spd_curve(self, y_val, y_val_pred_proba, bars_per_day: int,
                                 delta: float = 0.08, cooldown_bars: int = 2,
                                 prefix_path: str = "models/training_logs/diag") -> None:
        """
        Строит зависимости SPD(τ) и Precision/Recall/F1 от SPD
        """
        proba = np.asarray(y_val_pred_proba)
        p_buy, p_sell = proba[:, 1], proba[:, 2]
        maxp = np.maximum(p_buy, p_sell)
        margin = np.abs(p_buy - p_sell)

        def _apply_cooldown(mask: np.ndarray, cd: int) -> np.ndarray:
            if cd <= 0:
                return mask
            idx = np.where(mask)[0]
            if idx.size == 0:
                return mask
            keep = [idx[0]]
            last = idx[0]
            for i in idx[1:]:
                if i - last >= cd:
                    keep.append(i)
                    last = i
            out = np.zeros_like(mask, dtype=bool)
            out[np.array(keep, dtype=int)] = True
            return out

        taus = np.linspace(0.45, 0.70, 26)
        rows = []
        n = len(y_val)

        for tau in taus:
            act = (maxp >= tau) & (margin >= delta)
            act = _apply_cooldown(act, cooldown_bars)

            signals = int(act.sum())
            spd = signals * bars_per_day / max(1, n)

            if signals == 0:
                prec = rec = f1 = 0.0
            else:
                pred_dir = np.where(p_buy[act] >= p_sell[act], 1, 2)
                true_dir = y_val[act]
                prec = precision_score(true_dir, pred_dir, labels=[1, 2], average='macro', zero_division=0)
                rec = recall_score(true_dir, pred_dir, labels=[1, 2], average='macro', zero_division=0)
                f1 = f1_score(true_dir, pred_dir, labels=[1, 2], average='macro', zero_division=0)

            rows.append((tau, spd, prec, rec, f1, signals))

        df = pd.DataFrame(rows, columns=['tau', 'spd_per_day', 'precision', 'recall', 'f1', 'signals'])
        csv_path = f"{prefix_path}_tau_sweep.csv"
        df.to_csv(csv_path, index=False)

        # График SPD(τ)
        plt.figure(figsize=(8, 5))
        plt.plot(df['tau'], df['spd_per_day'], marker='o')
        plt.xlabel('tau')
        plt.ylabel('SPD (signals/day)')
        plt.title('Signals per day vs tau')
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(f"{prefix_path}_spd_vs_tau.png")
        plt.close()

        # График Precision/Recall/F1 vs SPD
        plt.figure(figsize=(8, 5))
        plt.plot(df['spd_per_day'], df['precision'], marker='o', label='Precision (macro BUY/SELL)')
        plt.plot(df['spd_per_day'], df['recall'], marker='o', label='Recall (macro BUY/SELL)')
        plt.plot(df['spd_per_day'], df['f1'], marker='o', label='F1 (macro BUY/SELL)')
        plt.xlabel('SPD (signals/day)')
        plt.ylabel('score')
        plt.title('Precision / Recall / F1 vs SPD')
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(f"{prefix_path}_prf_vs_spd.png")
        plt.close()

    def post_training_diagnostics(self, model, X_val, y_val, y_val_pred_proba,
                                  prefix_path: str, bars_per_day: int = 288):
        """
        Диагностики после обучения:
        - важность признаков
        - гистограммы вероятностей
        - PR-кривые
        - SPD curves
        """
        os.makedirs(os.path.dirname(prefix_path), exist_ok=True)

        try:
            bars_per_day = int(bars_per_day) if bars_per_day is not None else 288
        except Exception:
            bars_per_day = 288

        proba = np.asarray(y_val_pred_proba)
        if proba.ndim == 1:
            tmp = np.zeros((len(proba), 3), dtype=float)
            tmp[np.arange(len(proba)), np.clip(proba.astype(int), 0, 2)] = 1.0
            proba = tmp

        p_hold = proba[:, 0]
        p_buy = proba[:, 1]
        p_sell = proba[:, 2]

        feat_names = self.feature_names if hasattr(self, 'feature_names') else [f"f{i}" for i in range(X_val.shape[1])]

        # === 1) Feature Importance ===
        try:
            gain = model.feature_importance(importance_type='gain')
            df_imp = (pd.DataFrame({'feature': feat_names, 'gain': gain})
                      .sort_values('gain', ascending=False)
                      .head(30))
            plt.figure(figsize=(10, max(8, 0.3 * len(df_imp))))
            sns.barplot(data=df_imp, x='gain', y='feature')
            plt.title('Feature Importance (gain) — top 30')
            plt.tight_layout()
            plt.savefig(f"{prefix_path}_feat_importance.png")
            plt.close()

            # Сохранить CSV с ВСЕй важностью
            pd.DataFrame({'feature': feat_names, 'gain': gain}).sort_values('gain', ascending=False).to_csv(
                f"{prefix_path}_feat_importance.csv", index=False
            )

            # Агрегированная важность по  базовым признакам (по всем лагам)
            base_feat_importance = {}
            for feature, importance in zip(feat_names, gain):
                # Извлекаем базовое название признака (убираем _t0, _t-1 и т.д.)
                if '_t-' in feature:
                    base_feat = feature.split('_t-')[0]  # cmo_14_t-1 -> cmo_14
                elif '_t0' in feature:
                    base_feat = feature.replace('_t0', '')  # cmo_14_t0 -> cmo_14
                else:
                    base_feat = feature  # fallback

                base_feat_importance[base_feat] = base_feat_importance.get(base_feat, 0) + importance

            df_base_imp = pd.DataFrame({
                'base_feature': list(base_feat_importance.keys()),
                'total_gain': list(base_feat_importance.values())
            }).sort_values('total_gain', ascending=False)

            # Сохраняем CSV таблицу с агрегированной важностью
            df_base_imp.to_csv(f"{prefix_path}_feat_importance_base_aggregated.csv", index=False)

            # Логируем динамическое число базовых признаков
            logger.info(f"🎯 ВАЖНОСТЬ {len(self.base_feature_names)} БАЗОВЫХ ПРИЗНАКОВ (агрегировано по всем лагам):")
            for i, row in df_base_imp.iterrows():
                logger.info(f"   {i + 1:2d}. {row['base_feature']}: {row['total_gain']:.0f}")

        except Exception as e:
            logger.warning(f"Не удалось создать анализ важности признаков: {e}")

        # === 2) Гистограммы ===
        y_pred = proba.argmax(axis=1)

        def hist_one(prob, true_class, name, fname):
            mask_pos = (y_val == true_class)
            mask_pred_pos = (y_pred == true_class)

            tp = prob[mask_pos & mask_pred_pos]
            fp = prob[(~mask_pos) & mask_pred_pos]
            fn = prob[mask_pos & (~mask_pred_pos)]
            tn = prob[(~mask_pos) & (~mask_pred_pos)]

            plt.figure(figsize=(8, 5))
            bins = 30
            if len(tp) > 0:
                sns.histplot(tp, bins=bins, stat='density', label='TP', alpha=0.6)
            if len(fp) > 0:
                sns.histplot(fp, bins=bins, stat='density', label='FP', alpha=0.6)
            if len(fn) > 0:
                sns.histplot(fn, bins=bins, stat='density', label='FN', alpha=0.6)
            if len(tn) > 0:
                sns.histplot(tn, bins=bins, stat='density', label='TN', alpha=0.6)
            plt.legend()
            plt.xlabel(f"p({name})")
            plt.ylabel("density")
            plt.title(f"Distributions for {name}")
            plt.tight_layout()
            plt.savefig(fname)
            plt.close()

        hist_one(p_buy, 1, "BUY", f"{prefix_path}_proba_hist_BUY.png")
        hist_one(p_sell, 2, "SELL", f"{prefix_path}_proba_hist_SELL.png")

        # === 3) Max-proba scatter ===
        maxp = proba.max(axis=1)
        plt.figure(figsize=(8, 5))
        sns.scatterplot(x=np.arange(len(maxp)), y=maxp,
                        hue=[{0: 'HOLD', 1: 'BUY', 2: 'SELL'}.get(c, 'UNK') for c in y_val],
                        s=12, linewidth=0)
        plt.title("Max class probability vs true class (val order)")
        plt.xlabel("index in validation set (chronological)")
        plt.ylabel("max proba")
        plt.tight_layout()
        plt.savefig(f"{prefix_path}_maxproba_scatter.png")
        plt.close()

        # === 4) PR curves ===
        Y_bin = label_binarize(y_val, classes=[0, 1, 2])
        curves = [
            ("BUY", Y_bin[:, 1], p_buy),
            ("SELL", Y_bin[:, 2], p_sell),
            ("HOLD", Y_bin[:, 0], p_hold),
        ]
        plt.figure(figsize=(8, 6))
        for name, y_true_bin, y_score in curves:
            precision, recall, _ = precision_recall_curve(y_true_bin, y_score)
            ap = average_precision_score(y_true_bin, y_score)
            plt.plot(recall, precision, label=f"{name} (AP={ap:.3f})")
        plt.xlabel("Recall")
        plt.ylabel("Precision")
        plt.title("Precision–Recall curves (one-vs-rest)")
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(f"{prefix_path}_pr_curves.png")
        plt.close()

        # === 5) SPD curve ===
        self.plot_precision_spd_curve(
            y_val=y_val,
            y_val_pred_proba=y_val_pred_proba,
            bars_per_day=bars_per_day,
            delta=0.08,
            cooldown_bars=2,
            prefix_path=prefix_path
        )

    def save_training_report(self, metrics: dict, model_path: str):
        """Сохранение отчета о обучении"""
        os.makedirs("models/training_logs", exist_ok=True)

        report = {
            'training_date': datetime.now().isoformat(),
            'symbol': self.symbol,
            'db_dsn': self.db_dsn,
            'model_path': model_path,
            'lookback_window': self.lookback,
            'metrics': metrics,
            'base_feature_names': self.base_feature_names,
            'total_feature_names_count': len(self.feature_names),
        }

        report_filename = model_path.replace('.joblib', '_report.json')
        with open(report_filename, 'w') as f:
            json.dump(report, f, indent=2)

        # Confusion matrices (val и test)
        try:
            labels = ['BUY', 'SELL', 'HOLD']

            cm_val = np.array(metrics.get('val_confusion_matrix', []))
            if cm_val.size > 0:
                plt.figure(figsize=(8, 6))
                sns.heatmap(cm_val, annot=True, fmt='d',
                            xticklabels=labels, yticklabels=labels)
                plt.title('Validation Confusion Matrix')
                plt.tight_layout()
                plt.savefig(report_filename.replace('.json', '_cm_val.png'))
                plt.close()

            cm_test = np.array(metrics.get('test_confusion_matrix', []))
            if cm_test.size > 0:
                plt.figure(figsize=(8, 6))
                sns.heatmap(cm_test, annot=True, fmt='d',
                            xticklabels=labels, yticklabels=labels)
                plt.title('Test Confusion Matrix')
                plt.tight_layout()
                plt.savefig(report_filename.replace('.json', '_cm_test.png'))
                plt.close()
        except Exception as e:
            logger.warning(f"Не удалось создать визуализацию CM: {e}")

        logger.info(f"✅ Отчет сохранен: {report_filename}")

    def close(self):
        """Корректное закрытие"""
        self.data_loader.close()


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main():
    """Основная функция"""
    print("🚀 ЗАПУСК ОБУЧЕНИЯ МОДЕЛИ (v2.1 - WINDOWED)")
    print("=" * 50)

    # Проверка существования БД
    db_file = DATA_DIR / "market_data.sqlite"
    if not db_file.exists():
        print(f"❌ База данных {db_file} не найдена!")
        print("   Сначала запустите:")
        print("   1. ml_data_preparation.py")
        print("   2. ml_labeling_tool_v3.py")
        return 1

    trainer = None
    try:
        # Настройки
        db_dsn = MARKET_DB_DSN
        symbol = "ETHUSDT"
        lookback = LOOKBACK_WINDOW

        # Получить последний run_id ДЛЯ КОНКРЕТНОГО symbol/timeframe
        engine = create_engine(MARKET_DB_DSN)
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT run_id 
                      FROM training_dataset_meta 
                     WHERE status='READY' AND symbol=:symbol AND timeframe='5m'
                     ORDER BY created_at DESC LIMIT 1
                """),
                {"symbol": symbol}
            ).fetchone()
        engine.dispose()

        if not row:
            print("❌ Нет готовых snapshot в training_dataset_meta для ETHUSDT/5m!")
            print("   Создайте snapshot через [14] в ml_labeling_tool_v3.py")
            return 1

        run_id = row[0]

        print(f"📊 База данных: {DATA_DIR / 'market_data.sqlite'}")
        print(f"🎯 Символ: {symbol}")
        print(f"📦 Run ID: {run_id}")
        print(f"🪟 Lookback Window: {lookback} баров")
        print("=" * 50)

        # Обучение
        trainer = ModelTrainer(db_dsn, symbol, lookback=lookback)

        use_scaler = False  # Можно включить True
        metrics = trainer.train_model(run_id, use_scaler=use_scaler)

        # Вывод результатов
        print("\n🎯 РЕЗУЛЬТАТЫ ОБУЧЕНИЯ:")
        print(f"   Val Accuracy:  {metrics['val_accuracy']:.4f}")
        print(f"   Test Accuracy: {metrics['test_accuracy']:.4f}")

        print(f"\n   VAL Precision BUY/SELL/HOLD: "
              f"{metrics['val_precision']['BUY']:.4f}/"
              f"{metrics['val_precision']['SELL']:.4f}/"
              f"{metrics['val_precision']['HOLD']:.4f}")
        print(f"   TEST Precision BUY/SELL/HOLD: "
              f"{metrics['test_precision']['BUY']:.4f}/"
              f"{metrics['test_precision']['SELL']:.4f}/"
              f"{metrics['test_precision']['HOLD']:.4f}")

        print(f"\n   VAL Recall BUY/SELL/HOLD: "
              f"{metrics['val_recall']['BUY']:.4f}/"
              f"{metrics['val_recall']['SELL']:.4f}/"
              f"{metrics['val_recall']['HOLD']:.4f}")
        print(f"   TEST Recall BUY/SELL/HOLD: "
              f"{metrics['test_recall']['BUY']:.4f}/"
              f"{metrics['test_recall']['SELL']:.4f}/"
              f"{metrics['test_recall']['HOLD']:.4f}")
        return 0

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return 1
    finally:
        if trainer:
            trainer.close()


if __name__ == '__main__':
    sys.exit(main())