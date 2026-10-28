# -*- mode: python ; coding: utf-8 -*-
#
# LunarShroud backend PyInstaller spec（onedir）
#
# 使用方式（在 backend/ 目录）：
#   pyinstaller backend.spec
#   输出：backend/dist/backend/backend.exe + backend/dist/backend/_internal/
#
# 为什么是 onedir 而不是 onefile：
#   onefile 每次启动都要把 ~20 MB 运行时解压到临时目录。用户机器上
#   %TEMP% 被重定向/只读/清理策略拦截、或安全软件扫描解压过程时，
#   bootloader 只会弹 "Could not create temporary directory!"，启动器拿不到
#   可读的原因；强杀时解压目录还会残留在用户 Temp 里（实测堆积过 2.2 GB）。
#   onedir 直接把运行时放在安装目录，启动不做解压，既没有这个失败面，也更快。
#   部署脚本/安装包必须把 backend.exe 和 _internal 一起复制，见
#   tools/Publish-PortableRuntime.ps1 与 src-tauri/build.rs。

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

a = Analysis(
    ['main.py'],
    # pathex 使用相对路径，clone 仓库后可直接在本目录执行 pyinstaller backend.spec
    pathex=['.'],
    hiddenimports=(
        # uvicorn 及其子模块
        collect_submodules('uvicorn')
        # httpx 及其子模块
        + collect_submodules('httpx')
        # pydantic 及其子模块
        + collect_submodules('pydantic')
        + collect_submodules('pydantic_core')
        + collect_submodules('pydantic.v1')
        # websockets（uvicorn 依赖）
        + collect_submodules('websockets')
        # watchfiles（uvicorn 自动重载依赖，但打包时一并包含）
        + collect_submodules('watchfiles')
        # anyio 及其子模块
        + collect_submodules('anyio')
        + collect_submodules('sniffio')
        + collect_submodules('httpcore')
        + collect_submodules('h11')
        # pynvml（GPU 监控可选依赖，try/except 导入需显式收集）
        + collect_submodules('pynvml')
        # PyYAML is imported lazily inside model_manifest._load_yaml, so the
        # analyzer cannot see it. It is a locked runtime dependency and the
        # packaged build must be able to parse model.yaml.
        + collect_submodules('yaml')
        # Knowledge-base extraction imports these lazily (and pypdfium2 loads
        # a native PDFium binary), so the analyzer cannot see them either.
        + collect_submodules('pypdf')
        + collect_submodules('pypdfium2')
    ),
    binaries=collect_dynamic_libs('pypdfium2'),
    datas=collect_data_files('pypdfium2'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'test', 'unittest', 'pytest',
        'setuptools', 'pip', 'wheel',
        'matplotlib', 'scipy', 'pandas',
        'PIL', 'Pillow', 'cv2', 'opencv',
        'tensorflow', 'torch', 'keras',
        'IPython', 'jupyter', 'notebook',
        'sphinx', 'docutils',
        'cryptography', 'OpenSSL',
        'zmq', 'pyzmq',
        'boto3', 'botocore',
        'celery', 'redis',
        'flask', 'django',
        'lxml', 'beautifulsoup4',
        'Crypto', 'Cryptodome',
        'socks', 'PySocks',
        'pywin32', 'win32com',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Keep release output independent of whether an arbitrary UPX binary is on PATH.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,       # 无控制台窗口（后台服务）
    # 窗口化进程抛异常时不要弹模态对话框拖住 sidecar；崩溃详情由后端
    # rotating log（backend.log）与崩溃翻译承接。
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='backend',
)
