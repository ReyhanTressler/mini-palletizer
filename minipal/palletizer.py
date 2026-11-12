"""The palletizer's control logic: a state machine that runs the cell through the machine layer.

Two parts:
  1. The TRANSITION TABLE (in __init__): which state changes are allowed, and the guards on them.
     Anything not in the table is impossible -- e.g. there is no Fault -> Running row.
  2. step(): does the work for the current state once (move, wait for a case, check vacuum),
     then fires the next event. Like one pass of a PLC sequence step.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum

from transitions import Machine

from minipal.hardware import RobotException, SafetyState
from minipal.pattern import generate_pattern
from minipal.recipe import Recipe

log = logging.getLogger("minipal")
logging.getLogger("transitions").setLevel(logging.WARNING)   # hide the library's own chatter

RUN_STATES = ["waiting_for_box", "starved", "picking", "placing"]
STATES = ["ready", "homing", "idle", *RUN_STATES, "pallet_full", "protective_stop", "fault"]

PICK_XY = (-600.0, 500.0)    # end of the infeed conveyor, beside the pallet (pallet frame, mm)
APPROACH_MM = 150.0          # approach / retract height above a pick or place position
BOX_TIMEOUT_S = 30.0         # no case for this long -> Starved (watchdog timer)
VACUUM_SETTLE_S = 0.2        # time allowed for vacuum to build or release

# Plain-English reasons, used when a command is rejected
GUARD_TEXT = {
    "is_safe": "safety is not NORMAL",
    "has_pallet": "no pallet present",
    "gripper_empty": "a case is still on the gripper",
    "pallet_not_full": "pallet is already full",
}


class Mode(Enum):
    PRODUCTION = "production"    # real product: wait for cases, use vacuum, check Vacuum OK
    DRY_CYCLE = "dry"            # no product: robot runs every move, vacuum and case checks skipped


@dataclass
class Metrics:
    boxes_placed: int = 0
    cycle_times_s: list[float] = field(default_factory=list)   # pick start -> place done, per case
    stops: list[tuple[float, str]] = field(default_factory=list)  # (sim time, cause)
    starved_s: float = 0.0

    def summary(self, elapsed_s: float) -> dict:
        avg = sum(self.cycle_times_s) / len(self.cycle_times_s) if self.cycle_times_s else 0.0
        per_min = self.boxes_placed / (elapsed_s / 60) if elapsed_s else 0.0
        return {
            "boxes_placed": self.boxes_placed,
            "elapsed_s": round(elapsed_s, 1),
            "avg_cycle_s": round(avg, 2),
            "boxes_per_min": round(per_min, 2),
            "starved_s": round(self.starved_s, 1),
            "stops": [f"{t:.1f}s {cause}" for t, cause in self.stops],
        }


class Palletizer:
    def __init__(self, machine, recipe: Recipe, mode: Mode = Mode.PRODUCTION):
        # --- Devices, found by name (same calls as the real SDK) -------------------------------
        self.machine = machine
        self.clock = machine.clock                    # simulated clock (a real cell would use system time)
        self.robot = machine.get_robot("Robot")
        self.box_present = machine.get_input("Box Present")
        self.vacuum_ok = machine.get_input("Vacuum OK")
        self.pallet_present = machine.get_input("Pallet Present")
        self.vacuum_on = machine.get_output("Vacuum On")
        self.robot.on_system_state_change(self._on_safety_change)

        # --- Job data ---------------------------------------------------------------------------
        self.recipe = recipe
        self.mode = mode
        self.slots = generate_pattern(recipe)
        self.next_slot = 0                            # index into self.slots
        self.resume_to: str | None = None             # where to go back after a safeguard stop
        self.stop_requested = False
        self.fault_reason = ""
        self.history: list[dict] = []                 # every transition, with time (black box)
        self.metrics = Metrics()
        self._cycle_start = 0.0
        self._starved_since = 0.0

        # --- The transition table: your state diagram, as data ---------------------------------
        transitions = [
            {"trigger": "home", "source": "ready", "dest": "homing", "conditions": "is_safe"},
            {"trigger": "homed", "source": "homing", "dest": "idle"},
            {"trigger": "start", "source": "idle", "dest": "waiting_for_box",
             "conditions": ["is_safe", "has_pallet", "gripper_empty", "pallet_not_full"]},
            {"trigger": "box_arrived", "source": ["waiting_for_box", "starved"], "dest": "picking"},
            {"trigger": "box_timeout", "source": "waiting_for_box", "dest": "starved"},
            {"trigger": "picked", "source": "picking", "dest": "placing"},
            # Two rows for "placed": the first whose guard passes wins
            {"trigger": "placed", "source": "placing", "dest": "pallet_full", "conditions": "pallet_complete"},
            {"trigger": "placed", "source": "placing", "dest": "waiting_for_box"},
            {"trigger": "stop", "source": ["waiting_for_box", "starved"], "dest": "idle"},
            {"trigger": "pallet_swapped", "source": "pallet_full", "dest": "idle",
             "conditions": "has_pallet", "after": "_new_pallet"},
            {"trigger": "safeguard_stop", "source": RUN_STATES, "dest": "protective_stop",
             "before": "_remember_state"},
            # Resume goes back to exactly where the safeguard stop happened
            *[{"trigger": "resume", "source": "protective_stop", "dest": s,
               "conditions": ["is_safe", lambda event, s=s: self.resume_to == s]} for s in RUN_STATES],
            {"trigger": "fault", "source": "*", "dest": "fault"},
            {"trigger": "reset", "source": "fault", "dest": "ready", "conditions": "is_safe"},
        ]
        self._sm = Machine(
            model=self, states=STATES, transitions=transitions, initial="ready",
            send_event=True,                 # callbacks get an `event` describing the transition
            auto_transitions=False,          # no hidden "jump to any state" shortcuts
            ignore_invalid_triggers=True,    # an illegal command returns False instead of crashing
            after_state_change="_log_transition",
        )

    # === Operator commands (what an HMI or API would call) ==========================================
    def command(self, name: str) -> bool:
        """home, start, stop, resume, reset, pallet_swapped. Returns False if the command is rejected."""
        if name == "stop" and self.state in ("picking", "placing"):
            self.stop_requested = True       # controlled stop: finish the case in hand first
            log.info("%s STOP requested: finishing the current case first", self._t())
            return True
        accepted = self.trigger(name)
        if not accepted:
            log.warning("%s REJECTED '%s' in '%s': %s", self._t(), name, self.state, self._why_blocked(name))
        return accepted

    # === Guards: conditions that must be true for a transition =====================================
    def is_safe(self, event=None) -> bool:
        return self.robot.safety_state is SafetyState.NORMAL

    def has_pallet(self, event=None) -> bool:
        return self.pallet_present.state

    def gripper_empty(self, event=None) -> bool:
        return not self.vacuum_ok.state

    def pallet_not_full(self, event=None) -> bool:
        return self.next_slot < len(self.slots)

    def pallet_complete(self, event=None) -> bool:
        return self.next_slot >= len(self.slots)

    # === The work for each state ======================================================================
    def step(self) -> None:
        """Do the current state's work once, then fire the next event."""
        try:
            if self.state == "homing":
                self.robot.home()
                self.homed()
            elif self.state in ("waiting_for_box", "starved"):
                self._wait_for_box()
            elif self.state == "picking":
                self._pick()
            elif self.state == "placing":
                self._place()
            else:
                self.clock.sleep(0.5)        # ready / idle / pallet_full / protective_stop / fault: wait for an operator
        except RobotException as exc:
            self._handle_interrupted_move(str(exc))

    def run_until(self, done, max_sim_s: float = 3600.0) -> None:
        """Keep stepping until done() is True (or a time limit, so nothing loops forever)."""
        while not done() and self.clock.now < max_sim_s:
            self.step()

    def _wait_for_box(self) -> None:
        if self.stop_requested:
            self.stop_requested = False
            self.stop()
            return
        if self.mode is Mode.DRY_CYCLE:
            self.clock.sleep(1.0)            # dry cycle: no product needed, pretend a case is there
            self.box_arrived()
            return
        timeout = BOX_TIMEOUT_S if self.state == "waiting_for_box" else 1.0
        if self._wait_for(self.box_present, True, timeout):    # like WAIT DI[..]=ON TIMEOUT
            self.box_arrived()
        elif self.state == "waiting_for_box":
            self.box_timeout()

    def _pick(self) -> None:
        x, y = PICK_XY
        top = self.recipe.box_height_mm                        # top of the case on the conveyor
        self.robot.movel((x, y, top + APPROACH_MM))            # approach above the case
        self.robot.movel((x, y, top))                          # down onto the case
        if self.mode is Mode.PRODUCTION:
            self.vacuum_on.write(True)
            self.clock.sleep(VACUUM_SETTLE_S)
            if not self.vacuum_ok.state:                       # handshake: no confirmation -> fault
                self.vacuum_on.write(False)
                self._raise_fault("vacuum not confirmed at pick")
                return
        self.robot.movel((x, y, top + APPROACH_MM))            # lift
        self.picked()

    def _place(self) -> None:
        slot = self.slots[self.next_slot]
        above = (slot.x_mm, slot.y_mm, slot.z_mm + APPROACH_MM)
        self.robot.movel(above)                                # travel above the slot
        self.robot.movel((slot.x_mm, slot.y_mm, slot.z_mm))    # down to place height
        if self.mode is Mode.PRODUCTION:
            self.vacuum_on.write(False)                        # release
            self.clock.sleep(VACUUM_SETTLE_S)
        self.robot.movel(above)                                # retract
        self.next_slot += 1
        self.metrics.boxes_placed += 1
        self.metrics.cycle_times_s.append(self.clock.now - self._cycle_start)
        log.debug("%s placed case %d/%d (layer %d)", self._t(), slot.index, len(self.slots), slot.layer)
        if slot.index % self.recipe.boxes_per_layer == 0:
            log.info("%s LAYER %d COMPLETE (%d/%d cases)", self._t(), slot.layer, slot.index, len(self.slots))
        self.placed()

    def _wait_for(self, signal, value: bool, timeout_s: float) -> bool:
        waited = 0.0
        while signal.state != value:
            if waited >= timeout_s:
                return False
            self.clock.sleep(0.1)
            waited += 0.1
        return True

    # === Safety and faults ===========================================================================
    def _on_safety_change(self, new_state: SafetyState) -> None:
        """Called by the robot whenever the safety system changes state. We REACT; we never override."""
        log.warning("%s SAFETY -> %s", self._t(), new_state.value)
        if new_state in (SafetyState.EMERGENCY_STOP, SafetyState.UNKNOWN):     # unknown = unsafe
            self._raise_fault(f"safety {new_state.value}")
        elif new_state is SafetyState.SAFEGUARD_STOP:
            if self.state in RUN_STATES:
                self.metrics.stops.append((self.clock.now, "safeguard stop"))
                self.safeguard_stop()
            elif self.state == "homing":
                self._raise_fault("safeguard stop during homing")
        # NORMAL or REDUCED_SPEED: deliberately NO automatic action. Recovery needs an operator.

    def _handle_interrupted_move(self, reason: str) -> None:
        if self.state in RUN_STATES or self.state == "homing":  # not already handled by the safety callback
            self._raise_fault(reason)

    def _raise_fault(self, reason: str) -> None:
        if self.state == "fault":
            return
        self.fault_reason = reason
        self.metrics.stops.append((self.clock.now, reason))
        self.fault()

    # === Callbacks (run automatically by the state machine) ============================================
    def _remember_state(self, event) -> None:
        self.resume_to = event.transition.source

    def _new_pallet(self, event) -> None:
        self.next_slot = 0

    def on_enter_picking(self, event) -> None:
        if event.transition.source != "protective_stop":
            self._cycle_start = self.clock.now                 # cycle time starts at the pick

    def on_enter_starved(self, event) -> None:
        self._starved_since = self.clock.now

    def on_exit_starved(self, event) -> None:
        self.metrics.starved_s += self.clock.now - self._starved_since

    def on_enter_fault(self, event) -> None:
        # Vacuum is left ON on purpose: dropping a held case is worse than holding it.
        log.error("%s FAULT: %s", self._t(), self.fault_reason)

    def on_enter_pallet_full(self, event) -> None:
        log.info("%s PALLET FULL: %d cases placed", self._t(), self.next_slot)

    def _log_transition(self, event) -> None:
        record = {"t": round(self.clock.now, 1), "from": event.transition.source,
                  "trigger": event.event.name, "to": event.transition.dest}
        self.history.append(record)
        log.info("%s %-16s --%s--> %s", self._t(), record["from"], record["trigger"], record["to"])

    # === Helpers ======================================================================================
    def status(self) -> dict:
        """Everything an operator screen would show. A future API would stream this."""
        return {
            "state": self.state,
            "mode": self.mode.value,
            "safety": self.robot.safety_state.value,
            "boxes": f"{self.next_slot}/{len(self.slots)}",
            "fault": self.fault_reason if self.state == "fault" else "",
            "inputs": {s.name: s.state for s in (self.box_present, self.vacuum_ok, self.pallet_present)},
        }

    def _why_blocked(self, name: str) -> str:
        if name not in self._sm.get_triggers(self.state):
            return f"not allowed from '{self.state}'"
        failing = [text for guard, text in GUARD_TEXT.items() if not getattr(self, guard)()]
        return ", ".join(failing) or "conditions not met"

    def _t(self) -> str:
        return f"[{self.clock.now:7.1f}s]"
