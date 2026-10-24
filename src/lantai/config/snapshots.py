"""Apply edits from a loaded configuration to the latest stored configuration."""

from copy import deepcopy
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

_Model = TypeVar("_Model", bound=BaseModel)
_SNAPSHOT = "_lantai_config_read_snapshot"


def file_identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size


def remember_read(model: _Model, key: str, *, by_alias: bool = False) -> _Model:
    # Store the baseline outside serialized model fields. A caller may keep
    # this object across a restore, so a save must distinguish its own edits
    # from fields that changed independently on disk.
    object.__setattr__(model, _SNAPSHOT, (
        key, by_alias, deepcopy(model.model_dump(mode="json", by_alias=by_alias)),
    ))
    return model


def _apply_changes(before: Any, edited: Any, current: Any) -> Any:
    if before == edited:
        return deepcopy(current)
    if isinstance(before, dict) and isinstance(edited, dict):
        result = deepcopy(current) if isinstance(current, dict) else {}
        for key in before.keys() - edited.keys():
            result.pop(key, None)
        for key, value in edited.items():
            if key not in before:
                result[key] = deepcopy(value)
            elif before[key] != value:
                result[key] = _apply_changes(before[key], value, result.get(key))
        return result
    # Lists and scalar settings are user edits as a whole. Unchanged fields
    # and dictionary entries still retain the completed restore's values.
    return deepcopy(edited)


def merge_loaded_changes(model: _Model, current: _Model, key: str,
                         *, by_alias: bool = False) -> _Model:
    snapshot = getattr(model, _SNAPSHOT, None)
    if snapshot is None or snapshot[:2] != (key, by_alias):
        # Newly constructed models retain the public full replacement API.
        return model
    value = _apply_changes(
        snapshot[2], model.model_dump(mode="json", by_alias=by_alias),
        current.model_dump(mode="json", by_alias=by_alias),
    )
    return type(model).model_validate(value)


def acknowledge_save(model: _Model, saved: _Model, key: str,
                     *, by_alias: bool = False) -> None:
    # The caller can save the same object again. Refresh both its visible
    # configuration and read baseline only after the disk write succeeds.
    for name in type(model).model_fields:
        setattr(model, name, deepcopy(getattr(saved, name)))
    remember_read(model, key, by_alias=by_alias)
