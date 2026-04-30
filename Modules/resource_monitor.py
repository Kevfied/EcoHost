"""
Resource Monitoring - System CPU and RAM usage tracking.
"""

import logging

try:
    import psutil
except ImportError:
    psutil = None

logger = logging.getLogger(__name__)


def get_system_resources() -> tuple[float, float]:
    """Get CPU and RAM usage percentages."""
    if psutil is None:
        logger.error("psutil not available")
        return 0.0, 0.0
    
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        ram = psutil.virtual_memory().percent
        return cpu, ram
    except Exception as e:
        logger.error(f"Failed to get system resources: {e}")
        return 0.0, 0.0
