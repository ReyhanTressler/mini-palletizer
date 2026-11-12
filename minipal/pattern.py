"""Pallet pattern: turns a recipe into the exact place position for every case."""

from dataclasses import dataclass

from minipal.recipe import Recipe


@dataclass(frozen=True)
class Slot:                                  # one place position on the pallet
    index: int                               # 1..N, the order the robot places them
    layer: int                               # 1 = bottom layer
    x_mm: float                              # case centre, along the pallet length
    y_mm: float                              # case centre, along the pallet width
    z_mm: float                              # top of the case once placed (where the gripper releases)


def generate_pattern(recipe: Recipe) -> list[Slot]:
    """Every place position, in pallet-frame coordinates (origin = pallet corner).

    Cases are centred on the pallet and placed FAR row first, so the gripper
    never reaches over cases it has already placed.
    """
    x_offset = (recipe.pallet_length_mm - recipe.columns * recipe.box_length_mm) / 2
    y_offset = (recipe.pallet_width_mm - recipe.rows * recipe.box_width_mm) / 2

    slots: list[Slot] = []
    for layer in range(1, recipe.layers + 1):            # bottom layer first
        z = layer * recipe.box_height_mm                 # each layer sits one case higher
        for row in reversed(range(recipe.rows)):         # far row first (robot is on the y = 0 side)
            for col in range(recipe.columns):
                x = x_offset + col * recipe.box_length_mm + recipe.box_length_mm / 2
                y = y_offset + row * recipe.box_width_mm + recipe.box_width_mm / 2
                slots.append(Slot(index=len(slots) + 1, layer=layer, x_mm=x, y_mm=y, z_mm=z))
    return slots
