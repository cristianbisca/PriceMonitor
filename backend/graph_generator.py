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
    """
    if tz is None:
        tz = _get_default_timezone()

    # If datetime is naive (no timezone info), assume it's UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(tz)


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

    # Convert UTC timestamps to local timezone (Europe/Bucharest)
    dates = [_convert_to_local_time(entry['checked_at']) for entry in sorted_data]
    prices = [entry['price'] for entry in sorted_data]
    is_minimums = [entry.get('is_minimum', False) for entry in sorted_data]

    # Use configured timezone for matplotlib date formatting
    _tz = _get_default_timezone()
    plt.rcParams['timezone'] = _tz.key

    # Find minimum price
    min_price = min(prices)
    max_price = max(prices)

    # Create figure
    fig, ax = plt.subplots(figsize=(width / 100, height / 100), dpi=100)

    # Color scheme
    line_color = '#2563eb'
    min_color = '#dc2626'
    fill_color = '#2563eb20'

    # Plot main price line
    ax.plot(dates, prices, color=line_color, linewidth=2, marker='o', markersize=4, label='Price')

    # Fill area under the line
    ax.fill_between(dates, prices, alpha=0.1, color=line_color)

    # Highlight minimum price points
    min_dates = [dates[i] for i in range(len(dates)) if is_minimums[i]]
    min_prices_vals = [prices[i] for i in range(len(prices)) if is_minimums[i]]

    if min_dates:
        ax.scatter(
            min_dates, min_prices_vals,
            color=min_color, s=150, zorder=5,
            marker='*', label='Minimum Price'
        )

    # Draw horizontal line at minimum price
    ax.axhline(y=min_price, color=min_color, linestyle='--', alpha=0.5, linewidth=1)
    ax.annotate(
        f'Min: {min_price:.2f} {currency}',
        xy=(dates[-1], min_price),
        xytext=(10, 10),
        textcoords='offset points',
        color=min_color,
        fontweight='bold',
        fontsize=10,
    )

    # Add current price annotation
    ax.annotate(
        f'{prices[-1]:.2f} {currency}',
        xy=(dates[-1], prices[-1]),
        xytext=(10, -15),
        textcoords='offset points',
        color=line_color,
        fontweight='bold',
        fontsize=10,
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

    # Calculate price change
    if len(prices) > 1:
        change = prices[-1] - prices[0]
        change_pct = (change / prices[0]) * 100
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
        f'Min: {min(prices):.2f} | Max: {max(prices):.2f} | '
        f'Avg: {sum(prices) / len(prices):.2f}'
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