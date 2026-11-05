use std::path::{Path, PathBuf};

fn copy_dir_all(source: &Path, destination: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(destination)?;
    for entry in std::fs::read_dir(source)? {
        let entry = entry?;
        let target = destination.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir_all(&entry.path(), &target)?;
        } else {
            std::fs::copy(entry.path(), target)?;
        }
    }
    Ok(())
}

fn replace_dir(source: &Path, destination: &Path) {
    if destination.exists() {
        std::fs::remove_dir_all(destination).expect("failed to remove stale backend runtime");
    }
    copy_dir_all(source, destination).expect("failed to copy backend runtime directory");
}

fn main() {
    let manifest_dir = std::env::var("CARGO_MANIFEST_DIR").unwrap();
    let profile = std::env::var("PROFILE").unwrap();
    let manifest_path = std::path::Path::new(&manifest_dir);
    let project_root = manifest_path.parent().unwrap();

    // 必须跟随 CARGO_TARGET_DIR：README/CHANGELOG 都要求把目标目录指到仓库外的
    // 短路径（在 `src-tauri/target` 下 ring 的 C 编译会失败），而 Cargo 把
    // OUT_DIR 放在真实的目标目录里。硬编码 manifest/target 的后果实测过：
    // 指定 CARGO_TARGET_DIR 后 launcher 产在 `<CARGO_TARGET_DIR>/release/`，
    // 而 sidecar 被塞进 `src-tauri/target/release/`，两边对不上 —— 启动器找不到
    // backend.exe，用户看到"后端启动失败"，且错误信息完全指不到这里。
    //
    // OUT_DIR 形如 `<target>/<profile>/build/<crate>-<hash>/out`，上溯三级即
    // `<target>/<profile>`。
    let target_dir = std::env::var("OUT_DIR")
        .map(|out| {
            let mut dir = PathBuf::from(out);
            dir.pop(); // out
            dir.pop(); // <crate>-<hash>
            dir.pop(); // build
            dir
        })
        .unwrap_or_else(|_| manifest_path.join("target").join(&profile));

    // PyInstaller 的 dist/backend/（onedir）是唯一的后端构建来源。
    // backend.exe 必须与同目录的 _internal/ 一起部署；单独复制 exe 会在
    // 启动时因找不到 Python 运行时直接失败。
    let packaged_dir = project_root.join("backend").join("dist").join("backend");
    let packaged_backend = packaged_dir.join("backend.exe");
    // 编译产物旁的后端副本：开发态从 `cargo run` 启动时，外壳按
    // `<launcher_dir>\backend\backend.exe` 找 sidecar，而 launcher 就在
    // target/<profile>/ 下，所以这里必须预置一份。
    //
    // 曾经还有一个 `src-tauri/backend/` 的 staging 副本，它只为
    // `tauri.conf.json` 的 bundle resources（NSIS 安装包）服务。安装包通道
    // 已移除，这个目录不再有人读，因此不再复制 —— 少一份 200 MB 量级的重复。
    let dst: PathBuf = target_dir.join("backend");

    println!("cargo:rerun-if-changed={}", packaged_backend.display());
    println!(
        "cargo:rerun-if-changed={}",
        packaged_dir.join("_internal").display()
    );

    let packaged_internal = packaged_dir.join("_internal");
    if packaged_backend.is_file() {
        // backend.exe and _internal/ are one runtime. Checking only the exe
        // allowed a release build to ship a sidecar that exits immediately with
        // a missing-Python error.
        if profile == "release" {
            let has_internal_files = packaged_internal.is_dir()
                && std::fs::read_dir(&packaged_internal)
                    .map(|mut entries| entries.next().is_some())
                    .unwrap_or(false);
            if !has_internal_files {
                panic!(
                    "release build requires a non-empty {} (run PyInstaller backend.spec first)",
                    packaged_internal.display()
                );
            }
        }
        // 只刷新编译产物旁的那一份（开发态从 target/<profile>/ 启动时要找
        // 到 sidecar）。bundle resource 的 staging 副本随安装包通道一并移除。
        replace_dir(&packaged_dir, &dst);
    } else if profile == "release" {
        panic!(
            "release build requires {} (run PyInstaller backend.spec first)",
            packaged_backend.display()
        );
    }

    // Tauri validates configured bundle resources during its build hook, so the
    // generated backend must be staged before that validation runs.
    tauri_build::build();
}
