"""Command-line runner.

Examples:
    python -m minipal                                    # full production run, cereal recipe
    python -m minipal --mode dry                         # dry cycle: robot moves, no product
    python -m minipal --fault safeguard@60               # light curtain broken at 60 s
    python -m minipal --fault estop@90 --fault starve@150
    python -m minipal --speed 20                         # watch it run at 20x real time

A simulated OPERATOR responds to stops (clears the area, presses safety reset, Resume / Reset / Home / Start)
so a demo runs end to end. Every operator action is printed so you can see the recovery sequence.
"""

import argparse
import json
import logging
import sys

from minipal.hardware import FakeMachine
from minipal.palletizer import Mode, Palletizer
from minipal.recipe import RecipeError, load_recipe


def parse_fault(text: str) -> tuple[str, float]:
    name, _, at = text.partition("@")
    if not at:
        raise argparse.ArgumentTypeError(f"Use NAME@SECONDS, e.g. safeguard@60 (got '{text}')")
    return name, float(at)


def operate(cell: Palletizer, machine: FakeMachine, max_sim_s: float = 3600.0) -> None:
    """Run the cell, acting as the operator whenever a person would be needed."""
    def operator(msg: str) -> None:
        logging.getLogger("minipal").info("%s OPERATOR: %s", cell._t(), msg)

    cell.command("home")
    while cell.state != "pallet_full" and machine.clock.now < max_sim_s:
        if cell.state == "idle":
            if cell.command("start"):
                continue
            if not cell.gripper_empty():
                operator("removing the case left on the gripper (manual vacuum release)")
                machine.get_output("Vacuum On").write(False)
                continue
            operator("cannot start, ending the run")
            break
        elif cell.state == "protective_stop":
            machine.clock.sleep(5)
            operator("area clear, pressing safety reset, then Resume")
            machine.restore_safety()
            cell.command("resume")
        elif cell.state == "fault":
            machine.clock.sleep(10)
            operator(f"fixed the cause of '{cell.fault_reason}', safety reset, then Reset")
            machine.restore_safety()
            cell.command("reset")
        elif cell.state == "ready":
            cell.command("home")
        else:
            cell.step()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minipal", description="Simulated single-robot palletizer.")
    parser.add_argument("--recipe", default="recipes/cereal.json", help="recipe file (default: cereal)")
    parser.add_argument("--mode", choices=["production", "dry"], default="production")
    parser.add_argument("--fault", type=parse_fault, action="append", default=[], metavar="NAME@SECONDS",
                        help="inject estop, safeguard, vacuum_loss or starve at a simulated time")
    parser.add_argument("--speed", type=float, default=0.0, help="0 = instant (default), 20 = 20x real time")
    parser.add_argument("--verbose", action="store_true", help="also log every case placed")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s",
                        stream=sys.stdout)

    try:
        recipe = load_recipe(args.recipe)
    except (RecipeError, FileNotFoundError) as exc:
        print(f"Recipe error: {exc}")
        return 2

    machine = FakeMachine(speed=args.speed)
    for name, at in args.fault:
        machine.inject(name, at)
    mode = Mode.DRY_CYCLE if args.mode == "dry" else Mode.PRODUCTION
    cell = Palletizer(machine, recipe, mode)

    print(f"Recipe: {recipe.name} | {recipe.boxes_per_layer} per layer x {recipe.layers} layers "
          f"= {recipe.total_boxes} cases | mode: {mode.value}")
    operate(cell, machine)

    print("\nSummary:")
    print(json.dumps(cell.metrics.summary(machine.clock.now), indent=2))
    return 0 if cell.state == "pallet_full" else 1


if __name__ == "__main__":
    sys.exit(main())
