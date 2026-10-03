"""Process pool for independent road pieces that all read the same raster.

The raster is copied once into shared memory. Each worker maps it and does
not receive its own copy with the task. Numeric libraries are limited to one
thread per process so the processes do not fight over the same cores.
"""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from multiprocessing import shared_memory

import numpy as np

_ARRAYS: dict[str, np.ndarray] = {}
_SHM: list[shared_memory.SharedMemory] = []
_EXTRA = None


def workers() -> int:
    """Leave one core free, and stop at eight so the processes fit in memory."""
    n = os.cpu_count() or 1
    return max(1, min(n - 1, 8))


def _limit_threads() -> None:
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["NUMEXPR_NUM_THREADS"] = "1"


def _init(meta: dict, extra) -> None:
    global _EXTRA
    _limit_threads()
    _EXTRA = extra
    for key, (name, shape, dtype) in meta.items():
        shm = shared_memory.SharedMemory(name=name)
        _SHM.append(shm)
        _ARRAYS[key] = np.ndarray(shape, dtype=np.dtype(dtype), buffer=shm.buf)


def array(key: str) -> np.ndarray:
    return _ARRAYS[key]


def extra():
    return _EXTRA


class Pool:
    def __init__(self, arrays: dict[str, np.ndarray], extra=None, n: int | None = None):
        _limit_threads()
        self.n = workers() if n is None else max(1, int(n))
        self._arrays = arrays
        self._extra = extra
        self._owned: list[shared_memory.SharedMemory] = []
        self._ex: ProcessPoolExecutor | None = None
        if self.n <= 1:
            return
        meta = {}
        for key, arr in arrays.items():
            src = np.ascontiguousarray(arr)
            shm = shared_memory.SharedMemory(create=True, size=src.nbytes)
            view = np.ndarray(src.shape, dtype=src.dtype, buffer=shm.buf)
            view[:] = src
            self._owned.append(shm)
            meta[key] = (shm.name, tuple(int(v) for v in src.shape), src.dtype.str)
        self._ex = ProcessPoolExecutor(
            max_workers=self.n,
            mp_context=get_context("spawn"),
            initializer=_init,
            initargs=(meta, extra),
        )

    def imap(self, fn, tasks, chunksize: int = 1):
        tasks = list(tasks)
        if self._ex is None:
            global _EXTRA
            _ARRAYS.update(self._arrays)
            _EXTRA = self._extra
            for task in tasks:
                yield fn(task)
            return
        yield from self._ex.map(fn, tasks, chunksize=chunksize)

    def close(self) -> None:
        if self._ex is not None:
            self._ex.shutdown(wait=True, cancel_futures=False)
            self._ex = None
        for shm in self._owned:
            shm.close()
            try:
                shm.unlink()
            except FileNotFoundError:
                pass
        self._owned.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
