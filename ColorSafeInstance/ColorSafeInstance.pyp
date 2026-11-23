# -*- coding: utf-8 -*-
"""Cinema 4D 2023–2026 · 多颜色同模型转实例工具 · 麦旋风炸串。"""
import hashlib
from collections import Counter
from pathlib import Path
from struct import Struct
from uuid import uuid4

import c4d


# Registered Maxon Plugin ID for ColorSafeInstance.
PLUGIN_ID = 1074717
PLUGIN_NAME = "多颜色同模型转实例工具"


class KeepMesh(Exception):
    """The imported feature cannot be compared without ambiguity."""


SELECTION_TAGS = {c4d.Tpolygonselection, c4d.Tpointselection, c4d.Tedgeselection}
MESH_TAGS = SELECTION_TAGS | {
    c4d.Tpoint, c4d.Tpolygon, c4d.Ttexture, c4d.Tphong, c4d.Tuvw, c4d.Tnormal,
}
OBJECT_STYLE = (
    c4d.ID_BASEOBJECT_USECOLOR, c4d.ID_BASEOBJECT_COLOR, c4d.ID_BASEOBJECT_XRAY,
    c4d.ID_BASEOBJECT_GENERATOR_FLAG, c4d.ID_BASELIST_ICON_COLORIZE_MODE,
    c4d.ID_BASELIST_ICON_COLOR,
)


def walk(first):
    stack = [first] if first is not None else []
    while stack:
        obj = stack.pop()
        yield obj
        nxt, child = obj.GetNext(), obj.GetDown()
        if nxt is not None:
            stack.append(nxt)
        if child is not None:
            stack.append(child)


def vector_tuple(vec):
    return (vec.x, vec.y, vec.z)


def container_key(container):
    return tuple((idx, value_key(value)) for idx, value in container)


def value_key(value):
    if value is None or isinstance(value, (int, float, str, bytes)):
        return value
    if isinstance(value, c4d.Vector):
        return vector_tuple(value)
    if isinstance(value, c4d.BaseMaterial):
        return value  # Atom equality compares the native material identity.
    if isinstance(value, c4d.BaseContainer):
        return container_key(value)
    if isinstance(value, c4d.FieldList) and not value.HasContent():
        return ("empty field list",)
    raise KeepMesh("未支持的数据或链接 " + type(value).__name__)


def ranges(sel, count):
    return tuple(sel.GetRange(i, count) for i in range(sel.GetSegments()))


def raw_digest(tag):
    if tag is None:
        raise KeepMesh("缺少底层几何数据")
    data = tag.GetLowlevelDataAddressR()
    if data is None or (tag.GetDataCount() and not len(data)):
        raise KeepMesh("无法读取完整网格缓冲区")
    return (tag.GetDataCount(), len(data), hashlib.sha256(data).digest())


def legacy_uv_key(tag, polygon_count):
    # The legacy UVWTag stores UVW values on polygon corners (including triangle D).
    # Stream exact values to keep peak memory independent of the polygon count.
    digest = hashlib.sha256()
    pack_vector = Struct("<3d").pack
    for index in range(polygon_count):
        corners = tag.GetSlow(index)
        for corner in ("a", "b", "c", "d"):
            vec = corners[corner]
            digest.update(pack_vector(vec.x, vec.y, vec.z))
    pins = ranges(tag.GetPinSelection(), 4 * polygon_count)
    return ("corner UVW", polygon_count, digest.digest(), pins)


def uv_key(tag, polygon_count):
    if tag.GetDataCount() != polygon_count:
        raise KeepMesh("UVW 数据数量和几何面数不一致")

    if 2023000 <= c4d.GetC4DVersion() < 2026100:
        return legacy_uv_key(tag, polygon_count)

    # Later SDKs expose a second representation for editable UV islands. Retain
    # the topology test. If a build lacks the interface, keep that mesh intact.
    topology_api = (
        "GetUVWObjectR", "GetUVWPolygonCount", "GetUVWPointCount",
        "GetUVWPointS", "GetUVWPolygonS", "GetUVWEdgeS",
    )
    if not all(callable(getattr(tag, method, None)) for method in topology_api):
        raise KeepMesh("本版 C4D 未提供完整的 UV 拓扑读取接口")
    uv_mesh = tag.GetUVWObjectR()
    if uv_mesh is None:
        raise KeepMesh("无法核对 UV 拓扑")
    n = tag.GetUVWPolygonCount()
    if n != polygon_count:
        raise KeepMesh("UV 拓扑和几何面数不一致")
    return (
        raw_digest(tag), raw_digest(uv_mesh.GetTag(c4d.Tpoint)),
        raw_digest(uv_mesh.GetTag(c4d.Tpolygon)),
        ranges(tag.GetPinSelection(), 4 * n),
        ranges(tag.GetUVWPointS(), tag.GetUVWPointCount()),
        ranges(tag.GetUVWPolygonS(), n), ranges(tag.GetUVWEdgeS(), 4 * n),
    )


