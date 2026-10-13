fn main() {
    let manifest_dir = std::env::var("CARGO_MANIFEST_DIR").unwrap();
    let profile = std::env::var("PROFILE").unwrap();
    let manifest_path = std::path::Path::new(&manifest_dir);
    let project_root = manifest_path.parent().unwrap();
    let target_dir = manifest_path.join("target").join(&profile);

    // PyInstaller 的 dist/backend.exe 是唯一的后端构建来源。
    let packaged_backend = project_root
        .join("backend")
        .join("dist")
        .join("backend.exe");
    let staging_dir = manifest_path.join("backend");
    let dst = target_dir.join("backend");

    println!("cargo:rerun-if-changed={}", packaged_backend.display());
    println!(
        "cargo:rerun-if-changed={}",
        staging_dir.join("backend.exe").display()
    );

    if packaged_backend.is_file() {
        // tauri.conf.json 将 backend/backend.exe 作为 bundle resource，先更新
        // staging 副本，确保 NSIS 读取的文件与本次 PyInstaller 产物一致。
        std::fs::create_dir_all(&staging_dir).expect("failed to create backend staging directory");
        std::fs::copy(&packaged_backend, staging_dir.join("backend.exe"))
            .expect("failed to refresh bundled backend staging copy");
        std::fs::create_dir_all(&dst).expect("failed to create target backend directory");
        std::fs::copy(&packaged_backend, dst.join("backend.exe"))
            .expect("failed to copy backend into target directory");
    } else if profile == "release" {
        panic!(
            "release build requires {}. Build backend/backend.spec first",
            packaged_backend.display()
        );
    }

    // Tauri validates configured bundle resources during its build hook, so the
    // generated backend must be staged before that validation runs.
    tauri_build::build();
}
