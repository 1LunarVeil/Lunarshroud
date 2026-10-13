# Asset Licensing

Project icons stored in `src-tauri/icons/` (`artwork.png`, `icon.png`, `128x128.png`, `32x32.png`, `icon.ico`) and the application logo at `frontend/assets/logo.png` are distributed under the repository's MIT License unless a file carries a different notice. Screenshots, local test captures, generated installers, model files, and user data are intentionally excluded from the GitHub source package.

`artwork.png` is the single opaque master. Every other icon file and the in-app logo are derived from it by `py tools/gen_icons.py`; do not hand-edit them, because the rounded-corner mask and the 1.16 frame-suppression zoom are not invertible and re-processing a derived file compounds both. `backend/tests/test_icons.py` fails if a committed asset no longer matches the generator output.
