import json
from pathlib import Path

import pytest

from minipal.recipe import RecipeError, load_recipe

CEREAL = Path(__file__).parent.parent / "recipes" / "cereal.json"


def write_recipe(tmp_path, **changes):
    """Copy the cereal recipe with some fields changed, and return the new file's path."""
    data = json.loads(CEREAL.read_text())
    data.update(changes)
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(data))
    return path


def test_cereal_recipe_loads():
    recipe = load_recipe(CEREAL)
    assert recipe.boxes_per_layer == 9
    assert recipe.total_boxes == 45


def test_overhang_is_rejected(tmp_path):
    with pytest.raises(RecipeError, match="overhang the pallet length"):
        load_recipe(write_recipe(tmp_path, columns=4))       # 4 x 400 mm > 1200 mm pallet


def test_every_problem_is_reported_at_once(tmp_path):
    with pytest.raises(RecipeError) as err:
        load_recipe(write_recipe(tmp_path, box_weight_kg=-1, layers=20))
    message = str(err.value)
    assert "box_weight_kg must be > 0" in message
    assert "stack" in message                                # 20 x 250 mm > 1800 mm


def test_misspelled_field_is_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"name": "x", "box_lenght_mm": 400}))
    with pytest.raises(RecipeError, match="Bad recipe file"):
        load_recipe(path)
