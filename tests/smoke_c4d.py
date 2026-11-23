# -*- coding: utf-8 -*-
"""Synthetic regression. Run in c4dpy or call check() on a detached C4D document."""
import runpy
from pathlib import Path

import c4d


def check(plugin, test_button=False):
    api = runpy.run_path(str(plugin))
    source = c4d.documents.BaseDocument()
    red, green = [c4d.BaseMaterial(c4d.Mmaterial) for _ in range(2)]
    for mat, rgb in [(red, (1, 0, 0)), (green, (0, 1, 0))]:
        mat.SetName("Paint")
        mat[c4d.MATERIAL_COLOR_COLOR] = c4d.Vector(*rgb)
        source.InsertMaterial(mat)
    parent = c4d.BaseObject(c4d.Onull)
    parent.SetRelPos(c4d.Vector(9, 2, 1))
    source.InsertObject(parent)
    template = c4d.PolygonObject(4, 1)
    template.SetName("Tile")
    template.SetAllPoints([c4d.Vector(x, y, 0) for x, y in [(0, 0),(0, 1),(1, 1),(1, 0)]])
    template.SetPolygon(0, c4d.CPolygon(0, 1, 2, 3))
    uv = c4d.UVWTag(1)
    template.InsertTag(uv)
    a, b, c, d = template.GetAllPoints()
    uv.SetSlow(0, a, b, c, d)
    template.MakeTag(c4d.Ttexture).SetMaterial(red)
    template.Message(c4d.MSG_UPDATE)
    for i, mat in enumerate((red, red, green, red)):
        obj = template.GetClone()
        obj.InsertUnderLast(parent)
        obj.SetRelPos(c4d.Vector(2 * i, 1, 0))
        obj.GetTag(c4d.Ttexture).SetMaterial(mat)
        if i == 3:
            obj.GetTag(c4d.Tuvw).SetSlow(0, a, b, c, d + c4d.Vector(0.25, 0, 0))
        obj.Message(c4d.MSG_UPDATE)
    output, stats = api["convert_document"](source)
    if test_button:
        # Run only in a separate c4dpy process. Exercise the actual click handler
        # with native documents, recording its UI feedback without opening a window.
        class ButtonProbe(api["InstanceDialog"]):
            def Enable(self, item, state):
                self.enabled = state

            def SetString(self, item, message):
                self.status = message

        c4d.documents.InsertBaseDocument(source)
        c4d.documents.SetActiveDocument(source)
        panel = ButtonProbe()
        assert panel.Command(panel.BUTTON, c4d.BaseContainer())
        assert panel.enabled and "新副本" in panel.status
        output = c4d.documents.GetActiveDocument()
        assert output != source
    assert (stats["meshes_in"], stats["meshes_out"], stats["instances_created"]) == (4, 3, 1), stats
    assert not stats["skipped"], stats
    before = parent.GetChildren()
    after = output.GetFirstObject().GetChildren()
    assert len(before) == len(after) == 4
    assert all(o.GetType() == c4d.Opolygon for o in before)
    assert [o.GetType() for o in after] == [c4d.Opolygon, c4d.Oinstance, c4d.Opolygon, c4d.Opolygon]
    assert after[1][c4d.INSTANCEOBJECT_LINK] == after[0]
    output.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_NONE)
    for orig, obj in zip(before, after):
        effective = obj[c4d.INSTANCEOBJECT_LINK] if obj.GetType() == c4d.Oinstance else obj
        assert api["placement"](orig) == api["placement"](obj)
        assert orig.GetTag(c4d.Ttexture).GetMaterial()[c4d.MATERIAL_COLOR_COLOR] == effective.GetTag(c4d.Ttexture).GetMaterial()[c4d.MATERIAL_COLOR_COLOR]
        assert orig.GetTag(c4d.Tuvw).GetSlow(0) == effective.GetTag(c4d.Tuvw).GetSlow(0)
    cache = after[1].GetCache()
    assert cache is not None and cache.GetTag(c4d.Ttexture).GetMaterial() == after[0].GetTag(c4d.Ttexture).GetMaterial()
    return dict(version=c4d.GetC4DVersion(), **stats)


if __name__ == "__main__":
    print(check(Path(__file__).resolve().parents[1] / "ColorSafeInstance" / "ColorSafeInstance.pyp"), flush=True)
