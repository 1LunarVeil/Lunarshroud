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
    let target_dir = manifest_path.join("target").join(&profile);

    // PyInstaller 的 dist/backend/（onedir）是唯一的后端构建来源。
    // backend.exe 必须与同目录的 _internal/ 一起部署；单独复制 exe 会在
    // 启动时因找不到 Python 运行时直接失败。
    let packaged_dir = project_root.join("backend").join("dist").join("backend");
    let packaged_backend = packaged_dir.join("backend.exe");
    let staging_dir = manifest_path.join("backend");
    let dst: PathBuf = target_dir.join("backend");

    println!("cargo:rerun-if-changed={}", packaged_backend.display());
    println!(
        "cargo:rerun-if-changed={}",
        packaged_dir.join("_internal").display()
    );
    println!(
        "cargo:rerun-if-changed={}",
        staging_dir.join("backend.exe").display()
    );

    if packaged_backend.is_file() {
        // tauri.conf.json 将 backend/backend.exe 与 backend/_internal/** 作为
        // bundle resource，先刷新 staging 副本，确保 NSIS 读取的文件与本次
        // PyInstaller 产物一致。
        replace_dir(&packaged_dir, &staging_dir);
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
