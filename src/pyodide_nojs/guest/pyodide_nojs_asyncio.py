"""asyncio's default loop with no self-pipe (guest side).

The selector loop wakes itself through `socket.socketpair()`, which in this guest (no AF_UNIX) is a TCP pair over the
host's loopback: it would need `--tcp`. The guest has one thread, so nothing but a signal can wake a loop that waits
in `select`, and a signal's Python handler runs in the loop's own thread: the pair is not needed. `hook` (called by
`pyodide_nojs_start`) patches the two modules when they are imported (the real sockets of the loop work as before)."""
# pyright: reportAttributeAccessIssue=false

SLICE = 0.1  # seconds: how long the guest's poll waits before Python looks for signals again


def selector_events(m):
    """No pair: `_write_to_self` (call_soon_threadsafe, a signal's handler) only sets a flag. The host's `poll` with no
    descriptor (a loop that only waits for a timer) sleeps the whole timeout without a look for signals, so the
    selector waits in slices of SLICE: a Ctrl-C (Runner's handler cancels the task and calls call_soon_threadsafe)
    is then taken within a slice (a look for signals after each: measured 5 ms instead of ~1 s without it), not after
    `asyncio.sleep(30)`."""
    import signal
    import time

    loop = m.BaseSelectorEventLoop

    def _make_self_pipe(self):
        self._ssock = self._csock = None
        self._woken = False
        select = self._selector.select

        def sliced(timeout=None):
            end = None if timeout is None else time.monotonic() + timeout
            while True:
                left = None if end is None else end - time.monotonic()
                if self._woken or (left is not None and left <= SLICE):
                    woken, self._woken = self._woken, False
                    return select(0 if woken else max(left or 0, 0))
                events = select(SLICE)
                if events:
                    return events
                signal.pthread_sigmask(signal.SIG_BLOCK, [])  # a look for signals (Pyodide's check is every ~50th)

        self._selector.select = sliced

    def _close_self_pipe(self):
        self._ssock = self._csock = None

    def _write_to_self(self):
        self._woken = True

    loop._make_self_pipe, loop._close_self_pipe, loop._write_to_self = _make_self_pipe, _close_self_pipe, _write_to_self


def unix_events(m):
    import signal
    from asyncio import coroutines, events

    def add_signal_handler(self, sig, callback, *args):
        """As the original, but the Python handler of the signal is the loop's own (no wakeup fd to read)."""
        if coroutines.iscoroutine(callback) or coroutines._iscoroutinefunction(callback):
            raise TypeError("coroutines cannot be used with add_signal_handler()")
        self._check_signal(sig)
        self._check_closed()
        self._signal_handlers[sig] = events.Handle(callback, args, self, None)
        try:
            signal.signal(sig, lambda signum, frame: self._handle_signal(signum))
        except (ValueError, OSError) as exc:  # ValueError: not the main thread
            del self._signal_handlers[sig]
            raise RuntimeError(f"sig {sig} cannot be caught" if getattr(exc, "errno", 0) == 22 else str(exc)) from None

    m._UnixSelectorEventLoop.add_signal_handler = add_signal_handler


def hook():
    import pyodide_lock  # pyright: ignore[reportMissingImports]  # its sibling file in the guest

    pyodide_lock.after_import("asyncio.selector_events", selector_events)
    pyodide_lock.after_import("asyncio.unix_events", unix_events)
