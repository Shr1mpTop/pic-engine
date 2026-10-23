# -*- coding: utf-8 -*-
"""把 ComfyUI fork 当库用：严格按 main.py 的顺序做最小初始化。

顺序是铁律（docs/alignment.md 有逐步踩坑记录）：
  1. cuda_malloc          必须在 torch 首次加载前 import（设 PYTORCH_CUDA_ALLOC_CONF
                          =backend:cudaMallocAsync；native 分配器 = aimdo 钩子装不上）
  2. comfy_aimdo.control.init()  必须在 import nodes/model_patcher 之前：
                          host_buffer.py 第 6 行 `lib = control.lib` 是 import 时捕获，
                          DLL 晚加载 = host_buffer.lib 永远是 None
  3. import nodes 等      此时 torch 带 cudaMallocAsync 初始化，host_buffer 拿到真 lib
  4. init_devices + CoreModelPatcher = ModelPatcherDynamic
                          （main.py:281-305；不换 = 经典 ModelPatcher，
                          8.9GB 文本编码器整包 offload 到 CPU，TE 编码 25 分钟不完）

节点类全部 import 真实现（V1 实例方法 / V3 类方法 execute），保证与
ComfyUI 服务器跑出的结果逐位一致，引擎零复刻。
"""
import logging
import os
import sys


class Fork:
    """已导入的节点类集合（属性访问，避免到处 import）。"""


def _load_aimdo_lib(args):
    """main.py:72-87：加载 aimdo 原生 DLL + cuda-detour 钩子。
    必须发生在 nodes/model_patcher 导入之前（host_buffer 的 lib 是 import 时捕获）。"""
    import comfy_aimdo.control
    simple_vram_headroom = (None if args.reserve_vram is None
                            else int(args.reserve_vram * 1024 ** 3))
    try:
        comfy_aimdo.control.init(
            simple_vram_headroom=simple_vram_headroom,
            nvml_pressure=not args.disable_nvml_pressure)
    except TypeError:
        try:  # comfy-aimdo 0.4.10 协议
            comfy_aimdo.control.init(simple_vram_headroom=simple_vram_headroom)
        except TypeError:  # comfy-aimdo 0.4.9 协议
            comfy_aimdo.control.init()
    if os.name == "nt":
        os.environ["MIMALLOC_PURGE_DELAY"] = "0"


def _enable_dynamic_vram():
    """main.py:267-307：init_devices + 换 ModelPatcherDynamic + 打 aimdo 总开关。"""
    import comfy.memory_management
    import comfy.model_patcher
    import comfy_aimdo.control
    from comfy.cli_args import args

    def supported():
        if comfy.model_management.is_nvidia():
            return True
        if comfy.model_management.is_amd():
            return comfy.model_management.rocm_version >= (7, 14)
        return False

    enables = (args.enable_dynamic_vram
               or (not args.disable_dynamic_vram and not args.highvram
                   and not args.gpu_only and not args.novram and not args.cpu))
    if not (args.enable_dynamic_vram or (enables and supported())):
        return
    if (not args.enable_dynamic_vram) \
            and comfy.model_management.torch_version_numeric < (2, 8):
        logging.warning("torch < 2.8，DynamicVRAM 不启用（同 main.py 行为）")
        return
    try:
        devices = comfy.model_management.get_all_torch_devices()
        try:
            ok = comfy_aimdo.control.init_devices(
                (d.index, int(args.vram_headroom * 1024 ** 3)) for d in devices)
        except TypeError:
            # comfy-aimdo 0.4.9 协议
            ok = comfy_aimdo.control.init_devices(d.index for d in devices)
    except Exception as e:  # noqa: BLE001 - 初始化失败退回经典路径，不阻断启动
        logging.warning("comfy-aimdo 初始化失败，退回经典 ModelPatcher: %r" % e)
        return
    if ok:
        comfy.model_patcher.CoreModelPatcher = comfy.model_patcher.ModelPatcherDynamic
        comfy.memory_management.aimdo_enabled = True
        logging.info("DynamicVRAM support detected and enabled")
    else:
        logging.warning("No working comfy-aimdo install detected. DynamicVRAM "
                        "support disabled（同 main.py 行为，退回经典 ModelPatcher）")


def bootstrap(comfyui_root, input_dir, output_dir, comfy_args=None):
    # fork 的 comfy.* 全靠 logging 输出（Requested to load / loaded completely /
    # 量化 kernel 选择）。不配置就是瞎跑：模型加载进度、降级警告一概不可见。
    logging.basicConfig(level=logging.INFO,
                        format="[%(levelname)s] %(name)s: %(message)s")
    root = os.path.abspath(comfyui_root)
    if root not in sys.path:
        sys.path.insert(0, root)

    # 1) torch 首次加载前：分配器选 cudaMallocAsync
    import cuda_malloc  # noqa: F401  （副作用模块）
    from comfy.cli_args import args

    # comfy 启动参数透传（config.comfy_args）：必须在 import nodes/model_management
    # 之前打上（vram 状态/注意力实现都在导入期读取）。例：Mac 上
    # {"use_split_cross_attention": true} 替换默认的 sub-quadratic 注意力。
    for key, value in (comfy_args or {}).items():
        setattr(args, key, value)

    import folder_paths
    folder_paths.set_input_directory(os.path.abspath(input_dir))
    folder_paths.set_output_directory(os.path.abspath(output_dir))

    # 2) nodes/model_patcher 之前：aimdo DLL + 钩子
    _load_aimdo_lib(args)

    # 3) 真节点导入（此时 torch 才首次加载）
    import nodes as comfy_nodes
    from comfy_extras import nodes_qwen

    # 4) DynamicVRAM：init_devices + ModelPatcherDynamic
    _enable_dynamic_vram()

    f = Fork()
    # V1 节点：实例化后调方法（与 ComfyUI-SaveAsScript 生成的脚本同一姿势）
    f.LoadImage = comfy_nodes.LoadImage
    f.UNETLoader = comfy_nodes.UNETLoader
    f.CLIPLoader = comfy_nodes.CLIPLoader
    f.VAELoader = comfy_nodes.VAELoader
    f.EmptyLatentImage = comfy_nodes.EmptyLatentImage
    f.KSampler = comfy_nodes.KSampler
    f.VAEDecode = comfy_nodes.VAEDecode
    f.SaveImage = comfy_nodes.SaveImage
    # V3 节点：类方法 execute(...) -> io.NodeOutput（支持下标解包）
    f.TextEncodeQwenImage21 = nodes_qwen.TextEncodeQwenImage21
    f.QwenImage21Cache = nodes_qwen.QwenImage21Cache
    f.folder_paths = folder_paths
    f.root = root
    return f
