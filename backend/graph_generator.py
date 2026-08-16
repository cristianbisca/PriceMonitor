"""
Graph generation service for price history visualization.
Generates charts as PNG images and returns them as base64 or files.
"""

import io
import base64
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import matplotlib
matplotlib.use('Agg')  # Non-interactive backend for Docker
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.font_manager import FontProperties
import pytz
import os

logger = logging.getLogger(__name__)


def _get_default_timezone():
    """Get the configured timezone from environment variable TZ, defaulting to Europe/Bucharest."""
    tz_name = os.getenv('TZ', 'Europe/Bucharest')
    try:
        return pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        logger.warning(f"Unknown timezone '{tz_name}', falling back to Europe/Bucharest")
        return pytz.timezone('Europe/Bucharest')


def _convert_to_local_time(dt, tz=None):
    """Convert a datetime to the specified timezone (default: from TZ env var).

    Handles both naive and aware datetimes. If the datetime is already in UTC,
    it will be converted to the target timezone. If naive, it's assumed to be UTC.
    
    Returns a NAIVE datetime in the target timezone (tzinfo stripped) so that
    matplotlib displays it correctly without applying its own UTC conversion.
    """
    if tz is None:
        tz = _get_default_timezone()

    # If datetime is naive (no timezone info), assume it's UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    local_dt = dt.astimezone(tz)
    
    # Strip timezone info so matplotlib treats it as a plain wall-clock time
    # and does NOT apply its own UTC conversion on top.
    return local_dt.replace(tzinfo=None)


def generate_price_chart(
    price_history: List[Dict[str, any]],
    product_name: str,
    currency: str = "RON",
    width: int = 1200,
    height: int = 600,
) -> str:
    """
    Generate a price history chart and return it as a base64-encoded PNG.

    Args:
        price_history: List of dicts with 'price' and 'checked_at' keys
        product_name: Name of the product for the title
        currency: Currency symbol/code
        width: Chart width in pixels
        height: Chart height in pixels

    Returns:
        Base64-encoded PNG image string
    """
    if not price_history:
        logger.warning("No price history data to generate chart")
        return ""

    # Sort by date
    sorted_data = sorted(price_history, key=lambda x: x['checked_at'])

    # Group entries by source (main domain of the URL). The first source in
    # chronological order is treated as the main one and keeps the primary color.
    sources = []  # ordered list of unique source labels
    for entry in sorted_data:
        label = entry.get('source') or 'Price'
        if label not in sources:
            sources.append(label)

    # Use configured timezone for matplotlib date formatting
    _tz = _get_default_timezone()
    plt.rcParams['timezone'] = _tz.zone

    all_prices = [entry['price'] for entry in sorted_data]
    min_price = min(all_prices)
    max_price = max(all_prices)

    # Create figure
    fig, ax = plt.subplots(figsize=(width / 100, height / 100), dpi=100)

    # Color scheme: main source keeps the original blue; alternative sources get
    # distinct colors so they are easy to tell apart on the graph.
    line_color = '#2563eb'
    min_color = '#dc2626'
    alt_colors = ['#f59e0b', '#10b981', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316']

    # Plot one line per source, labeled with the main domain of that source
    for idx, source in enumerate(sources):
        color = line_color if idx == 0 else alt_colors[(idx - 1) % len(alt_colors)]
        entries = [e for e in sorted_data if (e.get('source') or 'Price') == source]
        s_dates = [_convert_to_local_time(e['checked_at']) for e in entries]
        s_prices = [e['price'] for e in entries]

        ax.plot(s_dates, s_prices, color=color, linewidth=2, marker='o', markersize=4, label=source)

        # Fill area under the main source line only (keeps the chart readable)
        if idx == 0:
            ax.fill_between(s_dates, s_prices, alpha=0.1, color=color)

    # Highlight minimum price points (global across all sources)
    min_entries = [e for e in sorted_data if e.get('is_minimum', False)]
    if min_entries:
        ax.scatter(
            [_convert_to_local_time(e['checked_at']) for e in min_entries],
            [e['price'] for e in min_entries],
            color=min_color, s=150, zorder=5,
            marker='*', label='Minimum Price'
        )

    # Draw horizontal line at minimum price
    ax.axhline(y=min_price, color=min_color, linestyle='--', alpha=0.5, linewidth=1)
    last_date = _convert_to_local_time(sorted_data[-1]['checked_at'])
    ax.annotate(
        f'Min: {min_price:.2f} {currency}',
        xy=(last_date, min_price),
        xytext=(10, 10),
        textcoords='offset points',
        color=min_color,
        fontweight='bold',
        fontsize=10,
    )

    # Add current price annotation for each source (its latest value)
    for idx, source in enumerate(sources):
        entries = [e for e in sorted_data if (e.get('source') or 'Price') == source]
        last_entry = entries[-1]
        color = line_color if idx == 0 else alt_colors[(idx - 1) % len(alt_colors)]
        ax.annotate(
            f'{last_entry["price"]:.2f}',
            xy=(_convert_to_local_time(last_entry['checked_at']), last_entry['price']),
            xytext=(8, -4),
            textcoords='offset points',
            color=color,
            fontweight='bold',
            fontsize=9,
        )

    # Formatting
    ax.set_title(f'Price History: {product_name}', fontsize=16, fontweight='bold', pad=20)
    ax.set_xlabel('Date', fontsize=12)
    ax.set_ylabel(f'Price ({currency})', fontsize=12)

    # Format x-axis dates
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%d %b %Y'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=45, ha='right')

    # Add grid
    ax.grid(True, alpha=0.3, linestyle='-')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)

    # Add legend
    ax.legend(loc='upper right', framealpha=0.9)

    # Calculate price change (based on the main source's series when available,
    # otherwise across all entries in chronological order)
    main_entries = [e for e in sorted_data if (e.get('source') or 'Price') == sources[0]]
    first_price = main_entries[0]['price']
    last_price = main_entries[-1]['price']
    if len(main_entries) > 1:
        change = last_price - first_price
        change_pct = (change / first_price) * 100
        change_symbol = '+' if change >= 0 else ''
        change_color = '#16a34a' if change <= 0 else '#dc2626'  # Green for price drop

        ax.text(
            0.01, 0.02,
            f'Change: {change_symbol}{change:.2f} ({change_symbol}{change_pct:.1f}%)',
            transform=ax.transAxes,
            fontsize=10,
            color=change_color,
            fontweight='bold',
            va='bottom',
        )

    # Add statistics box
    stats_text = (
        f'Min: {min(all_prices):.2f} | Max: {max(all_prices):.2f} | '
        f'Avg: {sum(all_prices) / len(all_prices):.2f}'
    )
    ax.text(
        0.99, 0.02,
        stats_text,
        transform=ax.transAxes,
        fontsize=9,
        color='#6b7280',
        ha='right',
        va='bottom',
    )

    plt.tight_layout()

    # Save to buffer
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    img_base64 = base64.b64encode(buf.read()).decode('utf-8')

    plt.close(fig)

    return img_base64


