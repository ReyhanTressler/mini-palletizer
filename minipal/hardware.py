"""Simulated hardware, shaped like Vention's MachineLogic SDK.

The control logic only ever calls get_robot / get_input / get_output, robot.movel,
robot.home, input.state and output.write -- the same shape as the real SDK.
Swapping FakeMachine for the real SDK is the "next step"; palletizer.py would not change.

Nothing here is real hardware:
  * moves take simulated time (distance / speed),
  * sensors follow simple rules (a case arrives every few seconds, vacuum confirms when switched on),
  * faults can be injected at a chosen simulated time to test how the logic reacts.
"""

import math
import time
from enum import Enum
from typing import Callable


class SafetyState(Enum):
    """Robot safety states. Same names as Vention's SDK."""
    NORMAL = "NORMAL"
    SAFEGUARD_STOP = "SAFEGUARD_STOP"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    REDUCED_SPEED = "REDUCED_SPEED"
    UNKNOWN = "UNKNOWN"


class MachineException(Exception):
    """Base error for the machine layer (e.g. asking for a device that does not exist)."""


class RobotException(MachineException):
    """A robot command could not finish, e.g. motion interrupted by the safety system."""


class SimClock:
    """Simulated time. sleep() moves time forward and fires any scheduled events that fall due."""

    def __init__(self, speed: float = 0.0):
        self.now = 0.0                       # simulated seconds since start
        self.speed = speed                   # 0 = run instantly; 10 = 10x real time (to watch it)
        self._events: list[tuple[float, Callable[[], None]]] = []

    def schedule(self, at_s: float, action: Callable[[], None]) -> None:
        self._events.append((at_s, action))
        self._events.sort(key=lambda event: event[0])      # keep the soonest event first

    def sleep(self, seconds: float) -> None:
        end = self.now + seconds
        while self._events and self._events[0][0] <= end:  # events that happen during this wait
            at_s, action = self._events.pop(0)
            self._advance_to(max(at_s, self.now))
            action()
        self._advance_to(end)

    def _advance_to(self, t: float) -> None:
        if self.speed > 0:
            time.sleep((t - self.now) / self.speed)
        self.now = t


class DigitalInput:
    def __init__(self, name: str, state: bool = False):
        self.name = name
        self.state = state                   # read-only for the control logic


class DigitalOutput:
    def __init__(self, name: str, on_write: Callable[[bool], None] | None = None):
        self.name = name
        self.state = False
        self.write_count = 0                 # handy for tests (e.g. dry cycle never uses vacuum)
        self._on_write = on_write

    def write(self, value: bool) -> None:
        self.state = value
        self.write_count += 1
        if self._on_write:
            self._on_write(value)            # let the simulation react (vacuum builds, case lifts)


class FakeRobot:
    def __init__(self, name: str, clock: SimClock, speed_mm_s: float = 800.0):
        self.name = name
        self._clock = clock
        self.speed_mm_s = speed_mm_s
        self.home_position = (0.0, -400.0, 1500.0)   # safe position above and in front of the pallet
        self.position = (0.0, -400.0, 1000.0)        # powers up somewhere that is NOT home
        self.safety_state = SafetyState.NORMAL
        self._listeners: list[Callable[[SafetyState], None]] = []

    def on_system_state_change(self, callback: Callable[[SafetyState], None]) -> None:
        """Register a function to call whenever the safety state changes (like the SDK callback)."""
        self._listeners.append(callback)

    def set_safety_state(self, new_state: SafetyState) -> None:
        """Called by the SIMULATED SAFETY SYSTEM only. The control logic never calls this."""
        if new_state is self.safety_state:
            return
        self.safety_state = new_state
        for callback in self._listeners:
            callback(new_state)

    def movel(self, target: tuple[float, float, float]) -> None:
        """Linear move. Takes distance / speed seconds; stops if the safety system stops the robot."""
        start = self.position
        duration = math.dist(start, target) / self.speed_mm_s
        elapsed = 0.0
        while elapsed < duration:
            if self.safety_state not in (SafetyState.NORMAL, SafetyState.REDUCED_SPEED):
                fraction = elapsed / duration                     # where the robot stopped
                self.position = tuple(s + (t - s) * fraction for s, t in zip(start, target))
                raise RobotException(f"motion interrupted: {self.safety_state.value}")
            step = min(0.1, duration - elapsed)                   # move in 0.1 s slices
            self._clock.sleep(step)
            elapsed += step
        if self.safety_state not in (SafetyState.NORMAL, SafetyState.REDUCED_SPEED):
            raise RobotException(f"motion interrupted: {self.safety_state.value}")
        self.position = target

    def home(self) -> None:
        self.movel(self.home_position)


