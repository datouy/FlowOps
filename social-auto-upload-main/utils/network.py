import asyncio
import time
from functools import wraps

from loguru import logger


def async_retry(timeout=60, max_retries=None):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start_time = time.time()
            attempts = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    attempts += 1
                    if max_retries is not None and attempts >= max_retries:
                        logger.warning("Reached maximum retries of {}", max_retries)
                        raise Exception(f"Failed after {max_retries} retries.") from e
                    if time.time() - start_time > timeout:
                        logger.warning("Function timeout after {} seconds", timeout)
                        raise TimeoutError(f"Function execution exceeded {timeout} seconds timeout.") from e
                    logger.warning("Attempt {} failed: {}. Retrying...", attempts, e)
                    await asyncio.sleep(1)  # Sleep to avoid tight loop or provide backoff logic here

        return wrapper

    return decorator
