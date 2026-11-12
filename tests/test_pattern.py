from pathlib import Path

from minipal.pattern import generate_pattern
from minipal.recipe import load_recipe

RECIPE = load_recipe(Path(__file__).parent.parent / "recipes" / "cereal.json")


def test_one_slot_per_case():
    slots = generate_pattern(RECIPE)
    assert len(slots) == 45
    assert [s.index for s in slots] == list(range(1, 46))


def test_far_row_is_placed_first():
    slots = generate_pattern(RECIPE)
    first_layer = slots[:9]
    assert first_layer[0].y_mm == max(s.y_mm for s in first_layer)   # farthest from the robot


def test_each_layer_is_one_case_higher():
    slots = generate_pattern(RECIPE)
    assert slots[0].z_mm == 250
    assert slots[9].z_mm == 500                                      # first case of layer 2
    assert slots[-1].z_mm == 1250


def test_every_case_is_on_the_pallet():
    for s in generate_pattern(RECIPE):
        assert RECIPE.box_length_mm / 2 <= s.x_mm <= RECIPE.pallet_length_mm - RECIPE.box_length_mm / 2
        assert RECIPE.box_width_mm / 2 <= s.y_mm <= RECIPE.pallet_width_mm - RECIPE.box_width_mm / 2