class FakeMachine:
    """The whole simulated cell: one robot, a conveyor with a case sensor, a vacuum gripper, a pallet."""

    FAULTS = ("estop", "safeguard", "vacuum_loss", "starve")

    def __init__(self, speed: float = 0.0, box_interval_s: float = 6.0, starve_s: float = 45.0):
        self.clock = SimClock(speed)
        self.box_interval_s = box_interval_s         # a new case reaches the pick point this often
        self.starve_s = starve_s                     # how long an injected "starve" stops the supply
        self._robots = {"Robot": FakeRobot("Robot", self.clock)}
        self._inputs = {name: DigitalInput(name) for name in ("Box Present", "Vacuum OK", "Pallet Present")}
        self._inputs["Pallet Present"].state = True
        self._outputs = {"Vacuum On": DigitalOutput("Vacuum On", self._on_vacuum_write)}
        self._vacuum_fault_armed = False
        self._starved_until = 0.0
        self.clock.schedule(2.0, self._box_arrives)  # first case arrives 2 s after power-up

    # --- SDK-shaped lookups: devices are found by their friendly names -----------------------
    def get_robot(self, name: str) -> FakeRobot:
        return self._lookup(self._robots, name, "robot")

    def get_input(self, name: str) -> DigitalInput:
        return self._lookup(self._inputs, name, "input")

    def get_output(self, name: str) -> DigitalOutput:
        return self._lookup(self._outputs, name, "output")

    @staticmethod
    def _lookup(table: dict, name: str, kind: str):
        if name not in table:
            raise MachineException(f"No {kind} named '{name}'")
        return table[name]

    # --- Fault injection (for testing and demos) ---------------------------------------------
    def inject(self, fault: str, at_s: float) -> None:
        """Schedule a fault at a simulated time, e.g. inject('safeguard', 60)."""
        robot = self._robots["Robot"]
        actions = {
            "estop": lambda: robot.set_safety_state(SafetyState.EMERGENCY_STOP),
            "safeguard": lambda: robot.set_safety_state(SafetyState.SAFEGUARD_STOP),
            "vacuum_loss": self._arm_vacuum_fault,
            "starve": self._start_starving,
        }
        if fault not in actions:
            raise ValueError(f"Unknown fault '{fault}'. Choose from: {', '.join(self.FAULTS)}")
        self.clock.schedule(at_s, actions[fault])

    def restore_safety(self) -> None:
        """Stands in for a person releasing the E-stop / clearing the area AND pressing safety reset."""
        self._robots["Robot"].set_safety_state(SafetyState.NORMAL)

    # --- Simulation rules ---------------------------------------------------------------------
    def _box_arrives(self) -> None:
        if self.clock.now < self._starved_until:     # upstream line has stopped feeding us
            self.clock.schedule(self._starved_until, self._box_arrives)
            return
        self._inputs["Box Present"].state = True

    def _on_vacuum_write(self, on: bool) -> None:
        vacuum_ok = self._inputs["Vacuum OK"]
        if not on:
            vacuum_ok.state = False
            return
        if self._vacuum_fault_armed:                 # injected fault: vacuum never builds
            self._vacuum_fault_armed = False
            vacuum_ok.state = False
            return
        vacuum_ok.state = True
        if self._inputs["Box Present"].state:        # the case is lifted off the conveyor sensor
            self._inputs["Box Present"].state = False
            self.clock.schedule(self.clock.now + self.box_interval_s, self._box_arrives)

    def _arm_vacuum_fault(self) -> None:
        self._vacuum_fault_armed = True

    def _start_starving(self) -> None:
        self._starved_until = self.clock.now + self.starve_s