def tag_key(tag, points, polys, document):
    kind = tag.GetType()
    if kind not in MESH_TAGS or tag.GetUserDataContainer() or tag.GetFirstCTrack():
        raise KeepMesh("特殊标签或动画")
    state = container_key(tag.GetDataInstance())
    payload = None
    if kind in (c4d.Tpoint, c4d.Tpolygon, c4d.Tnormal):
        payload = raw_digest(tag)
    elif kind == c4d.Tuvw:
        payload = uv_key(tag, polys)
    elif kind in SELECTION_TAGS:
        count = points if kind == c4d.Tpointselection else polys
        if kind == c4d.Tedgeselection:
            count *= 4
        payload = ranges(tag.GetBaseSelect(), count)
    elif kind == c4d.Ttexture:
        mat = tag.GetMaterial()
        if mat is None or mat.GetDocument() != document:
            raise KeepMesh("材质丢失或在其他文档")
        payload = (mat, tag[c4d.TEXTURETAG_RESTRICTION])
    return (kind, tag.GetName(), state, payload)


def mesh_key(obj, document):
    if obj.GetDown() or obj.GetUserDataContainer() or obj.GetFirstCTrack():
        raise KeepMesh("子层级、自定义数据或动画")
    if obj.GetNgonCount():
        raise KeepMesh("保留 N-gon 拓扑")
    tags = obj.GetTags()
    # A complete assignment is required. Untextured faces can inherit material
    # outside the object and need separate handling.
    if not any(t.GetType() == c4d.Ttexture and not t[c4d.TEXTURETAG_RESTRICTION]
               for t in tags):
        raise KeepMesh("缺少覆盖全件的本地材质标签")
    points, polys = obj.GetPointCount(), obj.GetPolygonCount()
    mg = obj.GetMg()
    parity = mg.v1.Cross(mg.v2).Dot(mg.v3)
    if abs(parity) < 1e-12:
        raise KeepMesh("零缩放")
    style = tuple(value_key(obj[k]) for k in OBJECT_STYLE)
    return (
        obj.GetUp(), obj.GetName(), obj.GetLayerObject(document), style,
        obj.GetEditorMode(), obj.GetRenderMode(), parity < 0,
        container_key(obj.GetDataInstance()), points, polys,
        ranges(obj.GetPhongBreak(), polys * 4),
        ranges(obj.GetPolygonH(), polys), ranges(obj.GetPointH(), points),
        ranges(obj.GetEdgeH(), polys * 4),
        ranges(obj.GetPolygonS(), polys), ranges(obj.GetPointS(), points),
        ranges(obj.GetEdgeS(), polys * 4),
        tuple(tag_key(t, points, polys, document) for t in tags),
    )


def supported_document(document, objects):
    # This tool is for an original static FBX. Reject rigs, linked generators,
    # expressions and scene-level overrides rather than break incoming links.
    takes = document.GetTakeData()
    if takes and takes.GetMainTake().GetDown():
        raise ValueError("请使用原始静态 FBX；当前文档含 Take 覆盖。")
    for obj in objects:
        if obj.GetType() not in (c4d.Onull, c4d.Opolygon):
            raise ValueError("请单独打开原始静态 FBX（此文档含其他对象类型）。")
        for tag in obj.GetTags():
            if tag.GetInfo() & c4d.TAG_EXPRESSION:
                raise ValueError("请使用原始静态 FBX；当前文档含表达式或约束。")
        if obj.GetFirstCTrack() or obj.GetUserDataContainer():
            raise ValueError("当前文档含动画或对象用户数据。请使用原始静态 FBX。")
    for mat in document.GetMaterials():
        if mat.GetType() != c4d.Mmaterial:
            raise ValueError("请在替换成渲染器材质之前，对原 FBX 执行保色转换。")
        if mat.GetFirstCTrack() or any(s.GetType() != c4d.Xbitmap
                                       for s in walk(mat.GetFirstShader())):
            raise ValueError("原始 FBX 的材质需为静态标准材质，可带位图。")


def placement(obj):
    m = obj.GetMg()
    return tuple(x for v in (m.off, m.v1, m.v2, m.v3) for x in vector_tuple(v))


def configure_instance(inst, obj, master, document):
    inst.SetName(obj.GetName())
    inst[c4d.INSTANCEOBJECT_LINK] = master
    inst[c4d.INSTANCEOBJECT_RENDERINSTANCE_MODE] = c4d.INSTANCEOBJECT_RENDERINSTANCE_MODE_NONE
    for key in OBJECT_STYLE:
        inst[key] = obj[key]
    inst.SetEditorMode(obj.GetEditorMode())
    inst.SetRenderMode(obj.GetRenderMode())
    layer = obj.GetLayerObject(document)
    if layer:
        inst.SetLayerObject(layer)
    inst.SetRotationOrder(obj.GetRotationOrder())
    inst.SetFrozenPos(obj.GetFrozenPos())
    inst.SetFrozenRot(obj.GetFrozenRot())
    inst.SetFrozenScale(obj.GetFrozenScale())
    inst.SetRelPos(obj.GetRelPos())
    inst.SetRelRot(obj.GetRelRot())
    inst.SetRelScale(obj.GetRelScale())


