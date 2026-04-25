"""Motor and pump actuation.

Two tiers:

- **Stubs** (:func:`move_eppendorf`, :func:`actuate_pump`): no hardware — just
  print. These are the defaults wired into the run cells.
- **Real drivers** over an Arduino CNC shield. One Arduino speaks to up to
  three stepper axes (X/Y/Z) over a single serial port, one command per line::

      {axis}{steps},{speed_steps}\\n        e.g. "X1000000000,4000\\n"

  Share the port via :class:`CncShield`, then bind axis-level drivers to it:

      shield = CncShield(port="COM3").connect()
      pump  = StepperFlowPump(shield,  axis="X")  # continuous flow
      stage = StepperPositioner(shield, axis="Y")  # discrete moves

  Each axis is independent — `pump.set_flow(0.5)` and `stage.move_mm(25)` can
  happen concurrently; the shield serializes writes with a lock.
"""
from __future__ import annotations

import threading
import time

# Lazy: keep the module importable even without pyserial. Classes raise
# ImportError only when someone actually tries to construct them.
try:
    import serial  # type: ignore  # pyserial
except ImportError:
    serial = None  # type: ignore


# ----------------------------------------------------------------------
# Stubs
# ----------------------------------------------------------------------

def move_eppendorf(position: int) -> None:
    """Move the eppendorf collection stage to *position* (integer slot index).

    Called once per sorted organoid. Placeholder — swap for the real driver
    call (e.g. bind to :meth:`StepperPositioner.move_to_slot`).
    """
    print(f"[motor] move_eppendorf(position={position})")


def actuate_pump(speed: int) -> None:
    """Set the flow-chamber pump speed (integer setpoint).

    Called every frame with a new flow-speed estimate. Placeholder — swap for
    the real driver call (e.g. bind to :meth:`StepperFlowPump.set_flow`).
    """
    print(f"[pump]  actuate_pump(speed={speed})")


def position_from_size(size: float, thresholds: list[tuple[float, int]]) -> int:
    """Pick a position from *size* using a list of ``(upper_bound, position)`` rules.

    First rule whose ``size <= upper_bound`` wins. The final rule should use
    ``float('inf')`` as the catch-all upper bound.

    Example:
        >>> position_from_size(1200, [(500, 1), (2000, 2), (float('inf'), 3)])
        2
    """
    for upper, pos in thresholds:
        if size <= upper:
            return pos
    raise ValueError(
        "No matching rule in thresholds — add a (float('inf'), ...) catch-all."
    )


# ----------------------------------------------------------------------
# Serial link: one per Arduino, shared across axes
# ----------------------------------------------------------------------

_VALID_AXES = ("X", "Y", "Z")


class CncShield:
    """Serial link to one Arduino CNC shield. Shared by all axis drivers.

    Call :meth:`connect` once, then attach :class:`StepperFlowPump`,
    :class:`StepperPositioner`, or other axis drivers by passing ``self``.
    :meth:`send` is thread-safe.
    """

    BOOT_DELAY_S = 2.0   # Arduino resets on serial open; give it time
    DISABLE_FORMAT = "{axis}OFF\n"  # firmware-side verb for cutting coil current

    def __init__(self, port: str, baud: int = 115200):
        if serial is None:
            raise ImportError(
                "pyserial is required for CncShield — `pip install pyserial`"
            )
        self.port = port
        self.baud = baud
        self._ser = None
        self._lock = threading.Lock()

    def connect(self) -> "CncShield":
        """Open the serial port. Blocks ~2 s while the Arduino resets."""
        self._ser = serial.Serial(self.port, self.baud, timeout=1)
        time.sleep(self.BOOT_DELAY_S)
        return self

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def __enter__(self) -> "CncShield":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    def send(self, axis: str, steps: int, speed_steps: int) -> None:
        """Write one motion command to the shield. Thread-safe.

        Raises:
            RuntimeError: if the shield is not connected.
            ValueError: if *axis* is not X/Y/Z.
        """
        if axis not in _VALID_AXES:
            raise ValueError(f"axis must be X/Y/Z; got {axis!r}")
        if self._ser is None:
            raise RuntimeError("CncShield is not connected; call .connect() first")
        cmd = f"{axis}{int(steps)},{int(speed_steps)}\n".encode()
        with self._lock:
            self._ser.write(cmd)

    def disable(self, axis: str) -> None:
        """Cut coil current on *axis* (release holding torque). Thread-safe.

        Sends ``DISABLE_FORMAT.format(axis=axis)`` — firmware must recognize
        this verb. Override :attr:`DISABLE_FORMAT` if your firmware differs.
        """
        if axis not in _VALID_AXES:
            raise ValueError(f"axis must be X/Y/Z; got {axis!r}")
        if self._ser is None:
            raise RuntimeError("CncShield is not connected; call .connect() first")
        cmd = self.DISABLE_FORMAT.format(axis=axis).encode()
        with self._lock:
            self._ser.write(cmd)


