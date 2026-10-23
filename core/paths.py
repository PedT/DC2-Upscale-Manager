"""Path checks shared by every module. The game install is only ever read."""
import hashlib
import os

# SHA-1 of Dino2.exe in the GOG English build; all keys in this project were derived from it.
GOG_ENGLISH_EXE_SHA1 = "30757dc64f562d9e8284a7836d822d9a20d2f836"


class PathError(ValueError):
    pass


def norm(path):
    return os.path.normcase(os.path.abspath(path))


def is_inside(path, folder):
    p, f = norm(path), norm(folder)
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def check_game_dir(game_dir):
    """Validate a DC2 install. Returns a list of warnings (empty when it is the GOG English build)."""
    if not os.path.isdir(game_dir):
        raise PathError(f"game folder not found: {game_dir}")
    exe = os.path.join(game_dir, "Dino2.exe")
    data = os.path.join(game_dir, "Data")
    if not os.path.isfile(exe) or not os.path.isdir(data):
        raise PathError(f"not a Dino Crisis 2 install (needs Dino2.exe and Data\\): {game_dir}")
    with open(exe, "rb") as fh:
        sha1 = hashlib.sha1(fh.read()).hexdigest()
    if sha1 != GOG_ENGLISH_EXE_SHA1:
        return [f"Dino2.exe SHA-1 is {sha1}, not the GOG English build ({GOG_ENGLISH_EXE_SHA1}); "
                f"the extracted images are fine, but replacement keys may not match this exe"]
    return []


def check_install_target(target, game_dir):
    """Where Install may put replacement files: the game folder itself (Install only ever adds new
    XXXXXXXX.dat files to Data\\ plus its record file, and never changes the game's own files) or a
    separate copy of the game outside C:\\GOG Games. Returns 'game' or 'copy'; raises PathError otherwise."""
    if game_dir and norm(target) == norm(game_dir):
        return "game"
    if game_dir and is_inside(target, game_dir):
        raise PathError(f"install into the game folder itself or a separate copy, not a folder inside it: {target}")
    if is_inside(target, r"C:\GOG Games"):
        raise PathError(f"under C:\\GOG Games only the game folder itself can be an install target: {target}")
    return "copy"


def check_writable_target(target, game_dir):
    """Refuse output folders inside the game install (or the GOG install folder in general)."""
    if game_dir and is_inside(target, game_dir):
        raise PathError(f"refusing to write inside the game install: {target}")
    if is_inside(target, r"C:\GOG Games"):
        raise PathError(f"refusing to write under C:\\GOG Games: {target}")