def convert_in_copy(document):
    live = c4d.documents.GetFirstDocument()
    while live is not None:
        if live == document:
            raise ValueError("只能处理未挂入 C4D 的独立文档副本。")
        live = live.GetNext()
    objects = list(walk(document.GetFirstObject()))
    supported_document(document, objects)
    masters, pairs = {}, []
    skipped = Counter()
    total = 0
    for obj in objects:
        if obj.GetType() != c4d.Opolygon:
            continue
        total += 1
        try:
            key = mesh_key(obj, document)
        except KeepMesh as reason:
            skipped[str(reason)] += 1
            continue
        master = masters.setdefault(key, obj)
        if master != obj:
            pairs.append((obj, master))
    # Stage all instances while their source objects still exist. A failing
    # placement or relink rolls back the added nodes in this detached copy.
    staged = []
    try:
        for obj, master in pairs:
            inst = c4d.BaseObject(c4d.Oinstance)
            staged.append(inst)
            inst.InsertBefore(obj)
            configure_instance(inst, obj, master, document)
            difference = zip(placement(obj), placement(inst))
            if inst[c4d.INSTANCEOBJECT_LINK] != master or any(
                    abs(a-b) > 1e-8 * max(1, abs(a), abs(b)) for a, b in difference):
                raise RuntimeError("副本引用或世界矩阵校验失败：" + obj.GetName())
    except Exception:
        for inst in staged:
            inst.Remove()
        raise
    for obj, master in pairs:
        obj.Remove()  # All replaced nodes belong exclusively to the output copy.
    return dict(meshes_in=total, instances_created=len(pairs),
                meshes_out=total - len(pairs), eligible_groups=len(masters),
                skipped=dict(skipped))


def convert_document(source_doc):
    result = source_doc.GetClone(c4d.COPYFLAGS_NONE)
    if result is None:
        raise MemoryError("无法创建完整工程副本。")
    result.SetDocumentName(Path(source_doc.GetDocumentName()).stem +
                           "_保色实例_" + uuid4().hex[:12] + ".c4d")
    result.SetDocumentPath(source_doc.GetDocumentPath())
    return result, convert_in_copy(result)


class InstanceDialog(c4d.gui.GeDialog):
    BUTTON = 1001
    STATUS = 1002

    def CreateLayout(self):
        self.SetTitle(PLUGIN_NAME)
        self.GroupBegin(0, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 1, 0)
        self.GroupBorderSpace(12, 12, 12, 10)
        self.GroupSpace(0, 8)
        self.AddButton(self.BUTTON, c4d.BFH_SCALEFIT, 0, 32, "转为保色实例")
        self.AddStaticText(self.STATUS, c4d.BFH_LEFT | c4d.BFV_TOP,
                           name="打开配色正确的静态 FBX，再点击转换。")
        self.AddStaticText(0, c4d.BFH_RIGHT | c4d.BFV_BOTTOM | c4d.BFV_SCALE,
                           name="多颜色同模型转实例工具--麦旋风炸串")
        self.GroupEnd()
        return True

    def Command(self, control_id, message):
        if control_id != self.BUTTON:
            return True
        doc = c4d.documents.GetActiveDocument()
        if doc is None or doc.GetFirstObject() is None:
            self.SetString(self.STATUS, "请先打开要转换的静态 FBX。")
            return True
        self.Enable(self.BUTTON, False)
        try:
            document, stats = convert_document(doc)
            made = stats["instances_created"]
            for reason, count in stats["skipped"].items():
                print("[{}] 保留 {} 件：{}".format(PLUGIN_NAME, count, reason))
            if made:
                c4d.documents.InsertBaseDocument(document)
                c4d.documents.SetActiveDocument(document)
                document.SetChanged()
                c4d.EventAdd()
                summary = "新副本：{} 个网格，{} 个实例。请另存。".format(stats["meshes_out"], made)
            else:
                summary = "未找到可合并的重复件。"
            if stats["skipped"]:
                summary += " 跳过 {} 件，详见控制台。".format(sum(stats["skipped"].values()))
            self.SetString(self.STATUS, summary)
        except Exception as exc:
            self.SetString(self.STATUS, "转换未完成，请检查提示。")
            c4d.gui.MessageDialog(str(exc))
        finally:
            self.Enable(self.BUTTON, True)
        return True


class InstanceCommand(c4d.plugins.CommandData):
    def __init__(self):
        super().__init__()
        self.dialog = InstanceDialog()

    def Execute(self, doc):
        return self.dialog.Open(c4d.DLG_TYPE_ASYNC, PLUGIN_ID,
                                defaultw=430, defaulth=116)

    def RestoreLayout(self, secret):
        return self.dialog.Restore(PLUGIN_ID, secret)


if __name__ == "__main__":
    registered = c4d.plugins.RegisterCommandPlugin(
        id=PLUGIN_ID, str=PLUGIN_NAME, info=0, icon=None,
        help="按几何和原始材质匹配，在新副本中创建普通实例。",
        dat=InstanceCommand(),
    )
    if not registered:
        print("[{}] 命令注册失败，请检查是否存在重复 ID：{}".format(PLUGIN_NAME, PLUGIN_ID))
