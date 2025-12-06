"""
ml_labeling_viewer.py
Интерактивный веб-интерфейс для просмотра и редактирования разметки
"""

import pandas as pd
import numpy as np
from typing import Tuple, Dict, Optional, List
from datetime import datetime
from pathlib import Path

# Plotly & Dash
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import dash
from dash import dcc, html, Input, Output, State, callback_context, clientside_callback, ClientsideFunction
import dash_bootstrap_components as dbc

# Database
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# Existing modules
from config import TABLES
from ml_labeling_tool_v3 import AdvancedLabelingTool, LabelingConfig

# Constants
DATA_DIR = Path("data")
MARKET_DB_DSN = f"sqlite:///{DATA_DIR}/market_data.sqlite"
DEFAULT_BLOCK_SIZE = 100

# Label colors
LABEL_COLORS = {
    'BUY': 'green',
    'SELL': 'red',
    'HOLD': 'gray'
}

LABEL_SYMBOLS = {
    'BUY': 'triangle-up',
    'SELL': 'triangle-down',
    'HOLD': 'circle'
}



# === CLASS LabelingViewer ===
class LabelingViewer:
    """
    Интерактивный просмотрщик и редактор разметки

    Features:
    - 3-блочное окно (BEFORE | CURRENT | AFTER)
    - Редактирование в любом блоке
    - Автосохранение при каждом изменении
    - Hotkeys через JavaScript
    - Dropdown на графике для добавления меток
    """

    def __init__(self, config: LabelingConfig):
        """
        Args:
            config: конфигурация разметки (symbol, timeframe)
        """
        self.config = config
        self.engine: Engine = create_engine(MARKET_DB_DSN)
        self.tool = AdvancedLabelingTool(config)

        # Data
        self.df_candles: Optional[pd.DataFrame] = None
        self.df_labels: Optional[pd.DataFrame] = None

        # Window state
        self.current_index = 0
        self.block_size = DEFAULT_BLOCK_SIZE

        # Filters
        self.filter_method: Optional[str] = None
        self.filter_type: Optional[int] = None

        # Dash app
        self.app: Optional[dash.Dash] = None

        print(f"✅ LabelingViewer initialized for {config.symbol} {config.timeframe}")
    # --- Data methods ---
    def load_data(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Загрузка свечей и разметки из БД

        Returns:
            (df_candles, df_labels)
        """
        # Загрузка свечей с индикаторами
        query_candles = text(f"""
            SELECT * FROM {TABLES['candles_5m']}
            WHERE symbol = :symbol
            ORDER BY ts
        """)

        df_candles = pd.read_sql(query_candles, self.engine, params={'symbol': self.config.symbol})

        if df_candles.empty:
            raise ValueError(f"No candles found for {self.config.symbol}")

        # Добавляем datetime
        if 'datetime' not in df_candles.columns:
            df_candles['datetime'] = pd.to_datetime(df_candles['ts'], unit='ms')

        # Загрузка меток
        query_labels = text("""
            SELECT 
                extreme_timestamp,
                reversal_label,
                reversal_confidence,
                labeling_method,
                price_change_after as pnl,
                is_high_quality
            FROM labeling_results
            WHERE symbol = :symbol
            ORDER BY extreme_timestamp
        """)

        df_labels = pd.read_sql(query_labels, self.engine, params={'symbol': self.config.symbol})

        # Merge labels с candles по ts
        df_candles = df_candles.merge(
            df_labels,
            left_on='ts',
            right_on='extreme_timestamp',
            how='left'
        )

        print(f"📊 Loaded {len(df_candles)} candles, {len(df_labels)} labels")

        return df_candles, df_labels

    def _reload_labels(self) -> pd.DataFrame:
        """Перезагрузка только меток после изменений"""
        query = text("""
            SELECT 
                extreme_timestamp,
                reversal_label,
                reversal_confidence,
                labeling_method,
                price_change_after as pnl,
                is_high_quality
            FROM labeling_results
            WHERE symbol = :symbol
            ORDER BY extreme_timestamp
        """)
        return pd.read_sql(query, self.engine, params={'symbol': self.config.symbol})

    def calculate_window_bounds(self, center_index: int, block_size: int) -> Dict[str, int]:
        """
        Рассчитывает границы 3 блоков

        Layout:
        [BEFORE: gray] | [CURRENT: white] | [AFTER: gray]

        Args:
            center_index: начало CURRENT блока
            block_size: размер каждого блока

        Returns:
            dict с границами всех блоков
        """
        total_len = len(self.df_candles)

        # CURRENT block
        current_start = max(0, center_index)
        current_end = min(total_len, current_start + block_size)

        # BEFORE block
        before_start = max(0, current_start - block_size)
        before_end = current_start

        # AFTER block
        after_start = current_end
        after_end = min(total_len, current_end + block_size)

        return {
            'before_start': before_start,
            'before_end': before_end,
            'current_start': current_start,
            'current_end': current_end,
            'after_start': after_start,
            'after_end': after_end,
            'total_start': before_start,
            'total_end': after_end
        }

    def create_figure(self, bounds: Dict) -> go.Figure:
        """
        Создание интерактивного графика с 4 subplots

        Subplots:
        1. Price (candlestick) + BB bands + labels
        2. Volume + volume_ratio_ema3
        3. CUSUM (zscore line + state bars)
        4. ATR normalized

        Args:
            bounds: границы 3-блочного окна

        Returns:
            plotly Figure
        """
        # Определяем какие subplots показывать
        subplots_to_show = self._determine_subplots()
        n_rows = len(subplots_to_show)

        if n_rows == 0:
            # Только price subplot
            subplots_to_show = ['price']
            n_rows = 1

        # Высоты subplots
        if n_rows == 1:
            row_heights = [1.0]
        elif n_rows == 2:
            row_heights = [0.7, 0.3]
        elif n_rows == 3:
            row_heights = [0.6, 0.2, 0.2]
        else:
            row_heights = [0.5, 0.15, 0.20, 0.15]

        # Создание subplots
        fig = make_subplots(
            rows=n_rows,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.02,
            row_heights=row_heights,
            subplot_titles=tuple([s.upper() for s in subplots_to_show])
        )

        # Добавляем каждый subplot
        row = 1
        for subplot_name in subplots_to_show:
            if subplot_name == 'price':
                self._add_price_subplot(fig, bounds, row)
            elif subplot_name == 'volume':
                self._add_volume_subplot(fig, bounds, row)
            elif subplot_name == 'cusum':
                self._add_cusum_subplot(fig, bounds, row)
            elif subplot_name == 'atr':
                self._add_atr_subplot(fig, bounds, row)
            row += 1

        # Фоновые зоны (серые области для BEFORE/AFTER)
        self._add_background_zones(fig, bounds)

        # Настройка layout
        fig.update_layout(
            height=800,
            showlegend=True,
            hovermode='x unified',
            xaxis_rangeslider_visible=False,
            margin=dict(l=50, r=50, t=80, b=50)
        )

        # X-axis labels только на нижнем графике
        fig.update_xaxes(title_text="Index", row=n_rows, col=1)

        return fig

    def _determine_subplots(self) -> List[str]:
        """
        Определяет какие subplots показывать на основе доступных данных

        Returns:
            список названий subplots
        """
        subplots = ['price']  # Price всегда показываем

        # Проверяем наличие индикаторов
        if 'volume' in self.df_candles.columns and self.df_candles['volume'].notna().any():
            subplots.append('volume')

        if 'cusum_zscore' in self.df_candles.columns and self.df_candles['cusum_zscore'].notna().any():
            subplots.append('cusum')

        if 'atr_14_normalized' in self.df_candles.columns and self.df_candles['atr_14_normalized'].notna().any():
            subplots.append('atr')

        return subplots

    def _add_price_subplot(self, fig: go.Figure, bounds: Dict, row: int):
        """
        Добавляет price subplot с candlestick, BB bands и метками

        Args:
            fig: plotly фигура
            bounds: границы окна
            row: номер строки subplot
        """
        df_window = self.df_candles.iloc[bounds['total_start']:bounds['total_end']]

        # Candlestick
        fig.add_trace(
            go.Candlestick(
                x=df_window.index,
                open=df_window['open'],
                high=df_window['high'],
                low=df_window['low'],
                close=df_window['close'],
                name='Price',
                increasing_line_color='green',
                decreasing_line_color='red'
            ),
            row=row, col=1
        )

        # BB bands (для всех 3 блоков)
        if 'bb_upper' in df_window.columns or all(
                col in df_window.columns for col in ['close', 'bb_width', 'bb_position']):
            self._add_bb_bands(fig, df_window, row)

        # Метки (markers)
        self._add_label_markers(fig, bounds, row)

    def _add_bb_bands(self, fig: go.Figure, df: pd.DataFrame, row: int):
        """
        Добавляет Bollinger Bands

        Note: BB bands рассчитываются если есть bb_width/bb_position
        или напрямую загружаются если есть bb_upper/bb_lower
        """
        # Попытка 1: прямые колонки bb_upper/bb_lower
        if 'bb_upper' in df.columns and 'bb_lower' in df.columns:
            upper = df['bb_upper']
            lower = df['bb_lower']
            middle = (upper + lower) / 2

        # Попытка 2: расчет через bb_width и SMA(20)
        elif 'close' in df.columns:
            # Простой SMA(20) как middle band
            middle = df['close'].rolling(20).mean()
            std = df['close'].rolling(20).std()
            upper = middle + 2 * std
            lower = middle - 2 * std
        else:
            return  # Нет данных для BB

        # Upper band
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=upper,
                name='BB Upper',
                line=dict(color='rgba(173, 216, 230, 0.5)', width=1),
                showlegend=False
            ),
            row=row, col=1
        )

        # Lower band
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=lower,
                name='BB Lower',
                line=dict(color='rgba(173, 216, 230, 0.5)', width=1),
                fill='tonexty',
                fillcolor='rgba(173, 216, 230, 0.1)',
                showlegend=False
            ),
            row=row, col=1
        )

        # Middle band
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=middle,
                name='BB Middle',
                line=dict(color='rgba(100, 149, 237, 0.7)', width=1, dash='dot'),
                showlegend=False
            ),
            row=row, col=1
        )

    def _add_volume_subplot(self, fig: go.Figure, bounds: Dict, row: int):
        """
        Добавляет volume subplot с барами + volume_ratio_ema3

        Args:
            fig: plotly фигура
            bounds: границы окна
            row: номер строки subplot
        """
        df_window = self.df_candles.iloc[bounds['total_start']:bounds['total_end']]

        # Volume bars
        colors = ['red' if row['close'] < row['open'] else 'green'
                  for _, row in df_window.iterrows()]

        fig.add_trace(
            go.Bar(
                x=df_window.index,
                y=df_window['volume'],
                name='Volume',
                marker_color=colors,
                opacity=0.6,
                showlegend=False
            ),
            row=row, col=1
        )

        # Volume ratio EMA3 (если есть)
        if 'volume_ratio_ema3' in df_window.columns:
            volume_ratio = df_window['volume_ratio_ema3'].dropna()

            if not volume_ratio.empty:
                fig.add_trace(
                    go.Scatter(
                        x=volume_ratio.index,
                        y=volume_ratio,
                        name='Vol Ratio EMA3',
                        line=dict(color='orange', width=1),
                        yaxis='y2'
                    ),
                    row=row, col=1
                )

                # Второй Y-axis для volume ratio
                fig.update_yaxes(
                    title_text="Volume",
                    row=row, col=1
                )
                fig.update_layout({
                    f'yaxis{row * 2}': dict(
                        title='Vol Ratio',
                        overlaying=f'y{row}',
                        side='right'
                    )
                })

    def _add_cusum_subplot(self, fig: go.Figure, bounds: Dict, row: int):
        """
        Добавляет CUSUM subplot

        Опция B:
        - Линия cusum_zscore (синяя)
        - Цветные бары cusum_state (зелёный=1, красный=-1, серый=0)

        Args:
            fig: plotly фигура
            bounds: границы окна
            row: номер строки subplot
        """
        df_window = self.df_candles.iloc[bounds['total_start']:bounds['total_end']]

        # CUSUM zscore line
        if 'cusum_zscore' in df_window.columns:
            zscore = df_window['cusum_zscore'].dropna()

            if not zscore.empty:
                fig.add_trace(
                    go.Scatter(
                        x=zscore.index,
                        y=zscore,
                        name='CUSUM Z-score',
                        line=dict(color='blue', width=2),
                        showlegend=False
                    ),
                    row=row, col=1
                )

        # CUSUM state bars (если есть)
        if 'cusum_state' in df_window.columns:
            state = df_window['cusum_state'].dropna()

            if not state.empty:
                # Цвета баров по состоянию
                bar_colors = state.map({
                    1: 'green',  # BUY
                    -1: 'red',  # SELL
                    0: 'gray'  # HOLD
                }).fillna('gray')

                fig.add_trace(
                    go.Bar(
                        x=state.index,
                        y=state,
                        name='CUSUM State',
                        marker_color=bar_colors,
                        opacity=0.4,
                        showlegend=False,
                        yaxis='y2'
                    ),
                    row=row, col=1
                )

                # Второй Y-axis для state bars
                fig.update_layout({
                    f'yaxis{row * 2}': dict(
                        title='State',
                        overlaying=f'y{row}',
                        side='right',
                        range=[-1.5, 1.5]
                    )
                })

        # Горизонтальная линия y=0
        fig.add_hline(
            y=0,
            line_dash="dash",
            line_color="gray",
            line_width=1,
            row=row, col=1
        )

        fig.update_yaxes(title_text="CUSUM Z-score", row=row, col=1)

    def _add_atr_subplot(self, fig: go.Figure, bounds: Dict, row: int):
        """
        Добавляет ATR normalized subplot

        Args:
            fig: plotly фигура
            bounds: границы окна
            row: номер строки subplot
        """
        df_window = self.df_candles.iloc[bounds['total_start']:bounds['total_end']]

        # ATR normalized line
        if 'atr_14_normalized' in df_window.columns:
            atr = df_window['atr_14_normalized'].dropna()

            if not atr.empty:
                fig.add_trace(
                    go.Scatter(
                        x=atr.index,
                        y=atr,
                        name='ATR Normalized',
                        line=dict(color='purple', width=2),
                        fill='tozeroy',
                        fillcolor='rgba(128, 0, 128, 0.1)',
                        showlegend=False
                    ),
                    row=row, col=1
                )

                fig.update_yaxes(title_text="ATR (normalized)", row=row, col=1)

    def _add_label_markers(self, fig: go.Figure, bounds: Dict, row: int):
        """
        Добавляет маркеры BUY/SELL/HOLD на график

        Схема:
        - BUY: green ▲ (high_quality=1: solid, =0: opacity 0.5)
        - SELL: red ▼ (high_quality=1: solid, =0: opacity 0.5)
        - HOLD: gray ● (всегда solid)
        """
        df_window = self.df_candles.iloc[bounds['total_start']:bounds['total_end']]

        # Фильтруем только строки с метками
        df_labeled = df_window[df_window['reversal_label'].notna()]

        if df_labeled.empty:
            return

        # Применяем фильтры (если установлены)
        if self.filter_method:
            df_labeled = df_labeled[df_labeled['labeling_method'] == self.filter_method]

        if self.filter_type is not None:
            df_labeled = df_labeled[df_labeled['reversal_label'] == self.filter_type]

        # Добавляем маркеры по типам
        for label_type in [1, 2, 0]:  # BUY, SELL, HOLD
            df_type = df_labeled[df_labeled['reversal_label'] == label_type]

            if df_type.empty:
                continue

            label_name = ['HOLD', 'BUY', 'SELL'][label_type]
            color = LABEL_COLORS[label_name]
            symbol = LABEL_SYMBOLS[label_name]

            # High quality метки
            df_hq = df_type[df_type['is_high_quality'] == 1] if label_type != 0 else df_type

            if not df_hq.empty:
                fig.add_trace(
                    go.Scatter(
                        x=df_hq.index,
                        y=df_hq['close'],
                        mode='markers',
                        marker=dict(
                            symbol=symbol,
                            size=12,
                            color=color,
                            line=dict(width=1, color='white')
                        ),
                        name=f'{label_name} (HQ)',
                        customdata=np.column_stack((
                            df_hq['ts'],
                            df_hq['reversal_confidence'],
                            df_hq['pnl'].fillna(0),
                            df_hq['labeling_method']
                        )),
                        hovertemplate=(
                                '<b>%{fullData.name}</b><br>' +
                                'Index: %{x}<br>' +
                                'Price: %{y:. 2f}<br>' +
                                'Timestamp: %{customdata[0]}<br>' +
                                'Confidence: %{customdata[1]:.2f}<br>' +
                                'PnL: %{customdata[2]:. 4f}<br>' +
                                'Method: %{customdata[3]}<extra></extra>'
                        )
                    ),
                    row=row, col=1
                )

            # Low quality метки (только для BUY/SELL)
            if label_type != 0:
                df_lq = df_type[df_type['is_high_quality'] == 0]

                if not df_lq.empty:
                    fig.add_trace(
                        go.Scatter(
                            x=df_lq.index,
                            y=df_lq['close'],
                            mode='markers',
                            marker=dict(
                                symbol=symbol,
                                size=12,
                                color=color,
                                opacity=0.5,
                                line=dict(width=1, color='white')
                            ),
                            name=f'{label_name} (LQ)',
                            customdata=np.column_stack((
                                df_lq['ts'],
                                df_lq['reversal_confidence'],
                                df_lq['pnl'].fillna(0),
                                df_lq['labeling_method']
                            )),
                            hovertemplate=(
                                    '<b>%{fullData. name}</b><br>' +
                                    'Index: %{x}<br>' +
                                    'Price: %{y:.2f}<br>' +
                                    'Timestamp: %{customdata[0]}<br>' +
                                    'Confidence: %{customdata[1]:.2f}<br>' +
                                    'PnL: %{customdata[2]:.4f}<br>' +
                                    'Method: %{customdata[3]}<extra></extra>'
                            )
                        ),
                        row=row, col=1
                    )

    # ...

    def _add_background_zones(self, fig: go.Figure, bounds: Dict):
        """
        Добавляет серые фоновые зоны для BEFORE и AFTER блоков

        BEFORE: серый фон слева
        CURRENT: белый фон (без фона)
        AFTER: серый фон справа

        Args:
            fig: plotly фигура
            bounds: границы окна
        """
        # BEFORE zone (серый)
        if bounds['before_start'] < bounds['before_end']:
            fig.add_vrect(
                x0=bounds['before_start'],
                x1=bounds['before_end'],
                fillcolor="rgba(200, 200, 200, 0.15)",
                layer="below",
                line_width=0
            )

        # AFTER zone (серый)
        if bounds['after_start'] < bounds['after_end']:
            fig.add_vrect(
                x0=bounds['after_start'],
                x1=bounds['after_end'],
                fillcolor="rgba(200, 200, 200, 0.15)",
                layer="below",
                line_width=0
            )

        # Вертикальные границы CURRENT блока (синие пунктирные линии)
        for x in [bounds['current_start'], bounds['current_end']]:
            fig.add_vline(
                x=x,
                line_dash="dash",
                line_color="blue",
                line_width=2,
                opacity=0.6
            )

    def create_dash_layout(self) -> html.Div:
        """
        Создает Dash layout с контролами и графиком

        Returns:
            html.Div с полным интерфейсом
        """
        return html.Div([
            # === HEADER: Controls ===
            dbc.Container([
                dbc.Row([
                    # Symbol selector
                    dbc.Col([
                        html.Label('Symbol:', style={'fontWeight': 'bold'}),
                        dcc.Dropdown(
                            id='symbol-dropdown',
                            options=[
                                {'label': 'ETHUSDT', 'value': 'ETHUSDT'},
                                {'label': 'BTCUSDT', 'value': 'BTCUSDT'}
                            ],
                            value=self.config.symbol,
                            clearable=False
                        )
                    ], width=2),

                    # Method filter
                    dbc.Col([
                        html.Label('Method:', style={'fontWeight': 'bold'}),
                        dcc.Dropdown(
                            id='method-filter',
                            options=[
                                {'label': 'ALL', 'value': 'ALL'},
                                {'label': 'CUSUM', 'value': 'CUSUM'},
                                {'label': 'BINSEG', 'value': 'BINSEG'},
                                {'label': 'EXTREMUM', 'value': 'EXTREMUM'},
                                {'label': 'PELT_OFFLINE', 'value': 'PELT_OFFLINE'}
                            ],
                            value='ALL',
                            clearable=False
                        )
                    ], width=2),

                    # Type filter
                    dbc.Col([
                        html.Label('Type:', style={'fontWeight': 'bold'}),
                        dcc.Dropdown(
                            id='type-filter',
                            options=[
                                {'label': 'ALL', 'value': 'ALL'},
                                {'label': 'BUY', 'value': 1},
                                {'label': 'SELL', 'value': 2},
                                {'label': 'HOLD', 'value': 0}
                            ],
                            value='ALL',
                            clearable=False
                        )
                    ], width=2),

                    # Block size
                    dbc.Col([
                        html.Label('Block Size:', style={'fontWeight': 'bold'}),
                        dcc.Dropdown(
                            id='block-size',
                            options=[
                                {'label': '50', 'value': 50},
                                {'label': '100', 'value': 100},
                                {'label': '200', 'value': 200}
                            ],
                            value=DEFAULT_BLOCK_SIZE,
                            clearable=False
                        )
                    ], width=2),

                    # Jump to index
                    dbc.Col([
                        html.Label('Jump to:', style={'fontWeight': 'bold'}),
                        dbc.InputGroup([
                            dbc.Input(
                                id='jump-input',
                                type='number',
                                value=0,
                                min=0,
                                step=1
                            ),
                            dbc.Button('Go', id='jump-btn', color='primary', size='sm')
                        ])
                    ], width=2),

                    # Navigation buttons
                    dbc.Col([
                        html.Label('\u00a0', style={'fontWeight': 'bold'}),  # nbsp
                        html.Div([
                            dbc.ButtonGroup([
                                dbc.Button('◄ Back', id='nav-back', color='secondary', size='sm'),
                                dbc.Button('Forward ►', id='nav-forward', color='secondary', size='sm')
                            ])
                        ])
                    ], width=2)
                ], className='mb-3')
            ], fluid=True, style={'backgroundColor': '#f8f9fa', 'padding': '15px', 'borderRadius': '5px'}),

            # === HOTKEYS INFO ===
            html.Div(
                'Hotkeys: A=Add | E=Edit | D=Delete | ← / → =Navigate (half block)',
                style={
                    'textAlign': 'center',
                    'color': 'gray',
                    'fontSize': '12px',
                    'margin': '10px 0',
                    'fontStyle': 'italic'
                }
            ),

            # === MAIN GRAPH ===
            dcc.Graph(
                id='main-chart',
                config={
                    'displayModeBar': True,
                    'scrollZoom': True,
                    'displaylogo': False
                },
                style={'height': '75vh'}
            ),

            # === SELECTED LABEL INFO ===
            html.Div(id='label-info', style={'margin': '20px'}),

            # === EDIT CONTROLS ===
            html.Div([
                dbc.ButtonGroup([
                    dbc.Button('Change to BUY', id='change-buy', color='success', size='sm'),
                    dbc.Button('Change to SELL', id='change-sell', color='danger', size='sm'),
                    dbc.Button('Change to HOLD', id='change-hold', color='secondary', size='sm'),
                    dbc.Button('Delete Label', id='delete-label', color='warning', size='sm')
                ], className='me-3'),

                dbc.InputGroup([
                    dbc.InputGroupText('Confidence:'),
                    dbc.Input(
                        id='confidence-input',
                        type='number',
                        placeholder='0.1-0.99',
                        min=0.1,
                    max=0.99,
        step = 0.05,
        style = {'width': '120px'}
        ),
        dbc.Button('Update', id='update-confidence', color='info', size='sm')
        ], style = {'width': '300px'})
        ], id = 'edit-controls', style = {'margin': '20px', 'display': 'none'}),

        # === ADD LABEL DROPDOWN (initially hidden) ===
        html.Div([
            dbc.Card([
                dbc.CardBody([
                    html.H6('Add Label at Index:', className='mb-2'),
                    html.Div(id='add-label-index', className='mb-2'),
                    dbc.ButtonGroup([
                        dbc.Button('BUY (0. 8)', id='add-buy', color='success', size='sm'),
                        dbc.Button('SELL (0.8)', id='add-sell', color='danger', size='sm'),
                        dbc.Button('HOLD (1. 0)', id='add-hold', color='secondary', size='sm'),
                        dbc.Button('✖ Cancel', id='add-cancel', color='light', size='sm')
                    ])
                ])
            ], style={'width': '300px'})
        ], id='add-label-dropdown', style={'display': 'none', 'position': 'absolute', 'zIndex': 1000}),

            # === HIDDEN STORES ===
        dcc.Store(id='current-index', data=0),
        dcc.Store(id='selected-label-ts', data=None),
        dcc.Store(id='add-label-click-index', data=None),

            # === KEYBOARD LISTENER ===
        html.Div(id='keyboard-listener', style={'display': 'none'})
        ])

    def setup_callbacks(self):
        """
        Настройка Python callbacks для основной логики
        """

        # === NAVIGATION ===
        @self.app.callback(
            Output('current-index', 'data'),
            [
                Input('nav-back', 'n_clicks'),
                Input('nav-forward', 'n_clicks'),
                Input('jump-btn', 'n_clicks')
            ],
            [
                State('current-index', 'data'),
                State('jump-input', 'value'),
                State('block-size', 'value')
            ],
            prevent_initial_call=True
        )
        def navigate(back_clicks, fwd_clicks, jump_clicks, current_idx, jump_val, block_size):
            """Навигация между блоками"""
            ctx = callback_context
            if not ctx.triggered:
                return current_idx

            btn_id = ctx.triggered[0]['prop_id'].split('.')[0]

            # Сдвиг на половину block_size (плавная прокрутка)
            half_block = block_size // 2

            if btn_id == 'nav-back':
                return max(0, current_idx - half_block)
            elif btn_id == 'nav-forward':
                max_idx = len(self.df_candles) - block_size
                return min(max_idx, current_idx + half_block)
            elif btn_id == 'jump-btn' and jump_val is not None:
                return max(0, min(jump_val, len(self.df_candles) - block_size))

            return current_idx

        # === GRAPH UPDATE ===
        @self.app.callback(
            Output('main-chart', 'figure'),
            [
                Input('current-index', 'data'),
                Input('block-size', 'value'),
                Input('method-filter', 'value'),
                Input('type-filter', 'value')
            ]
        )
        def update_graph(current_idx, block_size, method_filter, type_filter):
            """Обновление графика при изменении параметров"""
            self.current_index = current_idx
            self.block_size = block_size

            # Применяем фильтры
            self.filter_method = None if method_filter == 'ALL' else method_filter
            self.filter_type = None if type_filter == 'ALL' else type_filter

            bounds = self.calculate_window_bounds(current_idx, block_size)
            fig = self.create_figure(bounds)

            return fig

        # === LABEL SELECTION ===
        @self.app.callback(
            [
                Output('label-info', 'children'),
                Output('edit-controls', 'style'),
                Output('selected-label-ts', 'data')
            ],
            [Input('main-chart', 'clickData')],
            prevent_initial_call=True
        )
        def select_label(clickData):
            """Обработка клика на маркер метки"""
            if not clickData or 'points' not in clickData:
                return '', {'display': 'none'}, None

            point = clickData['points'][0]

            # Проверяем что клик был на маркер метки (есть customdata)
            if 'customdata' not in point:
                return '', {'display': 'none'}, None

            # Извлекаем данные метки
            extreme_ts = int(point['customdata'][0])
            confidence = float(point['customdata'][1])
            pnl = float(point['customdata'][2])
            method = str(point['customdata'][3])

            # Находим полную информацию о метке
            label_row = self.df_labels[self.df_labels['extreme_timestamp'] == extreme_ts]

            if label_row.empty:
                return '', {'display': 'none'}, None

            label_row = label_row.iloc[0]
            label_type = ['HOLD', 'BUY', 'SELL'][int(label_row['reversal_label'])]
            is_hq = 'High Quality' if label_row['is_high_quality'] == 1 else 'Low Quality'

            # Формируем информационную панель
            info = dbc.Card([
                dbc.CardBody([
                    html.H5(f'Selected Label: {label_type}', className='text-primary'),
                    html.Hr(),
                    html.P([
                        html.Strong('Timestamp: '), f'{extreme_ts}', html.Br(),
                        html.Strong('Index: '), f'{point["x"]}', html.Br(),
                        html.Strong('Price: '), f'${point["y"]:.2f}', html.Br(),
                        html.Strong('Confidence: '), f'{confidence:. 2f}', html.Br(),
                        html.Strong('PnL: '), f'{pnl:.4f} ({pnl * 100:.2f}%)', html.Br(),
                        html.Strong('Quality: '), is_hq, html.Br(),
                        html.Strong('Method: '), method
                    ])
                ])
            ], style={'backgroundColor': '#f8f9fa'})

            return info, {'margin': '20px', 'display': 'flex', 'gap': '10px'}, extreme_ts

    def setup_clientside_callbacks(self):

    @self.app.callback(
        Output('main-chart', 'figure', allow_duplicate=True),
        [
            Input('change-buy', 'n_clicks'),
            Input('change-sell', 'n_clicks'),
            Input('change-hold', 'n_clicks')
        ],
        [State('selected-label-ts', 'data')],
        prevent_initial_call=True
    )
    def change_label_type(self, buy_clicks, sell_clicks, hold_clicks, selected_ts):
        """Изменение типа метки с автосохранением"""
        if not selected_ts:
            return dash.no_update

        ctx = callback_context
        if not ctx.triggered:
            return dash.no_update

        btn_id = ctx.triggered[0]['prop_id'].split('. ')[0]

        # Определяем новый тип
        new_type_map = {
            'change-buy': 1,
            'change-sell': 2,
            'change-hold': 0
        }

        new_type = new_type_map.get(btn_id)
        if new_type is None:
            return dash.no_update

        # Обновляем метку в БД
        self._update_label_type(selected_ts, new_type)

        # Перезагружаем данные
        self.df_candles, self.df_labels = self.load_data()

        # Перерисовываем график
        bounds = self.calculate_window_bounds(self.current_index, self.block_size)
        return self.create_figure(bounds)

    # === UPDATE CONFIDENCE ===
    @self.app.callback(
        Output('main-chart', 'figure', allow_duplicate=True),
        [Input('update-confidence', 'n_clicks')],
        [
            State('selected-label-ts', 'data'),
            State('confidence-input', 'value')
        ],
        prevent_initial_call=True
    )
    def update_label_confidence(self, n_clicks, selected_ts, new_confidence):
        """Изменение confidence с автосохранением"""
        if not selected_ts or not new_confidence:
            return dash.no_update

        # Валидация
        if not (0.1 <= new_confidence <= 0.99):
            return dash.no_update

        # Обновляем confidence в БД
        self._update_confidence(selected_ts, new_confidence)

        # Перезагружаем данные
        self.df_candles, self.df_labels = self.load_data()

        # Перерисовываем график
        bounds = self.calculate_window_bounds(self.current_index, self.block_size)
        return self.create_figure(bounds)

    # === DELETE LABEL ===
    @self.app.callback(
        Output('main-chart', 'figure', allow_duplicate=True),
        [Input('delete-label', 'n_clicks')],
        [State('selected-label-ts', 'data')],
        prevent_initial_call=True
    )
    def delete_label(self,n_clicks, selected_ts):
        """Удаление метки без подтверждения"""
        if not selected_ts:
            return dash.no_update

        # Удаляем из БД
        self._delete_label(selected_ts)

        # Перезагружаем данные
        self.df_candles, self.df_labels = self.load_data()

        # Перерисовываем график
        bounds = self.calculate_window_bounds(self.current_index, self.block_size)
        return self.create_figure(bounds)

    # === SHOW ADD LABEL DROPDOWN ===
    @self.app.callback(
        [
            Output('add-label-dropdown', 'style'),
            Output('add-label-index', 'children'),
            Output('add-label-click-index', 'data')
        ],
        [Input('main-chart', 'clickData')],
        [State('keyboard-listener', 'children')],
        prevent_initial_call=True
    )
    def show_add_dropdown(clickData, keyboard_state):
        """
        Показывает dropdown для добавления метки при клике на свечу

        Note: Проверяем что клик НЕ на маркер метки (нет customdata)
        """
        if not clickData or 'points' not in clickData:
            return {'display': 'none'}, '', None

        point = clickData['points'][0]

        # Если клик на маркер (есть customdata) - не показываем dropdown
        if 'customdata' in point:
            return {'display': 'none'}, '', None

        # Получаем индекс свечи
        click_index = int(point['x'])

        # Позиционируем dropdown около клика
        # (в реальности нужны координаты мыши через clientside callback)
        dropdown_style = {
            'display': 'block',
            'position': 'absolute',
            'top': '400px',
            'left': '50%',
            'transform': 'translateX(-50%)',
            'zIndex': 1000
        }

        index_display = html.Span([
            html.Strong('Index: '),
            f'{click_index}'
        ])

        return dropdown_style, index_display, click_index

    # === ADD LABEL BUTTONS ===
    @self.app.callback(
        [
            Output('main-chart', 'figure', allow_duplicate=True),
            Output('add-label-dropdown', 'style', allow_duplicate=True)
        ],
        [
            Input('add-buy', 'n_clicks'),
            Input('add-sell', 'n_clicks'),
            Input('add-hold', 'n_clicks'),
            Input('add-cancel', 'n_clicks')
        ],
        [State('add-label-click-index', 'data')],
        prevent_initial_call=True
    )
    def add_label(self, buy_clicks, sell_clicks, hold_clicks, cancel_clicks, click_index):
        """Добавление новой метки через dropdown"""
        ctx = callback_context
        if not ctx.triggered or click_index is None:
            return dash.no_update, {'display': 'none'}

        btn_id = ctx.triggered[0]['prop_id'].split('.')[0]

        # Cancel - просто скрываем dropdown
        if btn_id == 'add-cancel':
            return dash.no_update, {'display': 'none'}

        # Определяем тип и confidence
        label_config = {
            'add-buy': (1, 0.8),  # BUY, confidence 0.8
            'add-sell': (2, 0.8),  # SELL, confidence 0.8
            'add-hold': (0, 1.0)  # HOLD, confidence 1.0
        }

        if btn_id not in label_config:
            return dash.no_update, {'display': 'none'}

        label_type, confidence = label_config[btn_id]

        # Добавляем метку в БД
        self._add_new_label(click_index, label_type, confidence)

        # Перезагружаем данные
        self.df_candles, self.df_labels = self.load_data()

        # Перерисовываем график
        bounds = self.calculate_window_bounds(self.current_index, self.block_size)

        return self.create_figure(bounds), {'display': 'none'}

    # --- Run ---
    def run(self, host='127.0.0.1', port=8050, debug=True):

        def _update_label_type(self, extreme_timestamp: int, new_type: int):
            """
            Изменение типа метки с немедленным сохранением в БД

            Args:
                extreme_timestamp: timestamp метки
                new_type: 0=HOLD, 1=BUY, 2=SELL
            """
            # Находим индекс метки в df_candles
            label_mask = self.df_candles['ts'] == extreme_timestamp
            if not label_mask.any():
                print(f"❌ Label not found: ts={extreme_timestamp}")
                return

            label_idx = self.df_candles[label_mask].index[0]

            # Определяем exit index для расчета PnL
            # Ищем следующую метку
            next_labels = self.df_labels[self.df_labels['extreme_timestamp'] > extreme_timestamp]

            if not next_labels.empty:
                next_ts = next_labels.iloc[0]['extreme_timestamp']
                exit_mask = self.df_candles['ts'] == next_ts
                if exit_mask.any():
                    exit_idx = self.df_candles[exit_mask].index[0] - 1  # Выход за 1 бар до следующей метки
                else:
                    exit_idx = label_idx + self.config.hold_bars
            else:
                exit_idx = label_idx + self.config.hold_bars

            # Проверка границ
            if exit_idx >= len(self.df_candles):
                exit_idx = len(self.df_candles) - 1

            # Расчет PnL
            signal_type_map = {0: 'HOLD', 1: 'BUY', 2: 'SELL'}
            signal_type = signal_type_map[new_type]

            if new_type == 0:  # HOLD не имеет PnL
                pnl = 0.0
                is_profitable = True
            else:
                pnl, is_profitable = self.tool._calculate_pnl_to_index(
                    self.df_candles, label_idx, signal_type, exit_idx
                )

            # UPDATE в БД
            with self.engine.begin() as conn:
                conn.execute(text("""
                    UPDATE labeling_results
                    SET reversal_label = :new_type,
                        price_change_after = :pnl,
                        is_high_quality = :is_hq
                    WHERE symbol = :symbol
                      AND extreme_timestamp = :ts
                """), {
                    'new_type': new_type,
                    'pnl': pnl,
                    'is_hq': 1 if is_profitable else 0,
                    'symbol': self.config.symbol,
                    'ts': extreme_timestamp
                })

            print(f"✅ Updated label {extreme_timestamp}: type={signal_type}, pnl={pnl:.4f}, is_hq={is_profitable}")

        def _update_confidence(self, extreme_timestamp: int, new_confidence: float):
            """
            Изменение confidence с автосохранением

            Args:
                extreme_timestamp: timestamp метки
                new_confidence: новый confidence (0.1-0.99)
            """
            with self.engine.begin() as conn:
                conn.execute(text("""
                    UPDATE labeling_results
                    SET reversal_confidence = :conf
                    WHERE symbol = :symbol
                      AND extreme_timestamp = :ts
                """), {
                    'conf': new_confidence,
                    'symbol': self.config.symbol,
                    'ts': extreme_timestamp
                })

            print(f"✅ Updated confidence for {extreme_timestamp}: {new_confidence:.2f}")

        def _delete_label(self, extreme_timestamp: int):
            """
            Удаление метки с автосохранением

            Args:
                extreme_timestamp: timestamp метки
            """
            with self.engine.begin() as conn:
                result = conn.execute(text("""
                    DELETE FROM labeling_results
                    WHERE symbol = :symbol
                      AND extreme_timestamp = :ts
                """), {
                    'symbol': self.config.symbol,
                    'ts': extreme_timestamp
                })

            print(f"✅ Deleted label {extreme_timestamp} (rows affected: {result.rowcount})")

    def _add_new_label(self, index: int, label_type: int, confidence: float):
        """
        Добавление новой метки с автосохранением

        Args:
            index: индекс свечи в df_candles
            label_type: 0=HOLD, 1=BUY, 2=SELL
            confidence: 0.8 для BUY/SELL, 1.0 для HOLD
        """
        # Проверка границ
        if index < 0 or index >= len(self.df_candles):
            print(f"❌ Invalid index: {index}")
            return

        # Получаем данные свечи
        candle_row = self.df_candles.iloc[index]
        extreme_timestamp = int(candle_row['ts'])
        extreme_price = float(candle_row['close'])

        # Проверка: метка уже существует?
        existing = self.df_labels[self.df_labels['extreme_timestamp'] == extreme_timestamp]
        if not existing.empty:
            print(f"⚠️  Label already exists at ts={extreme_timestamp}")
            return

        # Определяем exit index для расчета PnL
        next_labels = self.df_labels[self.df_labels['extreme_timestamp'] > extreme_timestamp]

        if not next_labels.empty:
            next_ts = next_labels.iloc[0]['extreme_timestamp']
            exit_mask = self.df_candles['ts'] == next_ts
            if exit_mask.any():
                exit_idx = self.df_candles[exit_mask].index[0] - 1
            else:
                exit_idx = index + self.config.hold_bars
        else:
            exit_idx = index + self.config.hold_bars

        # Проверка границ
        if exit_idx >= len(self.df_candles):
            exit_idx = len(self.df_candles) - 1

        # Расчет PnL
        signal_type_map = {0: 'HOLD', 1: 'BUY', 2: 'SELL'}
        signal_type = signal_type_map[label_type]

        if label_type == 0:  # HOLD
            pnl = 0.
            0
            is_profitable = True
        else:
            pnl, is_profitable = self.tool._calculate_pnl_to_index(
                self.df_candles, index, signal_type, exit_idx
            )

        # Формируем запись для вставки
        confirmation_index = index
        confirmation_timestamp = extreme_timestamp

        # INSERT в БД
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO labeling_results (
                    symbol,
                    timestamp,
                    timeframe,
                    reversal_label,
                    reversal_confidence,
                    labeling_method,
                    labeling_params,
                    extreme_index,
                    extreme_price,
                    extreme_timestamp,
                    confirmation_index,
                    confirmation_timestamp,
                    price_change_after,
                    features_json,
                    is_high_quality,
                    created_at
                ) VALUES (
                    :symbol,
                    :timestamp,
                    :timeframe,
                    :reversal_label,
                    :reversal_confidence,
                    :labeling_method,
                    :labeling_params,
                    :extreme_index,
                    :extreme_price,
                    :extreme_timestamp,
                    :confirmation_index,
                    :confirmation_timestamp,
                    :price_change_after,
                    :features_json,
                    :is_high_quality,
                    :created_at
                )
            """), {
                'symbol': self.config.symbol,
                'timestamp': extreme_timestamp,
                'timeframe': self.config.timeframe,
                'reversal_label': label_type,
                'reversal_confidence': confidence,
                'labeling_method': 'MANUAL',
                'labeling_params': None,
                'extreme_index': index,
                'extreme_price': extreme_price,
                'extreme_timestamp': extreme_timestamp,
                'confirmation_index': confirmation_index,
                'confirmation_timestamp': confirmation_timestamp,
                'price_change_after': pnl,
                'features_json': None,
                'is_high_quality': 1 if is_profitable else 0,
                'created_at': datetime.now().isoformat()
            })

        print(f"✅ Added new label: index={index}, type={signal_type}, confidence={confidence:.2f}, pnl={pnl:. 4f}")

        def setup_clientside_callbacks(self):
            """
            Настройка JavaScript callbacks для hotkeys

            Hotkeys:
            - A: режим добавления метки (показывает dropdown при клике)
            - E: режим редактирования (выделяет edit controls)
            - D: удаление выбранной метки
            - ← : навигация назад (половина block)
            - → : навигация вперед (половина block)
            """

            # JavaScript код для обработки клавиш
            clientside_callback(
                """
                function(n_intervals) {
                    // Обработчик клавиатурных событий
                    document.addEventListener('keydown', function(event) {
                        const key = event.key. toLowerCase();

                        // A - режим добавления (пока не реализован полностью)
                        if (key === 'a') {
                            console.log('Add mode activated');
                            // Здесь можно добавить визуальную индикацию режима
                        }

                        // E - фокус на edit controls
                        if (key === 'e') {
                            const editControls = document.getElementById('edit-controls');
                            if (editControls && editControls.style.display !== 'none') {
                                editControls.scrollIntoView({ behavior: 'smooth', block: 'center' });
                            }
                        }

                        // D - удаление выбранной метки
                        if (key === 'd') {
                            const deleteBtn = document.getElementById('delete-label');
                            if (deleteBtn && deleteBtn.style.display !== 'none') {
                                deleteBtn.click();
                            }
                        }

                        // ← - навигация назад
                        if (key === 'arrowleft') {
                            const backBtn = document.getElementById('nav-back');
                            if (backBtn) {
                                backBtn.click();
                                event.preventDefault();
                            }
                        }

                        // → - навигация вперед
                        if (key === 'arrowright') {
                            const fwdBtn = document.getElementById('nav-forward');
                            if (fwdBtn) {
                                fwdBtn.click();
                                event.preventDefault();
                            }
                        }
                    });

                    return '';
                }
                """,
                Output('keyboard-listener', 'children'),
                Input('main-chart', 'id')  # Dummy input для инициализации
            )

            def run(self, host='127.0.0.1', port=8050, debug=True):
                """
                Запуск Dash web-сервера

                Args:
                    host: адрес сервера (default: localhost)
                    port: порт (default: 8050)
                    debug: режим отладки
                """
                print("=" * 60)
                print("🚀 ML Labeling Viewer")
                print("=" * 60)

                # Загрузка данных
                try:
                    self.df_candles, self.df_labels = self.load_data()
                except Exception as e:
                    print(f"❌ Failed to load data: {e}")
                    return

                # Создание Dash app
                self.app = dash.Dash(
                    __name__,
                    external_stylesheets=[dbc.themes.BOOTSTRAP],
                    suppress_callback_exceptions=True
                )

                self.app.title = f"Labeling Viewer - {self.config.symbol}"
                self.app.layout = self.create_dash_layout()

                # Настройка callbacks
                self.setup_callbacks()
                self.setup_clientside_callbacks()

                # Информация
                total_blocks = len(self.df_candles) // self.block_size
                print(f"📊 Loaded: {len(self.df_candles)} candles, {len(self.df_labels)} labels")
                print(f"🎯 Block size: {self.block_size}, Total blocks: {total_blocks}")
                print(f"🌐 Starting server at http://{host}:{port}")
                print(f"💡 Press Ctrl+C to stop")
                print("=" * 60)

                # Запуск сервера
                self.app.run_server(host=host, port=port, debug=debug, use_reloader=False)

# === MAIN ===
if __name__ == '__main__':
    import argparse

    # Парсинг аргументов командной строки
    parser = argparse.ArgumentParser(description='ML Labeling Viewer')
    parser.add_argument('--symbol', type=str, default='ETHUSDT', help='Trading symbol')
    parser.add_argument('--timeframe', type=str, default='5m', help='Timeframe')
    parser.add_argument('--port', type=int, default=8050, help='Port for web server')
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Host address')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')

    args = parser.parse_args()

    # Создание конфигурации
    config = LabelingConfig(
        symbol=args.symbol,
        timeframe=args.timeframe
    )

    # Запуск viewer
    try:
        viewer = LabelingViewer(config)
        viewer.run(host=args.host, port=args.port, debug=args.debug)
    except KeyboardInterrupt:
        print("\n👋 Viewer stopped by user")
    except Exception as e:
        print(f"❌ Critical error: {e}")
        import traceback

        traceback.print_exc()