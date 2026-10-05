"""Sliding-window limiter tests (2026-10-05). Run: python tests/test_sliding_window.py
Real clock, short windows (0.6 s) so the suite finishes in ~3 s."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Local runs may lack the heavy runtime deps; the limiter itself needs only asyncio/time.
import types  # noqa: E402
for _mod, _attrs in (("markitdown", {"MarkItDown": object}), ("httpx", {"AsyncClient": object, "Response": object})):
    try:
        __import__(_mod)
    except ModuleNotFoundError:
        sys.modules[_mod] = types.SimpleNamespace(**_attrs)
from bedesten_mcp_module.client import _SlidingWindow, BedestenRateLimited  # noqa: E402


async def burst(win: _SlidingWindow, n: int, max_wait=None):
    t0 = time.monotonic()
    stamps = []
    for _ in range(n):
        await win.acquire(max_wait=max_wait)
        stamps.append(time.monotonic() - t0)
    return stamps


async def main() -> int:
    fails = 0

    # 1) limit within window: first `limit` instant, the next waits ~window
    win = _SlidingWindow(limit=3, window_s=0.6)
    st = await burst(win, 4)
    ok = st[2] < 0.1 and 0.5 <= st[3] <= 0.9
    print(f"1 pencere: ilk 3 anlik, 4. {st[3]:.2f}s bekledi -> {'OK' if ok else 'FAIL'}")
    fails += not ok

    # 2) max_wait: 4th request would wait ~0.6 s > max_wait 0.2 -> structured 429, not a sleep
    win = _SlidingWindow(limit=3, window_s=0.6)
    await burst(win, 3)
    t0 = time.monotonic()
    try:
        await win.acquire(max_wait=0.2)
        ok = False
    except BedestenRateLimited as e:
        ok = (time.monotonic() - t0) < 0.1 and 0.4 <= e.retry_after <= 0.7
    print(f"2 max_wait: aninda BedestenRateLimited -> {'OK' if ok else 'FAIL'}")
    fails += not ok

    # 3) 429: short pause (<= pause_429_s) and reduced limit for one window, not a long freeze
    win = _SlidingWindow(limit=4, window_s=0.6, pause_429_s=0.2, limit_drop_429=2)
    pause = win.penalize(retry_after=30.0)
    t0 = time.monotonic()
    await win.acquire()  # waits only ~0.2 s
    first = time.monotonic() - t0
    await win.acquire()  # effective limit 2 during reduced window
    t1 = time.monotonic()
    try:
        await win.acquire(max_wait=0.05)  # 3rd would exceed reduced limit -> wait ~window
        ok3 = False
    except BedestenRateLimited:
        ok3 = True
    ok = pause == 0.2 and 0.15 <= first <= 0.4 and ok3 and (time.monotonic() - t1) < 0.1
    print(f"3 429: duraklama {first:.2f}s (30 s degil), azaltilmis limit 2 -> {'OK' if ok else 'FAIL'}")
    fails += not ok

    # 4) smooth: 7 requests, limit 7, window 0.6 -> all instant; 8th waits until oldest expires
    win = _SlidingWindow(limit=7, window_s=0.6)
    st = await burst(win, 8)
    ok = st[6] < 0.1 and 0.5 <= st[7] <= 0.9
    print(f"4 7/pencere: 7 anlik, 8. {st[7]:.2f}s -> {'OK' if ok else 'FAIL'}")
    fails += not ok

    # 5) concurrency: 10 tasks, limit 5, window 0.6 -> exactly 5 pass instantly, rest after ~window
    win = _SlidingWindow(limit=5, window_s=0.6)
    t0 = time.monotonic()
    async def one():
        await win.acquire()
        return time.monotonic() - t0
    res = sorted(await asyncio.gather(*[one() for _ in range(10)]))
    ok = sum(1 for r in res if r < 0.1) == 5 and all(0.5 <= r <= 0.9 for r in res[5:])
    print(f"5 eszamanli 10 gorev: 5 anlik + 5 pencere sonrasi -> {'OK' if ok else 'FAIL'}")
    fails += not ok

    print(f"\n{'TUMU GECTI' if fails == 0 else str(fails) + ' FAIL'}")
    return fails


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
