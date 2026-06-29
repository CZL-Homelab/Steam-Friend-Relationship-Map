from __future__ import annotations

import asyncio
from typing import Callable


class AdaptiveRateLimiter:
    """自适应并发限速器。

    使用 AIMD (Additive-Increase/Multiplicative-Decrease) 算法调节请求间隔延迟。
    """

    def __init__(
        self,
        base_delay_ms: float,
        min_delay_ms: float = 100.0,
        max_delay_ms: float = 5000.0,
        on_change_callback: Callable[[float, float, str], None] | None = None,
    ) -> None:
        self.current_delay_ms = base_delay_ms
        self.min_delay_ms = min(min_delay_ms, base_delay_ms)
        self.max_delay_ms = max(max_delay_ms, base_delay_ms)
        self.decrease_step_ms = 10.0  # 每次成功减少 10ms 延迟（请求加快）
        self.increase_factor = 1.5    # 每次重试/失败增加 1.5 倍延迟（请求放慢）
        self.on_change_callback = on_change_callback
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        """根据当前延迟间隔进行等待"""
        async with self.lock:
            delay = self.current_delay_ms / 1000.0
            if delay > 0:
                await asyncio.sleep(delay)

    async def report_success(self) -> None:
        """报告请求成功，使用非线性曲线缩短延迟（加快）"""
        async with self.lock:
            old_delay = self.current_delay_ms
            # 比例渐进回收曲线：高延迟时恢复步长更大，低延迟时平缓趋近最小延迟
            # 基础步长 10.0ms + (当前延迟与最小延迟差值) 的 5%
            decrease_step = 10.0 + (old_delay - self.min_delay_ms) * 0.05
            new_delay = max(self.min_delay_ms, old_delay - decrease_step)
            if new_delay != old_delay:
                self.current_delay_ms = new_delay
                if self.on_change_callback:
                    self.on_change_callback(old_delay, new_delay, "success")

    async def report_backoff(self) -> None:
        """报告请求拥堵或受限，乘性延长延迟（退避）"""
        async with self.lock:
            old_delay = self.current_delay_ms
            new_delay = min(self.max_delay_ms, old_delay * self.increase_factor)
            if new_delay != old_delay:
                self.current_delay_ms = new_delay
                if self.on_change_callback:
                    self.on_change_callback(old_delay, new_delay, "backoff")
