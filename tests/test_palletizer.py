"""State machine tests. They run against FakeMachine, so no hardware is needed --
the same reason you dry-cycle a cell before running it with material."""

from pathlib import Path

import pytest

from minipal.hardware import FakeMachine, MachineException
from minipal.palletizer import RUN_STATES, Mode, Palletizer
from minipal.recipe import load_recipe

RECIPE = load_recipe(Path(__file__).parent.parent / "recipes" / "cereal.json")


def make_cell(mode=Mode.PRODUCTION, faults=()):
    machine = FakeMachine()
    for name, at in faults:
        machine.inject(name, at)
    return machine, Palletizer(machine, RECIPE, mode)


def home_and_start(cell):
    assert cell.command("home")
    cell.run_until(lambda: cell.state == "idle")
    assert cell.command("start")


# --- Normal operation -----------------------------------------------------------------

def test_start_is_rejected_before_homing():
    _, cell = make_cell()
    assert cell.command("start") is False
    assert cell.state == "ready"


def test_production_run_fills_the_pallet():
    _, cell = make_cell()
    home_and_start(cell)
    cell.run_until(lambda: cell.state == "pallet_full")
    assert cell.state == "pallet_full"
    assert cell.metrics.boxes_placed == 45


def test_dry_cycle_never_uses_the_vacuum():
    machine, cell = make_cell(Mode.DRY_CYCLE)
    home_and_start(cell)
    cell.run_until(lambda: cell.state == "pallet_full")
    assert cell.metrics.boxes_placed == 45
    assert machine.get_output("Vacuum On").write_count == 0


def test_start_is_blocked_without_a_pallet():
    machine, cell = make_cell()
    machine.get_input("Pallet Present").state = False
    cell.command("home")
    cell.run_until(lambda: cell.state == "idle")
    assert cell.command("start") is False
    assert cell.state == "idle"


def test_stop_finishes_the_current_case_then_idles():
    _, cell = make_cell()
    home_and_start(cell)
    cell.run_until(lambda: cell.state == "placing")
    assert cell.command("stop")
    cell.run_until(lambda: cell.state == "idle")
    assert cell.metrics.boxes_placed == 1                     # the case in hand was finished


# --- Safety and faults ----------------------------------------------------------------

def test_safeguard_stop_resumes_where_it_stopped():
    machine, cell = make_cell(faults=[("safeguard", 44.0)])
    home_and_start(cell)
    cell.run_until(lambda: cell.state == "protective_stop")
    stopped_in = cell.resume_to
    assert stopped_in in RUN_STATES

    assert cell.command("resume") is False                    # safety not restored yet
    machine.restore_safety()
    assert cell.state == "protective_stop"                    # clearing safety does NOT restart it
    assert cell.command("resume") is True                     # a person has to press Resume
    assert cell.state == stopped_in

    cell.run_until(lambda: cell.state == "pallet_full")
    assert cell.metrics.boxes_placed == 45


def test_estop_needs_safety_reset_then_reset_then_rehome():
    machine, cell = make_cell(faults=[("estop", 44.0)])
    home_and_start(cell)
    cell.run_until(lambda: cell.state == "fault")
    assert cell.command("reset") is False                     # E-stop still active
    machine.restore_safety()
    assert cell.command("reset") is True
    assert cell.state == "ready"                              # must home again before starting
    assert cell.command("start") is False


def test_case_left_on_gripper_blocks_start():
    machine, cell = make_cell(faults=[("estop", 44.0)])       # E-stop hits while carrying a case
    home_and_start(cell)
    cell.run_until(lambda: cell.state == "fault")
    machine.restore_safety()
    cell.command("reset")
    cell.command("home")
    cell.run_until(lambda: cell.state == "idle")
    assert cell.command("start") is False                     # vacuum still holding the case
    machine.get_output("Vacuum On").write(False)              # operator removes it
    assert cell.command("start") is True


def test_vacuum_loss_faults_the_cell():
    _, cell = make_cell(faults=[("vacuum_loss", 1.0)])
    home_and_start(cell)
    cell.run_until(lambda: cell.state == "fault")
    assert "vacuum" in cell.fault_reason


def test_starved_cell_recovers_when_cases_return():
    _, cell = make_cell(faults=[("starve", 20.0)])
    home_and_start(cell)
    cell.run_until(lambda: cell.state == "starved")
    cell.run_until(lambda: cell.state == "pallet_full")
    assert cell.metrics.boxes_placed == 45
    assert cell.metrics.starved_s > 0


def test_unknown_device_name_raises():
    with pytest.raises(MachineException):
        FakeMachine().get_input("Light Curtain 7")
