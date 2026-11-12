'''Determines what we are palletizing depending on the Recipes (Crate, Box, Jar'''

import json
from dataclasses import dataclass
from pathlib import Path

class RecipeError(ValueError):
    """Raised when a recipe is invalid."""
    
    
@dataclass(frozen=True)
class Recipe:
    name: str
    box_length_mm: float
    box_width_mm: float              
    box_height_mm: float
    box_weight_kg: float
    columns: int                     
    rows: int    
    layers: int                    
    pallet_length_mm: float = 1200   
    pallet_width_mm: float = 1000
    max_load_kg: float = 1000
    max_height_mm: float = 1800


    @property                        # makes this read like a field: recipe.boxes_per_layer
    def boxes_per_layer(self) -> int:
        return self.columns * self.rows

    @property
    def total_boxes(self) -> int:
        return self.boxes_per_layer * self.layers
    
    def validate(self) -> None:      # -> None: returns nothing, just raises if something's wrong
        problems: list[str] = []     # collect EVERY problem, not just the first one

        for field_name in ("box_length_mm", "box_width_mm", "box_height_mm",
                           "box_weight_kg", "columns", "rows", "layers"):
            value = getattr(self, field_name)        # read a field using its name as text
            if value <= 0:
                problems.append(f"{field_name} must be > 0 (got {value})")

        if self.columns * self.box_length_mm > self.pallet_length_mm:
            problems.append("boxes overhang the pallet length")
        if self.rows * self.box_width_mm > self.pallet_width_mm:
            problems.append("boxes overhang the pallet width")
        if self.total_boxes * self.box_weight_kg > self.max_load_kg:
            problems.append(f"load {self.total_boxes * self.box_weight_kg} kg exceeds {self.max_load_kg} kg")
        if self.layers * self.box_height_mm > self.max_height_mm:
            problems.append(f"stack {self.layers * self.box_height_mm} mm exceeds {self.max_height_mm} mm")

        if problems:                 # an empty list counts as False, so this runs only if problems exist
            raise RecipeError(f"Recipe '{self.name}' is invalid:\n  - " + "\n  - ".join(problems))


def load_recipe(path: str | Path) -> Recipe:   # accepts text or a Path; returns a Recipe
    data = json.loads(Path(path).read_text(encoding="utf-8"))   # file -> text -> dict
    try:
        recipe = Recipe(**data)      # ** unpacks the dict into named fields
    except TypeError as e:           # a field is missing or misspelled
        raise RecipeError(f"Bad recipe file {path}: {e}") from e
    recipe.validate()                # refuse to hand back a bad recipe
    return recipe