def generate_comparison_chart(
    products_data: List[Dict[str, any]],
    currency: str = "RON",
    width: int = 1400,
    height: int = 700,
) -> str:
    """
    Generate a comparison chart for multiple products.

    Args:
        products_data: List of dicts with 'name', 'prices' (list of {price, checked_at})
        currency: Currency code
        width: Chart width
        height: Chart height

    Returns:
        Base64-encoded PNG image
    """
    if not products_data:
        return ""

    fig, ax = plt.subplots(figsize=(width / 100, height / 100), dpi=100)

    colors = ['#2563eb', '#dc2626', '#16a34a', '#d97706', '#7c3aed', '#db2777']

    for idx, product in enumerate(products_data):
        color = colors[idx % len(colors)]
        data = sorted(product['prices'], key=lambda x: x['checked_at'])
        dates = [_convert_to_local_time(e['checked_at']) for e in data]
        prices = [e['price'] for e in data]

        ax.plot(dates, prices, color=color, linewidth=2, marker='o', markersize=3, label=product['name'])

    # Set timezone for date formatting
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%d %b', tz=_get_default_timezone()))

    ax.set_title('Price Comparison', fontsize=16, fontweight='bold', pad=20)
    ax.set_xlabel('Date', fontsize=12)
    ax.set_ylabel(f'Price ({currency})', fontsize=12)

    fig.autofmt_xdate(rotation=45, ha='right')

    ax.grid(True, alpha=0.3)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.legend(loc='upper right', framealpha=0.9)

    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    img_base64 = base64.b64encode(buf.read()).decode('utf-8')

    plt.close(fig)

    return img_base64


def get_price_statistics(price_history: List[Dict[str, any]]) -> Dict[str, float]:
    """Calculate price statistics from history."""
    if not price_history:
        return {}

    prices = [entry['price'] for entry in price_history]

    return {
        'min': min(prices),
        'max': max(prices),
        'avg': sum(prices) / len(prices),
        'current': prices[-1],
        'first': prices[0],
        'change': prices[-1] - prices[0],
        'change_percent': ((prices[-1] - prices[0]) / prices[0]) * 100 if prices[0] else 0,
        'total_checks': len(prices),
    }