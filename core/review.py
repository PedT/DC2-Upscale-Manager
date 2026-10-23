"""Review actions: approve / reject upscaled candidates, replace an approved image, reopen items.

  approve  - copies the item's pending candidate to approved/<kind>/<name>.png, marks it approved
             ("done"); any other pending candidates of that item are marked rejected.
  reject   - marks the candidate rejected, records its model in models_tried, sends the item back to
             'todo' so it can be retried with another model; deletes the candidate file by default.
  replace  - swaps an approved image for a hand-edited file (must be 4:3, at least the original size).
  reopen   - approved -> todo (the approved image is kept on disk, but no longer used).

Duplicates need no handling: identical images have identical replacement keys, so the master's
approved image covers them at install time.
"""
import io
import os
import shutil
import time

from PIL import Image

from .catalog import TransitionError

THUMB = (320, 240)
ASPECT_TOLERANCE = 0.01


class ReviewError(ValueError):
    pass


def _name(item):
    return os.path.splitext(os.path.basename(item["original_png"]))[0]


def _kind_dir(item):
    return "fullscreen" if item["kind"] == "fullscreen" else "rooms"


class Review:
    def __init__(self, catalog):
        self.cat = catalog
        self.ws = catalog.workspace

    def pending_candidate(self, item_id):
        cands = self.cat.candidates(item_id=item_id, verdict="pending")
        return cands[-1] if cands else None

    def queue(self):
        """Items waiting for review, each with its newest pending candidate."""
        out = []
        for it in self.cat.items(status="review"):
            cand = self.pending_candidate(it["id"])
            if cand:
                out.append({"item": it, "candidate": cand})
        return out

    def approve(self, item_id):
        it = self.cat.get_item(item_id)
        if it is None or it["status"] != "review":
            raise ReviewError(f"{item_id} is not waiting for review")
        cand = self.pending_candidate(item_id)
        if cand is None:
            raise ReviewError(f"{item_id} has no upscaled image to approve")
        src = os.path.join(self.ws, cand["path"])
        if not os.path.isfile(src):
            raise ReviewError(f"{item_id}: upscaled file is missing ({cand['path']})")
        rel = f"approved/{_kind_dir(it)}/{_name(it)}.png"
        os.makedirs(os.path.dirname(os.path.join(self.ws, rel)), exist_ok=True)
        shutil.copyfile(src, os.path.join(self.ws, rel))
        for other in self.cat.candidates(item_id=item_id, verdict="pending"):
            self.cat.set_verdict(other["id"], "approved" if other["id"] == cand["id"] else "rejected")
        self.cat.set_status(item_id, "approved", approved_png=rel)
        self.cat.add_note(item_id, f"approved: {cand['model']} (run {cand['run_id']})")
        return rel

    def reject(self, item_id, delete_file=True):
        it = self.cat.get_item(item_id)
        if it is None or it["status"] != "review":
            raise ReviewError(f"{item_id} is not waiting for review")
        cands = self.cat.candidates(item_id=item_id, verdict="pending")
        models = []
        for cand in cands:
            self.cat.set_verdict(cand["id"], "rejected")
            models.append(cand["model"])
            path = os.path.join(self.ws, cand["path"])
            if delete_file and os.path.isfile(path):
                os.remove(path)
            thumb = self._thumb_path(cand["path"])
            if delete_file and os.path.isfile(thumb):
                os.remove(thumb)
        self.cat.set_status(item_id, "todo", model=models[-1] if models else None)
        self.cat.add_note(item_id, f"rejected: {', '.join(models) or 'no candidate'}")

    def reopen(self, item_id):
        it = self.cat.get_item(item_id)
        if it is None or it["status"] != "approved":
            raise ReviewError(f"{item_id} is not approved")
        self.cat.set_status(item_id, "todo")
        self.cat.add_note(item_id, "reopened")

    def replace_approved(self, item_id, data, filename="edited.png"):
        """Use a hand-edited image for an approved item. Returns the new workspace-relative path."""
        it = self.cat.get_item(item_id)
        if it is None or it["status"] != "approved":
            raise ReviewError(f"{item_id} is not approved; approve it first")
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception as exc:
            raise ReviewError(f"{filename} is not a readable image ({exc})")
        with Image.open(os.path.join(self.ws, it["original_png"])) as orig:
            ow, oh = orig.size
        w, h = im.size
        if abs((w / h) / (ow / oh) - 1) > ASPECT_TOLERANCE:
            raise ReviewError(f"{filename} is {w}x{h}; it must have the original's {ow}:{oh} shape (e.g. {ow * 4}x{oh * 4})")
        if w < ow:
            raise ReviewError(f"{filename} is {w}x{h}, smaller than the original {ow}x{oh}")
        im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info else "RGB")
        rel = f"approved/{_kind_dir(it)}/{_name(it)}_edited_{int(time.time())}.png"
        os.makedirs(os.path.dirname(os.path.join(self.ws, rel)), exist_ok=True)
        im.save(os.path.join(self.ws, rel))
        self.cat.set_approved_png(item_id, rel)
        self.cat.add_note(item_id, f"approved image replaced with edited file {filename} ({w}x{h})")
        return rel

    # -- small previews of candidates for the bulk grid --
    def _thumb_path(self, rel):
        return os.path.join(self.ws, "thumbs", "candidates", os.path.splitext(rel)[0].replace("/", "_") + ".jpg")

    def candidate_thumb(self, rel):
        """Workspace-relative path of a cached 320x240 JPEG preview of a candidate image."""
        src = os.path.join(self.ws, rel)
        dst = self._thumb_path(rel)
        if not os.path.isfile(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with Image.open(src) as im:
                im.convert("RGB").resize(THUMB, Image.LANCZOS).save(dst, quality=88)
        return os.path.relpath(dst, self.ws).replace("\\", "/")

    def batch(self, action, item_ids, **kw):
        """Apply approve/reject/reopen to many items; returns {"ok": [...], "errors": {id: message}}."""
        fn = {"approve": self.approve, "reject": self.reject, "reopen": self.reopen}[action]
        ok, errors = [], {}
        for item_id in item_ids:
            try:
                fn(item_id, **kw)
                ok.append(item_id)
            except (ReviewError, TransitionError, KeyError, OSError) as exc:
                errors[item_id] = str(exc)
        return {"ok": ok, "errors": errors}