# ----------------------------------------------------------------------
# Continuous-flow pump (one axis)
# ----------------------------------------------------------------------

class StepperFlowPump:
    """Continuous-flow syringe pump driven by a stepper on one axis.

    The pump runs continuously by commanding a very large step count
    (``HUGE_STEPS``, ~1e9 — "forever" at any reasonable speed). :meth:`set_flow`
    re-issues the command at the new speed, which typical Arduino stepper
    firmwares (AccelStepper and derivatives) treat as a target override —
    the motor keeps running, only the speed changes.

    Calibration::

        steps_per_s = ml_per_s × mm_per_ml × steps_per_mm
    """

    HUGE_STEPS = 10**9

    def __init__(
        self,
        shield: CncShield,
        axis: str = "X",
        *,
        direction: int = 1,
        steps_per_mm: float = 8064.5,
        mm_per_ml: float = 0.2,
        steps_per_rev: float | None = None,
    ):
        if axis not in _VALID_AXES:
            raise ValueError(f"axis must be X/Y/Z; got {axis!r}")
        self.shield = shield
        self.axis = axis
        self.direction = 1 if direction >= 0 else -1
        self.steps_per_mm = float(steps_per_mm)
        self.mm_per_ml = float(mm_per_ml)
        self.steps_per_rev = float(steps_per_rev) if steps_per_rev is not None else None
        self._current_ml_s: float = 0.0
        self._last_flow_ml_s: float = 0.0
        self._cycle_stop_event = threading.Event()
        self._cycle_thread: threading.Thread | None = None

    # ---- Public API ---------------------------------------------------

    def set_flow(self, ml_per_s: float) -> None:
        """Start or update the pump to run at *ml_per_s*. Non-blocking.

        Sign of *ml_per_s* controls direction:
            * positive → forward (combined with ``self.direction``)
            * negative → reverse
            * zero     → stop

        If already running, this overrides the speed **without stopping the
        motor** — call as often as you like, even to flip direction mid-flow.
        """
        magnitude = abs(float(ml_per_s))
        if magnitude == 0:
            self.stop()
            return
        steps_per_s = self._ml_s_to_steps_per_s(magnitude)
        if steps_per_s <= 0:
            self.stop()
            return
        sign = 1 if ml_per_s > 0 else -1
        self.shield.send(self.axis, sign * self.direction * self.HUGE_STEPS, steps_per_s)
        self._current_ml_s = float(ml_per_s)
        self._last_flow_ml_s = float(ml_per_s)

    def start(self, ml_per_s: float | None = None) -> None:
        """Start (or resume) the flow. Non-blocking.

        If *ml_per_s* is given, start at that rate (equivalent to
        :meth:`set_flow`). Sign controls direction: positive = forward,
        **negative = reverse**, zero = stop.

        If omitted, resume at the rate most recently passed to
        ``set_flow``/``start`` (including its sign).

        Raises:
            RuntimeError: no prior rate to resume and *ml_per_s* is None.
        """
        if ml_per_s is None:
            if self._last_flow_ml_s == 0:
                raise RuntimeError(
                    "No previous flow rate to resume — pass ml_per_s or call set_flow first."
                )
            ml_per_s = self._last_flow_ml_s
        self.set_flow(ml_per_s)

    def stop(self) -> None:
        """Halt the motor. The last-commanded rate is remembered so a
        subsequent :meth:`start` (no argument) resumes at that rate.
        """
        # Protocol requires a positive speed even with steps=0; preserve the
        # last commanded speed (as magnitude) so any onboard deceleration
        # profile is sane. abs() handles reverse-flow state.
        last_speed = max(1, self._ml_s_to_steps_per_s(max(abs(self._current_ml_s), 0.01)))
        self.shield.send(self.axis, 0, last_speed)
        self._current_ml_s = 0.0

    def move_mm(self, mm: float, ml_per_s: float, *, release_after: bool = False) -> None:
        """Move the plunger by *mm* at *ml_per_s*. Non-blocking, one-shot.

        Sign of *mm* sets direction (combined with ``self.direction``). If
        *release_after* is True, coil current is cut after the estimated
        move duration (``mm / (ml_per_s × mm_per_ml)``).
        """
        if ml_per_s <= 0:
            raise ValueError("ml_per_s must be > 0")
        if mm == 0:
            return
        steps = int(round(abs(mm) * self.steps_per_mm))
        if steps == 0:
            return
        speed_steps = self._ml_s_to_steps_per_s(ml_per_s)
        if speed_steps <= 0:
            raise ValueError("converted speed must be > 0 steps/s")
        sign = (1 if mm > 0 else -1) * self.direction
        self.shield.send(self.axis, sign * steps, speed_steps)
        if release_after:
            self.release_after(abs(mm) / (ml_per_s * self.mm_per_ml))

    def move_rev(self, revolutions: float, ml_per_s: float, *, release_after: bool = False) -> None:
        """Move the plunger by *revolutions* at *ml_per_s*. Non-blocking, one-shot.

        Requires ``steps_per_rev`` to have been passed to the constructor.
        If *release_after* is True, coil current is cut after the estimated
        move duration (``steps / speed_steps``).
        """
        if self.steps_per_rev is None:
            raise RuntimeError("steps_per_rev was not configured — pass it to the constructor")
        if ml_per_s <= 0:
            raise ValueError("ml_per_s must be > 0")
        if revolutions == 0:
            return
        steps = int(round(abs(revolutions) * self.steps_per_rev))
        if steps == 0:
            return
        speed_steps = self._ml_s_to_steps_per_s(ml_per_s)
        if speed_steps <= 0:
            raise ValueError("converted speed must be > 0 steps/s")
        sign = (1 if revolutions > 0 else -1) * self.direction
        self.shield.send(self.axis, sign * steps, speed_steps)
        if release_after:
            self.release_after(steps / speed_steps)

    def release(self) -> None:
        """Cut coil current on this axis (release holding torque)."""
        self.shield.disable(self.axis)

    def release_after(self, seconds: float) -> None:
        """Schedule :meth:`release` after *seconds*. Non-blocking.

        Use after a discrete move to drop holding torque once the estimated
        duration has elapsed. Timing is open-loop — the shield has no
        position feedback, so this fires on the clock.
        """
        threading.Timer(max(0.0, float(seconds)), self.release).start()

    @property
    def is_running(self) -> bool:
        return self._current_ml_s != 0

    @property
    def current_flow_ml_s(self) -> float:
        return self._current_ml_s

    @property
    def last_flow_ml_s(self) -> float:
        """Last non-zero rate commanded; preserved across ``stop()``."""
        return self._last_flow_ml_s

    def run_cycles(
        self,
        cycle_volume_ml: float,
        pause_seconds: float,
        ml_per_s: float,
        cycle_count: int = 0,
    ) -> None:
        """Run stop/go pump cycles in a background thread. Non-blocking.

        Each cycle pumps *cycle_volume_ml* at *ml_per_s*, then pauses for
        *pause_seconds*. ``cycle_count=0`` runs forever until stopped.

        Sign of *cycle_volume_ml* sets direction (combined with
        ``self.direction``). Call :meth:`stop_cycles` to interrupt.
        """
        if self._cycle_thread is not None and self._cycle_thread.is_alive():
            raise RuntimeError("run_cycles is already active; call stop_cycles() first")
        if cycle_volume_ml == 0:
            raise ValueError("cycle_volume_ml must be non-zero")
        if pause_seconds < 0:
            raise ValueError("pause_seconds must be >= 0")
        if ml_per_s <= 0:
            raise ValueError("ml_per_s must be > 0")
        if cycle_count < 0:
            raise ValueError("cycle_count must be >= 0 (0 = continuous)")

        steps_magnitude = self._ml_to_steps(abs(cycle_volume_ml))
        speed_steps = self._ml_s_to_steps_per_s(ml_per_s)
        if speed_steps <= 0 or steps_magnitude <= 0:
            raise ValueError("converted steps/speed must be > 0")

        sign = (1 if cycle_volume_ml > 0 else -1) * self.direction
        move_seconds = abs(cycle_volume_ml) / ml_per_s
        signed_flow = ml_per_s if cycle_volume_ml > 0 else -ml_per_s

        self._cycle_stop_event.clear()
        self._cycle_thread = threading.Thread(
            target=self._cycle_worker,
            args=(sign * steps_magnitude, speed_steps, signed_flow,
                  move_seconds, pause_seconds, cycle_count),
            daemon=True,
        )
        self._cycle_thread.start()

    def stop_cycles(self) -> None:
        """Stop the cycle loop and halt the motor. Non-blocking."""
        self._cycle_stop_event.set()
        self.stop()

    # ---- Internals ----------------------------------------------------

    def _ml_s_to_steps_per_s(self, ml_per_s: float) -> int:
        return int(round(ml_per_s * self.mm_per_ml * self.steps_per_mm))

    def _ml_to_steps(self, ml: float) -> int:
        return int(round(ml * self.mm_per_ml * self.steps_per_mm))

    def _cycle_worker(
        self, steps_per_cycle, speed_steps, signed_flow,
        move_seconds, pause_seconds, cycle_count,
    ):
        infinite = cycle_count == 0
        cycle_index = 0
        try:
            while not self._cycle_stop_event.is_set() and (infinite or cycle_index < cycle_count):
                cycle_index += 1
                self.shield.send(self.axis, steps_per_cycle, speed_steps)
                self._current_ml_s = signed_flow
                self._last_flow_ml_s = signed_flow
                if self._cycle_stop_event.wait(timeout=move_seconds):
                    break
                if pause_seconds > 0 and (infinite or cycle_index < cycle_count):
                    self._current_ml_s = 0.0
                    if self._cycle_stop_event.wait(timeout=pause_seconds):
                        break
        finally:
            self._current_ml_s = 0.0


# ----------------------------------------------------------------------
# Discrete positioner (one axis)
# ----------------------------------------------------------------------

class StepperPositioner:
    """Discrete positioner on one axis — finite moves, not continuous flow.

    Use for eppendorf-slot moves, stage translation, or anything where you
    command a specific displacement. Moves are non-blocking; call :meth:`stop`
    to interrupt.

    If you pass ``slot_spacing_mm``, :meth:`move_to_slot` becomes available
    and tracks the current slot internally (assumes you started at slot 0).
    """

    def __init__(
        self,
        shield: CncShield,
        axis: str = "Y",
        *,
        steps_per_mm: float = 80.0,
        speed_steps_per_s: int = 20000,
        slot_spacing_mm: float | None = None,
    ):
        if axis not in _VALID_AXES:
            raise ValueError(f"axis must be X/Y/Z; got {axis!r}")
        self.shield = shield
        self.axis = axis
        self.steps_per_mm = float(steps_per_mm)
        self.speed_steps_per_s = int(speed_steps_per_s)
        self.slot_spacing_mm = slot_spacing_mm
        self._current_slot: int = 0

    # ---- Moves --------------------------------------------------------

    def move_steps(self, steps: int, *, release_after: bool = False) -> None:
        """Move by *steps* (signed). Non-blocking.

        If *release_after* is True, coil current is cut after the estimated
        move duration (``|steps| / speed_steps_per_s``).
        """
        steps_int = int(steps)
        self.shield.send(self.axis, steps_int, self.speed_steps_per_s)
        if release_after and steps_int != 0:
            self.release_after(abs(steps_int) / self.speed_steps_per_s)

    def move_mm(self, mm: float, *, release_after: bool = False) -> None:
        """Move by *mm* (signed). Non-blocking."""
        self.move_steps(int(round(mm * self.steps_per_mm)), release_after=release_after)

    def move_to_slot(self, slot: int, *, release_after: bool = False) -> None:
        """Move the stage to absolute *slot* (requires ``slot_spacing_mm``).

        Tracks position relative to slot 0 (starting point at construction).
        """
        if self.slot_spacing_mm is None:
            raise RuntimeError(
                "slot_spacing_mm was not configured — pass it to the constructor"
            )
        delta_slots = int(slot) - self._current_slot
        if delta_slots == 0:
            return
        self.move_mm(delta_slots * self.slot_spacing_mm, release_after=release_after)
        self._current_slot = int(slot)

    def stop(self) -> None:
        """Cancel any in-progress move."""
        self.shield.send(self.axis, 0, self.speed_steps_per_s)

    def release(self) -> None:
        """Cut coil current on this axis (release holding torque)."""
        self.shield.disable(self.axis)

    def release_after(self, seconds: float) -> None:
        """Schedule :meth:`release` after *seconds*. Non-blocking."""
        threading.Timer(max(0.0, float(seconds)), self.release).start()

    @property
    def current_slot(self) -> int:
        return self._current_slot
